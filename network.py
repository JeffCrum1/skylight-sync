import email.utils
import logging
import threading
import time
from collections import Counter
from datetime import datetime, timezone

import httpx

from config import REQUEST_PAUSE_SECONDS


def retry_after_seconds(value):
    """Parse Retry-After as seconds or an HTTP date."""
    if not value:
        return None

    try:
        return max(0.0, float(value))
    except ValueError:
        pass

    try:
        retry_time = email.utils.parsedate_to_datetime(value)
        if retry_time.tzinfo is None:
            retry_time = retry_time.replace(tzinfo=timezone.utc)

        return max(
            0.0,
            (retry_time - datetime.now(timezone.utc)).total_seconds(),
        )
    except (TypeError, ValueError, OverflowError):
        return None


class RateLimitedTransport(httpx.BaseTransport):
    """Enforce spacing and server backoff for every outbound HTTP request."""

    def __init__(self, logger: logging.Logger):
        self._transport = httpx.HTTPTransport()
        self._logger = logger
        self._lock = threading.Lock()
        self._next_request_at = 0.0
        self._request_count = 0
        self._status_counts = Counter()
        self._timeout_count = 0
        self._transport_error_count = 0
        self._rate_limits = []

    def handle_request(self, request):
        # Reserve a request slot without holding the lock while sleeping.
        with self._lock:
            now = time.monotonic()
            request_at = max(now, self._next_request_at)
            self._next_request_at = request_at + REQUEST_PAUSE_SECONDS

            self._request_count += 1
            request_number = self._request_count

        wait = request_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)

        started = time.monotonic()

        try:
            response = self._transport.handle_request(request)
        except httpx.TimeoutException:
            elapsed = time.monotonic() - started
            with self._lock:
                self._timeout_count += 1

            self._logger.warning(
                "HTTP request %d timed out after %.2f seconds.",
                request_number,
                elapsed,
            )
            raise

        except httpx.TransportError as exc:
            elapsed = time.monotonic() - started
            with self._lock:
                self._transport_error_count += 1

            self._logger.warning(
                "HTTP request %d failed after %.2f seconds: %s.",
                request_number,
                elapsed,
                type(exc).__name__,
            )
            raise

        elapsed = time.monotonic() - started
        status = response.status_code
        retry_delay = None

        with self._lock:
            self._status_counts[status] += 1

            if status == 429:
                retry_delay = retry_after_seconds(
                    response.headers.get("Retry-After")
                )

                self._rate_limits.append({
                    "request": request_number,
                    "time": datetime.now(timezone.utc),
                    "retry_after_seconds": retry_delay,
                })

                if retry_delay is not None:
                    self._next_request_at = max(
                        self._next_request_at,
                        time.monotonic() + retry_delay,
                    )

        if status == 429:
            self._logger.warning(
                "HTTP request %d received 429 after %.2f seconds; "
                "Retry-After: %s.",
                request_number,
                elapsed,
                f"{retry_delay:.1f}s"
                if retry_delay is not None
                else "not provided or unparseable",
            )
        elif status >= 500:
            self._logger.warning(
                "HTTP request %d returned HTTP %d after %.2f seconds.",
                request_number,
                status,
                elapsed,
            )
        else:
            self._logger.info(
                "HTTP request %d completed with HTTP %d in %.2f seconds.",
                request_number,
                status,
                elapsed,
            )

        return response

    def get_stats(self):
        """Return a snapshot of request statistics."""
        with self._lock:
            return {
                "requests": self._request_count,
                "status_counts": dict(self._status_counts),
                "timeouts": self._timeout_count,
                "transport_errors": self._transport_error_count,
                "rate_limits": list(self._rate_limits),
            }

    def close(self):
        self._transport.close()
