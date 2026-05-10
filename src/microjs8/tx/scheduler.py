"""Slot-aligned TX scheduler.

Once per JS8 slot (every 15 s), the scheduler:

  1. Walks the queue's WAIT_ACK rows and processes any that have
     timed out (re-queue for retry or abandon, based on attempt count).

  2. Picks the next QUEUED message (FIFO).

  3. Runs the safety checks (callsign, GPS, chrony, frame-rate limit).

  4. If all checks pass, calls TxBackend.transmit() inline and updates
     state based on the result.

  5. Sleeps until the next slot boundary.

The scheduler runs on its own thread, separate from the asyncio loop.
TxBackend.transmit() blocks for ~12-13 seconds (the duration of the
JS8 audio frame) — putting that on the asyncio loop would freeze the
UI and decoder pipelines. A dedicated thread keeps everything else
responsive.

Slot timing
-----------

JS8 slots are UTC-aligned to multiples of 15 seconds. We TX at the
START of each slot — that's the protocol convention so receivers
know when to expect signal. The scheduler wakes ~50 ms before the
slot boundary so we have time to do queue work, then PTT-on at the
boundary itself.

Concretely:
  - At slot_start - 200 ms: pick next message, do safety checks
  - At slot_start - 50 ms: call TxBackend.transmit(); ptt_on_delay
    (150 ms for QDX) puts actual audio start at slot_start + 100 ms,
    well within JS8's ±1 s tolerance.

Safety checks (all 4 from Step 6 spec)
--------------------------------------

  1. Callsign must not be N0CALL (unless emergency override)
  2. Configured grid must be set (unless emergency override)
  3. GPS fix required when emergency override (unconfigured station)
  4. chrony must report a sync source

Rate limit
----------

The scheduler enforces "max 1 TX per slot" by design — it runs once
per slot and only ever does one transmit per loop iteration. No extra
guard needed at the API surface.

Frame rate limit (additional)
-----------------------------

For repeat scheduling (heartbeats firing every 30 min), we never let
a slot's transmit overlap with the next slot. JS8 Normal frames are
~12.6 s; if for some reason a transmit overruns, the next slot's
transmit is suppressed (the queue waits another full slot).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, TYPE_CHECKING

from microjs8.modem.decoder import JS8_SLOT_SECONDS
from microjs8.modem.encoder import EncoderError
from microjs8.tx.queue import (
    ACK_TIMEOUT_S,
    MAX_ATTEMPTS,
    OutboundKind,
    OutboundMessage,
    OutboundQueue,
    OutboundState,
    infer_outbound_kind,
)
from microjs8.tx.tx_backend import TxBackend, TxResult

if TYPE_CHECKING:
    import numpy as np

    from microjs8.tx.encode_worker import EncodedAudioCache

_log = logging.getLogger(__name__)


# When the scheduler should fire relative to a slot boundary.
#
# We fire SHORTLY AFTER the slot boundary, not before it, because
# ``transmit_frame()`` does per-frame wall-clock alignment that
# targets ``slot_boundary + 500 ms`` (the JS8 protocol's delay_ms —
# see ``Modulator::start()`` in JS8Call's source). Firing after the
# boundary keeps mstr small (typically 50-300 ms), which is the
# regime where pipeline-latency compensation works cleanly.
#
# Concretely: scheduler wakes at ``slot_boundary + _TARGET_OFFSET_S``,
# does queue work + safety check, calls ``start_burst()`` (PTT-on +
# settle), then calls ``transmit_frame()`` which reads wall clock
# and computes silence using JS8Call's round-up formula to land
# modulation at the next 500 ms boundary in the slot.
#
# About firing late: thanks to JS8Call's round-up formula,
# transmit_frame() never fails on timing — if mstr is past 500 ms,
# it just rounds up to the next 500 ms boundary in the slot
# (modulation lands at slot+1000 ms instead of slot+500 ms). This
# eliminates the "too late to align" failure mode that earlier
# versions had to defend against. Hitting slot+500 ms cleanly is
# still the goal, but missing it is no longer fatal — just a
# quality-of-decode tradeoff (more boundaries in the slot are
# acceptable but use more on-air time).
#
# Margin breakdown (typical, on Pi Zero 2W with QDX):
#
#   _TARGET_OFFSET_S         10 ms  (this constant — when we wake)
#   queue work / mark_sending 5 ms  (DB UPDATE round-trip)
#   start_burst CAT roundtrip 30 ms (USB serial PTT-on)
#   ptt_on_delay_ms          50 ms  (radio settle, per-radio config)
#   ───────────────────────────────
#   total before transmit_frame's wall-clock read: ~95 ms
#
# That puts transmit_frame's mstr around 95 ms, well under the
# 500 ms first boundary — clean alignment with no round-up needed.
_TARGET_OFFSET_S = 0.01


# Burst watchdog: per-frame budget for a multi-frame TX. If the burst
# isn't complete after ``n_frames × _BURST_WATCHDOG_PER_FRAME_S``
# seconds, the scheduler force-ends the burst and abandons the
# message.
#
# Why a watchdog at all: we're holding PTT continuously across slot
# boundaries. If anything goes wrong between frames (Python exception
# in audio chain, scheduler thread hang, daemon stuck on I/O), PTT
# could remain keyed indefinitely. That's bad for the radio (PA
# stress, possible damage on long carrier), bad for the band
# (jamming), and bad for our credibility on-air. The watchdog is a
# defense in depth.
#
# Why 20 s per frame:
#   slot duration         15 s
#   frame audio duration  ~12.64 s (well within slot)
#   inter-frame gap       ~2.4 s (PTT held, no audio)
#   ─────────────────────────────────
#   normal per-frame time:  ~15 s
#   safety margin:           5 s
#   = 20 s/frame budget
#
# Examples:
#   1-frame heartbeat → 20 s deadline
#   3-frame MSG       → 60 s deadline
#   5-frame longer    → 100 s deadline
#
# If we hit this watchdog, something is genuinely wrong — we don't
# retry, we abandon the message with an explicit watchdog error so
# the operator sees what happened.
_BURST_WATCHDOG_PER_FRAME_S = 20.0


# Grace period the CAT-layer PTT watchdog gets BEYOND the scheduler-
# side burst watchdog. Two watchdogs are coordinated:
#
#   * **Scheduler** (this module's ``_check_burst_watchdog()``) is
#     the primary. It fires at ``n_frames × _BURST_WATCHDOG_PER_FRAME_S``
#     and runs proper cleanup: ``end_burst()``, mark row abandoned,
#     log loudly. Operator sees a clean error.
#
#   * **CAT layer** (``CatService._watchdog_loop()``) is a final
#     safety net. It fires at ``scheduler_deadline + _CAT_WATCHDOG_GRACE_S``
#     and just yanks PTT off — useful only if the scheduler itself
#     has stopped running (its thread crashed, deadlocked, etc.).
#
# 5 seconds is enough for the scheduler's once-per-slot wakeup to
# notice and fire its own watchdog first under normal conditions.
# We don't want this too long — if the scheduler IS stuck, we want
# PTT off the air fast.
_CAT_WATCHDOG_GRACE_S = 5.0


@dataclass
class _MultiFrameContext:
    """In-memory state for a multi-frame TX in progress.

    Created when the scheduler picks up a QUEUED row, encodes all
    frames at once, and TXs frame 0 in that slot. Subsequent slots
    consult this context to TX frames 1, 2, … one per slot.

    Strict consecutive-slot rule (per Step 6b spec): if any frame
    fails or is blocked by the safety gate, the WHOLE message fails
    — we clear this context and let the retry FSM start the message
    over from frame 0 next attempt.
    """
    audio_frames: "list[np.ndarray]"   # all frames pre-encoded
    next_frame_index: int               # 0-based; frame to TX in upcoming slot
    burst_deadline: float               # wall-clock deadline (epoch_s) for the
                                        # WHOLE multi-frame TX. If we cross it
                                        # while still in burst, watchdog fires.

    @property
    def total_frames(self) -> int:
        return len(self.audio_frames)

    @property
    def is_done(self) -> bool:
        return self.next_frame_index >= len(self.audio_frames)


@dataclass
class TxStatus:
    """Snapshot of transmit-related status, surfaced to the UI."""
    last_tx_at: Optional[float] = None
    last_tx_ok: Optional[bool] = None
    last_error: Optional[str] = None
    queue_active: int = 0           # active rows (QUEUED+SENDING+WAIT_ACK)
    last_blocked_reason: Optional[str] = None  # why most recent slot didn't TX


class _SafetyGateProto(Protocol):
    """Read-only view the scheduler uses for safety checks.

    Implemented by app.py. The scheduler doesn't know about UIState,
    Config, or GpsReader directly — it just asks "is it safe to TX?"
    and gets a yes/no with a reason string.
    """

    def check_can_transmit(self) -> tuple[bool, Optional[str]]:
        """Return (ok, reason). ``reason`` is a short human-readable
        explanation when ok==False; None when ok==True."""


class TxScheduler(threading.Thread):
    """Slot-aligned scheduler that drives the outbound queue."""

    def __init__(
        self,
        queue: OutboundQueue,
        backend: TxBackend,
        safety_gate: _SafetyGateProto,
        *,
        on_tx_complete: Optional[Callable[[OutboundKind, TxResult], None]] = None,
        on_status_change: Optional[Callable[[TxStatus], None]] = None,
        slot_seconds: int = JS8_SLOT_SECONDS,
        name: str = "tx-scheduler",
        encoded_audio_cache: "Optional[EncodedAudioCache]" = None,
    ) -> None:
        super().__init__(name=name, daemon=True)
        self._queue = queue
        self._backend = backend
        self._safety_gate = safety_gate
        self._on_tx_complete = on_tx_complete
        self._on_status_change = on_status_change
        self._slot_seconds = slot_seconds
        self._stop_event = threading.Event()
        self._status = TxStatus()
        # Per-message state for multi-frame TX in progress. Keyed by
        # outbound row id. Created when the scheduler picks up a
        # QUEUED row, removed when we mark the row DELIVERED /
        # WAIT_ACK / abandoned. At most one entry at a time in
        # practice (we don't pick a new QUEUED row while one is
        # SENDING) but the dict handles the general case cleanly.
        self._mf_state: dict[int, _MultiFrameContext] = {}
        # Encoded-audio cache. When supplied (production path), the
        # scheduler reads pre-encoded audio from the cache instead of
        # invoking backend.encode() inline. None = legacy fallback,
        # encode happens at TX time (~3 s on Pi Zero 2W). The cache
        # is populated by the EncodeWorker.
        self._encoded_cache = encoded_audio_cache
        # Public for tests: clock function (overridable for determinism).
        self._now = time.time
        self._monotonic = time.monotonic

    def stop(self) -> None:
        self._stop_event.set()

    @property
    def status(self) -> TxStatus:
        return self._status

    # ── Run loop ─────────────────────────────────────────────────────

    def run(self) -> None:
        _log.info("tx scheduler starting (slot=%ds)", self._slot_seconds)
        # Clean up any rows left in SENDING state from a previous
        # daemon run. These are messages that were mid-TX when we
        # stopped — partial multi-frame transmissions are
        # undecodable, so there's nothing to recover. Mark them
        # ABANDONED so they're visible in the Outbound view's audit
        # trail with a clear reason.
        try:
            cleared = self._queue.abandon_stale_sending(
                error="interrupted by daemon restart",
            )
            if cleared > 0:
                _log.warning(
                    "abandoned %d stale SENDING row(s) at startup "
                    "(interrupted by previous daemon shutdown)",
                    cleared,
                )
        except Exception:
            _log.exception("failed to clean up stale SENDING rows")
        while not self._stop_event.is_set():
            sleep_s = self._seconds_until_next_preflight()
            self._stop_event.wait(sleep_s)
            if self._stop_event.is_set():
                break
            try:
                self._tick()
            except Exception:
                # Never let an unexpected exception kill the scheduler.
                _log.exception("scheduler tick raised")
        _log.info("tx scheduler stopped")

    def _seconds_until_next_preflight(self) -> float:
        """Compute sleep duration until the next scheduler wake-up.

        We wake at slot_boundary + _TARGET_OFFSET_S so transmit_frame
        can pad fresh wall-clock-aligned silence and land modulation
        at slot_boundary + 500ms (the JS8 protocol's expected
        modulation start point — see Modulator.cpp delay_ms).

        Firing AFTER the boundary is critical: if we fire before,
        transmit_frame's wall-clock read sees mstr in the previous
        slot (e.g. mstr=14900 for a 15 s slot). transmit_frame's
        round-up alignment formula handles that gracefully (the
        modulation just lands at the next 500 ms boundary), but it's
        still wasteful — we want clean slot+500ms alignment whenever
        possible.
        """
        now = self._now()
        past = now % self._slot_seconds
        # Time until the next boundary, plus our target offset.
        until_boundary = self._slot_seconds - past
        target = until_boundary + _TARGET_OFFSET_S
        # Defensive: target should always be positive (until_boundary
        # in (0, slot_seconds] plus a positive offset). But if some
        # weird clock skew makes it non-positive, wait one slot.
        if target <= 0:
            target += self._slot_seconds
        return target

    # ── Per-slot work ────────────────────────────────────────────────

    def _tick(self) -> None:
        """One slot's worth of work: timeout sweep, then maybe TX
        a single frame.

        Two paths:

          a. **Burst continuation.** A row is mid-burst (PTT keyed
             from the previous slot, audio device closed during the
             gap, scheduler holding _mf_state for it). We TX the
             next frame via ``transmit_frame()``. PTT stays keyed.
             Safety gate is NOT re-checked: once a burst is started,
             we trust through the burst — dropping PTT mid-burst
             because of a transient gate failure (e.g. chrony losing
             sync briefly) is worse than completing the message.

          b. **New row.** A QUEUED row gets encoded, ``start_burst()``
             keys PTT, and frame 0 is TX'd via ``transmit_frame()``.
             Safety gate IS checked here — the start of every burst
             is gated. If the message has more than one frame, the
             burst remains active for subsequent ticks; otherwise
             ``end_burst()`` runs immediately.

        On failure (playback error, CAT drop):
        ``end_burst()`` is called to release PTT, the in-memory
        burst context is discarded, and the row goes back to QUEUED
        for retry (or ABANDONED at MAX_ATTEMPTS) — whole message
        retried from frame 0 next attempt.
        """
        # 1. Process timed-out WAIT_ACK rows first — independent of
        # any in-progress burst. Capture the set of IDs that were
        # freshly transitioned to QUEUED for retry: those rows are
        # deferred until the NEXT slot. Rationale: the same tick that
        # does the WAIT_ACK→QUEUED DB UPDATE plus encoding plus
        # start_burst can blow our 500ms slot-alignment budget. On a
        # Pi Zero 2W that combination has been measured at 250-560 ms.
        # Deferring puts ~14 seconds between the DB transition and
        # the next encode, plenty of room.
        freshly_retried = self._process_ack_timeouts()

        # 2. Burst watchdog. If any in-progress multi-frame TX has
        # exceeded its deadline (n_frames × _BURST_WATCHDOG_PER_FRAME_S
        # seconds since burst start), force-end the burst, abandon the
        # message, and log loudly. This is a defense in depth: under
        # normal operation we never hit this — a 3-frame burst takes
        # ~45 s and the deadline is 60 s. If we do hit it, something
        # has gone fundamentally wrong (audio chain hang, scheduler
        # thread blocked, etc.) and continuing would mean PTT stays
        # keyed indefinitely.
        self._check_burst_watchdog()

        # 3. Decide what to TX. In-progress bursts take priority
        # over new queue work. Rows that just got retried are
        # excluded from this slot — they'll be picked next slot.
        msg, is_mf_continuation = self._pick_for_this_slot(
            skip_ids=freshly_retried,
        )
        if msg is None:
            self._update_status(blocked_reason=None)
            return

        # 4. Safety gate. ONLY for fresh starts — mid-burst frames
        # bypass the gate so we don't drop PTT for a transient
        # condition while the radio is keyed.
        if not is_mf_continuation:
            ok, reason = self._safety_gate.check_can_transmit()
            if not ok:
                _log.info(
                    "TX blocked: %s (msg id=%d)", reason, msg.id,
                )
                self._update_status(blocked_reason=reason)
                return

        # 5. If this is a NEW message, get the encoded audio + start
        # the burst. Audio comes from the in-memory cache populated
        # by the EncodeWorker. Fallback: if no cache (legacy path) or
        # cache miss (rare — daemon restart between encode and TX,
        # or test setup that bypasses the worker), encode inline.
        if not is_mf_continuation:
            audio_frames: "Optional[list[np.ndarray]]" = None
            if self._encoded_cache is not None:
                audio_frames = self._encoded_cache.get(msg.id)

            if audio_frames is None:
                # Cache miss → inline encode. The slot-alignment math
                # downstream uses JS8Call's round-up formula, so a
                # late frame just lands on the next 500 ms boundary
                # — decode-able but not optimally aligned.
                if self._encoded_cache is not None:
                    _log.warning(
                        "audio cache miss for msg id=%d — falling back "
                        "to inline encode (slot alignment will degrade "
                        "for frame 1)",
                        msg.id,
                    )
                try:
                    audio_frames = self._backend.encode(msg.text)
                except EncoderError as exc:
                    _log.error(
                        "encode failed for msg id=%d: %s", msg.id, exc,
                    )
                    self._queue.mark_sending(msg.id)
                    self._queue.mark_abandoned(
                        msg.id, error=f"encode failed: {exc}",
                    )
                    self._discard_cached_audio(msg.id)
                    self._update_status(
                        last_tx_at=self._now(),
                        last_tx_ok=False,
                        last_error=f"encode failed: {exc}",
                        blocked_reason=None,
                    )
                    return

            n_frames = len(audio_frames)

            # Two watchdogs are coordinated here. The scheduler-side
            # one fires first at `n_frames * _BURST_WATCHDOG_PER_FRAME_S`
            # and runs the proper burst-cleanup path (force end_burst,
            # abandon row, log loudly). The CAT-side one is a final
            # safety net: it fires `_CAT_WATCHDOG_GRACE_S` LATER, so
            # it only ever triggers if the scheduler itself is stuck
            # (unable to run its own watchdog). Giving the CAT layer
            # this grace window prevents it from yanking PTT mid-
            # burst before the scheduler has a chance to react.
            scheduler_deadline_s = n_frames * _BURST_WATCHDOG_PER_FRAME_S
            cat_max_hold_s = scheduler_deadline_s + _CAT_WATCHDOG_GRACE_S
            self._backend.set_burst_hold_seconds(cat_max_hold_s)

            start_result = self._backend.start_burst()
            if not start_result.success:
                _log.warning(
                    "start_burst failed for msg id=%d: %s",
                    msg.id, start_result.message,
                )
                # Bump the row to SENDING for the retry FSM, then
                # apply retry-or-abandon. start_burst() failure means
                # PTT was never keyed, so no end_burst() needed.
                # Clear the override since we're not using it after all.
                self._backend.set_burst_hold_seconds(0)
                self._queue.mark_sending(msg.id)
                self._fail_message(
                    msg, error=start_result.message or "start_burst failed",
                    frame_index=0, n_frames=n_frames,
                    tx_started_at=start_result.tx_started_at,
                )
                return

            burst_deadline = self._now() + scheduler_deadline_s
            self._mf_state[msg.id] = _MultiFrameContext(
                audio_frames=audio_frames,
                next_frame_index=0,
                burst_deadline=burst_deadline,
            )
            self._queue.mark_sending(msg.id)
            _log.info(
                "starting TX of msg id=%d: %d frame(s), "
                "watchdog deadline %.0fs from now (CAT max-hold %.0fs)",
                msg.id, n_frames, scheduler_deadline_s, cat_max_hold_s,
            )

        # 6. TX this slot's frame. PTT is already keyed (either from
        # this tick's start_burst or a previous tick's).
        ctx = self._mf_state[msg.id]
        frame_index = ctx.next_frame_index
        n_frames = ctx.total_frames
        audio = ctx.audio_frames[frame_index]

        _log.info(
            "TX frame %d/%d of msg id=%d (%d samples)",
            frame_index + 1, n_frames, msg.id, len(audio),
        )
        result = self._backend.transmit_frame(audio)

        if not result.success:
            # Frame failed (playback / device error). Alignment can
            # never fail by itself anymore — JS8Call's round-up
            # formula always succeeds. End the burst to release PTT,
            # discard the in-memory state, retry-or-abandon the row.
            try:
                self._backend.end_burst()
            except Exception:
                _log.exception(
                    "end_burst raised after frame failure; "
                    "PTT state uncertain",
                )
            self._mf_state.pop(msg.id, None)
            self._fail_message(
                msg, error=result.message or "tx failed",
                frame_index=frame_index, n_frames=n_frames,
                tx_started_at=result.tx_started_at,
            )
            return

        # 7. Frame succeeded. Advance the index. If that was the last
        # frame, end the burst (release PTT) and finalize the row.
        ctx.next_frame_index += 1
        if ctx.is_done:
            try:
                self._backend.end_burst()
            except Exception:
                _log.exception("end_burst raised after final frame")
            self._mf_state.pop(msg.id, None)
            # Determine if this row should skip WAIT_ACK. We honor
            # the stored kind, but ALSO re-infer from the text — this
            # is the safety net for any path that bypassed the
            # Python-API classifier (manual SQL INSERTs, prototype
            # tooling, REPL sessions, future Compose UI, etc.). If
            # EITHER the stored or inferred kind says "no ACK
            # expected", we skip WAIT_ACK. This is JS8Call-correct:
            # the protocol-truth lives in the text itself, not in
            # whatever kind value happened to be persisted.
            no_ack_kinds = (
                OutboundKind.HEARTBEAT,
                OutboundKind.CQ,
                OutboundKind.ALLCALL,
                OutboundKind.REPLY,
            )
            inferred = infer_outbound_kind(msg.text)
            skip_wait_ack = (
                msg.kind in no_ack_kinds
                or inferred in no_ack_kinds
            )
            if skip_wait_ack:
                # Broadcasts and REPLY-kind messages succeed on TX
                # (no ACK expected). REPLY covers auto-ACKs, QUERY
                # MSGS notifications, and operator-originated
                # queries — JS8Call protocol treats all of these
                # as terminal in the exchange. Without this branch,
                # an outbound query like "KD8PGB QUERY MSGS" would
                # enter WAIT_ACK and retransmit every 90 s waiting
                # for an ACK that's never coming (the protocol
                # response is YES/NO, not ACK).
                self._queue.mark_delivered(msg.id)
                self._discard_cached_audio(msg.id)
                if msg.kind != inferred and inferred in no_ack_kinds:
                    # Diagnostic: stored kind was wrong; the safety
                    # net rescued us. Log so we can spot bypass paths.
                    _log.info(
                        "delivered: outbound id=%d kind=%s (inferred=%s — "
                        "safety net skipped WAIT_ACK) (%d frame(s) sent)",
                        msg.id, msg.kind.value, inferred.value, n_frames,
                    )
                else:
                    _log.info(
                        "delivered: outbound id=%d kind=%s (%d frame(s) sent)",
                        msg.id, msg.kind.value, n_frames,
                    )
            else:
                # Directed mail (MSG / MSG TO:) — recipient auto-ACKs.
                # Audio stays cached in case ACK timeout triggers a retry.
                self._queue.mark_wait_ack(msg.id)
                _log.info(
                    "wait_ack: outbound id=%d (%d frame(s) sent, "
                    "awaiting ACK)",
                    msg.id, n_frames,
                )
        else:
            # More frames coming next slot. PTT stays keyed; row stays
            # SENDING; the in-memory context tracks where we are.
            _log.debug(
                "msg id=%d: frame %d/%d sent, %d remaining",
                msg.id, frame_index + 1, n_frames,
                n_frames - ctx.next_frame_index,
            )

        self._update_status(
            last_tx_at=result.tx_started_at or self._now(),
            last_tx_ok=True,
            last_error=None,
            blocked_reason=None,
        )
        # Only fire on_tx_complete when the WHOLE message is done.
        if ctx.is_done:
            self._fire_tx_complete(msg.kind, result)

    def _check_burst_watchdog(self) -> None:
        """Force-end any burst that has exceeded its watchdog deadline.

        Called once per tick before any other burst-touching work. We
        check every entry in ``self._mf_state`` (in practice there's
        at most one) and any whose ``burst_deadline`` is in the past
        gets force-ended:

          * ``self._backend.end_burst()`` is invoked to drop PTT —
            this is the load-bearing safety call. If it raises, we
            log and continue (PTT may be stuck, but we can't do more
            from here).
          * The ``_mf_state`` entry is dropped.
          * The DB row (still in ``SENDING`` state from the start of
            this burst) is marked ``ABANDONED`` with an explicit
            watchdog error so the operator sees what happened. We do
            NOT retry — a watchdog timeout means something has
            fundamentally gone wrong and another attempt would likely
            hit the same problem.
          * We log at WARNING level. The error string is kept simple
            and operator-readable.

        Under normal operation this never fires — a 3-frame burst
        takes ~45 s and the deadline is 60 s. If we hit it, that's a
        signal of an abnormal condition (audio chain hang, scheduler
        thread blocked elsewhere, etc.).
        """
        now = self._now()
        for row_id in list(self._mf_state.keys()):
            ctx = self._mf_state[row_id]
            if now <= ctx.burst_deadline:
                continue
            overshoot = now - ctx.burst_deadline
            n_frames = ctx.total_frames
            frames_done = ctx.next_frame_index
            error_msg = (
                f"burst watchdog fired: {n_frames}-frame burst "
                f"exceeded deadline by {overshoot:.1f}s "
                f"(only {frames_done}/{n_frames} frames sent); "
                f"PTT force-released, message abandoned"
            )
            _log.warning(
                "BURST WATCHDOG: msg id=%d — %s",
                row_id, error_msg,
            )

            # Force PTT release. This is the load-bearing safety
            # action — the radio is currently keyed and we MUST get
            # it off the air.
            try:
                self._backend.end_burst()
            except Exception:
                _log.exception(
                    "end_burst raised in watchdog cleanup for msg "
                    "id=%d; PTT state UNCERTAIN", row_id,
                )

            # Drop the in-memory burst state.
            self._mf_state.pop(row_id, None)

            # Mark the row abandoned. We always abandon (never retry)
            # on watchdog — the failure mode is structural and won't
            # be fixed by another attempt.
            try:
                self._queue.mark_abandoned(row_id, error=error_msg)
            except Exception:
                _log.exception(
                    "mark_abandoned raised in watchdog cleanup for "
                    "msg id=%d", row_id,
                )
            # Free cached audio for the abandoned row.
            self._discard_cached_audio(row_id)

            # Update status so the UI can surface what happened.
            self._update_status(
                last_tx_at=self._now(),
                last_tx_ok=False,
                last_error=error_msg,
                blocked_reason=None,
            )

    def _discard_cached_audio(self, message_id: int) -> None:
        """Remove the encoded-audio cache entry for a message that
        has reached a terminal state (DELIVERED/ABANDONED).

        Called from every code path that marks a row terminal, so the
        cache doesn't leak entries. Idempotent — calling for an id
        that was never cached (or was already discarded) is fine.
        """
        if self._encoded_cache is not None:
            self._encoded_cache.discard(message_id)

    def _fail_message(
        self,
        msg: "OutboundMessage",
        *,
        error: str,
        frame_index: int,
        n_frames: int,
        tx_started_at: Optional[float],
    ) -> None:
        """Handle a failed TX (mid-burst frame error or burst start
        failure). Routes through the retry-or-abandon FSM.

        Caller must have already cleaned up the in-memory burst state
        (popped from ``self._mf_state``) and called ``end_burst()`` if
        the burst was active.
        """
        # Re-fetch attempts count: it was bumped by mark_sending when
        # the burst was first picked up. For a multi-frame message
        # spanning slots, that was a previous tick.
        current = self._queue.get(msg.id)
        current_attempts = (
            current.attempts if current is not None else msg.attempts
        )
        if current_attempts >= MAX_ATTEMPTS:
            self._queue.mark_abandoned(msg.id, error=error)
            self._discard_cached_audio(msg.id)
            _log.warning(
                "abandoned outbound id=%d after %d attempts: %s "
                "(failed at frame %d/%d)",
                msg.id, current_attempts, error,
                frame_index + 1, n_frames,
            )
        else:
            self._queue.mark_retry(msg.id, error=error)
            # Cached audio is preserved for retry — same text, same audio.
            _log.info(
                "retry queued: outbound id=%d (attempt %d failed at "
                "frame %d/%d): %s",
                msg.id, current_attempts, frame_index + 1, n_frames,
                error,
            )
        self._update_status(
            last_tx_at=tx_started_at or self._now(),
            last_tx_ok=False,
            last_error=error,
            blocked_reason=None,
        )
        result = TxResult(
            success=False, message=error, tx_started_at=tx_started_at,
        )
        self._fire_tx_complete(msg.kind, result)

    def _pick_for_this_slot(
        self, *, skip_ids: "Optional[set[int]]" = None,
    ) -> "tuple[Optional[OutboundMessage], bool]":
        """Return (message, is_continuation) for this slot.

        Continuation = an in-progress multi-frame message; we need
        to TX its next frame in this slot to keep frames consecutive.
        Falls back to a fresh QUEUED message if no continuation is
        in flight.

        ``skip_ids`` — row IDs that should NOT be picked this tick.
        Used by the slot-budget defer mechanism: rows that just
        transitioned WAIT_ACK→QUEUED in this tick get deferred to
        next slot to keep encode latency from blowing the alignment
        budget. Continuations are NEVER deferred — they have to TX
        in their next slot or the receiver can't reassemble.

        Returns (None, False) if there's nothing to TX this slot.
        """
        skip = skip_ids or set()

        # Continuation has priority over fresh QUEUED rows.
        # Continuations are NOT subject to skip_ids — a multi-frame
        # message in flight has to TX its next frame in the next
        # slot, period. (In practice they wouldn't be in skip_ids
        # anyway since skip_ids contains rows that just got retried,
        # not rows in active bursts.)
        for row_id in list(self._mf_state.keys()):
            mf_msg = self._queue.get(row_id)
            if mf_msg is None:
                # The row was deleted somehow — drop the dangling
                # state so we don't loop forever.
                _log.warning(
                    "msg id=%d gone from queue but had multi-frame "
                    "state; dropping",
                    row_id,
                )
                self._mf_state.pop(row_id, None)
                continue
            if mf_msg.state != OutboundState.SENDING:
                # External actor changed our row — abort cleanly.
                _log.warning(
                    "msg id=%d unexpectedly in state=%s during "
                    "multi-frame TX; dropping context",
                    row_id, mf_msg.state.value,
                )
                self._mf_state.pop(row_id, None)
                continue
            return mf_msg, True

        # No continuation; pick the next QUEUED row, if any —
        # excluding rows that were just retried this tick.
        msg = self._queue.pick_next()
        if msg is not None and msg.id in skip:
            # Defer to next slot. Don't log at INFO — we already
            # logged "deferred to next slot" when we did the retry.
            _log.debug(
                "msg id=%d freshly retried; deferring to next slot",
                msg.id,
            )
            return None, False
        return msg, False

    def _fire_tx_complete(
        self, kind: "OutboundKind", result: TxResult,
    ) -> None:
        """Invoke the on_tx_complete callback if configured."""
        if self._on_tx_complete is not None:
            try:
                self._on_tx_complete(kind, result)
            except Exception:
                _log.exception("on_tx_complete callback raised")

    def _process_ack_timeouts(self) -> "set[int]":
        """Sweep WAIT_ACK rows; retry or abandon as appropriate.

        Returns the set of row IDs that were freshly transitioned to
        QUEUED for retry on THIS tick. The caller defers picking
        these rows until the NEXT tick — see ``_tick()`` for why.

        IDs that got ABANDONED (max attempts exceeded) are NOT in the
        returned set: those rows are out of the active pipeline and
        won't be picked anyway.
        """
        now = self._now()
        timed_out = self._queue.find_timed_out_acks(now)
        freshly_retried: set[int] = set()
        for msg in timed_out:
            if msg.attempts >= MAX_ATTEMPTS:
                self._queue.mark_abandoned(
                    msg.id, error=f"no ACK after {msg.attempts} attempts",
                )
                self._discard_cached_audio(msg.id)
                _log.warning(
                    "abandoned outbound id=%d (no ACK after %d attempts, %.1fs)",
                    msg.id, msg.attempts,
                    (now - (msg.last_tx_at or now)),
                )
            else:
                self._queue.mark_retry(
                    msg.id,
                    error=f"no ACK in {ACK_TIMEOUT_S:.0f}s; retrying",
                )
                # Audio stays cached — same text, same audio, retry uses it.
                freshly_retried.add(msg.id)
                _log.info(
                    "retrying outbound id=%d (no ACK in %.1fs); "
                    "deferred to next slot",
                    msg.id, (now - (msg.last_tx_at or now)),
                )
        return freshly_retried

    def _update_status(
        self,
        *,
        last_tx_at: Optional[float] = None,
        last_tx_ok: Optional[bool] = None,
        last_error: Optional[str] = None,
        blocked_reason: Optional[str] = None,
    ) -> None:
        """Update internal status, fire status callback if changed."""
        new = TxStatus(
            last_tx_at=last_tx_at if last_tx_at is not None else self._status.last_tx_at,
            last_tx_ok=last_tx_ok if last_tx_ok is not None else self._status.last_tx_ok,
            last_error=last_error if last_error is not None else self._status.last_error,
            queue_active=self._queue.active_count(),
            last_blocked_reason=blocked_reason,
        )
        if new != self._status:
            self._status = new
            if self._on_status_change is not None:
                try:
                    self._on_status_change(new)
                except Exception:
                    _log.exception("on_status_change callback raised")
