from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT_DIR / "logs"


def get_research_logger(name: str = "research_center") -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    for filename, level in (("app.log", logging.INFO), ("task.log", logging.INFO), ("error.log", logging.ERROR)):
        handler = logging.FileHandler(LOG_DIR / filename, encoding="utf-8")
        handler.setLevel(level)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def log_task(message: str, **context: Any) -> None:
    logger = get_research_logger()
    if context:
        logger.info("%s | %s", message, context)
    else:
        logger.info(message)


def log_error(message: str, **context: Any) -> None:
    logger = get_research_logger()
    if context:
        logger.error("%s | %s", message, context)
    else:
        logger.error(message)
