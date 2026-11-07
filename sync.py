import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from pyskylight import SkylightAPIError

from assets import parse_created_at, select_assets
from checkpoint import initialize_checkpoint, load_checkpoint, save_checkpoint
from config import (
    CHECKPOINT,
    DESTINATION,
    HA_NOTIFICATIONS_ENABLED,
    HA_NOTIFICATION_TIMEOUT,
    HA_URL,
    HA_WEBHOOK_ID_FILE,
    LOOKBACK_HOURS,
    TIMEZONE,
)
from downloads import AssetDownloadError, download_asset
from logging_setup import configure_logging
from manifest import fetch_manifest
from skylight_auth import build_client


INITIALIZATION_MARKER = DESTINATION / ".skylight-sync-initializing.json"
INITIALIZATION_MARKER_VERSION = 1

ARCHIVE_FILENAME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}_"
    r"\d{2}-\d{2}-\d{2}\.\d{3}"
    r"[+-]\d{4}__.+\.(?:jpg|mp4)$",
    re.IGNORECASE,
)

ALLOWED_INTERNAL_FILES = {
    CHECKPOINT.name,
    INITIALIZATION_MARKER.name,
}

ALLOWED_METADATA_FILES = {
    "Thumbs.db",
}


def validate_destination():
    """Validate that the configured archive destination is usable."""
    if not DESTINATION.exists():
        raise RuntimeError(
            f"Destination directory does not exist: {DESTINATION}"
        )

    if not DESTINATION.is_dir():
        raise RuntimeError(
            f"Destination is not a directory: {DESTINATION}"
        )

    if not os.access(DESTINATION, os.W_OK | os.X_OK):
        raise RuntimeError(
            f"Destination is not writable: {DESTINATION}"
        )


def earliest_asset_timestamp(records):
    """Find the earliest active photo or video in the complete manifest."""
    timestamps = []

    for record in records:
        if not isinstance(record, dict):
            raise RuntimeError("Manifest contains an invalid record.")

        attributes = record.get("attributes")

        if not isinstance(attributes, dict):
            raise RuntimeError("Manifest record has invalid attributes.")

        if attributes.get("destroyed_at"):
            continue

        asset_type = attributes.get("asset_type")

        if asset_type not in ("photo", "video"):
            raise RuntimeError(
                f"Manifest contains an unexpected active asset type: "
                f"{asset_type!r}"
            )

        timestamps.append(parse_created_at(attributes.get("created_at")))

    if not timestamps:
        raise RuntimeError("Manifest contains no active photo or video assets.")

    return min(timestamps)


