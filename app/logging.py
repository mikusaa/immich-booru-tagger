"""Chinese business logs with explicit time zones and credential redaction."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo


class ZonedFormatter(logging.Formatter):
    def __init__(self, timezone="Asia/Shanghai", secrets=()):
        super().__init__("%(asctime)s %(levelname)s %(message)s")
        self.zone = ZoneInfo(timezone)
        self.secrets = sorted((s for s in secrets if s), key=len, reverse=True)

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, self.zone).isoformat(sep=" ", timespec="seconds")

    def format(self, record):
        result = super().format(record)
        for secret in self.secrets:
            result = result.replace(secret, "[已隐藏]")
        return result


def safe_error(error, secrets=()):
    message = str(error)
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        message = message.replace(secret, "[已隐藏]")
    return message.replace("\r", " ").replace("\n", " ")[:500]


def setup_logging(level="INFO", timezone="Asia/Shanghai", secrets=()):
    handler = logging.StreamHandler()
    handler.setFormatter(ZonedFormatter(timezone, secrets))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for name in ("httpx", "httpcore", "huggingface_hub", "timm", "aiohttp.access"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name):
    return logging.getLogger(name)
