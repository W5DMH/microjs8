"""UI state machine — the screen ring, field focus, and edit mode.

The state object is the single source of truth for what the display
thread renders. Every mutation goes through one of the methods here,
which:

  1. updates the relevant field, and
  2. sets the dirty flag so the render thread knows to redraw.

Concurrency: ``UIState`` is mutated only from the asyncio thread.
The render thread reads it via ``snapshot()``, which returns a frozen
``UISnapshot`` dataclass. The snapshot is immutable so the render
thread can take its time without worrying about torn reads.

Step 3 added field-focus and edit-mode state:

  - **Focus** is tracked per-screen as an integer index into a screen-
    local list of focusable items. The list of items is defined in
    ``_FOCUSABLE_FIELDS`` below.
  - **Edit mode** holds a per-edit (field name, working buffer,
    invalid-flag) tuple while a Setup field is being edited.
  - **Emergency override** is a one-way flag that an unconfigured
    station can flip via the Setup screen's "[EMERGENCY BEACON →]"
    button. Once set, ``tx_allowed`` reads as True and the operator
    can navigate to the Emergency screen. Per spec there's no return
    path until reboot.
"""

from __future__ import annotations

import enum
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from microjs8.activity import DirectedActivityEntry
from microjs8.gps.types import FixKind, GpsFix, no_fix
from microjs8.power.battery import BatteryState
from microjs8.protocol.types import HeardStation, ParsedFrame


class Screen(enum.IntEnum):
    """The screen ring, ordered to match spec §6.2 with INBOX added.

    DIRECTED is the chronological activity log of protocol exchanges
    with our station (SNR?, QUERY MSGS, ACKs, etc.). INBOX is the
    mailbox view (UNREAD/READ MSGs and held STORE rows). They are
    distinct screens with distinct data backends — DIRECTED is an
    in-memory ring buffer, INBOX is the persistent mailbox DB.
    """

    HOME = 0
    HEARD = 1
    DIRECTED = 2          # protocol-activity chat log (in-memory ring buffer)
    INBOX = 3             # mailbox: UNREAD/READ MSGs + held STORE
    COMPOSE = 4
    ALLCALL = 5
    DIRECTED_MENU = 6
    EMERGENCY = 7
    SETUP = 8
    # The shutdown screen is NOT part of the ring — it's only entered
    # via the both-buttons gesture, never via ← / → navigation.
    SHUTTING_DOWN = 9
    # Inbox detail view — entered via Enter on a focused inbox row
    # in the INBOX screen, exited via Esc back to INBOX. Not part
    # of the main screen ring.
    INBOX_DETAIL = 10


class ComposeCmd(enum.Enum):
    """Compose-screen CMD dropdown values.

    The enum value is the on-air verb token (or empty for FREE-form
    directed messages, which carry no verb). ``MYLOC`` is a special
    case — it's UI-only and renders as "GRID <my_grid>" on the wire,
    so the operator can broadcast their location with a single keypress
    instead of typing the grid square.

    Operators cycle through these with ↑/↓ when CMD is focused, in
    the order declared here (most-common first). FREE is the default
    so that a fresh COMPOSE behaves like a chat-window: type a body
    and send.
    """

    FREE = ""           # no verb — wire is "<TO> <TEXT>"
    MSG = "MSG"         # buffered, CRC-checksummed mail item
    STORE = "STORE"     # ask peer to hold for forward
    AGN_Q = "AGN?"      # "again?" — ask peer to retransmit last
    SNR_Q = "SNR?"      # request signal report
    GRID = "GRID"       # ask "what's your grid?"
    QUERY = "QUERY"     # generic — TEXT supplies the rest (MSGS / MSG <id> / CALL)
    MYLOC = "MYLOC"     # UI-only; expands to "GRID <my_grid>" on wire


# Display order for the CMD dropdown — controls cycle direction.
COMPOSE_CMD_ORDER: tuple[ComposeCmd, ...] = (
    ComposeCmd.FREE,
    ComposeCmd.MSG,
    ComposeCmd.STORE,
    ComposeCmd.AGN_Q,
    ComposeCmd.SNR_Q,
    ComposeCmd.GRID,
    ComposeCmd.QUERY,
    ComposeCmd.MYLOC,
)


def build_compose_wire(
    to: str,
    cmd: ComposeCmd,
    text: str,
    my_grid: str,
    my_call: str = "",
) -> Optional[str]:
    """Build the wire-format string for a compose action.

    Returns the string that would go on the air (without the
    auto-prefixed "<from>: " envelope — that's added by the
    encoder). Returns ``None`` if the compose is incomplete (no TO
    callsign, or a CMD that requires TEXT but TEXT is empty), OR if
    the operator targeted their own callsign — see the SELF-CALL
    note below.

    Wire forms by command:

      FREE     "<TO> <TEXT>"            — directed free-form
      MSG      "<TO> MSG <TEXT>"        — buffered mail
      STORE    "<TO> STORE <TEXT>"      — store-and-forward
      AGN?     "<TO> AGN?"              — verb-only, no body
      SNR?     "<TO> SNR?"              — verb-only
      GRID     "<TO> GRID"              — verb-only ("what's your grid?")
      QUERY    "<TO> QUERY <TEXT>"      — TEXT is "MSGS" / "MSG <id>" / "CALL"
      MYLOC    "<TO> GRID <my_grid>"    — auto-expand grid from station config

    The TO field is uppercased on output (JS8Call protocol convention)
    and stripped of leading/trailing whitespace. TEXT is used as-typed
    (whitespace-trimmed at the boundary, internal spaces preserved
    because the protocol layer handles multi-frame whitespace
    correctly per the recent reassembly fixes).

    SELF-CALL note: the gfsk8 library (Varicode.cpp::buildMessageFrames,
    AUTO_REMOVE_MYCALL block) silently strips the leading callsign
    when it equals our own — operators sometimes type their own
    callsign as a prefix, and the protocol auto-adds the from-envelope
    so the prefix is redundant. This means a wire like
    "W5DMH MSG hi" (where W5DMH is our own call) gets encoded as
    just "MSG hi" — stripping the to-callsign entirely, producing
    a malformed frame with no directed-message envelope. The receiver
    sees plain text, not a directed MSG. We reject TO == my_call
    here to prevent silently-malformed transmissions; sending to
    yourself isn't a meaningful JS8 op anyway.
    """
    to = (to or "").strip().upper()
    if not to:
        return None
    # Reject TO == self — see SELF-CALL note above. The check is
    # case-insensitive and also rejects "W5DMH" matching "w5dmh".
    if my_call and to == my_call.strip().upper():
        return None
    text = (text or "").strip()

    body_required = cmd in (
        ComposeCmd.FREE,
        ComposeCmd.MSG,
        ComposeCmd.STORE,
        ComposeCmd.QUERY,
    )
    if body_required and not text:
        return None

    if cmd is ComposeCmd.FREE:
        return f"{to} {text}"
    if cmd is ComposeCmd.MYLOC:
        grid = (my_grid or "").strip()
        if not grid:
            return None  # can't broadcast a grid we don't have
        return f"{to} GRID {grid}"
    verb = cmd.value
    if body_required:
        return f"{to} {verb} {text}"
    return f"{to} {verb}"


