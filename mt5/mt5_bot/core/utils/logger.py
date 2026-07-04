"""
Centralized logging setup for the MT5 trading bot.

Provides a single get_logger() factory that:
- writes to console,
- optionally writes to a rotating file under the configured log_dir,
- uses an ASCII-only format string (no special characters),
- is safe to call many times (handlers are not duplicated).

All text is standard ASCII English only.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Optional


_CONFIGURED_ROOT = False


def _level_from_name(name: str) -> int:
    name = (name or "INFO").upper()
    return getattr(logging, name, logging.INFO)


def configure_logging(
    level: str = "INFO",
    log_to_file: bool = True,
    log_dir: str = "logs",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """
    Configure the root logger once. Subsequent calls are ignored to avoid
    duplicate handlers (which would print every line multiple times).
    """
    global _CONFIGURED_ROOT
    if _CONFIGURED_ROOT:
        return

    root = logging.getLogger()
    root.setLevel(_level_from_name(level))

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler.
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # Rotating file handler.
    if log_to_file:
        try:
            os.makedirs(log_dir, exist_ok=True)
            file_path = os.path.join(log_dir, "mt5_bot.log")
            file_handler = RotatingFileHandler(
                file_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except Exception as exc:  # pragma: no cover - defensive
            # If file logging fails (permissions on Win7), keep console only.
            root.warning("File logging disabled: %s", exc)

    _CONFIGURED_ROOT = True


def get_logger(name: str, cfg: Optional[object] = None) -> logging.Logger:
    """
    Return a named logger. If cfg (the loaded config DotDict) is provided and
    the root logger has not been configured yet, configure it from cfg.logging.
    """
    if cfg is not None and not _CONFIGURED_ROOT:
        log_cfg = getattr(cfg, "logging", None)
        if log_cfg is not None:
            configure_logging(
                level=log_cfg.get("level", "INFO"),
                log_to_file=bool(log_cfg.get("log_to_file", True)),
                log_dir=log_cfg.get("log_dir", "logs"),
                max_bytes=int(log_cfg.get("max_bytes", 5 * 1024 * 1024)),
                backup_count=int(log_cfg.get("backup_count", 5)),
            )
    if not _CONFIGURED_ROOT:
        configure_logging()
    return logging.getLogger(name)
