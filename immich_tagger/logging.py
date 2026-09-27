"""Human-readable console logging."""
import logging


def setup_logging(level="INFO"):
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)
    for name in ("httpx", "httpcore", "huggingface_hub", "timm"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name):
    return logging.getLogger(name)