# Screens reachable through the main ← / → ring (excludes the
# transient SHUTTING_DOWN screen and the modal INBOX_DETAIL).
RING: tuple[Screen, ...] = (
    Screen.HOME,
    Screen.HEARD,
    Screen.DIRECTED,
    Screen.INBOX,
    Screen.COMPOSE,
    Screen.ALLCALL,
    Screen.DIRECTED_MENU,
    Screen.EMERGENCY,
    Screen.SETUP,
)


# Per-screen list of focusable items. Step 3 only populates SETUP;
# other screens get focusable items as their interactivity lands in
# later steps.
_FOCUSABLE_FIELDS: dict[Screen, tuple[str, ...]] = {
    Screen.HOME: (),
    Screen.HEARD: (),
    Screen.DIRECTED: (),    # activity log: scrollable but no per-row Enter action
    Screen.INBOX: (),       # focus is row-index-based (see _inbox_focused_index)
    Screen.COMPOSE: ("compose_to", "compose_cmd", "compose_text", "compose_send"),
    Screen.ALLCALL: (),  # populated in Step 6
    Screen.DIRECTED_MENU: (),  # populated in Step 6
    Screen.EMERGENCY: (),  # populated in Step 4/6
    Screen.SETUP: ("callsign", "grid", "groups", "units", "freq_hz", "radio", "emergency_bypass"),
    Screen.SHUTTING_DOWN: (),
    # INBOX_DETAIL has no named focusable fields — focus is the
    # implicit "the message being viewed". Up/Down scroll the body.
    Screen.INBOX_DETAIL: (),
}


@dataclass(frozen=True)
class DirectedRow:
    """A directed message to be displayed on the Directed screen.

    We keep the from-call separate so the render can format it,
    plus the body and timestamp.

    Note: superseded by ``InboxRow`` in the Phase 1+2 inbox UI.
    Kept for compatibility with code paths that haven't been
    converted yet.
    """

    from_call: str
    body: str
    received_at: float
    snr_db: int


@dataclass(frozen=True)
class InboxRow:
    """One row in the JS8-protocol inbox / mailbox UI list.

    Mirrors a subset of MailboxStore.InboxRecord fields the render
    layer needs. ``id`` is the JS8 protocol message id (= the
    inbox.db row id). ``is_read`` lets the render distinguish bold
    UNREAD from dim READ. ``utc_iso`` is the ISO 8601 timestamp the
    record was stored with — formatted for display by the renderer.

    Frozen so it's safe to embed directly in a UISnapshot.
    """

    id: int
    from_call: str
    body: str
    utc_iso: str
    snr_db: Optional[int]
    is_read: bool


@dataclass(frozen=True)
class UISnapshot:
    """Immutable snapshot of UI state, safe to read from any thread."""

    screen: Screen
    callsign: str
    grid: str
    units: str                # "miles" or "km"
    tx_allowed: bool          # True when configured OR emergency_override
    emergency_override: bool  # set by the unconfigured-bypass flow

    # JS8Call group memberships, e.g. ('@EMCOMM','@ARESGA').
    # Operator-configured custom groups only — '@ALLCALL' / '@HB'
    # are NEVER in this tuple (every station is implicitly in those;
    # storing them here would cause double-counting in the parser's
    # address set). Kept as a frozen tuple so UISnapshot stays
    # hashable for the dirty-checking diff at the renderer boundary.
    groups: tuple[str, ...] = ()

    # Shutdown countdown — populated when in SHUTTING_DOWN screen.
    shutdown_remaining: float = 1.0
    previous_screen: Screen = Screen.HOME

    # Phase 12: emergency beacon state. ``emergency_beacon_armed`` is
    # the persistent ARMED flag set by the 3-second ENTER-hold
    # gesture on the EMERGENCY screen; the beacon thread starts
    # transmitting SOS broadcasts when armed. A red SOS badge
    # renders in every screen header while armed.
    emergency_beacon_armed: bool = False
    # 1.0 → 0.0 over 3 seconds while a hold gesture is in progress.
    # None when no hold is active. The renderer uses this to draw
    # the arming/disarming progress bar.
    emergency_hold_progress: Optional[float] = None
    # "arm" or "disarm" — None when no hold active. Tells the
    # renderer whether to label the countdown "Arming…" or
    # "Disarming…".
    emergency_hold_direction: Optional[str] = None

    # Focus + edit state. focused_field is None when the current screen
    # has no focusable items.
    focused_field: Optional[str] = None
    editing_field: Optional[str] = None
    edit_buffer: str = ""
    edit_invalid: bool = False  # last commit attempt rejected

    # Default frequency / mode (per spec — JS8 7.078 MHz / Normal).
    # Step 6: freq_hz is now editable on the Setup screen.
    freq_hz: int = 7_078_000
    mode: str = "Normal"

    # CAT connection status (Step 6). True once rigctld is connected
    # and we can change frequency / assert PTT. The Home screen shows
    # this as a small indicator so the operator knows TX is reachable.
    cat_connected: bool = False    # GPS state (Step 4). Always present; ``gps.kind == NO_FIX`` until
    # we've got something. ``gps_grid`` is the GPS-derived 6-char
    # locator; None until a 2D-or-better fix arrives. Per Step 4 spec
    # we DISPLAY the GPS grid on Home but TX with the configured grid
    # only — the operator's typed value rules.
    gps: GpsFix = field(default_factory=lambda: no_fix(time.monotonic()))
    gps_grid: Optional[str] = None

    # Phase Y: the active time source label. Empty string when no
    # source is usable (TX is blocked). Header bar uses this to tag
    # the clock readout: "UTC" when running on chrony / GPS / NTP,
    # "CONSENSUS" when running on radio-derived consensus alignment.
    time_source: str = ""

    # Radio profile id — read from [radio] in config.toml. The PTT
    # factory reads this at daemon startup to decide whether to use
    # CatService (rigctld) or RtsPttService (direct pyserial). The
    # Setup screen exposes this as a cycling selector: Enter on the
    # Radio row advances to the next id, saves config.toml, and
    # exits the daemon cleanly. systemd (Restart=always) brings us
    # back up and the new radio path takes effect. One decisive
    # action — no half-states, no "(restart)" hint to forget.
    radio_id: str = "qdx"

    # Heard List (Step 5). Most-recent-first slice of HeardStation
    # records, populated by the decode pipeline. Render layer slices
    # this further to fit the panel.
    heard: tuple[HeardStation, ...] = ()

    # Directed messages addressed to us (Step 5). One row per decoded
    # directed-to-us frame, most recent first. Stored as raw text +
    # who-from + when so the render layer can format consistently.
    directed: tuple["DirectedRow", ...] = ()

    # Inbox (Phase 1+2). Replaces the older directed list as the
    # canonical UI source for received-MSG messages. UNREAD/READ
    # rows newest-first; the home-screen indicator uses
    # ``inbox_unread_count`` and ``inbox_held_count``.
    inbox_messages: tuple["InboxRow", ...] = ()
    inbox_unread_count: int = 0
    inbox_held_count: int = 0

    # Index into ``inbox_messages`` for the focused/highlighted row
    # on the INBOX screen list view. 0 = newest.
    inbox_focused_index: int = 0

    # When the operator opens detail-view, this stores the inbox
    # row id being shown. None elsewhere — the screen field tracks
    # whether we're in INBOX list or INBOX_DETAIL view.
    inbox_detail_id: Optional[int] = None

    # Directed activity log (this drop). Bounded ring buffer of
    # protocol-level exchanges with our station that aren't mail
    # content (SNR?, QUERY MSGS, ACKs, etc.). Both inbound from
    # remote stations AND our outbound replies. Backed by an in-
    # memory ``DirectedActivityLog``; fed by the asyncio decode
    # handler and the outbound-reply enqueue path.
    #
    # Newest entry is at the END of the tuple (matches the deque's
    # natural append order). Renderer iterates in reverse for chat-
    # style newest-first display.
    directed_log_entries: tuple["DirectedActivityEntry", ...] = ()

    # Compose screen state. The TO field is the recipient callsign
    # (may be empty until the operator types or pre-population fires).
    # CMD is one of the ComposeCmd enum values (defaulting to FREE).
    # TEXT is the operator-typed message body.
    # ``compose_focused_field`` is the focusable-field name string
    # ("compose_to", "compose_cmd", "compose_text", "compose_send")
    # OR None when not on the COMPOSE screen.
    compose_to: str = ""
    compose_cmd: ComposeCmd = ComposeCmd.FREE
    compose_text: str = ""
    compose_focused_field: Optional[str] = None

    # Phase 6: battery snapshot from the BQ27220 fuel gauge. None
    # when the reader hasn't run yet (or when discovery has failed
    # for ≥3 polls — see ``power.battery.BatteryReader``). The HOME
    # row renders '--' in this case rather than crashing or hiding
    # the row entirely. The TX safety gate (§6.11) treats None as
    # "not critical" so a missing fuel gauge doesn't block TX.
    battery: Optional["BatteryState"] = None


