"""Structured logging to stderr.

Output is ``key=value`` rather than prose because most of it will be read in a
GitHub Actions log, weeks later, while working out why a scheduled job did
something unexpected. Greppable beats pretty.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

__all__ = ["configure", "event", "get_logger"]

_configured = False


def configure(*, verbose: bool = False) -> None:
    global _configured
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        force=True,
    )
    # Log in UTC. Runners are UTC and laptops are not; one timezone in the
    # logs is worth more than local readability.
    logging.Formatter.converter = time.gmtime
    # httpx logs every request at INFO. Ours already log what matters, with
    # more context, and a backfill would otherwise emit hundreds of lines.
    logging.getLogger("httpx").setLevel(logging.DEBUG if verbose else logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    if not _configured:
        configure()
    return logging.getLogger(name)


def event(logger: logging.Logger, name: str, /, **fields: Any) -> None:
    """Log one structured event: ``name key=value key=value``."""
    if not fields:
        logger.info("%s", name)
        return
    rendered = " ".join(f"{key}={_render(value)}" for key, value in fields.items())
    logger.info("%s %s", name, rendered)


def _render(value: Any) -> str:
    text = "-" if value is None else str(value)
    return f'"{text}"' if " " in text else text
