import ctypes
import errno
import os
import tempfile
import time
from pathlib import Path

import httpx

from config import MAX_RETRIES, REQUEST_PAUSE_SECONDS


AT_FDCWD = -100
RENAME_NOREPLACE = 1

RETRYABLE_FILESYSTEM_ERRORS = {
    errno.EINTR,
    errno.EIO,
    errno.EBUSY,
    errno.ETIMEDOUT,
    errno.ESTALE,
}


class AssetDownloadError(RuntimeError):
    """A failed asset download with its actual network attempt count."""

    def __init__(self, asset_key, attempts, cause):
        self.asset_key = asset_key
        self.attempts = attempts
        self.cause = cause

        super().__init__(
            f"Download failed for asset {asset_key} "
            f"after {attempts} network attempt(s): "
            f"{type(cause).__name__}"
        )


def rename_no_replace(source, destination):
    """Atomically rename a file without replacing an existing destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = libc.renameat2

    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int

    result = renameat2(
        AT_FDCWD,
        os.fsencode(source),
        AT_FDCWD,
        os.fsencode(destination),
        RENAME_NOREPLACE,
    )

    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def validate_asset(content, asset_type):
    """Validate the basic file signature for a Skylight asset."""
    if not content:
        raise ValueError("Downloaded asset is empty.")

    if asset_type == "photo":
        if not content.startswith(b"\xff\xd8\xff"):
            raise ValueError("Downloaded photo does not have a JPEG signature.")

    elif asset_type == "video":
        if len(content) < 12 or content[4:8] != b"ftyp":
            raise ValueError("Downloaded video does not have an MP4 ftyp box.")

    else:
        raise ValueError(f"Unsupported asset type: {asset_type!r}")


def place_asset(content, destination):
    """Atomically place a file without overwriting an existing destination."""
    temporary_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".skylight-",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        rename_no_replace(temporary_path, destination)
        temporary_path = None

    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def place_asset_with_retries(content, destination, asset_key, logger):
    """Retry transient filesystem placement failures without redownloading."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            place_asset(content, destination)
            return

        except FileExistsError:
            raise

        except OSError as exc:
            retryable = exc.errno in RETRYABLE_FILESYSTEM_ERRORS

            logger.warning(
                "Filesystem placement failed for asset %s "
                "(attempt %d/%d, errno %s): %s.",
                asset_key,
                attempt + 1,
                MAX_RETRIES + 1,
                exc.errno,
                exc.strerror or type(exc).__name__,
            )

            if not retryable or attempt >= MAX_RETRIES:
                raise

            time.sleep(REQUEST_PAUSE_SECONDS)


def download_asset(asset, destination, client, logger):
    """
    Download, validate, and atomically place one asset.

    Network retries and filesystem placement retries are independent.
    Returns the final destination path.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists():
        raise FileExistsError(f"Destination already exists: {destination.name}")

    asset_type = asset["asset_type"]
    asset_key = asset["asset_key"]
    url = asset["asset_url"]

    for attempt in range(MAX_RETRIES + 1):
        started = time.monotonic()

        try:
            response = client.get(url)
            elapsed = time.monotonic() - started

            if response.status_code == 429:
                logger.warning(
                    "Rate limited downloading asset %s "
                    "(attempt %d/%d).",
                    asset_key,
                    attempt + 1,
                    MAX_RETRIES + 1,
                )

                if attempt >= MAX_RETRIES:
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise AssetDownloadError(
                            asset_key, attempt + 1, exc
                        ) from exc

                continue

            if 500 <= response.status_code <= 599:
                logger.warning(
                    "HTTP %d downloading asset %s "
                    "(attempt %d/%d, %.2fs).",
                    response.status_code,
                    asset_key,
                    attempt + 1,
                    MAX_RETRIES + 1,
                    elapsed,
                )

                if attempt >= MAX_RETRIES:
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise AssetDownloadError(
                            asset_key, attempt + 1, exc
                        ) from exc

                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise AssetDownloadError(
                    asset_key, attempt + 1, exc
                ) from exc

            content = response.content

            try:
                validate_asset(content, asset_type)
            except ValueError as exc:
                if attempt >= MAX_RETRIES:
                    raise AssetDownloadError(
                        asset_key, attempt + 1, exc
                    ) from exc

                logger.exception(
                    "Asset validation failed for %s (attempt %d/%d).",
                    asset_key,
                    attempt + 1,
                    MAX_RETRIES + 1,
                )
                continue

            place_asset_with_retries(
                content,
                destination,
                asset_key,
                logger,
            )

            logger.info(
                "Downloaded %s asset %s: %d bytes in %.2fs.",
                asset_type,
                asset_key,
                len(content),
                elapsed,
            )

            return destination

        except (httpx.TimeoutException, httpx.TransportError) as exc:
            elapsed = time.monotonic() - started

            logger.warning(
                "Network error downloading asset %s "
                "(attempt %d/%d, %.2fs): %s",
                asset_key,
                attempt + 1,
                MAX_RETRIES + 1,
                elapsed,
                type(exc).__name__,
            )

            if attempt >= MAX_RETRIES:
                raise AssetDownloadError(
                    asset_key, attempt + 1, exc
                ) from exc

        except AssetDownloadError:
            raise

    raise AssetDownloadError(
        asset_key,
        MAX_RETRIES + 1,
        RuntimeError("Retry limit exhausted."),
    )
