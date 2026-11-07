import logging
from logging.handlers import TimedRotatingFileHandler

from config import LOG_DIRECTORY


def configure_logging():
    LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("skylight_sync")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    handler = TimedRotatingFileHandler(
        LOG_DIRECTORY / "skylight-sync.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
        utc=False,
    )

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    return logger
