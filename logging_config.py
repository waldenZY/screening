"""Logging configuration shared by the filtering command-line tools."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_HANDLER_MARKER = "_screening_pipeline_handler"
_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def setup_logger(
    name: str,
    level: str = "INFO",
    log_file: Path | None = None,
    *,
    run_id: str = "standalone",
    stage: str = "general",
) -> logging.Logger:
    """Configure consistent console and optional UTF-8 file logging."""
    normalized_level = level.upper()
    if normalized_level not in _LOG_LEVELS:
        raise ValueError(f"不支持的日志级别: {level!r}")
    numeric_level = _LOG_LEVELS[normalized_level]

    logger = logging.getLogger(name)
    logger.setLevel(numeric_level)
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(
        f"%(asctime)s [%(levelname)s] [{run_id}] [{stage}] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    setattr(console_handler, _HANDLER_MARKER, True)
    logger.addHandler(console_handler)

    if log_file is not None:
        resolved_log_file = log_file.expanduser().resolve()
        resolved_log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(resolved_log_file, encoding="utf-8")
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        setattr(file_handler, _HANDLER_MARKER, True)
        logger.addHandler(file_handler)
    return logger
