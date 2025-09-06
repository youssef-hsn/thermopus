from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

import structlog


__all__ = [
    "init_logging",
    "get_logger",
    "set_level",
]


def init_logging(level: str = "INFO", json: bool = False) -> structlog.BoundLogger:
    """
    Configure stdlib logging + structlog to emit consistent logs.

    Args:
        level: "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL"
        json:  If True, emit JSON logs (good for Docker); else pretty console.

    Returns:
        A structlog logger already bound with process metadata.
    """
    # ---- stdlib base config (so 3rd-party libs log correctly) ----
    root_level = _coerce_level(level)
    logging.basicConfig(
        level=root_level,
        format="%(message)s",
        stream=sys.stderr,
        force=True,  # override anything pre-configured
    )

    # ---- structlog processors ----
    common_processors = [
        structlog.contextvars.merge_contextvars,        # include contextvars (if used)
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    if json:
        render_processor = structlog.processors.JSONRenderer()
    else:
        render_processor = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*common_processors, render_processor],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Make stdlib logging go through structlog style too
    _install_stdlib_bridge()

    logger = get_logger().bind(
        app="pi-ds18b20-worker",
        pid=os.getpid(),
    )
    logger.info("logging_initialized", level=level, json=json)
    return logger


def get_logger(name: Optional[str] = None) -> structlog.BoundLogger:
    """Get a bound structlog logger."""
    # getattr protects against None -> use root logger name
    return structlog.get_logger(name or "worker")


def set_level(level: str) -> None:
    """Dynamically change log level at runtime."""
    lvl = _coerce_level(level)
    logging.getLogger().setLevel(lvl)


# ---- internals ---------------------------------------------------------------

def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }.get(str(level).upper(), logging.INFO)


def _install_stdlib_bridge() -> None:
    """
    Configure stdlib logs to flow through structlog’s formatting.
    """
    # Remove existing handlers (basicConfig added one)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_StructlogLikeFormatter())
    root.addHandler(handler)


class _StructlogLikeFormatter(logging.Formatter):
    """
    A tiny formatter that keeps stdlib logs readable alongside structlog output.
    (We keep it minimal; structlog handles the heavy lifting.)
    """
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        # Render level + logger name to align with structlog’s add_log_level
        return f"{record.levelname.lower()} [{record.name}] {msg}"