def load_initialization_marker():
    """Load the initial-run recovery marker."""
    if not INITIALIZATION_MARKER.exists():
        return None

    try:
        with INITIALIZATION_MARKER.open("r", encoding="utf-8") as marker_file:
            data = json.load(marker_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Unable to read the initial synchronization marker."
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError("Initial synchronization marker is invalid.")

    if data.get("version") != INITIALIZATION_MARKER_VERSION:
        raise RuntimeError(
            "Initial synchronization marker version is missing or unsupported."
        )

    value = data.get("baseline")

    if not isinstance(value, str):
        raise RuntimeError("Initial synchronization marker has no baseline.")

    try:
        baseline = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError(
            "Initial synchronization marker has an invalid baseline."
        ) from exc

    if baseline.tzinfo is None or baseline.utcoffset() is None:
        raise RuntimeError(
            "Initial synchronization marker baseline has no timezone."
        )

    return baseline.astimezone(timezone.utc)


def create_initialization_marker(baseline):
    """Atomically create the initial-run recovery marker in the destination."""
    baseline = baseline.astimezone(timezone.utc)

    data = {
        "version": INITIALIZATION_MARKER_VERSION,
        "baseline": baseline.isoformat(),
    }

    temporary_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".skylight-init-",
            suffix=".tmp",
            dir=DESTINATION,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)

            json.dump(data, temporary_file, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        os.replace(temporary_path, INITIALIZATION_MARKER)
        temporary_path = None

    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def validate_archive_entries():
    """
    Reject unexpected entries in the archive directory.

    Visible files must use the agreed timestamp/original-key filename
    format and end in .jpg or .mp4.

    The checkpoint, initialization marker, recognized hidden temporary
    files, and known Windows metadata files are permitted.
    """
    unexpected = []

    for path in DESTINATION.iterdir():
        name = path.name

        if path.is_symlink():
            unexpected.append(f"{name} (symbolic link)")
            continue

        if path.is_dir():
            unexpected.append(f"{name} (directory)")
            continue

        if not path.is_file():
            unexpected.append(f"{name} (not a regular file)")
            continue

        if name in ALLOWED_INTERNAL_FILES:
            continue

        if name in ALLOWED_METADATA_FILES:
            continue

        if name.startswith(".skylight-") and name.endswith(".tmp"):
            continue

        if name.startswith("..skylight-sync.json-") and name.endswith(".tmp"):
            continue

        if ARCHIVE_FILENAME_PATTERN.fullmatch(name):
            continue

        unexpected.append(name)

    if unexpected:
        sample = ", ".join(unexpected[:10])
        remainder = len(unexpected) - min(len(unexpected), 10)

        detail = f" Unexpected entries: {sample}"

        if remainder:
            detail += f" and {remainder} more"

        raise RuntimeError(
            f"Archive directory contains {len(unexpected)} "
            f"unexpected entr{'y' if len(unexpected) == 1 else 'ies'}."
            f"{detail}"
        )


def existing_asset_files():
    """
    Discover archived files by their complete original asset keys.

    Duplicate keys are rejected rather than allowing an ambiguous file
    to count as present.
    """
    files = {}

    for path in DESTINATION.iterdir():
        if path.name.startswith("."):
            continue

        if path.suffix.lower() not in (".jpg", ".mp4"):
            continue

        if "__" not in path.name:
            continue

        asset_key = path.name.split("__", 1)[1]

        if not asset_key:
            continue

        if asset_key in files:
            raise RuntimeError(
                f"Multiple archive files use the same asset key: {asset_key}"
            )

        files[asset_key] = path

    return files


def existing_asset_keys():
    """Return the complete original asset keys currently archived."""
    return set(existing_asset_files())


def verify_asset_file(path, asset_type, asset_key):
    """Verify that an archived asset is a nonempty file of the expected type."""
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(
            f"Archived asset is not a regular file: {path.name}"
        )

    try:
        size = path.stat().st_size

        if size <= 0:
            raise RuntimeError(
                f"Archived asset is empty: {path.name}"
            )

        with path.open("rb") as asset_file:
            signature = asset_file.read(12)

    except OSError as exc:
        raise RuntimeError(
            f"Unable to read archived asset {path.name}: "
            f"{type(exc).__name__}"
        ) from exc

    if asset_type == "photo":
        if not signature.startswith(b"\xff\xd8\xff"):
            raise RuntimeError(
                f"Archived photo has an invalid JPEG signature: {path.name}"
            )

    elif asset_type == "video":
        if len(signature) < 12 or signature[4:8] != b"ftyp":
            raise RuntimeError(
                f"Archived video has an invalid MP4 signature: {path.name}"
            )

    else:
        raise RuntimeError(
            f"Unsupported asset type for {asset_key}: {asset_type!r}"
        )


def verify_archive_integrity(records, archived_files):
    """
    Verify every active manifest asset has one valid archived file.

    Reject duplicate active manifest keys and use this as the final
    integrity gate before checkpoint advancement.
    """
    verified = 0
    missing = []
    invalid = []
    seen_keys = set()

    for record in records:
        attributes = record.get("attributes")

        if not isinstance(attributes, dict):
            raise RuntimeError("Manifest record has invalid attributes.")

        if attributes.get("destroyed_at"):
            continue

        asset_type = attributes.get("asset_type")
        asset_key = attributes.get("asset_key")

        if asset_type not in ("photo", "video"):
            raise RuntimeError(
                f"Unexpected active asset type: {asset_type!r}"
            )

        if not isinstance(asset_key, str) or not asset_key:
            raise RuntimeError(
                "An active manifest asset has an invalid asset key."
            )

        if asset_key in seen_keys:
            raise RuntimeError(
                f"Manifest contains a duplicate active asset key: {asset_key}"
            )

        seen_keys.add(asset_key)

        expected_extension = ".jpg" if asset_type == "photo" else ".mp4"

        if not asset_key.lower().endswith(expected_extension):
            raise RuntimeError(
                f"Asset key extension does not match its type: {asset_key}"
            )

        path = archived_files.get(asset_key)

        if path is None:
            missing.append(asset_key)
            continue

        try:
            verify_asset_file(path, asset_type, asset_key)
        except RuntimeError as exc:
            invalid.append(str(exc))
            continue

        verified += 1

    if missing or invalid:
        details = []

        if missing:
            details.append(
                f"{len(missing)} missing asset(s): "
                + ", ".join(missing[:5])
            )

            if len(missing) > 5:
                details.append(f"{len(missing) - 5} additional missing asset(s)")

        if invalid:
            details.append(
                f"{len(invalid)} invalid asset(s): "
                + "; ".join(invalid[:5])
            )

            if len(invalid) > 5:
                details.append(f"{len(invalid) - 5} additional invalid asset(s)")

        raise RuntimeError(
            "Final archive integrity check failed. "
            + " | ".join(details)
        )

    return verified


def verify_historical_assets(records, checkpoint, present_keys):
    """
    Refuse to advance the checkpoint if older manifest assets are missing.

    Assets inside the normal lookback window are eligible for redownload.
    Assets older than that window must already exist in the archive.
    """
    cutoff = checkpoint - timedelta(hours=LOOKBACK_HOURS)
    cutoff = cutoff.astimezone(timezone.utc)

    missing = []

    for record in records:
        attributes = record["attributes"]

        if attributes.get("destroyed_at"):
            continue

        created_at = parse_created_at(attributes.get("created_at"))

        if created_at >= cutoff:
            continue

        asset_key = attributes.get("asset_key")

        if not isinstance(asset_key, str) or not asset_key:
            raise RuntimeError(
                "An older manifest asset has an invalid original asset key."
            )

        if asset_key not in present_keys:
            missing.append(asset_key)

    if missing:
        sample = ", ".join(missing[:5])
        remainder = len(missing) - min(len(missing), 5)

        detail = f" Missing asset keys: {sample}"

        if remainder:
            detail += f" and {remainder} more"

        raise RuntimeError(
            f"{len(missing)} historical asset(s) are missing from the "
            f"destination directory.{detail}"
        )


def safe_error_summary(exc):
    """Build useful failure details without exposing signed URLs or tokens."""
    if isinstance(exc, AssetDownloadError):
        cause = exc.cause

        if isinstance(cause, httpx.HTTPStatusError):
            return (
                f"Asset download failed after {exc.attempts} attempt(s): "
                f"HTTP {cause.response.status_code}."
            )

        return (
            f"Asset download failed after {exc.attempts} attempt(s): "
            f"{type(cause).__name__}."
        )

    if isinstance(exc, SkylightAPIError):
        if exc.status_code is not None:
            return f"Skylight API returned HTTP {exc.status_code}."
        return "Skylight API network request failed."

    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP request returned status {exc.response.status_code}."

    if isinstance(exc, OSError):
        detail = exc.strerror or type(exc).__name__
        return f"{type(exc).__name__}: {detail}"

    if isinstance(exc, (RuntimeError, ValueError)):
        return str(exc)

    return type(exc).__name__


def send_failure_notification(payload, logger):
    """Send a failure notification to the configured Home Assistant webhook."""
    if not HA_NOTIFICATIONS_ENABLED:
        return

    try:
        webhook_id = HA_WEBHOOK_ID_FILE.read_text(encoding="utf-8").strip()

        if not webhook_id:
            raise RuntimeError("Webhook ID file is empty.")

        webhook_url = (
            f"{HA_URL.rstrip('/')}/api/webhook/{webhook_id}"
        )

        response = httpx.post(
            webhook_url,
            json=payload,
            timeout=HA_NOTIFICATION_TIMEOUT,
        )
        response.raise_for_status()

        logger.info("Home Assistant failure notification sent.")

    except Exception as exc:
        logger.error(
            "Unable to send Home Assistant failure notification: %s",
            type(exc).__name__,
        )


def run_sync(logger):
    """Run one complete Skylight synchronization."""
    stage = "destination validation"
    current_asset = None
    http = None
    run_time = datetime.now(TIMEZONE).isoformat()

    try:
        validate_destination()
        logger.info("Destination directory is available: %s", DESTINATION)

        stage = "checkpoint and initialization state"

        checkpoint = load_checkpoint()
        initialization_baseline = load_initialization_marker()

        initial_run = checkpoint is None and initialization_baseline is None
        resuming_initial_run = initialization_baseline is not None

        if initial_run:
            existing_entries = list(DESTINATION.iterdir())

            if existing_entries:
                raise RuntimeError(
                    "Initial synchronization requires an empty destination. "
                    "No files were changed."
                )

            logger.info("Initial synchronization detected.")

        elif resuming_initial_run:
            logger.info(
                "Resuming initial synchronization from baseline %s.",
                initialization_baseline.isoformat(),
            )

            # The marker is authoritative. This recovers safely if a previous
            # run advanced the checkpoint but did not remove the marker.
            checkpoint = initialization_baseline
            save_checkpoint(checkpoint, CHECKPOINT)

        elif checkpoint is None:
            raise RuntimeError(
                "Checkpoint and initialization state could not be established."
            )

        stage = "Skylight authentication"
        client, frame_id, http = build_client(logger)

        stage = "manifest retrieval"
        records = fetch_manifest(client, frame_id, logger)

        logger.info("Manifest contains %d records.", len(records))

        if initial_run:
            stage = "initial checkpoint creation"

            earliest = earliest_asset_timestamp(records)
            initialization_baseline = (
                earliest - timedelta(hours=LOOKBACK_HOURS)
            )

            create_initialization_marker(initialization_baseline)

            checkpoint = initialize_checkpoint(earliest, CHECKPOINT)

            if checkpoint != initialization_baseline:
                raise RuntimeError(
                    "Initial checkpoint does not match the initialization "
                    "marker baseline."
                )

            logger.info(
                "Initial synchronization marker and baseline established "
                "at %s.",
                checkpoint.isoformat(),
            )

        elif resuming_initial_run:
            stage = "initial manifest validation"

            earliest = earliest_asset_timestamp(records)

            if initialization_baseline > (
                earliest - timedelta(hours=LOOKBACK_HOURS)
            ):
                raise RuntimeError(
                    "Initial synchronization marker is newer than the "
                    "current manifest baseline. Manual review is required."
                )

        if checkpoint is None:
            raise RuntimeError("Checkpoint initialization did not succeed.")

        stage = "archive entry validation"
        validate_archive_entries()

        stage = "asset selection"
        selected = select_assets(records, checkpoint)

        logger.info(
            "Selected %d assets within the checkpoint lookback.",
            len(selected),
        )

        stage = "existing asset discovery"
        archived_files = existing_asset_files()
        present_keys = set(archived_files)

        if not initial_run and not resuming_initial_run and not present_keys:
            raise RuntimeError(
                "A checkpoint exists but no archived assets were found. "
                "Refusing to treat this as a normal incremental run."
            )

        stage = "historical asset verification"
        verify_historical_assets(records, checkpoint, present_keys)

        skipped = 0
        downloaded = 0

        for asset in selected:
            current_asset = asset["asset_key"]

            if current_asset in present_keys:
                skipped += 1
                continue

            stage = "asset download"

            destination = DESTINATION / asset["filename"]

            download_asset(asset, destination, http, logger)

            present_keys.add(current_asset)
            archived_files[current_asset] = destination
            downloaded += 1

        current_asset = None

        logger.info(
            "Asset processing complete: %d downloaded, %d already present.",
            downloaded,
            skipped,
        )

        stage = "final archive integrity verification"
        verified = verify_archive_integrity(records, archived_files)

        logger.info(
            "Final archive integrity check passed: %d active assets verified.",
            verified,
        )

        stage = "checkpoint update"
        completed_at = datetime.now(timezone.utc)
        save_checkpoint(completed_at, CHECKPOINT)

        if initial_run or resuming_initial_run:
            stage = "initialization marker cleanup"
            INITIALIZATION_MARKER.unlink()
            logger.info("Initial synchronization marker removed.")

        logger.info(
            "Synchronization completed successfully. "
            "Checkpoint advanced to %s.",
            completed_at.isoformat(),
        )

        return True

    except Exception as exc:
        error_summary = safe_error_summary(exc)

        logger.error(
            "Synchronization failed during %s: %s",
            stage,
            error_summary,
        )

        if isinstance(exc, AssetDownloadError):
            attempts = str(exc.attempts)
        else:
            # This includes filesystem placement failures: the network
            # download may already have succeeded, so no network attempt
            # count is applicable.
            attempts = "N/A"

        payload = {
            "run_time": run_time,
            "stage": stage,
            "asset": current_asset or "Not applicable",
            "attempts": attempts,
            "error": error_summary,
        }

        send_failure_notification(payload, logger)

        logger.error("Synchronization did not complete successfully.")

        return False

    finally:
        if http is not None:
            http.close()


def main():
    logger = configure_logging()

    logger.info("Starting Skylight synchronization.")

    if run_sync(logger):
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
