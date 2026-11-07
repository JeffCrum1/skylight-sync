import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import CHECKPOINT, LOOKBACK_HOURS


CHECKPOINT_VERSION = 1


def normalize_timestamp(value):
    """Require a timezone-aware datetime and normalize it to UTC."""
    if not isinstance(value, datetime):
        raise TypeError("Checkpoint timestamp must be a datetime.")

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Checkpoint timestamp must include a timezone.")

    return value.astimezone(timezone.utc)


def load_checkpoint(path=CHECKPOINT):
    """
    Load the last successful synchronization timestamp.

    Returns None when no checkpoint exists.
    Raises an exception if an existing checkpoint is invalid.
    """
    path = Path(path)

    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as checkpoint_file:
            data = json.load(checkpoint_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Unable to read checkpoint file: {path}"
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError("Checkpoint has an invalid structure.")

    if data.get("version") != CHECKPOINT_VERSION:
        raise RuntimeError("Checkpoint version is missing or unsupported.")

    value = data.get("last_successful_sync")

    if not isinstance(value, str):
        raise RuntimeError("Checkpoint timestamp is missing or invalid.")

    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError("Checkpoint timestamp is not valid ISO format.") from exc

    try:
        return normalize_timestamp(timestamp)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "Checkpoint timestamp does not include a valid timezone."
        ) from exc


def save_checkpoint(timestamp, path=CHECKPOINT):
    """
    Atomically save a checkpoint timestamp.

    The temporary file is created in the checkpoint directory so the
    final replacement stays on the same filesystem.
    """
    path = Path(path)
    timestamp = normalize_timestamp(timestamp)

    if not path.parent.is_dir():
        raise RuntimeError(
            f"Checkpoint directory does not exist: {path.parent}"
        )

    data = {
        "version": CHECKPOINT_VERSION,
        "last_successful_sync": timestamp.isoformat(),
    }

    temporary_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)

            json.dump(data, temporary_file, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        os.replace(temporary_path, path)
        temporary_path = None

    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def initialize_checkpoint(earliest_created_at, path=CHECKPOINT):
    """
    Create the initial baseline checkpoint if one does not exist.

    The baseline is LOOKBACK_HOURS before the earliest known asset.
    The normal selection lookback therefore reaches at least 48 hours
    earlier still, ensuring the first synchronization includes everything.

    An existing checkpoint is never reset by initialization.
    """
    path = Path(path)

    existing = load_checkpoint(path)

    if existing is not None:
        return existing

    earliest = normalize_timestamp(earliest_created_at)
    baseline = earliest - timedelta(hours=LOOKBACK_HOURS)

    save_checkpoint(baseline, path)

    return baseline
