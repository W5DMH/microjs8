"""Input router.

Receives ``KeyEvent`` from any input source (keyboard thread, eventually
also synthesized events from tests) and applies the right effect:

  - **Global hotkeys** (Ctrl-S, Ctrl-Q, Ctrl-H) work from any screen,
    any mode, except when an edit is in progress (so Ctrl-S in the
    middle of typing a callsign doesn't jump screens — it'd be lost
    typing).
  - **Edit mode**: when a field is being edited, all printable keys go
    into the edit buffer, Backspace deletes, Enter commits, Esc reverts.
  - **Ring navigation**: ←/→ cycle the screen ring; locked when the
    station is unconfigured (per Step 3 spec — operator must finish
    Setup or use Emergency Beacon bypass).
  - **Field focus**: Tab/Shift-Tab cycle the focused field within a
    screen.
  - **Activation**: Enter on a focused interactive element fires the
    appropriate action (start edit, jump screen, arm beacon, …).

The router is purely a function of state; it has no I/O. That makes
the entire keyboard pipeline testable without GPIO, evdev, or the TFT.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, Protocol

from microjs8.input.events import Key, KeyEvent
from microjs8.ui.state import ComposeCmd, Screen, UIState

if TYPE_CHECKING:
    # Phase 12: emergency arm gesture is wired via forward reference so
    # tests that don't exercise the emergency flow can pass None without
    # paying the import cost. The actual instance is constructed in
    # app.py at daemon startup.
    from microjs8.input.emergency_arm_gesture import EmergencyArmGesture

_log = logging.getLogger(__name__)


# Maximum input lengths for editable fields. Beyond these we ignore
# additional characters — better than truncating after-the-fact.
_MAX_CALLSIGN = 10
_MAX_GRID = 6
_MAX_UNITS = 5  # "miles" / "km"
# Groups field: comma-separated list of up to 4 entries, each '@' +
# 1-8 alphanumeric/slash chars. Worst case ~44 chars; allow 64 for
# operator whitespace before the config validator normalises.
_MAX_GROUPS_FIELD = 64
# Max length for the freq_hz edit buffer. Up to "14078000" (8 chars) for
# Hz form, or "14.078" (6 chars) for MHz form. 12 is comfortable margin.
_MAX_FREQ_HZ = 12


# Type alias for the emergency-bypass action (jumps to Emergency screen
# with N0CALL identity, gated on GPS fix in Step 4).
EmergencyBypass = Callable[[], None]


# Structural types for the system-key services. We use Protocol rather
# than importing the concrete classes because the router shouldn't
# depend on ``power.backlight`` or ``input.shutdown_gesture`` at module
# load time — keeps test fakes trivial (any object with a matching
# method shape just works).
class _Backlight(Protocol):
    def toggle(self) -> None: ...


class _ShutdownGesture(Protocol):
    def arm(self) -> None: ...
    def cancel(self) -> None: ...


class InputRouter:
    """Translate KeyEvents into UIState mutations.

    Construct with the UIState, a callback for atomic config saves,
    and a callback for emergency bypass. Feed it KeyEvents via
    ``handle()``.
    """

    def __init__(
        self,
        ui: UIState,
        save_config: Callable[..., bool],
        emergency_bypass: EmergencyBypass,
        set_frequency: Optional[Callable[[int], bool]] = None,
        cycle_radio: Optional[Callable[[], bool]] = None,
        mark_inbox_read: Optional[Callable[[int], bool]] = None,
        delete_inbox_row: Optional[Callable[[int], bool]] = None,
        compose_send: Optional[Callable[[str, "ComposeCmd", str], bool]] = None,
        backlight: Optional["_Backlight"] = None,
        shutdown_gesture: Optional["_ShutdownGesture"] = None,
        emergency_arm_gesture: Optional["EmergencyArmGesture"] = None,
    ) -> None:
        self._ui = ui
        # save_config(callsign, grid, units) -> True if saved cleanly.
        # The UIState refreshes its identity from a successful save.
        self._save_config = save_config
        self._emergency_bypass = emergency_bypass
        # set_frequency(hz) -> True if the radio accepted the change.
        # Optional because the daemon may run without a CAT-capable
        # radio attached (headless tests, broken hardware). When None,
        # the freq_hz field on Setup behaves as read-only (commit fails).
        self._set_frequency = set_frequency
        # cycle_radio() -> True if the new radio_id was saved cleanly.
        # The callback is responsible for picking the next id from the
        # registry, updating UIState, and writing config.toml. The
        # daemon must be restarted for the change to take effect; the
        # Setup screen surfaces a "(restart)" hint in that interval.
        # Optional for testability — None disables the cycle.
        self._cycle_radio = cycle_radio
        # mark_inbox_read(row_id) -> True if the mailbox UPDATE took
        # effect (row was UNREAD; now it's READ). Called when the
        # operator opens detail-view on an inbox row. Optional so
        # tests can construct a router without a mailbox.
        self._mark_inbox_read = mark_inbox_read
        # delete_inbox_row(row_id) -> True if the row was DELETED from
        # the persistent store. Called when the operator presses
        # Delete on an inbox row. Hard delete — the row is gone from
        # inbox.db permanently. Optional so tests can construct a
        # router without a mailbox; when None, the in-memory cache
        # still updates so the UI stays responsive (but the row will
        # come back on the next refresh from disk).
        self._delete_inbox_row = delete_inbox_row
        # compose_send(to, cmd, text) -> True if the compose was
        # accepted by the outbound queue. The callback handles wire-
        # format building (using the station's grid for MYLOC) and
        # enqueue. Optional so tests can construct a router without
        # an outbound queue; when None, the SEND button is a visual
        # no-op (compose state still clears so the UI doesn't get
        # stuck).
        self._compose_send = compose_send
        # CardputerZero system-key services. Both optional — when None,
        # Fn+B / Fn+Q events are silently dropped, which is the
        # right behaviour in headless tests and during early bring-up
        # before the backlight node exists.
        self._backlight = backlight
        self._shutdown_gesture = shutdown_gesture
        # Phase 12: emergency arm gesture owns the 3-second arm/disarm
        # hold for the SOS beacon. Optional so tests that don't
        # exercise the emergency flow can omit it; ENTER/ESC on the
        # EMERGENCY screen are quiet no-ops when not provided.
        self._emergency_arm_gesture = emergency_arm_gesture

    # ── Late-binding setters ────────────────────────────────────────
    # These exist so app.py can construct the router early (before
    # subsystems like the backlight controller exist) and inject the
    # late-arriving services without touching private attributes.

    def set_backlight(self, backlight: Optional["_Backlight"]) -> None:
        """Inject (or replace) the backlight controller. ``None`` disables Fn+B."""
        self._backlight = backlight

    def set_shutdown_gesture(self, gesture: Optional["_ShutdownGesture"]) -> None:
        """Inject (or replace) the shutdown gesture handler. ``None`` disables Fn+Q."""
        self._shutdown_gesture = gesture

    def set_emergency_arm_gesture(
        self, gesture: Optional["EmergencyArmGesture"],
    ) -> None:
        """Inject (or replace) the emergency arm/disarm gesture handler.

        ``None`` disables ENTER/ESC processing on the EMERGENCY
        screen (those keys fall through to ring nav).
        """
        self._emergency_arm_gesture = gesture

    def handle(self, event: KeyEvent) -> None:
        """Top-level dispatcher. Wraps any handler exception so a
        single bad key doesn't take the input subsystem down."""
        try:
            self._handle(event)
        except Exception:
            _log.exception("input router raised on event=%r", event)

    def _handle(self, event: KeyEvent) -> None:
        snapshot = self._ui.snapshot()

        # 0) System keys — Fn+B (backlight toggle) and Fn+Q (shutdown
        # gesture). These work from ANY screen, including mid-edit:
        # the operator must always be able to dim the screen or power
        # down regardless of UI state. Routed before edit mode and
        # before global hotkeys.
        if event.key is Key.FN_B:
            if event.pressed and self._backlight is not None:
                self._backlight.toggle()
            return
        if event.key is Key.FN_Q:
            if self._shutdown_gesture is not None:
                if event.pressed:
                    self._shutdown_gesture.arm()
                else:
                    self._shutdown_gesture.cancel()
            return

        # 1) If we're in edit mode, the field eats every key first.
        if self._ui.is_editing():
            self._handle_edit_key(event)
            return

        # 2) Global hotkeys (only when NOT editing).
        if event.key is not None and self._handle_global_hotkey(event.key, snapshot):
            return

        # 3) Per-screen handling.
        self._handle_screen_key(event, snapshot)

    # ── Edit mode ────────────────────────────────────────────────────

    def _handle_edit_key(self, event: KeyEvent) -> None:
        """Field is being edited. Every key goes here."""
        if event.key is Key.ENTER:
            self._commit_edit()
            return
        if event.key is Key.ESC or event.key is Key.CTRL_C:
            self._ui.cancel_edit()
            return
        if event.key is Key.BACKSPACE:
            self._ui.edit_backspace()
            return
        if event.char is not None:
            # Drop characters that exceed the field's max length.
            field = self._ui.editing_field()
            buf = self._ui.edit_buffer()
            limit = self._field_max_len(field)
            if len(buf) >= limit:
                return
            # JS8Call's wire protocol is uppercase-only — callsigns,
            # grids, command verbs, free-text bodies all transmit
            # uppercase. We normalise at input time so the operator's
            # keyboard state (Shift / CapsLock / autocorrect) doesn't
            # produce mixed-case content that would either fail to
            # transmit cleanly or render inconsistently against
            # peers' uppercase displays. The single-character
            # ``.upper()`` is a no-op for digits, punctuation, and
            # already-uppercase letters — only ``a..z`` flip.
            #
            # Exception: ``units`` is a UI-local preference (miles vs
            # km) that never appears on the air, and the existing
            # validator rejects ``KM`` / ``MILES`` so uppercasing
            # would break the operator's input. Skip the uppercase
            # conversion for that field only.
            ch = event.char if field == "units" else event.char.upper()
            self._ui.edit_append(ch)

    def _field_max_len(self, field_name: Optional[str]) -> int:
        if field_name == "callsign":
            return _MAX_CALLSIGN
        if field_name == "grid":
            return _MAX_GRID
        if field_name == "units":
            return _MAX_UNITS
        if field_name == "groups":
            # Worst-case: 4 groups × (1 "@" + 8 chars + ", ") = ~44.
            # Allow some slack for operator whitespace before commit —
            # the config validator strips/normalises on save.
            return _MAX_GROUPS_FIELD
        if field_name == "freq_hz":
            return _MAX_FREQ_HZ
        return 0

    def _commit_edit(self) -> None:
        """Validate and write the in-progress edit; refresh identity."""
        field = self._ui.editing_field()
        buf = self._ui.edit_buffer()
        if field is None:
            return

        # Frequency edits don't touch the persistent config — they
        # change the radio's VFO directly via CAT. Validation happens
        # in the set_frequency callback (or here for the parse).
        if field == "freq_hz":
            self._commit_frequency_edit(buf)
            return

        # Read the OTHER current values from the snapshot so we save a
        # complete config, not a partial one.
        snap = self._ui.snapshot()
        new_call = snap.callsign
        new_grid = snap.grid
        new_units = snap.units
        new_groups = snap.groups

        if field == "callsign":
            new_call = buf
        elif field == "grid":
            new_grid = buf
        elif field == "units":
            new_units = buf
        elif field == "groups":
            # Hand the raw operator input straight to the save path —
            # config._validate_groups handles comma-splitting,
            # uppercase normalisation, dedup, implicit-group filtering,
            # and per-entry format validation. If the buffer is
            # malformed, save_config returns False and we re-render
            # the operator can correct without losing typed content.
            new_groups = buf

        ok = self._save_config(new_call, new_grid, new_units, new_groups=new_groups)
        if ok:
            self._ui.commit_edit()
        else:
            # Show the operator that the value was rejected. The save
            # function is expected to log the reason; we surface it by
            # marking the edit as invalid (UIState handles this).
            self._ui.mark_edit_invalid()

    def _commit_frequency_edit(self, buf: str) -> None:
        """Parse a frequency edit (in MHz) and push to CAT.

        Accepts forms like "7.078", "7078", "7,078" (comma replaced
        with dot for European keyboards). Out-of-range values are
        rejected by the CAT layer or the radio itself.
        """
        text = buf.strip().replace(",", ".")
        if not text:
            self._ui.mark_edit_invalid()
            return

        # Heuristic: if the value is < 1000, treat as MHz; otherwise
        # treat as Hz directly. Operators may type "7.078" or "7078000".
        try:
            if "." in text:
                hz = int(round(float(text) * 1_000_000))
            else:
                value = int(text)
                hz = value if value >= 1_000_000 else value * 1_000_000
        except ValueError:
            self._ui.mark_edit_invalid()
            return

        # Sanity bounds: HF amateur range plus a generous margin. The
        # QDX is band-limited by hardware; out-of-range values get
        # rejected by the radio anyway, but flagging them at the UI
        # layer gives faster feedback.
        if not (100_000 <= hz <= 60_000_000):
            self._ui.mark_edit_invalid()
            return

        if self._set_frequency is None:
            # No CAT — can't change the radio's actual frequency.
            self._ui.mark_edit_invalid()
            return

        if self._set_frequency(hz):
            # Success: update the displayed frequency and exit edit mode.
            self._ui.set_freq_hz(hz)
            self._ui.commit_edit()
        else:
            self._ui.mark_edit_invalid()

    # ── Global hotkeys ───────────────────────────────────────────────

    def _handle_global_hotkey(self, key: Key, snapshot) -> bool:
        """Returns True if the key was consumed as a global hotkey."""
        # Even unconfigured stations can use Ctrl-S to jump to Setup
        # (it's where they need to be anyway). Other hotkeys we gate.
        if key is Key.CTRL_S:
            self._ui.set_screen(Screen.SETUP)
            return True
        # Disallow other hotkeys when the station is unconfigured —
        # they don't yet have a meaningful effect, and we don't want
        # the operator to navigate away from Setup.
        if not snapshot.tx_allowed and not snapshot.emergency_override:
            return False
        if key is Key.CTRL_Q:
            self._ui.set_screen(Screen.ALLCALL)
            return True
        if key is Key.CTRL_H:
            # Heartbeat toggle is wired in Step 6.
            _log.info("Ctrl-H pressed (heartbeat toggle wired in Step 6)")
            return True
        return False

    # ── Per-screen handlers ──────────────────────────────────────────

    def _handle_screen_key(self, event: KeyEvent, snapshot) -> None:
        # Inbox detail view: only Esc (back to list) is meaningful.
        # ↑/↓ are reserved for future scroll-within-body, currently
        # no-op. Other keys ignored — explicitly NOT including ←/→
        # ring nav so the operator can't accidentally lose their
        # place by hitting the cycle keys.
        if snapshot.screen is Screen.INBOX_DETAIL:
            if event.key is Key.ESC:
                self._ui.inbox_close_detail()
                return
            # ↑/↓ no-op for now (future: scroll long body)
            if event.key in (Key.UP, Key.DOWN):
                return
            # Any other key is ignored in detail view — explicit
            # "do nothing" to avoid bleeding into ring nav.
            return

        # Inbox list view (Screen.INBOX): ↑/↓/Enter/Delete operate
        # on the mailbox list. Other keys fall through to ring nav.
        if snapshot.screen is Screen.INBOX:
            if event.key is Key.UP:
                self._ui.inbox_focus_up()
                return
            if event.key is Key.DOWN:
                self._ui.inbox_focus_down()
                return
            if event.key is Key.ENTER:
                self._handle_inbox_enter(snapshot)
                return
            if event.key is Key.DELETE:
                self._handle_inbox_delete(snapshot)
                return
            # ←/→ continue to ring nav below.

        # Directed activity log (Screen.DIRECTED): ↑/↓ reserved for
        # future scroll-up-into-history (the bottom of the list is
        # always the newest). Currently no-op — operator can see
        # whatever fits on screen, no detail view in this drop.
        # ←/→ continue to ring nav.
        if snapshot.screen is Screen.DIRECTED:
            if event.key in (Key.UP, Key.DOWN, Key.ENTER):
                return

        # Compose screen (Screen.COMPOSE): a four-field editor.
        # Tab cycles TO → CMD → TEXT → SEND → TO. Type-to-edit on
        # TO/TEXT, ↑/↓ cycles the CMD dropdown, Enter on SEND fires.
        # Esc clears and returns to the previous screen. ←/→ STILL
        # navigate the ring (operators can leave Compose mid-edit
        # without losing data — the in-progress fields persist until
        # they explicitly clear).
        if snapshot.screen is Screen.COMPOSE:
            if self._handle_compose_key(event, snapshot):
                return
            # Otherwise fall through to ring nav / generic handling.

        # Phase 12: EMERGENCY screen — arm/disarm gesture handling.
        # ENTER on idle begins a 3-second arm hold. ESC during arming
        # cancels. ESC on armed begins a 3-second disarm hold. The
        # handler returns True (consume) during a hold to prevent
        # accidental key-stroke escape; False (fall through) when
        # the operator is armed and pressing LEFT/RIGHT to navigate
        # away while the beacon keeps TXing in the background.
        if snapshot.screen is Screen.EMERGENCY:
            if self._handle_emergency_key(event, snapshot):
                return

        # Ring nav with ← / → (locked when unconfigured).
        if event.key is Key.LEFT:
            if self._ring_locked(snapshot):
                return
            self._ui.retreat_ring()
            return
        if event.key is Key.RIGHT:
            if self._ring_locked(snapshot):
                return
            self._ui.advance_ring()
            return

        # Field focus cycling (Tab / Shift-Tab — not implementing
        # Shift-Tab in Step 3 since it requires us to track Shift in the
        # router; Tab forward is enough for the small Setup field set).
        if event.key is Key.TAB:
            self._ui.cycle_focus()
            return

        # Activation
        if event.key is Key.ENTER:
            self._handle_enter(snapshot)
            return

        # Type-to-edit: if a printable character is pressed while a
        # focused editable field is selected, automatically enter edit
        # mode and consume the character. This matches the mental model
        # that "Tab to a field, then type" works, instead of forcing the
        # operator to remember an explicit Enter to begin editing.
        if event.char is not None and snapshot.screen is Screen.SETUP:
            field = self._ui.focused_field_name()
            # Phase 11: 'groups' is editable. Comma-separated list of
            # @<NAME> entries; the config validator handles parsing.
            if field in ("callsign", "grid", "groups", "units", "freq_hz"):
                self._ui.begin_edit(field)
                # Replace the prefilled buffer with the typed character —
                # the operator clearly wants to overwrite, not append.
                # (begin_edit pre-filled with current value; we clear it.)
                while self._ui.edit_buffer():
                    self._ui.edit_backspace()
                # Same uppercase rule as the in-edit path: JS8Call wire
                # protocol is uppercase-only, except 'units' which is a
                # UI-local preference.
                ch = event.char if field == "units" else event.char.upper()
                self._ui.edit_append(ch)
                return

    def _handle_emergency_key(
        self, event: KeyEvent, snapshot,
    ) -> bool:
        """EMERGENCY screen — arm/disarm via 3-second hold gestures.

        Returns True if the key was consumed; False if it should fall
        through to ring nav (left/right cycle to other screens).

        The state machine has four states:
          1. IDLE (not armed, no hold)
          2. ARMING (3-second arm hold in progress)
          3. ARMED (beacon TXing)
          4. DISARMING (3-second disarm hold in progress)

        Transitions:
          IDLE  + ENTER → begin_arming
          ARMING + ESC → cancel hold (back to IDLE)
          ARMING + other → ignored
          ARMED + ESC → begin_disarming
          ARMED + non-Esc nav keys → fall through to ring nav
          DISARMING + ENTER → cancel hold (back to ARMED)
          DISARMING + other → ignored

        Returning False allows the operator to navigate away from
        EMERGENCY while ARMED — the beacon continues to TX from its
        background thread, and the SOS badge in every screen header
        keeps the armed state visible.
        """
        if self._emergency_arm_gesture is None:
            # No gesture wired — quiet no-op so test fixtures that
            # don't exercise this path don't crash.
            return False

        gesture = self._emergency_arm_gesture
        is_holding = gesture.is_active()
        is_armed = snapshot.emergency_beacon_armed

        # ── During a hold: handle cancel, ignore everything else ────
        if is_holding:
            if gesture.is_arming():
                # Arming → ESC cancels.
                if event.key is Key.ESC:
                    gesture.cancel(source="keyboard ESC")
                    return True
            else:
                # Disarming → ENTER cancels (since ESC started it,
                # we'd loop on ESC; ENTER is the "go back" key here).
                if event.key is Key.ENTER:
                    gesture.cancel(source="keyboard ENTER")
                    return True
            # Any other key during a hold: ignored. This prevents
            # accidental cancel via stray keypresses but also blocks
            # ring nav — operator must complete or cancel the hold
            # before navigating.
            return True

        # ── No hold in progress: handle ENTER / ESC ─────────────────
        if not is_armed:
            # IDLE state.
            if event.key is Key.ENTER:
                gesture.begin_arming(source="keyboard ENTER")
                return True
            # ESC on idle — let it bubble. Esc usually means "back"
            # but EMERGENCY has nothing to back out of. Falling
            # through to ring nav is harmless (ring nav doesn't
            # consume Esc either, so this is effectively a no-op).
            return False
        else:
            # ARMED state — beacon is running.
            if event.key is Key.ESC:
                gesture.begin_disarming(source="keyboard ESC")
                return True
            # Other keys (LEFT/RIGHT for ring nav, ENTER, etc.):
            # fall through. Operator can navigate to INBOX/HEARD
            # while the beacon keeps transmitting.
            return False

    def _ring_locked(self, snapshot) -> bool:
        """Ring navigation is locked when station is unconfigured AND
        emergency bypass hasn't been activated."""
        return not snapshot.tx_allowed and not snapshot.emergency_override

    def _handle_enter(self, snapshot) -> None:
        """Enter on the focused element of the current screen."""
        if snapshot.screen is Screen.SETUP:
            field = self._ui.focused_field_name()
            if field == "emergency_bypass":
                self._emergency_bypass()
                return
            if field == "radio":
                # Radio is a cycling selector — Enter advances to the
                # next radio_id in the registry. The cycle callback
                # writes config.toml and updates UIState. NOT a text
                # edit (no keyboard buffer).
                if self._cycle_radio is not None:
                    self._cycle_radio()
                return
            if field in ("callsign", "grid", "groups", "units", "freq_hz"):
                self._ui.begin_edit(field)
                return
        # Other screens have no Enter binding in Step 3.

    def _handle_inbox_enter(self, snapshot) -> None:
        """Enter on the focused inbox row → open detail-view + mark READ.

        Side effects:
          1. UIState.inbox_open_detail() returns the row id and
             transitions screen → INBOX_DETAIL.
          2. If the row was UNREAD, persist mark_read via the daemon
             callback (mailbox UPDATE) and update the in-memory cache
             via mark_inbox_row_read_locally so the UI reflects the
             change immediately on return to the list.

        If the inbox is empty (open_detail returns None), this is a
        no-op — the operator pressed Enter on a blank list.
        """
        row_id = self._ui.inbox_open_detail()
        if row_id is None:
            return
        # Look up the row to decide whether to mark READ — only
        # UNREAD rows need the transition (READ → READ is a no-op
        # but writes to disk, which we want to avoid on re-opens).
        focused_row = None
        for row in snapshot.inbox_messages:
            if row.id == row_id:
                focused_row = row
                break
        if focused_row is None or focused_row.is_read:
            return
        # Update the persistent store via the daemon callback. If
        # the callback is None (test harness with no mailbox) we
        # still update the local UI cache so the UI is consistent.
        try:
            if self._mark_inbox_read is not None:
                self._mark_inbox_read(row_id)
        except Exception:
            _log.exception("mark_inbox_read raised on row id=%d", row_id)
        self._ui.mark_inbox_row_read_locally(row_id)

    def _handle_inbox_delete(self, snapshot) -> None:
        """Delete on the focused inbox row → hard-delete + UI dropout.

        Side effects:
          1. UIState.inbox_delete_focused() returns the row id and
             removes it from the in-memory cache so the UI updates
             instantly. Focus index is clamped (last-row → new last
             row, empty list → focus 0).
          2. The daemon's delete_inbox_row callback removes the row
             from inbox.db permanently. Hard delete — no recovery
             via SQL after this. If the callback is None (test
             harness), the in-memory drop still happens; the row
             will reappear on the next periodic refresh from disk.

        If the inbox is empty (delete_focused returns None), this is
        a no-op — operator pressed Delete on a blank list.

        Note: no confirmation prompt (the operator chose this binding
        explicitly). If we ever observe accidental deletes on-air
        we can add a "press again to confirm" debounce, but starting
        without it keeps the keypath simple.
        """
        row_id = self._ui.inbox_delete_focused()
        if row_id is None:
            return
        try:
            if self._delete_inbox_row is not None:
                self._delete_inbox_row(row_id)
        except Exception:
            _log.exception("delete_inbox_row raised on row id=%d", row_id)

    def _handle_compose_key(self, event: KeyEvent, snapshot) -> bool:
        """Dispatch a key on the COMPOSE screen. Returns True if handled.

        The handler dispatches based on (focused_field, key) pairs:

        Always-handled keys (any focused field):
          - Tab → cycle_focus (TO → CMD → TEXT → SEND → TO)
          - Esc → compose_clear, return to previous screen
          - ↑/↓ on CMD → cycle the dropdown enum

        Field-specific keys:
          - TO/TEXT focused, printable char → append to value
          - TO/TEXT focused, Backspace → drop last char
          - SEND focused, Enter → build wire string and enqueue, then
            clear and exit COMPOSE

        Returning False means the router falls through to ring-nav
        (← / → still navigate even from COMPOSE — the in-progress
        fields are preserved, so leaving and coming back is safe).
        """
        focused = snapshot.compose_focused_field

        if event.key is Key.TAB:
            self._ui.cycle_focus()
            return True

        if event.key is Key.ESC:
            # Esc clears the compose fields but stays on COMPOSE —
            # the operator can navigate away with ← / → if they want.
            # We don't auto-retreat-ring because Compose is in the
            # main ring at index 4, so retreat_ring would send them
            # to INBOX which usually isn't where they came from
            # (most likely they came from HEARD via repeated →).
            self._ui.compose_clear()
            return True

        # ↑/↓ on CMD field cycles the dropdown.
        if focused == "compose_cmd":
            if event.key is Key.UP:
                self._ui.compose_cycle_cmd(forward=False)
                return True
            if event.key is Key.DOWN:
                self._ui.compose_cycle_cmd(forward=True)
                return True
            # Other keys on CMD field don't do anything on the field
            # itself — fall through so ring nav etc. still works.
            return False

        # Type-to-edit on TO and TEXT.
        if focused == "compose_to":
            if event.char is not None:
                # Auto-uppercase callsigns. Strip whitespace inline —
                # callsigns don't contain spaces.
                ch = event.char.upper()
                if ch.strip():
                    self._ui.compose_set_to(snapshot.compose_to + ch)
                return True
            if event.key is Key.BACKSPACE:
                if snapshot.compose_to:
                    self._ui.compose_set_to(snapshot.compose_to[:-1])
                return True
            # Other keys (UP/DOWN) — let them fall through.
            return False

        if focused == "compose_text":
            if event.char is not None:
                self._ui.compose_set_text(snapshot.compose_text + event.char)
                return True
            if event.key is Key.SPACE:
                self._ui.compose_set_text(snapshot.compose_text + " ")
                return True
            if event.key is Key.BACKSPACE:
                if snapshot.compose_text:
                    self._ui.compose_set_text(snapshot.compose_text[:-1])
                return True
            return False

        if focused == "compose_send":
            if event.key is Key.ENTER:
                self._handle_compose_send(snapshot)
                return True
            return False

        return False

    def _handle_compose_send(self, snapshot) -> None:
        """SEND button activated → fire the compose, clear, jump to DIRECTED.

        Side effects:
          1. Invokes the compose_send callback (which builds the wire
             string and enqueues for TX). Wrapped in try/except so a
             queue error doesn't crash the input thread.
          2. compose_clear() blanks all fields and resets focus to TO.
          3. set_screen(DIRECTED) jumps to the activity log so the
             operator sees their just-sent message land in the chat
             stream — closes the loop visually. They can ←/→ back to
             COMPOSE to send another message.

        The compose state is cleared regardless of callback success —
        if the queue rejects the message (e.g., empty TO), we still
        return the operator to a clean state rather than leaving a
        confusing half-sent message on screen.
        """
        try:
            if self._compose_send is not None:
                self._compose_send(
                    snapshot.compose_to,
                    snapshot.compose_cmd,
                    snapshot.compose_text,
                )
        except Exception:
            _log.exception("compose_send raised; UI state will be cleared anyway")
        self._ui.compose_clear()
        # Jump to DIRECTED so the operator sees the message they just
        # sent appear in the activity log. JS8Call's send-and-watch
        # workflow — operator gets immediate visual confirmation the
        # message went into the system, plus they're parked on the
        # screen where any reply will land.
        self._ui.set_screen(Screen.DIRECTED)
