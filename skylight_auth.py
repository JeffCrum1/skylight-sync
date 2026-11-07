import time

import httpx

from pyskylight import SkylightClient
from pyskylight.auth import refresh
from pyskylight.config import Settings, TokenCache

from config import FRAME_ID, REQUEST_TIMEOUT_SECONDS
from network import RateLimitedTransport


def build_client(logger):
    settings = Settings.from_env()
    cache = TokenCache()

    credentials = cache.load(settings.base_url)

    if credentials is None:
        raise RuntimeError("No cached Skylight credentials found.")

    http = httpx.Client(
        transport=RateLimitedTransport(logger),
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
    )

    try:
        if credentials.is_expired(time.time(), leeway=300):
            if not credentials.refresh_token:
                raise RuntimeError(
                    "Skylight access token expired and no refresh token "
                    "is available."
                )

            logger.info(
                "Refreshing expired or soon-to-expire Skylight credentials."
            )

            credentials = refresh(
                credentials.refresh_token,
                base_url=settings.base_url,
                http=http,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            cache.save(credentials, settings.base_url)

            logger.info("Skylight credentials refreshed and cache updated.")

        client = SkylightClient(
            credentials,
            base_url=settings.base_url,
            http=http,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

        return client, FRAME_ID, http

    except Exception:
        http.close()
        raise
