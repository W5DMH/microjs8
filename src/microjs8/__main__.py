"""Entry point for ``python -m microjs8``.

systemd's ``ExecStart`` invokes this module; it can also be run by hand
during development. Argument parsing is intentionally minimal — the
real configuration lives in the TOML file, not on the command line.

Exit codes:
  0  clean shutdown (SIGTERM / SIGINT received)
  1  unhandled exception during run
  2  configuration error (refused to start)
  3  --version / --help printed (treated as success by systemd anyway)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from microjs8 import __version__, config, logging_setup
from microjs8.app import MicroJS8App


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="microjs8",
        description="MicroJS8 — JS8 transceiver for Raspberry Pi.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"MicroJS8 {__version__}",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate the configuration and exit without entering the run loop",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help=(
            "run a hardware + configuration diagnostic and exit. "
            "Useful as the first thing to run on a fresh install."
        ),
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help=(
            "skip display/button initialisation; useful for host-side "
            "debugging without GPIO/SPI"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    logging_setup.setup()
    log = logging.getLogger("microjs8.main")

    # --doctor runs BEFORE config.load() so the diagnostic still
    # produces a useful report even when the config is broken.
    # The doctor's check_config() does its own config.load() and
    # surfaces the failure as a structured FAIL line.
    if args.doctor:
        from microjs8 import doctor
        return doctor.run_diagnostic_report()

    try:
        cfg = config.load()
    except config.ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2

    if args.check_config:
        log.info("configuration OK; exiting because --check-config was given")
        return 0

    app = MicroJS8App(cfg, headless=args.headless)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        # asyncio's signal handler will normally beat us to this; this
        # branch covers the Python<3.11 case and the rare race where
        # SIGINT lands before add_signal_handler() is in place.
        log.info("interrupted; shutting down")
    except Exception:
        log.exception("unhandled exception in run loop")
        return 1

    # v0.0.9: the daemon exit code differentiates a clean shutdown
    # (0 — operator clicked EXIT, or SIGTERM from systemctl stop;
    # systemd's Restart=on-failure leaves us down) from a restart
    # request (75 — radio profile cycled; systemd's
    # RestartForceExitStatus=75 brings us back with the new config).
    return app.exit_code


if __name__ == "__main__":
    sys.exit(main())
