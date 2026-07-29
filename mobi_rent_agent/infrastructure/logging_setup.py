"""Logging configuration for the agent daemon."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


def configure_logging(log_level: str = "INFO", log_file: str | None = "logs/agent.log") -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_path, maxBytes=5 * 1024 * 1024, backupCount=5
            )
        )

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=handlers,
    )
