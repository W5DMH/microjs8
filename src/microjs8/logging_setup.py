"""Logging configuration for MicroJS8.

Two destinations, both always on:

  1. **stderr** — captured by systemd-journald when running under the unit;
     visible interactively with ``journalctl -u microjs8 -f``. When run from a
     terminal during development, stderr appears directly.

  2. **Rotating file** at ``log_file()`` — survives reboots and is readable
     when the SD card is removed and inspected on another machine, which is
     critical for diagnosing problems on a unit that won't boot to a
     functional state.

Why not use ``systemd.journal.JournalHandler``: it requires the
``python-systemd`` package which is a system-level dep (not pip-installable
cleanly inside a venv). Plain stderr is captured by journald with full
fidelity for our purposes; we can upgrade to JournalHandler later if we
need structured fields surfaced in journalctl.

Log lines look like:

    2026-04-27 14:33:12.456 [INFO    ] microjs8.app: starting MicroJS8 0.1.0

The component name is always the logger name (``microjs8.app``,
``microjs8.config``, etc.) so ``journalctl -u microjs8 | grep modem`` works.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from typing import Final

from microjs8 import paths

_LOG_FORMAT: Final = (
    "%(asctime)s.%(msecs)03d [%(levelname)-8s] %(name)s: %(message)s"
)
_DATE_FORMAT: Final = "%Y-%m-%d %H:%M:%S"

# 5 files * 1 MiB each = 5 MiB cap on log space. SD-card-friendly.
_LOG_MAX_BYTES: Final = 1 * 1024 * 1024
_LOG_BACKUP_COUNT: Final = 5

_configured = False


def setup() -> None:
    """Configure the root logger.

    Idempotent — calling more than once is a no-op so tests can invoke it
    freely without doubling handlers.

    Log level:
      - ``DEBUG`` if env var ``MICROJS8_DEV=1`` is set (developer mode).
      - ``INFO`` otherwise (production default; minimizes SD wear).
    """
    global _configured
    if _configured:
        return

    level = logging.DEBUG if os.environ.get("MICROJS8_DEV") == "1" else logging.INFO

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(level)

    # stderr → journald (under systemd) or terminal (interactive).
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)

    # Rotating file — best-effort. If the writable dir is missing or we
    # lack permission (e.g. someone ran us as the wrong user), log a warning
    # to stderr and carry on without the file handler. We do NOT crash here:
    # losing the file log is not a reason to refuse to boot.
    try:
        paths.ensure_writable_dirs()
        file_handler = logging.handlers.RotatingFileHandler(
            paths.log_file(),
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        root.warning(
            "could not open log file at %s (%s); continuing with stderr only",
            paths.log_file(),
            exc,
        )

    _configured = True
