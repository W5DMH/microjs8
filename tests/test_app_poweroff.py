"""Tests for v0.0.13+ HOME EXIT button -> graceful Pi poweroff.

What changed in v0.0.13:
  - MicroJS8App.request_poweroff() method that calls
    microjs8.input.systemctl_poweroff() (an async helper).
  - InputRouter wired with request_exit=app.request_poweroff (was
    app.request_stop in v0.0.12 and earlier).

What changed in v0.0.14:
  - The async helper is now properly scheduled on the running event
    loop via asyncio.create_task() instead of a bare call. The bare
    call created a coroutine object that was never executed, so the
    Pi never shut down. v0.0.14 fixes the scheduling.

What stays the same:
  - request_stop() is unchanged -- still used by SIGTERM handlers
    and any direct 'systemctl stop microjs8' invocation.
  - request_restart() is unchanged.
  - The EXIT_CONFIRM modal's NO/YES focus behavior is unchanged.

The tests below patch both systemctl_poweroff and asyncio.create_task
so they don't need a running event loop or actually halt the host.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestRequestPoweroff:
    """App.request_poweroff() schedules systemctl_poweroff() as a task."""

    def test_schedules_systemctl_poweroff_as_task(self) -> None:
        # Import lazily so the test runs even on hosts where
        # microjs8 isn't installed system-wide (CI, dev boxes).
        from microjs8 import app as app_module

        # Instantiate a bare App via __new__ to avoid pulling in all
        # the config / display / GPS init that a real App() call
        # triggers. request_poweroff only needs the logger and the
        # imported names at module scope, so this shortcut is safe.
        instance = app_module.MicroJS8App.__new__(app_module.MicroJS8App)

        # A sentinel object stands in for the coroutine that
        # systemctl_poweroff would normally return. We don't care
        # what the object IS, only that the same object reaches
        # asyncio.create_task -- proving the full chain works.
        fake_coro = object()

        # Force plain MagicMock (NOT AsyncMock) for the helper patch.
        # unittest.mock auto-detects that systemctl_poweroff is
        # 'async def' and substitutes AsyncMock by default. AsyncMock
        # returns its own coroutine when called -- our return_value
        # sentinel never reaches create_task, so the assertion fails
        # with a confusing "coroutine vs sentinel" mismatch. Using
        # MagicMock explicitly makes the helper a plain callable that
        # returns the sentinel directly.
        plain_helper = MagicMock(return_value=fake_coro)
        with patch.object(
            app_module, "systemctl_poweroff", plain_helper,
        ), patch(
            "asyncio.create_task",
        ) as mock_create_task:
            instance.request_poweroff()

        # The helper was invoked (which would have created the coroutine).
        plain_helper.assert_called_once_with()
        # And create_task was invoked with that coroutine.
        # This catches the v0.0.14 bug: the previous code called
        # systemctl_poweroff() without scheduling it, so create_task
        # was never invoked.
        mock_create_task.assert_called_once_with(fake_coro)

    def test_does_not_set_stop_event_directly(self) -> None:
        # request_poweroff relies on systemd to SIGTERM us; it
        # shouldn't set the stop event directly. systemd's SIGTERM
        # handler (which is request_stop) does that for us. This
        # separation matters because we want the daemon to still be
        # running until systemd tells us to stop -- that way the
        # operator's UI doesn't freeze before systemctl poweroff has
        # been invoked.
        from microjs8 import app as app_module

        instance = app_module.MicroJS8App.__new__(app_module.MicroJS8App)
        # Stub the stop event so we can verify it's NOT set.
        mock_stop = MagicMock()
        mock_stop.is_set.return_value = False
        instance._stop = mock_stop

        with patch.object(app_module, "systemctl_poweroff"), \
                patch("asyncio.create_task"):
            instance.request_poweroff()

        mock_stop.set.assert_not_called()

    def test_logs_poweroff_request(self, caplog) -> None:
        # The poweroff is significant -- log it at WARNING so it's
        # visible in journalctl without -v even on a quiet system.
        from microjs8 import app as app_module

        instance = app_module.MicroJS8App.__new__(app_module.MicroJS8App)

        with patch.object(app_module, "systemctl_poweroff"), \
                patch("asyncio.create_task"):
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
