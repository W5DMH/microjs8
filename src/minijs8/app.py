"""MiniJS8 application orchestrator.

Step 3 wires:
  - ``UIState`` — single source of UI truth.
  - ``DisplayDevice`` + ``RenderThread`` — owns SPI traffic in a
    dedicated thread.
  - ``ButtonWatcher`` — translates GPIO button events into UI commands
    and the shutdown gesture.
  - ``KeyboardThread`` — reads /dev/input/by-id/*-event-kbd in a
    dedicated thread, emits typed KeyEvents into the asyncio loop.
  - ``InputRouter`` — dispatches KeyEvents into UIState mutations.

On startup, if the station is unconfigured (N0CALL or empty grid),
the daemon force-jumps to the SETUP screen and locks the screen ring
until either the operator completes Call+Grid editing OR uses the
"[EMERGENCY BEACON →]" bypass.

Future steps add:
  - GPS NMEA reader (Step 4) — pushes (lat, lon, fix, grid) into UIState
  - Modem decode → protocol layer (Step 5) — pushes heard list, msgs
  - Beacon scheduler, CAT, message store, TX (Step 6)
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Optional

from minijs8 import __version__, config as config_mod
from minijs8.audio import (
    AudioCapture,
    AudioPlayback,
    PlaybackError,
    RadioDeviceNotFound,
    find_radio_input_device,
)
from minijs8.cat import CatService, PttService, build_ptt_service, get_radio
from minijs8.config import Config, ConfigError
from minijs8.gps import GpsFix, GpsReader
from minijs8.input import (
    ButtonWatcher,
    InputRouter,
    KeyboardThread,
    KeyEvent,
    systemctl_poweroff,
)
from minijs8.modem import DecodeThread
from minijs8.paths import data_dir, inbox_db_path
from minijs8.protocol import (
    DecodedFrame,
    FrameKind,
    HeardStation,
    distance_and_bearing,
    parse as parse_frame,
)
from minijs8.protocol.grammar import (
    is_query_msgs,
    parse_msg,
    parse_msg_to,
    parse_query_msg_id,
)
from minijs8.activity import DirectedActivityLog, Direction
from minijs8.protocol.reassembly import (
    AssembledMessage,
    MessageAssembler,
    is_buffered_protocol_frame,
)
from minijs8.store import MessageStore
from minijs8.store.inbox import MailboxStore, MailboxError
from minijs8.timing import TimingTracker
from minijs8.tx import (
    EncodedAudioCache,
    EncodeWorker,
    OutboundKind,
    OutboundQueue,
    RealTxBackend,
    TxSafetyGate,
    TxScheduler,
    default_chrony_ok,
)
from minijs8.ui import (
    DirectedRow,
    DisplayDevice,
    RenderThread,
    Screen,
    UIState,
    load_fonts,
)

_log = logging.getLogger(__name__)

_SHUTDOWN_GRACE_SEC = 3.0


class MiniJS8App:
    """Owns the asyncio loop and the lifecycle of all subsystems."""

    def __init__(self, config: Config, *, headless: bool = False) -> None:
        self._config = config
        self._headless = headless
        self._stop = asyncio.Event()
        self._ui_state: Optional[UIState] = None
        self._render_thread: Optional[RenderThread] = None
        self._buttons: Optional[ButtonWatcher] = None
        self._display: Optional[DisplayDevice] = None
        self._keyboard: Optional[KeyboardThread] = None
        self._router: Optional[InputRouter] = None
        self._gps: Optional[GpsReader] = None
        # Step 5: audio capture + decode pipeline + message store.
        self._audio: Optional[AudioCapture] = None
        self._decode: Optional[DecodeThread] = None
        self._store: Optional[MessageStore] = None
        # Inbox / mailbox store — separate SQLite file from the
        # decode log because the lifecycles are different (decodes
        # are retention-swept, inbox rows persist indefinitely).
        # Schema is JS8Call-compatible (see store/inbox.py).
        self._mailbox: Optional[MailboxStore] = None
        # Multi-frame message reassembler. Drives the inbox state
        # machine for buffered commands (MSG, MSG TO:, QUERY MSGS,
        # etc.). Constructed once at app startup; lock-free because
        # only the asyncio decode handler feeds it.
        self._assembler: MessageAssembler = MessageAssembler()
        # Directed activity log — chronological, in-memory, bounded.
        # Captures the BACK-AND-FORTH of protocol-level directed
        # exchanges with our station: SNR?, INFO, GRID?, QUERY MSGS,
        # QUERY MSG <id>, ACKs, etc. MSG / MSG TO: content lives in
        # the mailbox DB instead — this log is for the surrounding
        # protocol activity so the operator can see the round-trip.
        # Default cap of 200 entries (~40 hours of typical activity);
        # snapshot pushed to UI on every record_in/record_out.
        self._directed_activity: DirectedActivityLog = DirectedActivityLog()
        self._retention_task: Optional[asyncio.Task[None]] = None
        # Periodic non-buffered reassembly sweep — fires every 5 s
        # so timed-out non-buffered buffers (YES, NO, INFO, STATUS,
        # etc.) get emitted promptly even on a quiet channel.
        self._reassembly_sweep_task: Optional[asyncio.Task[None]] = None
        # Step 6: CAT + TX pipeline.
        self._cat: Optional[PttService] = None
        self._playback: Optional[AudioPlayback] = None
        self._outbound_queue: Optional[OutboundQueue] = None
        self._tx_scheduler: Optional[TxScheduler] = None
        # Encode-at-queue-time worker: renders audio off the slot-
        # aligned scheduler tick so frame 1 isn't delayed by the
        # ~3 second encode pass.
        self._encoded_audio_cache: Optional[EncodedAudioCache] = None
        self._encode_worker: Optional[EncodeWorker] = None
        # Step 6b Phase A: track decoded-frame timing offsets so we can
        # measure how far our local clock / TX pipeline is off from the
        # network consensus. The tracker is fed by the decode dispatcher
        # below and queried periodically for the running median dt.
        self._timing_tracker: TimingTracker = TimingTracker()
        self._timing_log_task: Optional[asyncio.Task[None]] = None
        # Sounddevice index for the QDX (cached after audio discovery so
        # we can use the same device for capture AND playback).
        self._radio_audio_index: Optional[int] = None

    def request_stop(self) -> None:
        if not self._stop.is_set():
            _log.info("shutdown requested")
            self._stop.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.request_stop)

        _log.info(
            "MiniJS8 %s starting | callsign=%s grid=%s tx_allowed=%s",
            __version__,
            self._config.station.callsign,
            self._config.station.grid or "(unset)",
            self._config.tx_allowed,
        )

        if not self._config.tx_allowed:
            _log.info(
                "TX is disabled until callsign and grid are configured "
                "(use the on-device Setup screen, or edit %s)",
                self._config.source_path,
            )

        # ── UI state ────────────────────────────────────────────────
        self._ui_state = UIState(
            callsign=self._config.station.callsign,
            grid=self._config.station.grid,
            tx_allowed=self._config.tx_allowed,
            units=self._config.units_distance,
        )
        # Seed the radio selector from the loaded config. Cycling on
        # the Setup screen saves a new value and exits the daemon —
        # systemd brings us back with the new radio path active.
        self._ui_state.set_radio_id(self._config.radio_id)

        # Force the operator into Setup on first boot. They can either
        # complete the form, or hit the "[EMERGENCY BEACON →]" button.
        if not self._config.tx_allowed:
            self._ui_state.set_screen(Screen.SETUP)

        # ── Input router ────────────────────────────────────────────
        # Note: set_frequency callback is supplied later via
        # _wire_router_set_frequency() after the CAT service starts.
        # Construct here so the UI can edit non-frequency fields even
        # if CAT never comes up.
        self._router = InputRouter(
            self._ui_state,
            save_config=self._save_config_sync,
            emergency_bypass=self._trigger_emergency_bypass,
            cycle_radio=self._cycle_radio_sync,
            mark_inbox_read=self._mark_inbox_read_sync,
            delete_inbox_row=self._delete_inbox_row_sync,
            compose_send=self._compose_send_sync,
        )

        # ── Display, buttons, keyboard, GPS ──────────────────────────
        self._start_display_thread_best_effort()
        self._start_buttons_best_effort(loop)
        self._start_keyboard_thread_best_effort(loop)
        self._start_gps_reader_best_effort(loop)

        # ── Step 5: message store + audio + decode + retention ───────
        self._start_message_store_best_effort()
        self._start_mailbox_store_best_effort()
        self._populate_initial_ui_lists()
        self._start_audio_and_decode_best_effort(loop)
        self._start_retention_task(loop)
        # Phase A timing-consensus tracker — logs the running median
        # of decoded-frame dt values once per minute. No behavior
        # change; pure observation.
        self._start_timing_log_task(loop)

        # ── Step 6: CAT + TX backend + outbound queue + scheduler ────
        self._start_cat_service_best_effort()
        self._start_tx_pipeline_best_effort()

        try:
            await self._stop.wait()
        finally:
            await self._cleanup_with_grace()
            _log.info("MiniJS8 %s stopped cleanly", __version__)

    # ── Subsystem starters ───────────────────────────────────────────

    def _start_display_thread_best_effort(self) -> None:
        if self._headless:
            _log.info("running headless — display thread skipped")
            return
        try:
            self._display = DisplayDevice.open()
        except Exception:
            _log.exception(
                "could not initialise display — daemon continuing headless. "
                "check SPI is enabled and the panel is seated firmly."
            )
            return
        try:
            fonts = load_fonts()
        except Exception:
            _log.exception("font load failed — display thread skipped")
            return
        assert self._ui_state is not None
        self._render_thread = RenderThread(self._display, self._ui_state, fonts)
        self._render_thread.start()

    def _start_buttons_best_effort(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._headless:
            _log.info("running headless — buttons skipped")
            return
        assert self._ui_state is not None
        try:
            self._buttons = ButtonWatcher(
                self._ui_state, loop, shutdown_callback=systemctl_poweroff,
            )
            self._buttons.start()
        except Exception:
            _log.exception(
                "could not initialise buttons — keyboard navigation still works"
            )
            self._buttons = None

    def _start_keyboard_thread_best_effort(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the USB keyboard reader thread.

        The thread itself handles 'no keyboard plugged in' gracefully —
        it just retries discovery every 2 s, so we don't need to skip
        the start in headless or no-keyboard cases.
        """
        if self._headless:
            _log.info("running headless — keyboard thread skipped")
            return
        assert self._router is not None
        try:
            self._keyboard = KeyboardThread(loop, self._router.handle)
            self._keyboard.start()
        except Exception:
            _log.exception(
                "could not start keyboard thread — TFT buttons still work"
            )
            self._keyboard = None

    def _start_gps_reader_best_effort(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the GPS reader thread.

        The thread handles "gpsd not yet up / dongle not plugged in"
        gracefully via its 2 s reconnect loop, so we always start it.
        Headless mode skips it (no GPS on a build host).
        """
        if self._headless:
            _log.info("running headless — gps reader skipped")
            return
        assert self._ui_state is not None
        try:
            self._gps = GpsReader(loop, self._on_gps_fix)
            self._gps.start()
        except Exception:
            _log.exception(
                "could not start GPS reader — daemon continuing without GPS"
            )
            self._gps = None

    # ── Step 5 starters ──────────────────────────────────────────────

    def _start_message_store_best_effort(self) -> None:
        """Open the SQLite message store.

        Database lives at ``$MINIJS8_DATA_DIR/messages.db`` (typically
        /var/minijs8/messages.db). Failure to open is logged but does
        not abort the daemon — we'll just have no decode persistence
        and the heard list / directed list will be empty.
        """
        try:
            db_path = data_dir() / "messages.db"
            self._store = MessageStore(db_path)
            _log.info("message store opened: %s", db_path)
        except Exception:
            _log.exception(
                "could not open message store — decodes will not be persisted"
            )
            self._store = None

    def _start_mailbox_store_best_effort(self) -> None:
        """Open the inbox / mailbox SQLite database.

        Lives at ``$MINIJS8_DATA_DIR/inbox.db`` (typically
        /var/minijs8/inbox.db), separate from messages.db so the two
        have independent retention policies. Schema is JS8Call-
        compatible (see store/inbox.py).

        Failure to open is logged but does not abort the daemon — the
        daemon stays operational, but inbound MSG / MSG TO: directives
        will be classified and ACK'd, just not persisted to the inbox.
        """
        try:
            mb_path = inbox_db_path()
            self._mailbox = MailboxStore(mb_path)
            _log.info("mailbox store opened: %s", mb_path)
        except Exception:
            _log.exception(
                "could not open mailbox store — inbox messages will not "
                "be persisted"
            )
            self._mailbox = None

    def _populate_initial_ui_lists(self) -> None:
        """Pre-fill heard list + inbox from the stores on boot.

        Without this the operator would see "No stations heard yet"
        on the first display refresh after a reboot, even if there's
        recent traffic. Heard list comes from messages.db; inbox
        comes from inbox.db (MailboxStore).
        """
        if self._store is None or self._ui_state is None:
            return
        try:
            heard = tuple(self._store.heard_stations(limit=50))
            self._ui_state.set_heard(heard)
            _log.info("preloaded UI lists: %d heard", len(heard))
        except Exception:
            _log.exception("could not pre-populate heard list")

        # Inbox preload — separate try/except so a mailbox-store
        # failure doesn't keep the heard list from rendering.
        if self._mailbox is not None:
            self._refresh_inbox_ui()

    def _start_audio_and_decode_best_effort(
        self, loop: asyncio.AbstractEventLoop
    ) -> None:
        """Open the QDX (or DigiRig) audio device + start the decode thread.

        The audio device is required for any decoded traffic. If we
        can't find one, we log loudly and the daemon runs without
        audio — the operator can plug in the radio later, but they'll
        need to restart the daemon to pick it up. (Hot-plug audio
        device handling is not in scope for Step 5.)
        """
        if self._headless:
            _log.info("running headless — audio + decode skipped")
            return
        # Look up the radio's preferred audio card. If the radio uses
        # external audio (DigiRig CM108), this picks "Device" so we
        # don't accidentally grab the radio's own USB audio (which
        # the G90 has but we don't want for a DigiRig setup).
        preferred_substring: Optional[str] = None
        preferred_label: Optional[str] = None
        try:
            radio = get_radio(self._config.radio_id)
            preferred_substring = radio.audio_card_substring
            preferred_label = radio.display_name
        except KeyError:
            _log.exception(
                "unknown radio_id in config — falling back to "
                "first-match audio device discovery",
            )
        try:
            device_index, label = find_radio_input_device(
                preferred_card_substring=preferred_substring,
                preferred_card_label=preferred_label,
            )
        except RadioDeviceNotFound:
            _log.warning(
                "no radio audio device — decode pipeline disabled. "
                "Plug in QDX/DigiRig and restart the daemon."
            )
            return
        except Exception:
            _log.exception("audio device discovery failed")
            return

        # Cache the index so playback (Step 6) can reuse it without
        # re-running discovery.
        self._radio_audio_index = device_index

        try:
            self._audio = AudioCapture(device_index)
            self._audio.start()
        except Exception:
            _log.exception(
                "could not open audio device — fail-loud per Step 5 "
                "design. Decode pipeline will not start."
            )
            self._audio = None
            return
        _log.info(
            "audio capture running on %s (sounddevice index %d)",
            label, device_index,
        )

        try:
            self._decode = DecodeThread(self._audio, loop, self._on_decoded_frame)
            self._decode.start()
        except Exception:
            _log.exception("could not start decode thread")
            if self._audio is not None:
                self._audio.stop()
                self._audio = None
            self._decode = None

    def _start_retention_task(self, loop: asyncio.AbstractEventLoop) -> None:
        """Background task: prune decodes older than retention_days."""
        if self._store is None:
            return
        self._retention_task = loop.create_task(self._retention_loop())
        # Also start the reassembly-sweep task. Non-buffered buffers
        # complete via timeout (~20s) — we need to drain them
        # periodically even when there's no decode traffic, so a
        # quiet channel doesn't leave a half-assembled message
        # invisible until something else triggers a feed() call.
        self._reassembly_sweep_task = loop.create_task(
            self._reassembly_sweep_loop()
        )

    async def _reassembly_sweep_loop(self) -> None:
        """Drain timed-out reassembly buffers and surface to operator.

        Runs every 5 seconds. Two responsibilities:

        1. **Non-buffered timeouts** (``sweep_completed``) — emits
           protocol-level directed messages (YES, NO, INFO, STATUS,
           HEARING, free-text) once their one-slot grace period
           expires. These get dispatched to the directed-activity
           log.

        2. **Buffered timeouts** (``sweep_timeouts``) — emits MSG /
           MSG TO: / QUERY* / CMD buffers whose CRC never validated.
           These would otherwise sit in the buffer dict forever
           (yesterday's bug: a 4-frame MSG with mid-space boundary
           failed CRC, sat invisible, operator wasted 5 minutes
           wondering why no inbox row appeared). We dispatch them
           with ``checksum_valid=False`` so ``_dispatch_assembled``
           routes to the directed-activity log with an "⚠ INCOMPLETE"
           tag — the operator visibly sees something arrived corrupt.

        Cheap: typical buffer count is 0-2. The sweep just checks
        last_frame_at against deadlines; no I/O.
        """
        try:
            while not self._stop.is_set():
                try:
                    completions = self._assembler.sweep_completed()
                except Exception:
                    _log.exception("reassembly sweep_completed raised")
                    completions = []
                try:
                    timeouts = self._assembler.sweep_timeouts()
                except Exception:
                    _log.exception("reassembly sweep_timeouts raised")
                    timeouts = []
                # Dispatch order: completions first (good messages),
                # then timeouts (corrupt). Operator sees them in
                # roughly arrival order this way.
                for assembled in completions + timeouts:
                    try:
                        # Pass a None frame — the assembler holds
                        # the original frequency on the message.
                        # _dispatch_assembled is robust to None
                        # frames for non-buffered AND for buffered-
                        # timeout messages (no SNR/freq read).
                        self._dispatch_assembled(assembled, None)
                    except Exception:
                        _log.exception(
                            "dispatch (timeout sweep) raised"
                        )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _retention_loop(self) -> None:
        """Run prune_older_than once at startup, then once per hour."""
        # Slight delay before the first sweep so the daemon has time
        # to settle. After that, fire every hour until shutdown.
        try:
            await asyncio.sleep(60.0)
            while not self._stop.is_set():
                if self._store is not None:
                    try:
                        n = await asyncio.to_thread(
                            self._store.prune_older_than,
                            self._config.retention_days,
                        )
                        if n:
                            _log.info("retention sweep: pruned %d old decodes", n)
                    except Exception:
                        _log.exception("retention sweep raised")
                # Wait an hour OR until stop is requested.
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=3600.0)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    def _start_timing_log_task(self, loop: asyncio.AbstractEventLoop) -> None:
        """Periodically log the timing-tracker consensus.

        Phase A only — measure-and-display. The scheduler does NOT
        consume this value yet. We just want operator visibility into
        what the network consensus says about our slot alignment.
        """
        self._timing_log_task = loop.create_task(self._timing_log_loop())

    async def _timing_log_loop(self) -> None:
        """Every minute, log the running median dt + sample count.

        Phase Y: also pushes the active time-source label to UIState
        so the TFT's header bar can show "UTC" vs "CONSENSUS". We
        consult the safety gate (when constructed) for the canonical
        decision — same answer the scheduler uses for slot timing,
        keeps the UI honest about which source is actually in use.
        """
        # Lazy import to avoid circular imports at module load.
        from minijs8.time_source import time_source_status

        try:
            # Wait a minute so the first decoder slot or two have come
            # in (otherwise we'd just log 'no samples yet' immediately).
            await asyncio.wait_for(self._stop.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            return
        try:
            while not self._stop.is_set():
                median = self._timing_tracker.median_dt()
                count = self._timing_tracker.sample_count()
                if median is None:
                    _log.info(
                        "timing consensus: insufficient samples (n=%d, "
                        "need >=3 to publish)",
                        count,
                    )
                else:
                    _log.info(
                        "timing consensus: median dt = %+.2fs (n=%d)",
                        median, count,
                    )
                # Publish the active time-source to UI. We compute it
                # the same way the safety gate does so the UI tag and
                # the gate's TX decision can't drift apart.
                if self._ui_state is not None:
                    try:
                        ts = time_source_status(
                            chrony_ok_fn=default_chrony_ok,
                            timing_tracker=self._timing_tracker,
                        )
                        self._ui_state.set_time_source(ts.source)
                    except Exception:
                        _log.exception("time_source_status raised")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=60.0)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    # ── Step 6 starters ──────────────────────────────────────────────

    def _start_cat_service_best_effort(self) -> None:
        """Connect to the configured radio's PTT service.

        The factory picks CatService (rigctld over TCP) or
        RtsPttService (direct pyserial RTS toggle) based on the
        radio's ``cat_required`` field:

          * QDX → CatService (rigctld with TS-480 emulation)
          * G90+DigiRig → CatService (rigctld with G90 driver +
            ``-P RTS`` PTT-via-RTS on the same port)
          * DigiRig RTS-only → RtsPttService (no rigctld; we just
            open the port and toggle RTS for FM walkies / uSDX /
            anything without CAT)

        For the CAT cases, rigctld is owned by systemd (launched by
        the launcher script ``minijs8-rigctld-launcher`` which inspects
        the same config). We just connect to localhost:4532. Failure
        is non-fatal — daemon runs RX-only with "CAT --" in the UI
        until the connection comes up. RTS-only failures are also
        non-fatal — the reconnect loop retries until the device shows
        up.
        """
        if self._headless:
            _log.info("running headless — PTT service skipped")
            return
        if self._ui_state is None:
            return
        try:
            radio = get_radio(self._config.radio_id)
        except KeyError:
            _log.exception(
                "unknown radio_id in config — PTT service disabled",
            )
            return
        try:
            self._cat = build_ptt_service(
                radio,
                on_status_change=self._on_cat_status_change,
            )
            self._cat.start()
        except Exception:
            _log.exception(
                "could not start PTT service for %s", radio.id,
            )
            self._cat = None
            return

        # Now that the service is wired, give the router the
        # set_frequency callback so the operator can change frequency
        # from Setup. RTS-only services soft-fail on set_frequency
        # (return False), which the router handles cleanly.
        self._wire_router_set_frequency()

    def _wire_router_set_frequency(self) -> None:
        """Plug the set_frequency callback into the existing router."""
        if self._router is None or self._cat is None:
            return
        # Late-binding hack: assign the optional callback. The router
        # was constructed without it.
        self._router._set_frequency = self._set_radio_frequency

    def _set_radio_frequency(self, hz: int) -> bool:
        """Router callback: change the QDX VFO frequency via CAT."""
        if self._cat is None:
            return False
        return self._cat.set_frequency_hz(hz)

    def _on_cat_status_change(self, connected: bool) -> None:
        """CatService callback: surface connection status to the UI.

        Runs on the CatService background thread; UIState mutators
        are thread-safe.
        """
        if self._ui_state is None:
            return
        self._ui_state.set_cat_connected(connected)
        # When CAT first connects, snapshot the radio's actual VFO
        # frequency and show it on the UI. This avoids displaying
        # 7.078 MHz when the operator has the radio tuned elsewhere.
        if connected and self._cat is not None:
            try:
                hz = self._cat.get_frequency_hz()
                if hz is not None:
                    self._ui_state.set_freq_hz(hz)
            except Exception:
                _log.debug("initial freq query failed", exc_info=True)

    def _start_tx_pipeline_best_effort(self) -> None:
        """Open audio playback, build TxBackend, scheduler.

        Runs after CAT and after audio discovery. Failure is non-fatal
        (RX-only mode still works).
        """
        if self._headless:
            _log.info("running headless — TX pipeline skipped")
            return
        if self._cat is None:
            _log.info(
                "CAT service unavailable — skipping TX pipeline. "
                "Receive-only mode."
            )
            return
        if self._radio_audio_index is None:
            _log.info(
                "no radio audio device — skipping TX pipeline. "
                "Receive-only mode."
            )
            return
        if self._store is None:
            _log.warning(
                "message store unavailable — TX pipeline cannot persist queue"
            )
            return

        # 1. Open audio playback on the same device as capture.
        try:
            self._playback = AudioPlayback(self._radio_audio_index)
            self._playback.start()
        except PlaybackError:
            _log.exception("could not start audio playback — TX disabled")
            self._playback = None
            return

        # 2. Look up the configured radio definition.
        try:
            radio = get_radio(self._config.radio_id)
        except KeyError:
            # Should never happen — config.py validates radio_id at load.
            _log.exception("unknown radio_id in config — TX disabled")
            return

        # 3. Build the real TX backend.
        # identity_factory pulls fresh callsign+grid from UIState at TX
        # time, so config edits via the Setup screen propagate without
        # restarting the daemon. The encoder needs both fields.
        backend = RealTxBackend(
            cat=self._cat,
            playback=self._playback,
            radio=radio,
            identity_factory=self._tx_identity,
        )

        # 4. Outbound queue, sharing the message store's connection.
        self._outbound_queue = OutboundQueue(self._store.connection)

        # Recover from any prior daemon run: encoded-audio cache is
        # in-memory only, so any rows in QUEUED state from before the
        # restart have lost their cached audio. Push them back to
        # ENCODING so the worker re-renders. Also handles ENCODING
        # rows that never finished (rare — daemon died mid-encode).
        try:
            n_reset = self._outbound_queue.reset_unencoded_to_encoding()
            if n_reset > 0:
                _log.info(
                    "encode recovery: %d row(s) reset to ENCODING "
                    "(audio cache lost across restart)",
                    n_reset,
                )
        except Exception:
            _log.exception(
                "encode recovery failed; encode worker will still "
                "process whatever rows happen to be in ENCODING state",
            )

        # 5. Encoded-audio cache + worker. The worker runs the
        # ~3-second encode off the slot-aligned hot path so the
        # scheduler can pick up rows with audio already rendered.
        # Without this, frame 1 of every burst lands ~3s late on
        # the Pi Zero 2W (encoder ran inline in scheduler tick).
        self._encoded_audio_cache = EncodedAudioCache()
        self._encode_worker = EncodeWorker(
            queue=self._outbound_queue,
            backend=backend,
            cache=self._encoded_audio_cache,
        )
        self._encode_worker.start()

        # 6. Safety gate. Phase Y: pass the timing tracker so the
        # gate can fall back to consensus when chrony is unavailable
        # (basement / no-GPS scenarios) and so the scheduler can read
        # the same TimeSource decision for slot-grid alignment.
        gate = TxSafetyGate(
            self._ui_state,
            timing_tracker=self._timing_tracker,
        )

        # 7. Scheduler.
        self._tx_scheduler = TxScheduler(
            queue=self._outbound_queue,
            backend=backend,
            safety_gate=gate,
            encoded_audio_cache=self._encoded_audio_cache,
        )
        self._tx_scheduler.start()
        _log.info("TX pipeline running")

    def _tx_identity(self) -> Optional[tuple[str, str]]:
        """Identity factory used by TxBackend at every TX call.

        Returns the current callsign + grid as seen by UIState (which
        reflects any operator edits made since boot), or None if
        identity is not configured. Single-shot — recomputed at each
        TX so config changes don't require a daemon restart.
        """
        if self._ui_state is None:
            return None
        snap = self._ui_state.snapshot()
        if not snap.callsign or snap.callsign == "N0CALL":
            return None
        if not snap.grid:
            return None
        return snap.callsign, snap.grid

    # ── Callbacks invoked by the router ──────────────────────────────

    def _save_config_sync(self, callsign: str, grid: str, units: str) -> bool:
        """Atomic config save invoked from the asyncio thread.

        Returns True on success, False on validation or write error.
        On success, refreshes the in-memory Config and the UIState
        identity so the operator sees the change immediately.
        """
        try:
            new_cfg = config_mod.save_atomic(callsign, grid, units)
        except ConfigError as exc:
            _log.warning("config save rejected: %s", exc)
            return False
        except Exception:
            _log.exception("unexpected error during config save")
            return False

        self._config = new_cfg
        if self._ui_state is not None:
            self._ui_state.set_identity(
                new_cfg.station.callsign,
                new_cfg.station.grid,
                new_cfg.units_distance,
                new_cfg.tx_allowed,
            )
        return True

    def _cycle_radio_sync(self) -> bool:
        """Cycle to the next radio profile and restart the daemon.

        Called by the router when the operator presses Enter on the
        "Radio" row of the Setup screen. The flow:

          1. Read current radio_id from UIState
          2. Pick the next id from ``known_radio_ids()`` (wraps)
          3. Write config.toml atomically (preserves callsign/grid/units)
          4. Update UIState so the operator sees the new value
          5. Schedule a clean daemon exit (sys.exit(0)) — systemd's
             ``Restart=always`` brings us back up, this time loading
             the new radio_id and constructing the matching PTT
             service (CatService for QDX/G90, RtsPttService for the
             RTS-only path).

        Returns True if the save succeeded (and exit was scheduled);
        False on validation/write error (no exit, no state change
        the operator can see).

        Why exit instead of restarting in-place? The PTT factory
        consults radio_id ONCE at startup. Switching radios at
        runtime would require tearing down the entire TX pipeline,
        recreating PttService + audio device + scheduler + encode
        worker — many threads, many handles, many failure modes.
        A clean exit is dramatically simpler and avoids a class of
        partial-rewire bugs. The cost is ~5 seconds of downtime
        while systemd restarts us, which the operator already
        expects since they're in Setup.

        Why this is safe even mid-TX: it's vanishingly unlikely
        the operator is mid-TX while changing radio in Setup.
        Defense-in-depth is in the systemd unit anyway: stopping
        the daemon releases PTT (CatService.stop / RtsPttService.stop
        both send a final PTT-off before closing).
        """
        if self._ui_state is None:
            return False
        # Lazy import to avoid pulling cat at module-load time during
        # headless tests that don't exercise the radio registry.
        from minijs8.cat.radios import known_radio_ids

        snap = self._ui_state.snapshot()
        ids = known_radio_ids()
        if not ids:
            _log.warning("cycle_radio: registry is empty; no-op")
            return False
        try:
            idx = ids.index(snap.radio_id)
        except ValueError:
            # Current id isn't in the registry — shouldn't happen if
            # config validation accepted it, but be defensive.
            _log.warning(
                "cycle_radio: current id %r not in registry; "
                "resetting to first entry", snap.radio_id,
            )
            idx = -1
        next_id = ids[(idx + 1) % len(ids)]

        try:
            new_cfg = config_mod.save_atomic(
                snap.callsign, snap.grid, snap.units, radio_id=next_id,
            )
        except ConfigError as exc:
            _log.warning("cycle_radio: save rejected: %s", exc)
            return False
        except Exception:
            _log.exception("cycle_radio: unexpected error during save")
            return False

        self._config = new_cfg
        self._ui_state.set_radio_id(new_cfg.radio_id)
        _log.warning(
            "cycle_radio: %s → %s — exiting cleanly so systemd "
            "restarts us with the new radio path",
            snap.radio_id, new_cfg.radio_id,
        )
        # Schedule the exit on the asyncio loop so we don't kill the
        # daemon mid-handler (the router would observe a half-cleaned
        # state on its way out). _request_exit walks through the
        # normal cleanup path (stop scheduler / encode worker / CAT
        # / playback / etc.) before calling sys.exit.
        self._request_clean_exit_for_radio_change()
        return True

    def _request_clean_exit_for_radio_change(self) -> None:
        """Schedule a clean shutdown that exits with code 0.

        Called by ``_cycle_radio_sync`` after the new radio is saved
        to config. systemd's ``Restart=always`` directive on the
        minijs8.service unit brings us back up with the new radio
        path active.

        Why this is its own helper rather than inlined: keeps the
        systemd-coupling explicit in one place. If the restart policy
        ever changes (or we add a different reason to want a clean
        exit), this is the spot to update.

        Implementation: sets the same asyncio.Event used by SIGTERM /
        SIGINT (``self._stop``). The main run loop awaits this event,
        falls through to the cleanup path, and ``run()`` returns
        cleanly. ``__main__.py`` translates a clean return to exit
        code 0.

        Thread safety: the cycle handler runs on the asyncio thread
        (router callbacks are scheduled there), so a direct call to
        ``request_stop()`` is safe. We don't need
        ``call_soon_threadsafe`` here.
        """
        self.request_stop()

    def _trigger_emergency_bypass(self) -> None:
        """Activate the unconfigured-emergency override.

        Called when the operator selects [EMERGENCY BEACON →] on the
        Setup screen. This is a one-way trip — only a reboot clears
        the override (per spec, no accidental abandonment of an
        emergency).
        """
        _log.warning("EMERGENCY BYPASS ACTIVATED — N0CALL identity, awaiting GPS fix")
        if self._ui_state is not None:
            self._ui_state.trigger_emergency_override()

    def _on_gps_fix(self, fix: GpsFix) -> None:
        """Callback for the GPS reader thread.

        Runs on the asyncio thread (call_soon_threadsafe in the reader).
        Updates UIState; the render thread picks up the dirty flag and
        redraws if anything visible changed.
        """
        if self._ui_state is not None:
            self._ui_state.set_gps(fix)

    def _on_decoded_frame(self, frame: DecodedFrame) -> None:
        """Callback for each decoded JS8 frame.

        Runs on the asyncio thread. Pipeline:
          1. Parse the frame text into a ParsedFrame.
          2. Persist the decode in SQLite.
          3. If the frame has a callsign, upsert the heard-station row
             (with distance + bearing computed from configured grid).
          4. If the frame is directed-to-us, push it onto the
             directed list.
          5. Refresh the UI's heard list snapshot (cap at 50 rows).

        All wrapped in try/except so a single bad frame doesn't bring
        down the pipeline.
        """
        try:
            our_call = self._config.station.callsign or None
            parsed = parse_frame(frame, our_call)
            _log.info(
                "decoded: kind=%s from=%s to=%s body=%r snr=%d freq=%.1f dt=%+.2f",
                parsed.kind.value, parsed.from_call, parsed.to_call,
                parsed.body[:40], frame.snr_db, frame.frequency_hz,
                frame.dt_seconds,
            )

            # Step 6b Phase A: feed dt into the consensus tracker. Every
            # decode contributes regardless of from-call, kind, or SNR
            # — if it decoded cleanly it's a valid timing reference.
            self._timing_tracker.add(frame.dt_seconds)

            if self._store is not None:
                # Persist the decode. SQLite write is fast (<1ms in WAL
                # mode for inserts of this size) — we do it inline
                # rather than via to_thread to keep ordering simple.
                try:
                    self._store.insert_decode(parsed)
                except Exception:
                    _log.exception("insert_decode failed")

            # Upsert heard-station if we have a from-call.
            if parsed.from_call:
                self._update_heard_for(parsed)

            # ── Inbox / mailbox dispatch (Phase 1+2) ──────────────
            #
            # JS8Call's directed-message protocol drives an inbox
            # state machine. The decode handler is responsible for:
            #
            #   - "<from>: <us> MSG <text>"
            #         → store as UNREAD in our inbox; auto-ACK back.
            #   - "<from>: <us> MSG TO:<dest> <text>"
            #         → hold for <dest> as STORE; auto-ACK back to
            #           the originating <from>.
            #   - "<from>: <us|@ALLCALL> QUERY MSGS"
            #         → if we hold STORE rows for <from>, reply with
            #           the oldest msg id; if direct-to-us with no
            #           holding, reply NO. Silent on broadcast-empty
            #           (don't pollute the band).
            #   - "<from>: <us> QUERY MSG <id>"
            #         → if <id> is STORE-held for <from>, deliver the
            #           body. (No state transition yet; ACK match
            #           transitions to DELIVERED below.)
            #   - "<from>: <us> ACK"
            #         → already handled below (existing outbound-
            #           queue match). Inbox state transitions to
            #           DELIVERED handled there as well.
            #
            # Multi-frame reassembly happens HERE:
            # ─────────────────────────────────────
            # JS8 messages longer than ~13 chars span multiple
            # consecutive 15-s slots. The decoder hands us each
            # frame independently, so we feed every parsed frame
            # to the MessageAssembler and only dispatch when it
            # tells us a buffered command is complete + checksum-
            # valid. ACK fires only on validated checksum — that's
            # the JS8Call protocol contract.
            try:
                assembled_list = self._assembler.feed(parsed)
            except Exception:
                _log.exception("reassembler.feed raised")
                assembled_list = []
            for assembled in assembled_list:
                self._dispatch_assembled(assembled, frame)

            # Single-frame ACK logging at the decode handler.
            # ──────────────────────────────────────────────
            # ACKs are intentionally NOT routed through the
            # reassembler — they're always single-frame and the
            # outbound-queue match in step 6 needs to fire
            # immediately so the scheduler's WAIT_ACK transitions
            # to DELIVERED without delay. The reassembler's non-
            # buffered timeout (~20s) would push that latency past
            # the scheduler's retry window.
            #
            # Other directed frames (DIRECTED_MESSAGE / COMMAND /
            # QUERY) DO route through the reassembler — they may be
            # multi-frame ("YES MSG ID 57") and the operator wants
            # the complete body in the activity log. Those get
            # logged from _dispatch_assembled when emitted.
            try:
                if (
                    parsed.is_for_us
                    and parsed.kind is FrameKind.ACK
                    and not is_buffered_protocol_frame(parsed)
                ):
                    self._log_directed_in(parsed)
            except Exception:
                _log.exception("directed-activity log (inbound ACK) failed")

            # Step 6: ACK forwarding. Any ACK frame addressed to us
            # might be the response to a directed message we sent.
            # The queue matches on the ACK's sender (their from_call =
            # our outbound's to_call) and transitions WAIT_ACK→DELIVERED.
            if (
                parsed.kind is FrameKind.ACK
                and parsed.is_for_us
                and parsed.from_call
                and self._outbound_queue is not None
            ):
                try:
                    matched = self._outbound_queue.record_ack(parsed.from_call)
                    if matched is not None:
                        # Free the cached audio for the now-delivered
                        # directed message. Audio was kept around in
                        # case the ACK timed out and the message
                        # needed retry — we no longer need it.
                        if self._encoded_audio_cache is not None:
                            self._encoded_audio_cache.discard(matched)
                        _log.info(
                            "ACK from %s matched outbound id=%d",
                            parsed.from_call, matched,
                        )
                        # If this outbound was a held-mail delivery
                        # ("<asker> MSG <id> <body>" form), mark the
                        # corresponding inbox STORE row as DELIVERED.
                        # We back-correlate by parsing the outbound
                        # text — no need for a join column.
                        self._maybe_mark_inbox_delivered(
                            outbound_id=matched,
                        )
                except Exception:
                    _log.exception("record_ack raised")
        except Exception:
            _log.exception("decoded-frame handler raised")

    def _update_heard_for(self, parsed) -> None:
        """Compute distance/bearing, upsert the heard row, refresh UI list."""
        their_grid = parsed.grid  # set on heartbeat / CQ; None on directed
        our_grid = self._config.station.grid or None
        # Distance/bearing — compute only when we have both grids.
        # For directed messages without an embedded grid, we'll get
        # the grid from a prior heartbeat sighting (the upsert COALESCEs
        # NULL incoming grid with the existing row's stored value).
        dist_mi, bearing = distance_and_bearing(
            our_grid, their_grid,
            units=self._config.units_distance,
        )
        station = HeardStation(
            callsign=parsed.from_call,
            snr_db=parsed.decoded.snr_db,
            grid=their_grid,
            frequency_hz=parsed.decoded.frequency_hz,
            distance_mi=dist_mi,
            bearing_deg=bearing,
            last_heard=parsed.decoded.received_at,
        )
        if self._store is not None:
            try:
                self._store.upsert_heard_station(station)
                # Refresh the in-memory list from the store so the UI
                # reflects most-recent ordering across all callsigns.
                heard = tuple(self._store.heard_stations(limit=50))
                if self._ui_state is not None:
                    self._ui_state.set_heard(heard)
            except Exception:
                _log.exception("upsert_heard_station failed")

    # ── Inbox dispatch (Phase 1+2) ───────────────────────────────────

    def _mark_inbox_read_sync(self, row_id: int) -> bool:
        """Router callback: mark an inbox row READ in the mailbox store.

        Wrapped in try/except so a router-driven UI action never
        crashes the input thread on a transient store error. Returns
        True if the row was UNREAD and is now READ; False if the
        store is closed, the row doesn't exist, or it was already
        READ. The router doesn't act on the return value — it always
        updates the in-memory UI cache regardless — but tests rely on
        the boolean.
        """
        if self._mailbox is None:
            return False
        try:
            return self._mailbox.mark_read(row_id)
        except Exception:
            _log.exception("mark_read failed for inbox row id=%d", row_id)
            return False

    def _delete_inbox_row_sync(self, row_id: int) -> bool:
        """Router callback: hard-delete an inbox row from the store.

        The operator pressed Delete on the focused inbox row. We
        permanently remove the row from inbox.db — no recovery via
        SQL afterward. The router has already dropped the row from
        the in-memory UI cache so the screen has updated; this call
        persists the removal.

        Returns True if a row was actually deleted; False if the
        store is closed or the row didn't exist (idempotent — repeat
        Delete on a row already gone is a no-op, not an error).

        Wrapped in try/except so a transient store error doesn't
        crash the input thread. The user's mental model — "Delete
        removed the row" — is satisfied by the in-memory drop even
        if disk persistence later fails; the next periodic refresh
        from disk would resurrect it but that's a degraded fallback,
        not a crash.
        """
        if self._mailbox is None:
            return False
        try:
            return self._mailbox.delete(row_id)
        except Exception:
            _log.exception("delete failed for inbox row id=%d", row_id)
            return False

    def _compose_send_sync(
        self,
        to: str,
        cmd,            # ComposeCmd, untyped here to avoid import cycle
        text: str,
    ) -> bool:
        """Router callback: build wire string from compose fields and enqueue.

        Operator pressed Enter on the SEND button. We build the wire-
        format string using ``build_compose_wire`` (which handles the
        per-CMD format rules and the MYLOC grid-substitution) and
        enqueue via ``OutboundQueue.enqueue_for_encoding``. The encode
        worker will pick it up and the scheduler will TX in the next
        aligned window.

        Returns True if the message was successfully enqueued, False
        if:
          - The compose was incomplete (empty TO, or empty TEXT for a
            CMD that requires it). build_compose_wire returns None.
          - The outbound queue isn't initialized (test harness, early
            startup).
          - The queue is full (rare — the queue is large).

        Wrapped in try/except so a transient queue error doesn't
        crash the input thread. The router will clear the compose
        state regardless of return value, so a failed send doesn't
        leave the operator stuck.
        """
        from minijs8.ui.state import build_compose_wire  # avoid import cycle

        if self._outbound_queue is None:
            _log.warning("compose_send: no outbound queue available")
            return False
        wire = build_compose_wire(
            to=to,
            cmd=cmd,
            text=text,
            my_grid=self._config.station.grid,
            my_call=self._config.station.callsign,
        )
        if wire is None:
            _log.info(
                "compose_send: incomplete (to=%r cmd=%s text=%r) — not sending",
                to, cmd, text,
            )
            return False
        try:
            row_id = self._outbound_queue.enqueue_for_encoding(
                wire,
                to_call=(to or "").strip().upper() or None,
            )
        except Exception:
            _log.exception("compose_send: enqueue_for_encoding failed for %r", wire)
            return False
        if row_id is None:
            _log.warning("compose_send: queue full, dropped %r", wire)
            return False
        _log.info("compose_send: enqueued row %d: %r", row_id, wire)
        # Mirror the message into the DIRECTED activity log so the
        # operator sees their TX'd message land in the chat view.
        # We do this AFTER the enqueue succeeds so a failed enqueue
        # doesn't leave a phantom "we sent X" line in the log.
        try:
            self._log_directed_out(
                text=wire,
                to_call=(to or "").strip().upper(),
            )
        except Exception:
            _log.exception("compose_send: directed-activity log failed")
        return True

    def _dispatch_inbox(self, parsed, frame) -> None:
        """Route a parsed directed frame through the inbox state machine.

        Called from the decode handler after the heard list is updated.
        Wrapped in try/except so a malformed inbox dispatch doesn't
        bring down the rest of the decode pipeline.

        See the inline comment block at the call site for the full
        protocol behavior. This method only fires when:

          - The mailbox store is open (best-effort startup may have
            failed; we degrade to ACK-only without persistence)
          - The frame has a from_call (anonymous frames don't drive
            inbox state)

        Note about QUERY MSGS: per the JS8 protocol, this command is
        commonly broadcast to @ALLCALL or a group rather than direct-
        to-us, because asking-everyone is more efficient than polling
        each station individually. We accept both forms but reply
        differently:

          direct-to-us + holding for asker → reply <asker> MSG <id>
          direct-to-us + empty             → reply <asker> NO
          @ALLCALL  + holding for asker    → reply <asker> MSG <id>
          @ALLCALL  + empty                → silent (don't pollute band)

        Group callsigns are deferred — for this phase we always treat
        @ALLCALL targets as participating, and ignore other @<group>
        targets. Group config will land with the Compose UI session.
        """
        if self._mailbox is None:
            return
        if not parsed.from_call:
            return

        body = parsed.body or ""
        from_call = parsed.from_call.upper()
        our_call = (self._config.station.callsign or "").upper()
        if not our_call:
            # Station identity not configured; can't dispatch.
            return

        # Three target categories drive different handler logic:
        #   - direct-to-us:           parsed.is_for_us is True
        #   - @ALLCALL broadcast:     to_call == "@ALLCALL"
        #   - other (group, peer):    ignored for inbox purposes
        is_direct = parsed.is_for_us
        is_allcall = (parsed.to_call or "").upper() == "@ALLCALL"

        # Branch order matters. Most specific patterns first:
        #
        #   1. MSG TO:<dest> <text> — must be checked before MSG so
        #      the latter's regex doesn't swallow the TO: form.
        #   2. MSG <text> — simple inbox-store directed at us.
        #   3. QUERY MSG <id> — deliver one specific held row.
        #   4. QUERY MSGS — list any holding for asker.
        #
        # Every path is wrapped in try/except so one corrupted DB row
        # doesn't sink the whole pipeline.

        if is_direct:
            try:
                # 1. MSG TO: store-for-other
                msg_to = parse_msg_to(body)
                if msg_to is not None:
                    recipient_call, text = msg_to
                    self._handle_inbound_msg_to(
                        sender_call=from_call,
                        recipient_call=recipient_call,
                        text=text,
                        frame=frame,
                    )
                    return

                # 2. MSG store-in-our-inbox
                msg_text = parse_msg(body)
                if msg_text is not None:
                    self._handle_inbound_msg_for_us(
                        sender_call=from_call,
                        text=msg_text,
                        frame=frame,
                    )
                    return

                # 3. QUERY MSG <id>
                requested_id = parse_query_msg_id(body)
                if requested_id is not None:
                    self._handle_query_msg_id(
                        asker_call=from_call,
                        requested_id=requested_id,
                    )
                    return

                # 4. QUERY MSGS (direct-to-us form)
                if is_query_msgs(body):
                    self._handle_query_msgs(
                        asker_call=from_call,
                        is_broadcast=False,
                    )
                    return
            except Exception:
                _log.exception("inbox dispatch (direct) failed")
                return

        if is_allcall:
            try:
                # On @ALLCALL we only care about QUERY MSGS — other
                # @ALLCALL traffic (CQ, plain broadcasts) is handled
                # elsewhere and isn't an inbox concern.
                if is_query_msgs(body):
                    self._handle_query_msgs(
                        asker_call=from_call,
                        is_broadcast=True,
                    )
                    return
            except Exception:
                _log.exception("inbox dispatch (allcall) failed")
                return

    def _dispatch_assembled(self, assembled: AssembledMessage, frame) -> None:
        """Route a checksum-validated AssembledMessage through inbox handlers.

        Multi-frame reassembly produces an AssembledMessage when the
        buffered command's checksum validates (or, for verb-only
        commands like ``QUERY MSGS``, immediately on the first frame).
        This method dispatches to the same handler suite as the old
        single-frame ``_dispatch_inbox`` — but the body has already
        been validated and the checksum stripped.

        ACK rule (protocol-correct):
          - We auto-ACK only for messages with ``checksum_valid=True``.
          - Failed-checksum or timed-out messages get logged, but no
            ACK is sent. JS8Call expects this — ACK is the sender's
            confirmation that delivery succeeded.

        Verb dispatch:
          - ``MSG`` (cmd 9):       store body in our inbox, ACK
          - ``MSG TO:`` (cmd 10):  hold body for a recipient, ACK
                                   (NOTE: see body-format caveat below)
          - ``QUERY MSGS`` (12):   look up holding for asker, reply
          - ``QUERY`` (cmd 11):    inspect body for "MSG <id>" form,
                                   reply with body if held
          - other verbs:           ignored at this phase

        MSG TO: caveat: the assembler currently includes the recipient
        callsign in the body string (e.g. ``body="KC1WDO HELLO WORLD"``).
        That's because the recipient is part of the directed-message
        envelope on the wire AND the parser delivers it inline. We
        re-parse it here via ``parse_msg_to`` against a reconstructed
        ``"MSG TO:<body>"`` string. The checksum match still fires
        because gfsk8.pack uses the same pre-recipient layout —
        the validation against our reassembled string works in
        practice for single-frame MSG TO: but may need refinement
        for multi-frame MSG TO: with long bodies (deferred until
        we have on-air data).
        """
        if not assembled.checksum_valid:
            _log.info(
                "assembled message (cs invalid) from=%s to=%s verb=%s "
                "raw_text=%r — surfacing as INCOMPLETE (no ACK)",
                assembled.from_call, assembled.to_call,
                assembled.verb, assembled.raw_text[:60],
            )
            # Operator-visible diagnostic: surface to the directed-
            # activity log so the operator sees that something
            # arrived but couldn't be recovered. We DON'T auto-ACK
            # (CRC is the protocol contract that says "I got it
            # intact" — we didn't, so we don't lie). We also DON'T
            # write to the inbox (incomplete content is not mail).
            #
            # Only log if the frame was for-us or @ALLCALL — random
            # corrupt frames addressed to other stations are noise.
            our_call = (self._config.station.callsign or "").upper()
            to_upper = (assembled.to_call or "").upper()
            if (
                assembled.from_call
                and (to_upper == our_call or to_upper == "@ALLCALL")
            ):
                try:
                    self._log_directed_in_incomplete(assembled)
                except Exception:
                    _log.exception("activity log (incomplete) raised")
            return
        if self._mailbox is None or not assembled.from_call:
            return
        our_call = (self._config.station.callsign or "").upper()
        if not our_call:
            return

        from_call = assembled.from_call.upper()
        to_call = (assembled.to_call or "").upper()
        is_direct = (to_call == our_call)
        is_allcall = (to_call == "@ALLCALL")
        verb = assembled.verb
        body = assembled.body

        _log.info(
            "assembled+validated: from=%s to=%s verb=%s body=%r frames=%d "
            "buffered=%s",
            from_call, to_call, verb, body[:60], assembled.frame_count,
            assembled.was_buffered_command,
        )

        # Non-buffered directed messages (YES, NO, INFO, GRID, STATUS,
        # HEARING, free-text directed messages) get logged to the
        # directed-activity feed and that's it — there's no inbox
        # dispatch logic, no auto-ACK, no protocol reply. The body
        # is shown to the operator and they decide how (or whether)
        # to follow up. Matches JS8Call's "directed pane" behavior.
        if not assembled.was_buffered_command:
            if is_direct or is_allcall:
                try:
                    self._log_directed_in_assembled(assembled)
                except Exception:
                    _log.exception(
                        "directed-activity log (non-buffered assembled) failed"
                    )
            return

        # Log to directed-activity feed for non-mail-content commands.
        # MSG and MSG TO: are inbox events (their content goes to the
        # mailbox screen) — explicitly excluded here so the chat log
        # isn't polluted with mail content the operator can already
        # see in INBOX. Everything else (QUERY MSGS, QUERY MSG <id>,
        # QUERY CALL, CMD, relay) is protocol activity and shows up
        # in the DIRECTED chat view.
        if verb not in ("MSG", "MSG TO:") and (is_direct or is_allcall):
            try:
                self._log_directed_in_assembled(assembled)
            except Exception:
                _log.exception("directed-activity log (assembled) failed")

        try:
            if is_direct:
                # MSG (simple inbox-store)
                if verb == "MSG":
                    if body:
                        self._handle_inbound_msg_for_us(
                            sender_call=from_call, text=body, frame=frame,
                        )
                    return

                # MSG TO: (hold for recipient)
                if verb == "MSG TO:":
                    msg_to = parse_msg_to(f"MSG TO:{body}")
                    if msg_to is not None:
                        recipient_call, text = msg_to
                        self._handle_inbound_msg_to(
                            sender_call=from_call,
                            recipient_call=recipient_call,
                            text=text, frame=frame,
                        )
                    return

                # QUERY MSGS (direct-to-us form)
                if verb == "QUERY MSGS":
                    self._handle_query_msgs(
                        asker_call=from_call, is_broadcast=False,
                    )
                    return

                # QUERY MSG <id> — encoded as verb="QUERY" body="MSG <id>"
                if verb == "QUERY":
                    requested_id = parse_query_msg_id(f"QUERY {body}")
                    if requested_id is not None:
                        self._handle_query_msg_id(
                            asker_call=from_call,
                            requested_id=requested_id,
                        )
                    return

            if is_allcall:
                # @ALLCALL only handles QUERY MSGS for now (group config
                # comes in a later phase).
                if verb == "QUERY MSGS":
                    self._handle_query_msgs(
                        asker_call=from_call, is_broadcast=True,
                    )
                    return

        except Exception:
            _log.exception("dispatch_assembled handler failed")

    def _handle_inbound_msg_for_us(
        self, *, sender_call: str, text: str, frame
    ) -> None:
        """Persist a "MSG <text>" addressed to us as UNREAD + auto-ACK."""
        assert self._mailbox is not None
        try:
            row_id = self._mailbox.add_unread(
                from_call=sender_call,
                text=text,
                offset_hz=int(frame.frequency_hz),
                snr_db=int(frame.snr_db),
                our_call=self._config.station.callsign or "",
            )
            _log.info(
                "inbox: stored UNREAD id=%d from=%s text=%r",
                row_id, sender_call, text[:40],
            )
        except MailboxError:
            _log.exception("inbox add_unread failed")
            return
        # Refresh UI snapshot so the operator sees the new row.
        self._refresh_inbox_ui()
        # Auto-ACK back. The standard JS8Call directed-message ACK
        # body is just "ACK".
        self._queue_ack_to(sender_call)

    def _handle_inbound_msg_to(
        self,
        *,
        sender_call: str,
        recipient_call: str,
        text: str,
        frame,
    ) -> None:
        """Persist a "MSG TO:<recipient> <text>" as STORE + auto-ACK."""
        assert self._mailbox is not None
        try:
            row_id = self._mailbox.add_remote_store(
                sender_call=sender_call,
                recipient_call=recipient_call,
                text=text,
                offset_hz=int(frame.frequency_hz),
                snr_db=int(frame.snr_db),
            )
            _log.info(
                "inbox: stored STORE id=%d from=%s for=%s text=%r",
                row_id, sender_call, recipient_call, text[:40],
            )
        except MailboxError:
            _log.exception("inbox add_remote_store failed")
            return
        # Refresh UI (held count changed).
        self._refresh_inbox_ui()
        # Auto-ACK back to the sender so they know we accepted the
        # hold-for-recipient request.
        self._queue_ack_to(sender_call)

    def _handle_query_msgs(
        self, *, asker_call: str, is_broadcast: bool
    ) -> None:
        """Reply to a QUERY MSGS depending on what we're holding.

        Direct-to-us:  hold → "<asker> MSG <id>";  empty → "<asker> NO"
        Broadcast:     hold → "<asker> MSG <id>";  empty → silent
        """
        assert self._mailbox is not None
        held = self._mailbox.list_holding_for(asker_call, limit=1)
        if held:
            oldest = held[0]
            # Per JS8Call protocol, QUERY MSGS reply is just the id;
            # the asker then sends QUERY MSG <id> to retrieve text.
            text = f"{asker_call} MSG {oldest.id}"
            # kind=REPLY: this is an informational notification, not a
            # content delivery. JS8Call protocol does NOT auto-ACK these.
            # If queued as DIRECTED the scheduler would loop on WAIT_ACK.
            self._enqueue_directed_reply(
                text=text, to_call=asker_call, kind=OutboundKind.REPLY,
            )
            _log.info(
                "QUERY MSGS from %s: replying with held id=%d",
                asker_call, oldest.id,
            )
            return
        if is_broadcast:
            # Empty + broadcast → silent. Replying NO to every
            # @ALLCALL QUERY MSGS would jam the channel.
            _log.info(
                "QUERY MSGS @ALLCALL from %s: nothing held, staying silent",
                asker_call,
            )
            return
        # Direct-to-us + empty → informative NO.
        text = f"{asker_call} NO"
        # kind=REPLY: same reasoning as above — terminal informational
        # reply, not ACK-eligible.
        self._enqueue_directed_reply(
            text=text, to_call=asker_call, kind=OutboundKind.REPLY,
        )
        _log.info(
            "QUERY MSGS from %s: nothing held, replying NO",
            asker_call,
        )

    def _handle_query_msg_id(
        self, *, asker_call: str, requested_id: int
    ) -> None:
        """Deliver a specific held message body in response to QUERY MSG <id>."""
        assert self._mailbox is not None
        record = self._mailbox.get(requested_id)
        if record is None:
            _log.info(
                "QUERY MSG %d from %s: id not found",
                requested_id, asker_call,
            )
            return
        # Only deliver if the row is STORE (not UNREAD/READ — those
        # are our own inbox, not held mail) AND it's actually for the
        # asker (not someone else's mail).
        if record.type != "STORE":
            _log.info(
                "QUERY MSG %d from %s: row type=%s (not STORE), refusing",
                requested_id, asker_call, record.type,
            )
            return
        if record.to_call.upper() != asker_call.upper():
            _log.info(
                "QUERY MSG %d from %s: row TO=%s, not for asker",
                requested_id, asker_call, record.to_call,
            )
            return
        # Format the reply. Convention: "<asker> MSG <id> <body>".
        # The id allows the recipient to correlate this delivery
        # back to their original QUERY MSG <id> request.
        text = f"{asker_call} MSG {record.id} {record.text}"
        self._enqueue_directed_reply(text=text, to_call=asker_call)
        _log.info(
            "QUERY MSG %d from %s: delivering body (%d chars)",
            requested_id, asker_call, len(record.text),
        )

    def _queue_ack_to(self, to_call: str) -> None:
        """Queue a plain ACK back to a callsign.

        Uses kind=REPLY (not DIRECTED) because per JS8Call protocol
        the recipient does NOT auto-ACK an ACK. If we enqueued as
        DIRECTED the scheduler would enter WAIT_ACK and retransmit
        every 90 s, creating an infinite loop.
        """
        if self._outbound_queue is None:
            return
        text = f"{to_call} ACK"
        self._enqueue_directed_reply(
            text=text, to_call=to_call, kind=OutboundKind.REPLY,
        )

    def _enqueue_directed_reply(
        self,
        *,
        text: str,
        to_call: str,
        kind: OutboundKind = OutboundKind.DIRECTED,
    ) -> None:
        """Enqueue an outbound message to a specific callsign.

        Uses ``enqueue_for_encoding`` so the EncodeWorker renders
        audio off the slot-aligned scheduler tick. Returns silently
        on queue-full or no-queue conditions — the upstream caller
        already logged its intent.

        Parameters
        ----------
        kind : OutboundKind, default DIRECTED
            ``DIRECTED`` (the default) for content deliveries that
            expect the recipient to auto-ACK back — like a QUERY
            MSG <id> body delivery using the MSG verb. The scheduler
            puts these in WAIT_ACK; the inbound ACK transitions them
            to DELIVERED.

            ``REPLY`` for terminal-in-exchange replies that JS8Call
            protocol does NOT auto-ACK — auto-ACKs to received MSGs,
            and QUERY MSGS notification replies ("<asker> NO" or
            "<asker> MSG <id>"). Scheduler marks DELIVERED on TX
            completion (no WAIT_ACK).
        """
        if self._outbound_queue is None:
            return
        try:
            self._outbound_queue.enqueue_for_encoding(
                text=text,
                kind=kind,
                to_call=to_call,
            )
        except Exception:
            _log.exception("enqueue_for_encoding (inbox reply) failed")
            return
        # Log the outbound to the directed-activity feed so the
        # operator sees the round-trip in the chat view. We do this
        # AFTER the enqueue succeeds so a failed enqueue doesn't
        # leave a misleading "we sent X" entry in the log.
        try:
            self._log_directed_out(text=text, to_call=to_call)
        except Exception:
            _log.exception("directed-activity log (outbound) failed")

    def _refresh_inbox_ui(self) -> None:
        """Push the current inbox snapshot to the UI state.

        Called whenever the mailbox table changes. Reads UNREAD+READ
        rows newest-first plus the holding count, builds the UI
        snapshot, and hands it off. Cheap because both lists are
        capped at 50/100 rows and the JSON-path indices make the
        SELECT O(log n).
        """
        if self._mailbox is None or self._ui_state is None:
            return
        try:
            inbox_records = self._mailbox.list_inbox(limit=50)
            held_count = self._mailbox.count_holding()
            unread_count = self._mailbox.count_unread()
            self._ui_state.set_inbox(
                records=tuple(inbox_records),
                held_count=held_count,
                unread_count=unread_count,
            )
        except Exception:
            _log.exception("could not refresh inbox UI snapshot")

    def _refresh_directed_log_ui(self) -> None:
        """Push the current directed-activity snapshot to the UI state.

        Called after every record_in/record_out so the DIRECTED screen
        reflects new entries on the next render tick. The set_directed_log
        setter on UIState short-circuits if the snapshot didn't change,
        so this is cheap to call per-frame.
        """
        if self._ui_state is None:
            return
        try:
            self._ui_state.set_directed_log(
                self._directed_activity.snapshot()
            )
        except Exception:
            _log.exception("could not refresh directed-log UI snapshot")

    def _log_directed_in(self, parsed) -> None:
        """Record an inbound directed frame in the activity log.

        Called from the decode handler for non-buffered single frames
        (SNR?, INFO, GRID, STATUS, ACK from a remote, etc.) and from
        ``_dispatch_assembled`` for buffered commands that aren't
        MSG/MSG TO: (those are inbox events, not chat-log events).

        Verb extraction: take the first whitespace-separated token of
        the body. For directed frames the body never includes the
        sender's callsign — that's parsed off into ``parsed.from_call``
        — so the first token IS the verb.
        """
        body = (parsed.body or "").strip()
        if not body:
            verb, rest = "", ""
        else:
            split = body.split(None, 1)
            verb = split[0]
            rest = split[1] if len(split) > 1 else ""
        # Special case: ACK frames carry kind=ACK and body="ACK", but
        # we want the verb to literally read "ACK" so the chat row
        # rendering is consistent. The split above gives us that.
        try:
            self._directed_activity.record_in(
                from_call=parsed.from_call or "",
                verb=verb,
                body=rest,
                snr_db=int(parsed.decoded.snr_db) if parsed.decoded else None,
                freq_hz=float(parsed.decoded.frequency_hz) if parsed.decoded else None,
            )
        except Exception:
            _log.exception("activity.record_in raised")
            return
        self._refresh_directed_log_ui()

    def _log_directed_in_assembled(self, assembled) -> None:
        """Record an assembled (multi-frame) buffered command in the log.

        Used from ``_dispatch_assembled`` for verbs that are NOT
        MSG/MSG TO: — i.e., QUERY MSGS, QUERY MSG <id>, QUERY CALL,
        CMD, relay '>'. Also for non-buffered directed messages (YES,
        NO, INFO, GRID, STATUS, HEARING, free-text directed). MSG/
        MSG TO: are explicitly skipped because their content goes to
        the INBOX screen, not the directed log.

        Verb-prefix dedup
        -----------------
        For non-buffered messages, ``_on_non_buffered_starter`` keeps
        the entire frame body (verb included) so the assembler can
        treat the body as opaque text. When we render the activity
        log, the renderer concatenates ``"<call> <verb> <body>"`` —
        if body already starts with the verb, we'd display
        ``"KD8PGB YES YES MSG ID 57"`` (24 chars) and waste row space.
        Strip the leading verb here so the body stored in the log is
        the meaningful continuation.

        For buffered commands, the assembler already strips the verb
        from body (the verb is parsed off the front and stored
        separately), so the strip below is a no-op for those.

        We don't have access to the original raw DecodedFrame here
        (the assembler aggregates many frames into one event), so we
        report freq from ``offset_hz`` and skip SNR — the operator
        cares more about WHO and WHAT than the SNR of a multi-frame
        protocol exchange.
        """
        verb = assembled.verb or ""
        body = assembled.body or ""
        # If body starts with the verb token (case-insensitive),
        # strip it. This dedups the verb in the activity log without
        # losing any actual content. We must match a WORD boundary
        # so "YES" doesn't strip the leading 3 chars of an unrelated
        # body that happens to start with "YES..." — require either
        # an exact-equal match OR verb followed by whitespace.
        body_upper = body.upper()
        verb_upper = verb.upper()
        if body_upper == verb_upper:
            body = ""
        elif (
            verb_upper
            and len(body) > len(verb_upper)
            and body_upper.startswith(verb_upper)
            and body[len(verb_upper)].isspace()
        ):
            body = body[len(verb_upper):].lstrip()

        try:
            self._directed_activity.record_in(
                from_call=assembled.from_call or "",
                verb=verb,
                body=body,
                snr_db=None,
                freq_hz=float(assembled.offset_hz),
            )
        except Exception:
            _log.exception("activity.record_in (assembled) raised")
            return
        self._refresh_directed_log_ui()

    def _log_directed_in_incomplete(self, assembled) -> None:
        """Record a buffered message that timed out without CRC validation.

        Used when ``_dispatch_assembled`` receives ``checksum_valid=
        False``. The buffer's frames decoded but the CRC didn't match —
        could be: a missed continuation frame in poor copy, a flaky
        decoder, or an actual codec bug we haven't found yet.

        We surface the entry to the directed-activity log with a
        ⚠ INCOMPLETE prefix on the verb so the operator visibly sees
        that something arrived but was unrecoverable. Better than the
        previous behavior (silent drop, operator stares at an empty
        screen wondering if the daemon is even running).

        We do NOT:
        - Write to the inbox (incomplete content is not mail)
        - Auto-ACK (CRC is the protocol contract for "intact receipt")
        - Trigger any reply logic (the verb might be wrong; the body
          might be partial)
        """
        try:
            # Tag the verb so the renderer shows it distinctively.
            tagged_verb = f"⚠ INCOMPLETE {assembled.verb}"
            # Surface the partial body so the operator can see how
            # close to a recovery we got. Truncate aggressively in
            # case of garbage — 60 chars is plenty for the ops view.
            body = (assembled.body or "")[:60]
            self._directed_activity.record_in(
                from_call=assembled.from_call or "",
                verb=tagged_verb,
                body=body,
                snr_db=None,
                freq_hz=float(assembled.offset_hz),
            )
        except Exception:
            _log.exception("activity.record_in (incomplete) raised")
            return
        self._refresh_directed_log_ui()

    def _log_directed_out(self, *, text: str, to_call: str) -> None:
        """Record an outbound reply in the activity log.

        Called from ``_enqueue_directed_reply`` so every reply we
        send to a specific callsign appears in the chat-style view
        alongside the inbound frame that prompted it. Operators get
        to see the round-trip without having to cross-reference logs.

        Verb extraction: outbound text is formatted as
        ``"<to_call> <verb> [<body>...]"`` — split twice to peel off
        the callsign and verb, treat the remainder as body. For
        well-known short replies (ACK, NO) the body will be empty.
        """
        text = (text or "").strip()
        if not text:
            return
        # Tokenize: tokens[0]=to_call, tokens[1]=verb, tokens[2]=body.
        parts = text.split(None, 2)
        if len(parts) < 2:
            # Not in the expected "<to> <verb> ..." form. Log as best
            # we can — at least the operator sees that something was
            # sent. Falls back to verb=text, body="".
            verb = parts[0] if parts else ""
            body = ""
        else:
            verb = parts[1]
            body = parts[2] if len(parts) >= 3 else ""
        try:
            self._directed_activity.record_out(
                to_call=to_call or "",
                verb=verb,
                body=body,
            )
        except Exception:
            _log.exception("activity.record_out raised")
            return
        self._refresh_directed_log_ui()

    def _maybe_mark_inbox_delivered(self, *, outbound_id: int) -> None:
        """If outbound was a held-mail delivery, mark inbox row DELIVERED.

        We send held-mail deliveries with text formatted as:
            "<asker> MSG <inbox_id> <body>"
        After the recipient ACKs, we look up the outbound row and
        back-correlate the inbox row id from the second whitespace-
        separated token.

        This is a cheap heuristic and may produce a false positive
        if a normal directed message happens to start with "<asker>
        MSG <number> ...". But that string IS the JS8 protocol form
        for held-mail delivery — only software speaking the protocol
        would emit it intentionally — so the heuristic is safe in
        practice.
        """
        if self._mailbox is None or self._outbound_queue is None:
            return
        try:
            msg = self._outbound_queue.get(outbound_id)
        except Exception:
            _log.exception("outbound_queue.get raised in inbox match")
            return
        if msg is None:
            return
        text = msg.text or ""
        # Expected form: "<callsign> MSG <id> <body...>"
        # We just need tokens [1]="MSG" and [2]=<digits>.
        parts = text.split(maxsplit=3)
        if len(parts) < 3:
            return
        if parts[1].upper() != "MSG":
            return
        if not parts[2].isdigit():
            return
        inbox_id = int(parts[2])
        try:
            ok = self._mailbox.mark_delivered(inbox_id)
        except Exception:
            _log.exception("mailbox.mark_delivered raised")
            return
        if ok:
            _log.info(
                "inbox row id=%d marked DELIVERED (outbound id=%d ACK'd)",
                inbox_id, outbound_id,
            )
            self._refresh_inbox_ui()

    # ── Cleanup ──────────────────────────────────────────────────────

    async def _cleanup_with_grace(self) -> None:
        _log.info("shutting down")
        try:
            await asyncio.wait_for(self._cleanup(), timeout=_SHUTDOWN_GRACE_SEC)
        except asyncio.TimeoutError:
            _log.warning(
                "cleanup did not complete within %.1fs; exiting anyway",
                _SHUTDOWN_GRACE_SEC,
            )

    async def _cleanup(self) -> None:
        # ── Step 6: TX path teardown (CRITICAL ORDER) ────────────────
        # 1. Stop the scheduler FIRST so no new TX can start during shutdown.
        if self._tx_scheduler is not None:
            self._tx_scheduler.stop()
            await asyncio.to_thread(self._tx_scheduler.join, 3.0)
            if self._tx_scheduler.is_alive():
                _log.warning("tx scheduler did not stop within 3s")
            self._tx_scheduler = None

        # 1b. Stop the encode worker. After the scheduler is stopped
        # there's no consumer for new cached audio, so the worker can
        # finish its current encode (if any) and exit. Worker.stop()
        # waits up to 5s for an in-progress encode to complete — the
        # encoder is not cancellable. Memory-only cache is dropped
        # implicitly when the worker is torn down.
        if self._encode_worker is not None:
            try:
                self._encode_worker.stop()
            except Exception:
                _log.exception("encode_worker.stop() raised")
            self._encode_worker = None
        self._encoded_audio_cache = None

        # 2. Stop CAT BEFORE closing playback. CatService.stop() sends
        # a final "T 0" to release PTT — must happen before we tear
        # down anything else. The scheduler being stopped above means
        # nothing is mid-TX-cycle right now.
        if self._cat is not None:
            try:
                self._cat.stop()
            except Exception:
                _log.exception("cat.stop() raised")
            self._cat = None

        # 3. Close audio playback (PortAudio output stream).
        if self._playback is not None:
            try:
                self._playback.stop()
            except Exception:
                _log.exception("playback.stop() raised")
            self._playback = None

        # The outbound queue is just a SQLite-backed object; it doesn't
        # own a thread. The connection is owned by MessageStore (which
        # closes below).
        self._outbound_queue = None

        # ── Step 5 / earlier teardown ────────────────────────────────
        # Retention task — cancel first so it doesn't try to write
        # to the store during shutdown.
        if self._retention_task is not None and not self._retention_task.done():
            self._retention_task.cancel()
            try:
                await self._retention_task
            except (asyncio.CancelledError, Exception):
                pass
            self._retention_task = None

        # Reassembly sweep task — same pattern as retention. Cancel
        # before the timing log so we don't try to dispatch new
        # completions during shutdown.
        if (
            self._reassembly_sweep_task is not None
            and not self._reassembly_sweep_task.done()
        ):
            self._reassembly_sweep_task.cancel()
            try:
                await self._reassembly_sweep_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reassembly_sweep_task = None

        # Timing log task — same pattern as retention.
        if self._timing_log_task is not None and not self._timing_log_task.done():
            self._timing_log_task.cancel()
            try:
                await self._timing_log_task
            except (asyncio.CancelledError, Exception):
                pass
            self._timing_log_task = None

        # Decode thread before audio (decoder reads from audio buffer).
        if self._decode is not None:
            self._decode.stop()
            await asyncio.to_thread(self._decode.join, 2.0)
            if self._decode.is_alive():
                _log.warning("decode thread did not stop within 2s")
            self._decode = None

        # Audio capture (PortAudio thread).
        if self._audio is not None:
            try:
                self._audio.stop()
            except Exception:
                _log.exception("audio.stop() raised")
            self._audio = None

        # Message store: close last so retention/decode can flush.
        if self._store is not None:
            try:
                self._store.close()
            except Exception:
                _log.exception("store.close() raised")
            self._store = None

        # Mailbox store — close after message store. The mailbox
        # only sees writes from the decode handler, which is
        # already stopped by the time we get here.
        if self._mailbox is not None:
            try:
                self._mailbox.close()
            except Exception:
                _log.exception("mailbox.close() raised")
            self._mailbox = None

        # GPS first — closing the gpsd socket is fast.
        if self._gps is not None:
            self._gps.stop()
            await asyncio.to_thread(self._gps.join, 2.0)
            if self._gps.is_alive():
                _log.warning("gps reader did not stop within 2s")
            self._gps = None

        if self._keyboard is not None:
            self._keyboard.stop()
            await asyncio.to_thread(self._keyboard.join, 2.0)
            if self._keyboard.is_alive():
                _log.warning("keyboard thread did not stop within 2s")
            self._keyboard = None

        if self._buttons is not None:
            self._buttons.stop()
            self._buttons = None

        if self._render_thread is not None:
            self._render_thread.stop()
            await asyncio.to_thread(self._render_thread.join, 2.0)
            if self._render_thread.is_alive():
                _log.warning("render thread did not stop within 2s")
            self._render_thread = None
            self._display = None
