import logging
import time

import httpx

from pyskylight import SkylightAPIError, SkylightRateLimitError

from config import MAX_RETRIES


def fetch_manifest(client, frame_id, logger: logging.Logger):
    """
    Fetch every page of the Skylight manifest.

    Retry transient network failures, HTTP 5xx responses, and HTTP 429
    responses up to MAX_RETRIES times.

    HTTP 429 backoff is handled by the shared RateLimitedTransport.
    """
    records = []
    page = 1
    total_pages = None

    while total_pages is None or page <= total_pages:
        for attempt in range(MAX_RETRIES + 1):
            started = time.monotonic()

            logger.info(
                "Fetching manifest page %d (attempt %d/%d).",
                page,
                attempt + 1,
                MAX_RETRIES + 1,
            )

            try:
                result = client.list_messages(frame_id, page=page)

                if not isinstance(result, dict):
                    raise RuntimeError(
                        f"Manifest page {page} returned an unexpected response."
                    )

                page_records = result.get("data")
                meta = result.get("meta")

                if not isinstance(page_records, list) or not isinstance(meta, dict):
                    raise RuntimeError(
                        f"Manifest page {page} is missing data or pagination metadata."
                    )

                current_page = meta.get("current_page")
                reported_pages = meta.get("num_pages")

                if current_page != page:
                    raise RuntimeError(
                        f"Manifest pagination mismatch: requested page {page}, "
                        f"received page {current_page!r}."
                    )

                if (
                    not isinstance(reported_pages, int)
                    or isinstance(reported_pages, bool)
                    or reported_pages < 1
                ):
                    raise RuntimeError(
                        "Manifest returned invalid total-page metadata."
                    )

                if total_pages is not None and reported_pages != total_pages:
                    raise RuntimeError(
                        "Manifest page count changed during retrieval."
                    )

                for record in page_records:
                    if not isinstance(record, dict):
                        raise RuntimeError(
                            f"Manifest page {page} contains an invalid record."
                        )

                    attributes = record.get("attributes")

                    if not isinstance(attributes, dict):
                        raise RuntimeError(
                            f"Manifest page {page} contains a record "
                            "without attributes."
                        )

                elapsed = time.monotonic() - started

                if total_pages is None:
                    total_pages = reported_pages

                records.extend(page_records)

                logger.info(
                    "Manifest page %d/%d: %d records in %.2f seconds.",
                    page,
                    total_pages,
                    len(page_records),
                    elapsed,
                )

                break

            except SkylightRateLimitError:
                elapsed = time.monotonic() - started

                logger.warning(
                    "Skylight rate limit fetching manifest page %d "
                    "(attempt %d/%d, %.2fs).",
                    page,
                    attempt + 1,
                    MAX_RETRIES + 1,
                    elapsed,
                )

                if attempt >= MAX_RETRIES:
                    raise

            except SkylightAPIError as exc:
                elapsed = time.monotonic() - started
                status = exc.status_code

                retryable = status is None or 500 <= status <= 599

                if not retryable:
                    raise

                logger.warning(
                    "Transient Skylight API error fetching manifest page %d "
                    "(attempt %d/%d, HTTP %s, %.2fs).",
                    page,
                    attempt + 1,
                    MAX_RETRIES + 1,
                    status if status is not None else "network error",
                    elapsed,
                )

                if attempt >= MAX_RETRIES:
                    raise

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                elapsed = time.monotonic() - started

                logger.warning(
                    "Network error fetching manifest page %d "
                    "(attempt %d/%d, %.2fs): %s.",
                    page,
                    attempt + 1,
                    MAX_RETRIES + 1,
                    elapsed,
                    type(exc).__name__,
                )

                if attempt >= MAX_RETRIES:
                    raise

        else:
            raise RuntimeError(
                f"Unable to retrieve manifest page {page} after "
                f"{MAX_RETRIES + 1} attempts."
            )

        page += 1

    logger.info("Manifest retrieval complete: %d total records.", len(records))
    return records