class UIState:
    """Mutable UI state. Mutate from asyncio thread only."""

    def __init__(
        self,
        callsign: str,
        grid: str,
        tx_allowed: bool,
        units: str = "miles",
        *,
        groups: tuple[str, ...] = (),
    ) -> None:
        self._screen: Screen = Screen.HOME
        self._previous_screen: Screen = Screen.HOME
        self._callsign = callsign
        self._grid = grid
        self._units = units
        self._configured_tx_allowed = tx_allowed
        self._emergency_override = False
        # JS8Call group memberships. Tuple of uppercase strings each
        # starting with '@', e.g. ('@EMCOMM','@ARESGA'). NEVER
        # contains the implicit '@ALLCALL' / '@HB' groups — those
        # are handled by the parser's address-set builder so we
        # don't double-count.
        self._groups: tuple[str, ...] = groups
        self._shutdown_remaining: float = 1.0
        # Phase 12: emergency beacon state — see UISnapshot for the
        # field semantics. Mutators below: begin_emergency_arm_hold,
        # begin_emergency_disarm_hold, update_emergency_hold_progress,
        # cancel_emergency_hold, arm_emergency_beacon,
        # disarm_emergency_beacon.
        self._emergency_beacon_armed: bool = False
        self._emergency_hold_progress: Optional[float] = None
        self._emergency_hold_direction: Optional[str] = None
        # App.py registers a callback here that constructs/starts/
        # stops the EmergencyBeacon thread when armed-state changes.
        # Mirrors the heartbeat-mode callback pattern that arrives
        # in Phase 13.
        self._em_arm_change_cb: Optional[Callable[[bool], None]] = None
        # Focus index per screen, default 0.
        self._focus_index: dict[Screen, int] = {s: 0 for s in Screen}
        # Edit state.
        self._editing_field: Optional[str] = None
        self._edit_buffer: str = ""
        self._edit_invalid: bool = False
        # Frequency / mode.
        self._freq_hz: int = 7_078_000
        self._mode: str = "Normal"
        # GPS — initialized to NO_FIX so consumers don't need None checks.
        self._gps: GpsFix = no_fix(time.monotonic())
        self._gps_grid: Optional[str] = None
        # CAT connection status (Step 6). False until CatService says
        # otherwise via set_cat_connected().
        self._cat_connected: bool = False
        # Phase Y: active time-source label for the header clock tag.
        self._time_source: str = ""
        # Radio profile id (Setup screen selector). Initially the
        # value loaded from config; cycled by the operator via Enter
        # on the Radio row. Each cycle saves to config.toml and
        # restarts the daemon — there's never a half-state where the
        # UI shows one thing and the running radio path is something
        # else. Set by app.py from the loaded Config at startup.
        self._radio_id: str = "qdx"
        # Heard list (Step 5). Tuple to make it cheap to share across
        # threads (immutable). Rebuilt on every set_heard() call.
        self._heard: tuple[HeardStation, ...] = ()
        # Directed-to-us messages, most recent first. Legacy from
        # Step 5; inbox_messages is the new canonical source.
        self._directed: tuple[DirectedRow, ...] = ()
        # Inbox / mailbox (Phase 1+2). Renders on Screen.INBOX.
        self._inbox_messages: tuple[InboxRow, ...] = ()
        self._inbox_unread_count: int = 0
        self._inbox_held_count: int = 0
        self._inbox_focused_index: int = 0
        self._inbox_detail_id: Optional[int] = None
        # Directed activity log (this drop). Snapshot of the in-memory
        # ring buffer at app level. Renders on Screen.DIRECTED. This
        # is the chronological chat-style view of protocol-level
        # exchanges (SNR?, QUERY MSGS, ACKs, etc.) — both inbound
        # and outbound. Newest entry at the END of the tuple.
        self._directed_log_entries: tuple[DirectedActivityEntry, ...] = ()

        # Compose screen state — fields, focus, and helpers. The TO
        # field defaults to "" but gets pre-populated from the Heard
        # list whenever the operator navigates into COMPOSE (see
        # ``compose_prepopulate_from_heard``). CMD defaults to FREE
        # so a fresh COMPOSE behaves like a chat window — type a
        # body and send a directed message with no protocol verb.
        # TEXT is always free-typed.
        self._compose_to: str = ""
        self._compose_cmd: ComposeCmd = ComposeCmd.FREE
        self._compose_text: str = ""
        # Pointer into the heard+groups cycle so successive ↑/↓ keypresses
        # walk the list in order rather than jumping back to position 0.
        # None means "no cycle position yet" — next press lands on index 0
        # (or n-1 for ↑). Reset to None whenever the operator hand-types
        # a new TO value via begin_edit/commit.
        self._compose_to_heard_index: Optional[int] = None

        # Phase 6: battery snapshot (None until BatteryReader fires
        # the first successful poll, or back to None if discovery
        # fails or 3 consecutive reads error).
        self._battery: Optional[BatteryState] = None

        self._lock = threading.Lock()
        self._dirty = threading.Event()
        self._dirty.set()

    # ── Properties for the router ────────────────────────────────────

    @property
    def tx_allowed(self) -> bool:
        return self._configured_tx_allowed or self._emergency_override

    def is_editing(self) -> bool:
        return self._editing_field is not None

    def editing_field(self) -> Optional[str]:
        return self._editing_field

    def edit_buffer(self) -> str:
        return self._edit_buffer

    def focused_field_name(self) -> Optional[str]:
        fields = _FOCUSABLE_FIELDS.get(self._screen, ())
        if not fields:
            return None
        idx = self._focus_index.get(self._screen, 0)
        return fields[idx % len(fields)]

    # ── Ring navigation ──────────────────────────────────────────────

    def advance_ring(self) -> None:
        idx = RING.index(self._screen) if self._screen in RING else 0
        self._screen = RING[(idx + 1) % len(RING)]
        self._on_screen_entered()
        self._dirty.set()

    def retreat_ring(self) -> None:
        idx = RING.index(self._screen) if self._screen in RING else 0
        self._screen = RING[(idx - 1) % len(RING)]
        self._on_screen_entered()
        self._dirty.set()

    def set_screen(self, screen: Screen) -> None:
        """Jump to a specific screen (used by hotkeys and bypass)."""
        if self._screen is not screen:
            self._screen = screen
            # If we're entering edit mode and switching away, abandon.
            self._editing_field = None
            self._edit_buffer = ""
            self._edit_invalid = False
            self._on_screen_entered()
            self._dirty.set()

    def _on_screen_entered(self) -> None:
        """Hook for per-screen actions taken on transition INTO that screen.

        Currently:
          - Entering COMPOSE pre-populates the TO field with the most-
            recently-heard callsign that ISN'T our own. The pre-
            populate helper is non-destructive: it won't overwrite a
            TO field that the operator has already typed into. We
            skip self-decodes (which can happen if the radio loop-
            backs our own TX into the receiver) so the operator
            doesn't accidentally try to send a message to themselves.

        Other screens may grow similar hooks here over time; keeping
        the dispatch in one place makes it obvious where to look
        when adding cross-screen state effects.
        """
        if self._screen is Screen.COMPOSE:
            latest_call: Optional[str] = None
            our_call_upper = self._callsign.upper()
            for station in self._heard:
                if station.callsign.upper() != our_call_upper:
                    latest_call = station.callsign
                    break
            self.compose_prepopulate_from_heard(latest_call)

    # ── Focus cycling ────────────────────────────────────────────────

    def cycle_focus(self) -> None:
        fields = _FOCUSABLE_FIELDS.get(self._screen, ())
        if not fields:
            return
        idx = self._focus_index.get(self._screen, 0)
        self._focus_index[self._screen] = (idx + 1) % len(fields)
        # Cancel any in-progress edit when focus moves.
        self._editing_field = None
        self._edit_buffer = ""
        self._edit_invalid = False
        self._dirty.set()

    # ── Edit mode ────────────────────────────────────────────────────

    def begin_edit(self, field: str) -> None:
        """Start editing. Pre-fills the buffer with the current value."""
        self._editing_field = field
        self._edit_invalid = False
        if field == "callsign":
            self._edit_buffer = self._callsign if self._callsign != "N0CALL" else ""
        elif field == "grid":
            self._edit_buffer = self._grid
        elif field == "units":
            self._edit_buffer = self._units
        elif field == "groups":
            # Pre-fill with the current groups as a comma-separated
            # string. The Setup screen edits and saves this format —
            # the on-wire intuition: "type the groups, separated by
            # commas, with @ in front of each". On commit, the router
            # passes the raw buffer to config._validate_groups, which
            # canonicalises (uppercase, dedup, drop implicit).
            self._edit_buffer = ", ".join(self._groups)
        elif field == "freq_hz":
            # Pre-fill with the current frequency in MHz, e.g. "7.078".
            # Easier for the operator than typing 7 digits of Hz.
            self._edit_buffer = f"{self._freq_hz / 1_000_000:.3f}"
        else:
            self._edit_buffer = ""
        self._dirty.set()

    def edit_append(self, ch: str) -> None:
        if self._editing_field is None:
            return
        self._edit_buffer += ch
        self._edit_invalid = False
        self._dirty.set()

    def edit_backspace(self) -> None:
        if self._editing_field is None:
            return
        if self._edit_buffer:
            self._edit_buffer = self._edit_buffer[:-1]
            self._edit_invalid = False
            self._dirty.set()

    def cancel_edit(self) -> None:
        if self._editing_field is None:
            return
        self._editing_field = None
        self._edit_buffer = ""
        self._edit_invalid = False
        self._dirty.set()

    def commit_edit(self) -> None:
        """Mark the in-progress edit as accepted.

        Note: the actual config write happens in the router; this method
        only flips the UI out of edit mode. Caller must update the
        identity fields via ``set_identity()`` if they changed.
        """
        self._editing_field = None
        self._edit_buffer = ""
        self._edit_invalid = False
        self._dirty.set()

    def mark_edit_invalid(self) -> None:
        """Visually flag a rejected commit so the operator notices."""
        self._edit_invalid = True
        self._dirty.set()

    # ── Identity refresh ─────────────────────────────────────────────

    def set_identity(
        self,
        callsign: str,
        grid: str,
        units: str,
        tx_allowed: bool,
        *,
        groups: tuple[str, ...] = (),
    ) -> None:
        """Refresh identity — used after config save or reload."""
        if (callsign, grid, units, tx_allowed, groups) != (
            self._callsign, self._grid, self._units,
            self._configured_tx_allowed, self._groups,
        ):
            self._callsign = callsign
            self._grid = grid
            self._units = units
            self._configured_tx_allowed = tx_allowed
            self._groups = groups
            self._dirty.set()

    # ── Read-only groups accessor (used by parser and Setup screen) ──

    @property
    def groups(self) -> tuple[str, ...]:
        """Current operator-configured group memberships.

        NEVER contains the implicit ``@ALLCALL`` or ``@HB`` groups —
        the parser's address-set builder handles those separately.
        """
        return self._groups

    def set_freq_hz(self, freq_hz: int) -> None:
        """Refresh the displayed frequency.

        Called by app.py after a successful frequency edit on Setup,
        or periodically when polling the radio's actual VFO via CAT.
        """
        if freq_hz != self._freq_hz:
            self._freq_hz = freq_hz
            self._dirty.set()

    def set_cat_connected(self, connected: bool) -> None:
        """Update the CAT connection status indicator on Home screen.

        Called by app.py from the CatService status callback. Edge-
        triggered: only marks dirty when the status actually changes,
        so periodic re-affirmations don't churn the screen.
        """
        if connected != self._cat_connected:
            self._cat_connected = connected
            self._dirty.set()

    def set_time_source(self, source: str) -> None:
        """Update the active time-source label.

        ``source`` is "chrony", "consensus", or "" (no source). The
        header bar shows "UTC" for chrony, "CONSENSUS" for consensus,
        and an explicit warning indicator for empty.

        Edge-triggered like set_cat_connected — no churn when steady.
        """
        if source != self._time_source:
            self._time_source = source
            self._dirty.set()

    # ── Radio profile selector ───────────────────────────────────────

    def set_radio_id(self, radio_id: str) -> None:
        """Update the displayed radio id.

        Validation is the caller's responsibility — pass a string that
        ``known_radio_ids()`` accepts. Edge-triggered: only marks
        dirty when the id changes.

        Called both at startup (from app.py with the loaded config
        value) and at runtime (from the cycle handler, just before
        the daemon exits to be restarted by systemd).
        """
        if radio_id != self._radio_id:
            self._radio_id = radio_id
            self._dirty.set()

    # ── Emergency bypass ─────────────────────────────────────────────

    def trigger_emergency_override(self) -> None:
        """Activate the unconfigured-emergency path.

        Per spec, there is no programmatic way to deactivate this — it
        clears only on reboot. The flag elevates ``tx_allowed`` to True
        so the operator can reach the Emergency screen and arm the
        beacon.
        """
        if not self._emergency_override:
            self._emergency_override = True
            self._screen = Screen.EMERGENCY
            self._dirty.set()

    # ── Shutdown gesture ─────────────────────────────────────────────

    def begin_shutdown(self) -> None:
        if self._screen is not Screen.SHUTTING_DOWN:
            self._previous_screen = self._screen
        self._screen = Screen.SHUTTING_DOWN
        self._shutdown_remaining = 1.0
        self._dirty.set()

    def update_shutdown_progress(self, remaining: float) -> None:
        clamped = max(0.0, min(1.0, remaining))
        if clamped != self._shutdown_remaining:
            self._shutdown_remaining = clamped
            self._dirty.set()

    def cancel_shutdown(self) -> None:
        if self._screen is Screen.SHUTTING_DOWN:
            self._screen = self._previous_screen
            self._shutdown_remaining = 1.0
            self._dirty.set()

    # ── Phase 12: Emergency beacon arm/disarm gesture state ──────────

    def set_emergency_arm_change_callback(
        self, cb: Callable[[bool], None],
    ) -> None:
        """Register a callback invoked when the armed state changes.

        Mirrors ``set_hb_mode_change_callback`` — app.py wires this
        to construct/start/stop the EmergencyBeacon thread. Single-
        slot (latest registration wins).
        """
        self._em_arm_change_cb = cb

    def begin_emergency_arm_hold(self) -> None:
        """Operator pressed ENTER on EMERGENCY (idle) — start arm hold.

        Sets the hold-progress to 1.0 (full) and the direction to
        'arm'. The gesture's countdown task will tick this down
        toward 0.0 over 3 seconds; when it reaches 0 the gesture
        calls ``arm_emergency_beacon`` which flips the armed flag
        and fires the app-level callback to start the TX thread.
        """
        self._emergency_hold_progress = 1.0
        self._emergency_hold_direction = "arm"
        self._dirty.set()

    def begin_emergency_disarm_hold(self) -> None:
        """Operator pressed ESC on EMERGENCY (armed) — start disarm hold.

        Same shape as ``begin_emergency_arm_hold`` but with the
        opposite direction. Completion calls ``disarm_emergency_beacon``.
        """
        self._emergency_hold_progress = 1.0
        self._emergency_hold_direction = "disarm"
        self._dirty.set()

    def update_emergency_hold_progress(self, remaining: float) -> None:
        """Tick the in-progress hold's progress bar.

        Called by the gesture's countdown coroutine at 20 Hz. ``remaining``
        is clamped to [0.0, 1.0]. Idempotent if the value hasn't changed,
        so the render-dirty flag only fires on actual visible change.
        """
        clamped = max(0.0, min(1.0, remaining))
        if clamped != self._emergency_hold_progress:
            self._emergency_hold_progress = clamped
            self._dirty.set()

    def cancel_emergency_hold(self) -> None:
        """Operator cancelled the in-flight hold — abort transition.

        Does NOT change the armed flag — that flips only on hold-
        completion via ``arm_emergency_beacon`` / ``disarm_emergency_beacon``.
        Cancel just clears the hold-progress state so the screen
        renders the appropriate idle view (either "Beacon: not
        armed" or "Beacon: ARMED" depending on what the operator
        was doing).
        """
        if (
            self._emergency_hold_progress is not None
            or self._emergency_hold_direction is not None
        ):
            self._emergency_hold_progress = None
            self._emergency_hold_direction = None
            self._dirty.set()

    def arm_emergency_beacon(self) -> None:
        """Called by the gesture on completion of an arm-hold.

        Flips the armed flag to True, clears the hold state, and
        fires the registered callback so app.py can spin up the
        beacon thread. Idempotent: arming an already-armed beacon
        is a no-op (the callback isn't refired, so we won't
        accidentally start two beacons).
        """
        if self._emergency_beacon_armed:
            # Already armed — clear any leftover hold state defensively
            # and return without refiring the callback.
            self._emergency_hold_progress = None
            self._emergency_hold_direction = None
            self._dirty.set()
            return
        self._emergency_beacon_armed = True
        self._emergency_hold_progress = None
        self._emergency_hold_direction = None
        self._dirty.set()
        if self._em_arm_change_cb is not None:
            try:
                self._em_arm_change_cb(True)
            except Exception:
                # The renderer / state mutation already happened; a
                # raising callback shouldn't roll us back since the
                # operator already saw the armed transition. App.py
                # logs the exception via its own exception handler.
                pass

    def disarm_emergency_beacon(self) -> None:
        """Called by the gesture on completion of a disarm-hold.

        Flips the armed flag to False, clears hold state, fires the
        callback so app.py can stop the beacon thread. Idempotent.
        """
        if not self._emergency_beacon_armed:
            self._emergency_hold_progress = None
            self._emergency_hold_direction = None
            self._dirty.set()
            return
        self._emergency_beacon_armed = False
        self._emergency_hold_progress = None
        self._emergency_hold_direction = None
        self._dirty.set()
        if self._em_arm_change_cb is not None:
            try:
                self._em_arm_change_cb(False)
            except Exception:
                pass

    # ── Heard list / Directed list (Step 5) ──────────────────────────

    def set_heard(self, heard: tuple[HeardStation, ...]) -> None:
        """Replace the heard-list snapshot.

        Caller passes the most-recent-first slice from the message
        store; we don't sort here. The render layer respects the
        order it's given.

        Marks dirty if the list actually changed (callsign membership
        OR most-recent timestamps), so high-frequency updates of the
        same N stations don't churn the screen.
        """
        if heard == self._heard:
            return
        self._heard = heard
        self._dirty.set()

    def append_directed(self, row: DirectedRow) -> None:
        """Add a new directed message to the head of the directed list."""
        self._directed = (row,) + self._directed
        # Keep the in-memory list bounded; the SQLite store is the
        # canonical record.
        if len(self._directed) > 100:
            self._directed = self._directed[:100]
        self._dirty.set()

    def set_directed(self, directed: tuple[DirectedRow, ...]) -> None:
        """Replace the directed-list snapshot (used during initial load)."""
        if directed == self._directed:
            return
        self._directed = directed
        self._dirty.set()

    # ── Inbox / mailbox (Phase 1+2) ──────────────────────────────────

    def set_inbox(
        self,
        *,
        records,
        held_count: int,
        unread_count: int,
    ) -> None:
        """Replace the inbox snapshot from a fresh MailboxStore query.

        Called by app.py whenever the mailbox table has changed
        (UNREAD added, mark_read, mark_delivered, delete, or any of
        the STORE-row events). The records argument is a tuple of
        ``MailboxStore.InboxRecord`` instances; we convert each to
        the lighter-weight ``InboxRow`` for the UI.

        Marks dirty only on observable change — the held/unread
        counters changing or the message tuple changing. Avoids
        churn from re-running the same query.
        """
        # Convert MailboxStore.InboxRecord → UI's InboxRow. We accept
        # any iterable so tests can pass plain tuples, not just the
        # store's class. ``type`` is on the MailboxStore record;
        # we map it to is_read for the UI.
        new_messages: list[InboxRow] = []
        for r in records:
            # MailboxStore returns UNREAD + READ rows for list_inbox().
            # Map the type discriminator to is_read for UI styling.
            type_str = getattr(r, "type", "")
            new_messages.append(
                InboxRow(
                    id=int(getattr(r, "id")),
                    from_call=str(getattr(r, "from_call", "") or ""),
                    body=str(getattr(r, "text", "") or ""),
                    utc_iso=str(getattr(r, "utc_iso", "") or ""),
                    snr_db=getattr(r, "snr_db", None),
                    is_read=(type_str == "READ"),
                )
            )
        new_tuple = tuple(new_messages)

        changed = (
            new_tuple != self._inbox_messages
            or held_count != self._inbox_held_count
            or unread_count != self._inbox_unread_count
        )
        if not changed:
            return
        self._inbox_messages = new_tuple
        self._inbox_held_count = held_count
        self._inbox_unread_count = unread_count

        # If our focus index is now out of bounds (a row was deleted
        # or we're newly empty), clamp it back into range. The clamp
        # is idempotent — focused_index=0 on an empty list is harmless;
        # the renderer just won't draw a chevron.
        if self._inbox_focused_index >= len(new_tuple):
            self._inbox_focused_index = max(0, len(new_tuple) - 1)

        self._dirty.set()

    def set_directed_log(
        self,
        entries: tuple[DirectedActivityEntry, ...],
    ) -> None:
        """Replace the directed-activity snapshot.

        Called by app.py after every record_in/record_out on the
        underlying log so the UI sees fresh data on the next render
        tick. Marks dirty only when the snapshot actually changed,
        to avoid burning render cycles on no-op updates (the log is
        appended to often; we don't want to rerender every time even
        if the visible window didn't move).

        ``entries`` is the full snapshot from ``DirectedActivityLog
        .snapshot()`` — caller does not need to slice; the renderer
        will take the most-recent N and the operator can scroll
        upward through history.
        """
        if entries == self._directed_log_entries:
            return
        self._directed_log_entries = entries
        self._dirty.set()

    def inbox_focus_up(self) -> None:
        """Move focused inbox row up (toward newer / index 0).

        No-op if already at index 0 or the list is empty. Marks dirty
        only on observable change so holding the up-arrow at the top
        doesn't cause repeated repaints.
        """
        if self._inbox_focused_index <= 0:
            return
        self._inbox_focused_index -= 1
        self._dirty.set()

    def inbox_focus_down(self) -> None:
        """Move focused inbox row down (toward older / higher index).

        No-op if at the end of the list. Note: the renderer is
        responsible for clipping to the visible window — this method
        always moves the logical focus, even if the row would be
        off-screen.
        """
        if self._inbox_focused_index >= len(self._inbox_messages) - 1:
            return
        self._inbox_focused_index += 1
        self._dirty.set()

    def inbox_open_detail(self) -> Optional[int]:
        """Transition from inbox list to detail view of the focused row.

        Returns the focused inbox row id (caller uses it to mark
        the row as READ in MailboxStore). Returns None if the inbox
        is empty — there's nothing to focus, so detail-view is a
        no-op.

        Side effects:
          - ``screen`` transitions to ``INBOX_DETAIL``
          - ``inbox_detail_id`` set to the focused row's id
          - ``previous_screen`` saved so back-button returns correctly
        """
        if not self._inbox_messages:
            return None
        idx = self._inbox_focused_index
        if idx < 0 or idx >= len(self._inbox_messages):
            return None
        row = self._inbox_messages[idx]
        self._previous_screen = self._screen
        self._screen = Screen.INBOX_DETAIL
        self._inbox_detail_id = row.id
        self._dirty.set()
        return row.id

    def inbox_close_detail(self) -> None:
        """Return from INBOX_DETAIL to the previous (list) screen.

        No-op if we're not currently in INBOX_DETAIL. Restoring
        previous_screen rather than hard-coding DIRECTED preserves
        the navigation arc — the operator gets back to wherever
        they were when they entered detail-view.
        """
        if self._screen is not Screen.INBOX_DETAIL:
            return
        self._screen = self._previous_screen
        self._inbox_detail_id = None
        self._dirty.set()

    def inbox_delete_focused(self) -> Optional[int]:
        """Delete the currently-focused inbox row from the in-memory cache.

        Returns the deleted row's id (caller forwards it to the
        mailbox-store delete callback). Returns None if the inbox
        is empty — Delete on an empty list is a no-op.

        Side effects:
          - The focused row is removed from ``self._inbox_messages``.
          - The focus index is clamped: if it was the last row, focus
            moves up to the new last row (or stays at 0 if the list
            is now empty). This matches the operator's mental model
            "after I delete this, the next visible row is now where
            my cursor sits".
          - Marks dirty so the renderer repaints with the row gone.

        Note: this method ONLY mutates the in-memory cache. The
        caller is responsible for invoking the daemon's mailbox-
        delete callback to remove the row from disk. We do the
        in-memory drop here (rather than waiting for the next
        ``set_inbox_messages`` from the periodic refresh) so the UI
        feels instant — operator sees the row vanish on keypress.
        """
        if not self._inbox_messages:
            return None
        idx = self._inbox_focused_index
        if idx < 0 or idx >= len(self._inbox_messages):
            return None
        row = self._inbox_messages[idx]
        # Drop from the tuple by rebuilding without the focused index.
        # _inbox_messages is a tuple (frozen-ish for cheap-snapshot
        # semantics), so we rebuild rather than mutate.
        self._inbox_messages = tuple(
            r for i, r in enumerate(self._inbox_messages) if i != idx
        )
        # Clamp focus: if we deleted the last row, move up.
        # Empty list → focus stays at 0 (no-op next keypress).
        if self._inbox_focused_index >= len(self._inbox_messages):
            self._inbox_focused_index = max(0, len(self._inbox_messages) - 1)
        self._dirty.set()
        return row.id

    def mark_inbox_row_read_locally(self, row_id: int) -> None:
        """Update the in-memory cache to reflect READ state for a row.

        The persistent store update happens in app.py
        (MailboxStore.mark_read); this method updates the UI cache
        so the change appears immediately without waiting for the
        next set_inbox() refresh. Called from the input router after
        the operator opens detail-view on an UNREAD row.
        """
        new_messages = list(self._inbox_messages)
        changed = False
        for i, row in enumerate(new_messages):
            if row.id == row_id and not row.is_read:
                new_messages[i] = InboxRow(
                    id=row.id,
                    from_call=row.from_call,
                    body=row.body,
                    utc_iso=row.utc_iso,
                    snr_db=row.snr_db,
                    is_read=True,
                )
                changed = True
                break
        if not changed:
            return
        self._inbox_messages = tuple(new_messages)
        # Decrement local unread count if it was non-zero. The
        # canonical count comes from MailboxStore.count_unread() on
        # the next set_inbox() call; this is just to keep the UI
        # consistent in the meantime.
        if self._inbox_unread_count > 0:
            self._inbox_unread_count -= 1
        self._dirty.set()

    # ── Compose ────────────────────────────────────────────────────────

    def compose_set_to(self, value: str) -> None:
        """Set the COMPOSE TO field. Called from the router on type/edit.

        Empty string is a valid intermediate value — the operator may
        be deleting characters before typing a new callsign. The wire-
        format builder rejects an empty TO at send time, so transient
        empties don't matter here.
        """
        self._compose_to = value
        self._dirty.set()

    def compose_set_text(self, value: str) -> None:
        """Set the COMPOSE TEXT field. Called from the router on type/edit."""
        self._compose_text = value
        self._dirty.set()

    def compose_cycle_cmd(self, *, forward: bool) -> None:
        """Cycle the CMD dropdown one step.

        ``forward=True`` means ↓ (next in COMPOSE_CMD_ORDER, wraps to
        first). ``forward=False`` means ↑ (previous, wraps to last).
        Operators cycle this when CMD is the focused field; other
        fields don't consume ↑/↓.
        """
        try:
            idx = COMPOSE_CMD_ORDER.index(self._compose_cmd)
        except ValueError:
            idx = 0
        n = len(COMPOSE_CMD_ORDER)
        idx = (idx + 1) % n if forward else (idx - 1) % n
        self._compose_cmd = COMPOSE_CMD_ORDER[idx]
        self._dirty.set()

    # ── TO field ↑/↓ cycle (heard stations + groups) ──────────────────

    def _heard_for_compose_dropdown(self) -> tuple[HeardStation, ...]:
        """Return the heard list as the COMPOSE TO dropdown sees it.

        Filters out our own callsign (operators don't compose messages
        to themselves and gfsk8's AUTO_REMOVE_MYCALL would strip them
        on the wire anyway). Order is most-recent first, matching the
        HEARD screen.
        """
        our = self._callsign.upper() if self._callsign else ""
        return tuple(
            st for st in self._heard
            if (st.callsign or "").upper() != our
        )

    def _compose_to_picks(self) -> tuple[str, ...]:
        """Build the ordered list of TO-field picks for ↑/↓ cycling.

        The cycle covers both:
          1. Heard stations (most-recent first), minus our own callsign
          2. Configured JS8Call group memberships (alphabetical)

        Groups land at the END of the cycle. Operators are most likely
        to want a heard station (real reply target), so we lead with
        those; pressing ↓ enough times reaches the groups. Alphabetical
        order within groups gives predictable navigation regardless of
        which order they were typed into Setup.

        Both lists are de-duplicated against each other (a heard call
        that happens to start with '@' won't appear twice).
        """
        seen: set[str] = set()
        picks: list[str] = []
        for st in self._heard_for_compose_dropdown():
            cs = st.callsign
            if cs and cs.upper() not in seen:
                seen.add(cs.upper())
                picks.append(cs)
        for g in sorted(self._groups):
            if g and g.upper() not in seen:
                seen.add(g.upper())
                picks.append(g)
        return tuple(picks)

    def _compose_to_cycle(self, *, forward: bool) -> None:
        """Cycle the TO field through heard stations + configured groups.

        First call (when ``_compose_to_heard_index`` is None) lands on
        index 0 (most-recent heard, or the first group if there are no
        heard stations); subsequent calls advance / retreat with wrap.
        Empty pick list → no-op.
        """
        picks = self._compose_to_picks()
        if not picks:
            return
        n = len(picks)
        if self._compose_to_heard_index is None:
            idx = 0 if forward else (n - 1)
        else:
            i = self._compose_to_heard_index
            idx = (i + 1) % n if forward else (i - 1) % n
        self._compose_to_heard_index = idx
        self._compose_to = picks[idx]
        self._dirty.set()

    def compose_to_cycle_heard_next(self) -> None:
        """Operator pressed ↓ on focused TO field."""
        self._compose_to_cycle(forward=True)

    def compose_to_cycle_heard_prev(self) -> None:
        """Operator pressed ↑ on focused TO field."""
        self._compose_to_cycle(forward=False)


    def compose_clear(self) -> None:
        """Reset COMPOSE to its initial state. Called on Esc and after send.

        Returns focus to the TO field and the CMD dropdown to FREE.
        TO and TEXT are blanked. The ``compose_prepopulate_from_heard``
        method is called explicitly by the daemon when the operator
        navigates back into COMPOSE — we don't auto-prepopulate here
        because clear-after-send shouldn't yank the previous TO back.
        """
        self._compose_to = ""
        self._compose_cmd = ComposeCmd.FREE
        self._compose_text = ""
        # Reset focus to the TO field (index 0 in COMPOSE focusables).
        self._focus_index[Screen.COMPOSE] = 0
        self._editing_field = None
        self._edit_buffer = ""
        self._edit_invalid = False
        self._dirty.set()

    def compose_prepopulate_from_heard(self, callsign: Optional[str]) -> None:
        """Pre-fill the COMPOSE TO field with a Heard callsign.

        Called when the operator navigates INTO Compose. ``callsign``
        is typically the most-recently-focused row from the Heard
        screen, or ``None`` if Heard is empty / has no focus.

        Behavior:
          - If ``callsign`` is non-empty AND TO is currently empty,
            populate TO. This preserves any in-progress compose if
            the operator left and came back.
          - If ``callsign`` is None or empty, no-op.
          - We never overwrite a non-empty TO — operators frequently
            type a callsign manually and we don't want to clobber
            their work.
        """
        if not callsign:
            return
        if self._compose_to:
            return
        self._compose_to = callsign.upper()
        self._dirty.set()

    @property
    def compose_to(self) -> str:
        return self._compose_to

    @property
    def compose_cmd(self) -> ComposeCmd:
        return self._compose_cmd

    @property
    def compose_text(self) -> str:
        return self._compose_text

    def compose_focused_field(self) -> Optional[str]:
        """Return the currently-focused COMPOSE field name, or None.

        Returns one of ``"compose_to"``, ``"compose_cmd"``,
        ``"compose_text"``, ``"compose_send"`` when on the COMPOSE
        screen, else ``None``. The router uses this to dispatch
        keystrokes (type-to-edit on TO/TEXT, ↑/↓ on CMD, Enter on SEND).
        """
        if self._screen is not Screen.COMPOSE:
            return None
        fields = _FOCUSABLE_FIELDS.get(Screen.COMPOSE, ())
        if not fields:
            return None
        idx = self._focus_index.get(Screen.COMPOSE, 0)
        if idx < 0 or idx >= len(fields):
            return None
        return fields[idx]

    def set_battery(self, state: Optional[BatteryState]) -> None:
        """Update the battery snapshot.

        Marks dirty only when the displayed fields actually change.
        Polled at 1 Hz by ``BatteryReader``, but the BQ27220 typically
        only changes capacity once every 30-60 s under normal load —
        we'd otherwise wake the render thread for every poll with no
        on-screen change.

        ``state=None`` is the explicit "battery state unknown" signal
        (sustained read failures or no fuel gauge); the HOME row
        renders '--' in this case. Going from a known state back to
        None always marks dirty since the row will visibly change.
        """
        old = self._battery
        if old is None and state is None:
            return  # already unknown; no work
        if old is not None and state is not None:
            # Same-or-different known state. Re-render only on the
            # fields HOME actually displays: capacity and status.
            if (old.capacity, old.status) == (state.capacity, state.status):
                self._battery = state  # keep the fresh voltage/current
                return
        self._battery = state
        self._dirty.set()

    def set_gps(self, fix: GpsFix) -> None:
        """Update the current GPS fix.

        Recomputes ``gps_grid`` if the fix has a position. Marks dirty
        only when the displayed fields actually change — avoids
        re-rendering at NMEA's typical 1 Hz cadence when nothing
        meaningful has changed.
        """
        # Avoid the import cycle by resolving the converter at call time.
        from microjs8.gps.grid import latlon_to_grid

        new_grid: Optional[str] = None
        if fix.has_position and fix.lat is not None and fix.lon is not None:
            new_grid = latlon_to_grid(fix.lat, fix.lon, precision=6)

        # The fix.received_at field changes on every callback, so we
        # cannot do "is fix == self._gps". Detect meaningful changes:
        # fix kind, grid, satellites_used. Position changes *within
        # the same grid* do not redraw the home screen, which is
        # exactly what we want — 6-char grid resolution is plenty.
        meaningful_change = (
            self._gps.kind != fix.kind
            or self._gps_grid != new_grid
            or self._gps.satellites_used != fix.satellites_used
        )

        self._gps = fix
        self._gps_grid = new_grid
        if meaningful_change:
            self._dirty.set()

    def snapshot(self) -> UISnapshot:
        return UISnapshot(
            screen=self._screen,
            callsign=self._callsign,
            grid=self._grid,
            units=self._units,
            tx_allowed=self.tx_allowed,
            emergency_override=self._emergency_override,
            groups=self._groups,
            shutdown_remaining=self._shutdown_remaining,
            previous_screen=self._previous_screen,
            emergency_beacon_armed=self._emergency_beacon_armed,
            emergency_hold_progress=self._emergency_hold_progress,
            emergency_hold_direction=self._emergency_hold_direction,
            focused_field=self.focused_field_name(),
            editing_field=self._editing_field,
            edit_buffer=self._edit_buffer,
            edit_invalid=self._edit_invalid,
            freq_hz=self._freq_hz,
            mode=self._mode,
            gps=self._gps,
            gps_grid=self._gps_grid,
            cat_connected=self._cat_connected,
            time_source=self._time_source,
            heard=self._heard,
            directed=self._directed,
            radio_id=self._radio_id,
            inbox_messages=self._inbox_messages,
            inbox_unread_count=self._inbox_unread_count,
            inbox_held_count=self._inbox_held_count,
            inbox_focused_index=self._inbox_focused_index,
            inbox_detail_id=self._inbox_detail_id,
            directed_log_entries=self._directed_log_entries,
            compose_to=self._compose_to,
            compose_cmd=self._compose_cmd,
            compose_text=self._compose_text,
            compose_focused_field=self.compose_focused_field(),
            battery=self._battery,
        )

    # ── Render-side dirty-flag plumbing ──────────────────────────────

    @property
    def dirty(self) -> threading.Event:
        return self._dirty

    def consume_dirty(self) -> bool:
        with self._lock:
            if self._dirty.is_set():
                self._dirty.clear()
                return True
            return False
