"""Shared, stderr-only logging suitable for an MCP stdio server."""

import logging
import os
import sys


def configure_logging() -> None:
    level_name = os.getenv("YTMUSIC_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
