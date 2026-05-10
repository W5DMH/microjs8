"""Canonical filesystem paths for MicroJS8.

Production layout (CardputerZero after .deb install — Phase 7):
  /usr/share/APPLaunch/lib/microjs8/  — Python source (read-only)
  /usr/share/APPLaunch/bin/microjs8   — launcher script (read-only)
  /etc/microjs8/             — shipped default config (read-only)
  /var/lib/microjs8/         — writable runtime state (config, db, logs)

The data directory moved from MiniJS8's ``/var/microjs8`` to
``/var/lib/microjs8`` in Phase 7 for two reasons:

  1. Debian convention puts daemon state under ``/var/lib/<package>/``.
  2. The Phase 7 systemd unit declares
     ``ReadWritePaths=/var/lib/microjs8`` under ``ProtectSystem=strict``,
     which actively blocks writes anywhere else under /var. The
     paths constant here MUST agree with the unit, otherwise the
     daemon silently fails to persist state.

For host-side development and unit tests on the Pi 4 build host, two
environment variables override the production roots:

  MICROJS8_DATA_DIR    overrides /var/lib/microjs8
  MICROJS8_ETC_DIR     overrides /etc/microjs8

This keeps tests from needing root access and keeps developer machines
free of /var/lib/microjs8 directories that don't belong to them.
"""

from __future__ import annotations

import os
from pathlib import Path

# Production roots (overridden by env vars when running off-target).
# This MUST agree with packaging/microjs8.service.in's
# ReadWritePaths and WorkingDirectory; tests/test_paths.py asserts
# the value so a future drift is caught immediately.
_DEFAULT_DATA_DIR = Path("/var/lib/microjs8")
_DEFAULT_ETC_DIR = Path("/etc/microjs8")


def data_dir() -> Path:
    """Writable runtime state directory.

    Holds the live config, message store, heard-list cache, and rotating
    log files. On the production image this is mounted read-write while
    the rest of root is read-only via overlayfs.
    """
    override = os.environ.get("MICROJS8_DATA_DIR")
    return Path(override) if override else _DEFAULT_DATA_DIR


def etc_dir() -> Path:
    """Read-only shipped-defaults directory.

    Holds the default config that gets copied to ``data_dir() / config.toml``
    on first boot if no live config exists. Never written by the application
    at runtime.
    """
    override = os.environ.get("MICROJS8_ETC_DIR")
    return Path(override) if override else _DEFAULT_ETC_DIR


def config_path() -> Path:
    """Live, editable config file path."""
    return data_dir() / "config.toml"


def default_config_path() -> Path:
    """Shipped default config file path."""
    return etc_dir() / "config.toml"


def log_dir() -> Path:
    """Rotating log file directory."""
    return data_dir() / "log"


def log_file() -> Path:
    """Primary log file path."""
    return log_dir() / "microjs8.log"


def db_path() -> Path:
    """Message-store SQLite database (created in Step 5)."""
    return data_dir() / "messages.db"


def inbox_db_path() -> Path:
    """Inbox / mailbox SQLite database.

    Separate file from messages.db on purpose. messages.db is a
    write-once decode log subject to retention sweeps (~30 days);
    inbox.db is the operator's mailbox with indefinite-lifetime
    rows (UNREAD / READ / STORE / DELIVERED). Mixing them would
    confuse retention logic, and the JS8Call ecosystem also keeps
    them in separate files (js8call.log vs inbox.db3) so this
    matches the convention.

    Schema is JS8Call-compatible (one polymorphic table with JSON
    blobs and JSON-path indices). See store.inbox.MailboxStore.
    """
    return data_dir() / "inbox.db"


def ensure_writable_dirs() -> None:
    """Create the writable directories if they don't yet exist.

    Safe to call repeatedly. Used by first-boot path and by the test
    harness so tests don't have to mkdir manually.
    """
    data_dir().mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)
