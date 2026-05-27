"""Tests for v0.0.13 HOME EXIT button → graceful Pi poweroff.

What changed in v0.0.13:
  - ``App.request_poweroff()`` is a new method that calls
    ``microjs8.input.systemctl_poweroff()``.
  - The InputRouter is wired with ``request_exit=app.request_poweroff``
    (was ``request_exit=app.request_stop`` in v0.0.12 and earlier).

What stays the same:
  - ``request_stop()`` is unchanged — still used by SIGTERM handlers
    and any direct ``systemctl stop microjs8`` invocation.
  - ``request_restart()`` is unchanged — radio-cycle path still uses it.
  - The EXIT_CONFIRM modal's NO/YES focus behavior is unchanged.

These tests verify the wiring rather than the full daemon lifecycle.
``systemctl_poweroff`` is patched so the test runner doesn't actually
try to halt the host (which would be a very bad test side-effect).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestRequestPoweroff:
    """App.request_poweroff() calls systemctl_poweroff()."""

    def test_calls_systemctl_poweroff(self) -> None:
        # Import lazily so this test runs even on hosts where
        # microjs8 isn't installed system-wide (CI, dev boxes).
        from microjs8 import app as app_module

        # We instantiate a bare App via __new__ to avoid pulling in
        # all the config / display / GPS init that a real App() call
        # triggers. request_poweroff only needs the logger, so this
        # is a safe shortcut.
        instance = app_module.MicroJS8App.__new__(app_module.MicroJS8App)

        with patch.object(app_module, "systemctl_poweroff") as mock_poweroff:
            instance.request_poweroff()
            mock_poweroff.assert_called_once_with()

    def test_does_not_set_stop_event_directly(self) -> None:
        # request_poweroff relies on systemd to SIGTERM us; it shouldn't
        # set the stop event directly. systemd's SIGTERM handler (which
        # is request_stop) does that for us. This separation matters
        # because we want the daemon to still be running until systemd
        # tells us to stop — that way the operator's UI doesn't freeze
        # before systemctl poweroff has been invoked.
        from microjs8 import app as app_module

        instance = app_module.MicroJS8App.__new__(app_module.MicroJS8App)
        # Stub the stop event so we can verify it's NOT set.
        mock_stop = MagicMock()
        mock_stop.is_set.return_value = False
        instance._stop = mock_stop

        with patch.object(app_module, "systemctl_poweroff"):
            instance.request_poweroff()

        mock_stop.set.assert_not_called()

    def test_logs_poweroff_request(self, caplog) -> None:
        # The poweroff is significant — log it at WARNING so it's
        # visible in journalctl without -v even on a quiet system.
        from microjs8 import app as app_module

        instance = app_module.MicroJS8App.__new__(app_module.MicroJS8App)

        with patch.object(app_module, "systemctl_poweroff"):
            with caplog.at_level("WARNING"):
                instance.request_poweroff()

        # Find at least one WARNING-or-above record mentioning poweroff.
        poweroff_records = [
            r for r in caplog.records
            if r.levelno >= 30 and "poweroff" in r.message.lower()
        ]
        assert poweroff_records, (
            "expected a WARNING-level log message mentioning poweroff; "
            f"got records: {[(r.levelname, r.message) for r in caplog.records]}"
        )


class TestRequestStopUnchanged:
    """request_stop and request_restart behave exactly as in v0.0.12."""

    def test_request_stop_sets_stop_event(self) -> None:
        from microjs8 import app as app_module

        instance = app_module.MicroJS8App.__new__(app_module.MicroJS8App)
        mock_stop = MagicMock()
        mock_stop.is_set.return_value = False
        instance._stop = mock_stop

        instance.request_stop()
        mock_stop.set.assert_called_once()

    def test_request_stop_idempotent(self) -> None:
        # Calling twice should only fire .set() once (the guard in
        # request_stop short-circuits the second call).
        from microjs8 import app as app_module

        instance = app_module.MicroJS8App.__new__(app_module.MicroJS8App)
        mock_stop = MagicMock()
        # First call: not set yet. Second call: now set.
        mock_stop.is_set.side_effect = [False, True]
        instance._stop = mock_stop

        instance.request_stop()
        instance.request_stop()
        mock_stop.set.assert_called_once()
