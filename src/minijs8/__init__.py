"""MiniJS8 — JS8 transceiver application for Raspberry Pi.

Top-level package. Concrete subsystems live in submodules:

  audio    — PortAudio capture/playback (Step 5)
  modem    — GFSK8 wrapper (Step 5)
  cat      — QDX TS-480 CAT control (Step 6)
  gps      — u-blox NMEA reader (Step 4)
  protocol — JS8 directed-message grammar, ACK rules (Step 6)
  store    — SQLite append-only message log (Step 5)
  ui       — ST7789 display + screen state machine (Step 2)
  input    — USB keyboard + GPIO buttons (Step 3)

The orchestrator (`app.py`) wires these modules together inside a single
asyncio event loop, with a dedicated thread for the audio I/O hot path.
See MiniJS8_Build_Specification.md §3 for the full architecture.
"""

from minijs8.version import __version__

__all__ = ["__version__"]
