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
    # Slot 6 was Screen.DIRECTED_MENU through v0.0.7. Phase 19 (v0.0.8)
    # removed it — Compose handles directed sends natively (to-call
    # field + verb dropdown). The numeric slot is left as a gap rather
    # than renumbered so any persisted screen-index value from a prior
    # version simply lands on EMERGENCY (a safe screen) instead of an
    # arbitrary other screen.
    EMERGENCY = 7
    SETUP = 8
    # The shutdown screen is NOT part of the ring — it's only entered
    # via the both-buttons gesture, never via ← / → navigation.
    SHUTTING_DOWN = 9
    # Inbox detail view — entered via Enter on a focused inbox row
    # in the INBOX screen, exited via Esc back to INBOX. Not part
    # of the main screen ring.
    INBOX_DETAIL = 10
    # Phase 13: Heartbeat-mode selector — entered via Enter on the
    # HEARTBEAT row of the ALLCALL screen, exited via Enter (commit)
    # or Esc (cancel) back to ALLCALL. Not part of the main screen ring.
    HB_MODE_SELECT = 11
    # Phase 19 v0.0.9: Exit-confirmation modal — entered via Enter on
    # the EXIT button on HOME, exited via:
    #   - Enter on YES → daemon exits (request_exit callback fires)
    #   - Enter on NO or Esc → back to HOME (no exit)
    # Default focus is on NO (safer; one stray Enter cancels the
    # confirmation rather than exits). Not part of the main screen
    # ring — operators reach it only via the HOME Exit button, never
    # via ←/→ cycle.
    EXIT_CONFIRM = 12


class HbMode(enum.Enum):
    """Heartbeat broadcast cadence. See spec §5.5 / §6.9.

    The mode determines whether the beacon runs, and at what
    interval. OFF is the default at boot; the operator opts in
    explicitly via the ALLCALL/HEARTBEAT sub-screen.

    SINGLE is a "fire one and revert" mode: the beacon emits one
    @HB in the next aligned TX window, then the app flips the mode
    back to OFF automatically. Useful for "I'm here, who's around?"
    without committing to a periodic schedule.

    20 MIN / 1 HR are repeating intervals. They cycle until the
    operator picks OFF or SINGLE on the sub-screen.

    The ``.value`` field is the operator-facing label shown on the
    HOME row, the ALLCALL row, and the HB_MODE_SELECT sub-screen.
    """

    OFF = "OFF"
    SINGLE = "SINGLE"
    TWENTY_MIN = "20 MIN"
    ONE_HR = "1 HR"


# Ordered tuple of HbMode values for index-based focus on the
# HB_MODE_SELECT sub-screen. Order matches the spec's display order.
HB_MODES_ORDERED: tuple[HbMode, ...] = (
    HbMode.OFF,
    HbMode.SINGLE,
    HbMode.TWENTY_MIN,
    HbMode.ONE_HR,
)


class ComposeCmd(enum.Enum):
    """Compose-screen CMD dropdown values.

    The enum value is the on-air verb token (or empty for FREE-form
    directed messages, which carry no verb). ``MYLOC`` and ``STORE``
    are special cases:
      - ``MYLOC`` is a UI sugar that pre-fills the TEXT field with
        the operator's current GPS position ("<lat>,<lon>") so they
        can broadcast their location without typing it. The wire
        envelope is built as a MSG ("<TO> MSG <lat>,<lon> [more]")
        because JS8Call has no MYLOC verb. v0.0.20 redesign per
        operator feedback; v0.0.19 used a different MYLOC format
        and v0.0.18 and earlier sent "GRID <my_grid>" instead.
      - ``STORE`` is a LOCAL action -- it writes a row to our mailbox
        for later delivery to the TO callsign, and nothing goes on
        the air at compose time. When the TO station later sends us
        ``QUERY MSGS`` we deliver the stored body. The verb is held
        here for UI consistency but never appears on the wire.

    ``MSG_TO`` is store-and-forward via a relay station: we ask the
    TO station to hold a message addressed to a third party (the FOR
    callsign). The wire form is ``<TO> MSG TO:<FOR> <TEXT>``. The
    FOR callsign is supplied via the extra COMPOSE field that only
    renders for this command.

    Operators cycle through these with up/down when CMD is focused,
    in the order declared here (most-common first). FREE is the
    default so that a fresh COMPOSE behaves like a chat-window:
    type a body and send.
    """

    FREE = ""               # no verb -- wire is "<TO> <TEXT>"
    MSG = "MSG"             # buffered, CRC-checksummed mail item
    MSG_TO = "MSG TO"       # relay via TO: hold for FOR. Wire: "<TO> MSG TO:<FOR> <TEXT>"
    STORE = "STORE"         # LOCAL action -- store for TO in our mailbox; no wire
    AGN_Q = "AGN?"          # "again?" -- ask peer to retransmit last
    SNR_Q = "SNR?"          # request signal report
    GRID_Q = "GRID?"        # ask "what's your grid?"  (JS8Call ignores GRID without the ?)
    QUERY_MSGS = "QUERY MSGS"   # ask if peer holds messages for us
    QUERY_MSG = "QUERY MSG"     # fetch a specific buffered message by id; TEXT is the integer id
    MYLOC = "MYLOC"         # UI sugar; pre-fills "<lat>,<lon>" in TEXT, wire is "<TO> MSG <body>"


# Display order for the CMD dropdown — controls cycle direction.
COMPOSE_CMD_ORDER: tuple[ComposeCmd, ...] = (
    ComposeCmd.FREE,
    ComposeCmd.MSG,
    ComposeCmd.MSG_TO,
    ComposeCmd.STORE,
    ComposeCmd.AGN_Q,
    ComposeCmd.SNR_Q,
    ComposeCmd.GRID_Q,
    ComposeCmd.QUERY_MSGS,
    ComposeCmd.QUERY_MSG,
    ComposeCmd.MYLOC,
)


def build_compose_wire(
    to: str,
    cmd: ComposeCmd,
    text: str,
    my_grid: str,
    my_call: str = "",
    for_call: str = "",
) -> Optional[str]:
    """Build the wire-format string for a compose action.

    Returns the string that would go on the air (without the
    auto-prefixed "<from>: " envelope — that's added by the
    encoder). Returns ``None`` if the compose is incomplete (no TO,
    or a CMD that requires TEXT but TEXT is empty, or MSG TO with
    no FOR), OR if the operator targeted their own callsign — see
    the SELF-CALL note below.

    STORE is a special case: it returns ``None`` because nothing
    goes on the air for STORE. The caller (app.py's
    ``_compose_store_sync``) reads (to, text) directly from the
    UIState and writes a mailbox row; no wire is involved. The
    ``None`` return from this function for STORE is "no wire built,
    don't enqueue", NOT "incomplete compose" — callers must
    distinguish.

    Wire forms by command:

      FREE        "<TO> <TEXT>"                      — directed free-form
      MSG         "<TO> MSG <TEXT>"                  — buffered mail
      MSG TO      "<TO> MSG TO:<FOR> <TEXT>"         — relay via TO, hold for FOR
      STORE       (no wire — local mailbox action)
      AGN?        "<TO> AGN?"                        — verb-only
      SNR?        "<TO> SNR?"                        — verb-only
      GRID?       "<TO> GRID?"                       — verb-only (JS8Call requires the ?)
      QUERY MSGS  "<TO> QUERY MSGS"                  — verb-only, ask peer for held msgs
      MYLOC       "<TO> MSG <TEXT>"                 -- UI sugar: TEXT pre-filled with "<lat>,<lon>"

    The TO field is uppercased on output (JS8Call protocol
    convention) and stripped of leading/trailing whitespace. TEXT is
    used as-typed (whitespace-trimmed at the boundary, internal
    spaces preserved — the protocol layer handles multi-frame
    whitespace correctly per the reassembly fix).

    SELF-CALL note: the gfsk8 library (Varicode.cpp::buildMessageFrames,
    AUTO_REMOVE_MYCALL block) silently strips the leading callsign
    when it equals our own — operators sometimes type their own
    callsign as a prefix, and the protocol auto-adds the from-envelope
    so the prefix is redundant. This means a wire like
    "W5DMH MSG hi" (where W5DMH is our own call) gets encoded as
    just "MSG hi" — stripping the to-callsign entirely, producing
    a malformed frame with no directed-message envelope. The receiver
    sees plain text, not a directed MSG. We reject TO == my_call
    here to prevent silently-malformed transmissions for ALL
    transmitting CMDs; STORE is allowed to have TO == self since it
    doesn't transmit.
    """
    to = (to or "").strip().upper()
    if not to:
        return None
    text = (text or "").strip()

    # STORE has no wire — caller must use the local-mailbox path.
    if cmd is ComposeCmd.STORE:
        return None

    # Reject TO == self for everything else (transmits would be
    # malformed by gfsk8's AUTO_REMOVE_MYCALL strip).
    if my_call and to == my_call.strip().upper():
        return None

    if cmd is ComposeCmd.MSG_TO:
        # Need FOR + TEXT. FOR cannot equal TO (relay holding for
        # itself isn't meaningful and is almost certainly an
        # operator typo).
        for_call_n = (for_call or "").strip().upper()
        if not for_call_n or not text:
            return None
        if for_call_n == to:
            return None
        return f"{to} MSG TO:{for_call_n} {text}"

    if cmd is ComposeCmd.QUERY_MSG:
        # QUERY MSG <id> — fetch a buffered message by its mailbox row
        # ID from the TO station. The body is the integer id only;
        # we accept the operator's text input but reject anything that
        # isn't a positive integer to keep malformed protocol off the
        # air. JS8Call's mailbox row IDs are 1-based and small (rarely
        # > 100 for a single station's queue), so 1..999999 is a more
        # than generous range. Leading/trailing whitespace in text is
        # already stripped above; we just need the remaining content
        # to be digits.
        if not text or not text.isdigit():
            return None
        try:
            msg_id = int(text)
        except ValueError:
            return None
        if msg_id < 1:
            return None
        return f"{to} QUERY MSG {msg_id}"

    body_required = cmd in (
        ComposeCmd.FREE,
        ComposeCmd.MSG,
        # v0.0.20: MYLOC now requires a body too. The compose-state
        # MYLOC handler pre-fills "<lat>,<lon>" when GPS has a fix,
        # so this is satisfied automatically; with no fix the
        # handler also blocks the operator with a warning so the
        # wire builder never runs. Defense in depth.
        ComposeCmd.MYLOC,
    )
    if body_required and not text:
        return None

    if cmd is ComposeCmd.FREE:
        return f"{to} {text}"
    if cmd is ComposeCmd.MYLOC:
        # v0.0.20: MYLOC is a UI sugar -- pre-fills the text field
        # with the operator's current GPS position. The over-air
        # verb is MSG; JS8Call has no MYLOC verb. ``my_grid`` is
        # no longer consulted (was used in v0.0.19 and earlier to
        # broadcast the SETUP grid; superseded by the GPS-driven
        # text pre-fill in the compose-state handler).
        return f"{to} MSG {text}"
    verb = cmd.value
    if body_required:
        return f"{to} {verb} {text}"
    return f"{to} {verb}"


# Screens reachable through the main ← / → ring (excludes the
# transient SHUTTING_DOWN screen and the modal INBOX_DETAIL).
# Phase 19 (v0.0.8): DIRECTED_MENU removed — Compose handles
# directed sends.
RING: tuple[Screen, ...] = (
    Screen.HOME,
    Screen.HEARD,
    Screen.DIRECTED,
    Screen.INBOX,
    Screen.COMPOSE,
    Screen.ALLCALL,
    Screen.EMERGENCY,
    Screen.SETUP,
)


# Per-screen list of focusable items. Step 3 only populates SETUP;
# other screens get focusable items as their interactivity lands in
# later steps.
_FOCUSABLE_FIELDS: dict[Screen, tuple[str, ...]] = {
    Screen.HOME: ("home_exit",),  # Phase 19 v0.0.8: Exit button (only focusable)
    Screen.HEARD: (),
    Screen.DIRECTED: (),    # activity log: scrollable but no per-row Enter action
    Screen.INBOX: (),       # focus is row-index-based (see _inbox_focused_index)
    Screen.COMPOSE: ("compose_to", "compose_cmd", "compose_text", "compose_send"),
    Screen.ALLCALL: (),  # populated in Step 6
    Screen.EMERGENCY: (),  # populated in Step 4/6
    Screen.SETUP: ("callsign", "grid", "groups", "units", "freq_hz", "radio", "emergency_bypass"),
    Screen.SHUTTING_DOWN: (),
    # INBOX_DETAIL has no named focusable fields — focus is the
    # implicit "the message being viewed". Up/Down scroll the body.
    Screen.INBOX_DETAIL: (),
    # Phase 19 v0.0.9: EXIT_CONFIRM modal. NO comes first so default
    # focus lands on the safer choice; YES requires the operator to
    # explicitly cycle to it before pressing Enter. This is the
    # intentional friction-against-accidental-exit per operator
    # request from the May 21, 2026 field test.
    Screen.EXIT_CONFIRM: ("exit_no", "exit_yes"),
}


def _compose_focus_cycle(cmd: ComposeCmd) -> tuple[str, ...]:
    """Return the COMPOSE field-cycle for the given CMD.

    Phase 14b: MSG_TO inserts ``compose_for`` between ``compose_cmd``
    and ``compose_text`` so the operator can fill in the final-
    recipient callsign. All other CMDs use the standard 4-field
    cycle. The router uses this for Tab/Shift-Tab navigation and
    the renderer uses it to decide whether to draw the FOR row.
    """
    if cmd is ComposeCmd.MSG_TO:
        return (
            "compose_to",
            "compose_cmd",
            "compose_for",
            "compose_text",
            "compose_send",
        )
    return (
        "compose_to",
        "compose_cmd",
        "compose_text",
        "compose_send",
    )


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

    Store-row support (Phase 14b)
    -----------------------------
    ``is_stored=True`` indicates this row is a held STORE — mail we
    hold to deliver to another station's QUERY MSGS, NOT inbound
    traffic for us. The renderer flips the row color to amber and
    swaps the FROM/TO so the operator sees ``→KD8PGB`` (= "for
    KD8PGB"). The ``recipient`` field holds the destination
    callsign in that case (= what ``→KD8PGB`` is showing).

    For UNREAD/READ rows (= our own inbox), ``is_stored=False``,
    ``recipient`` is None, and we display the ``from_call`` field
    as the sender — the existing convention.
    """

    id: int
    from_call: str
    body: str
    utc_iso: str
    snr_db: Optional[int]
    is_read: bool
    # STORE-row metadata (Phase 14b unified-mailbox UI). For non-STORE
    # rows these stay at their defaults and the renderer ignores them.
    is_stored: bool = False
    recipient: Optional[str] = None


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

    # Phase 13: heartbeat broadcast mode + ALLCALL screen state.
    # hb_mode is OFF until the operator picks a non-OFF value on the
    # HB_MODE_SELECT sub-screen. allcall_focus tracks the highlighted
    # row on the ALLCALL screen (0=HEARTBEAT, 1=QUERY MSGS, 2=CQ).
    # hb_select_focus is the highlighted row on the HB_MODE_SELECT
    # modal sub-screen (0..3 indexing HB_MODES_ORDERED).
    hb_mode: HbMode = HbMode.OFF
    allcall_focus: int = 0
    hb_select_focus: int = 0

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

    # v0.0.10: scroll offsets for the Directed and Heard screens.
    # 0 = show newest entries at the top of the visible window.
    # Operator ↓ advances into older entries (higher offset). The
    # renderer derives the footer "▲ N above / ▼ M below" hint from
    # these values + the visible-row count.
    directed_scroll_offset: int = 0
    heard_scroll_offset: int = 0

    # Compose screen state. The TO field is the recipient callsign
    # (may be empty until the operator types or pre-population fires).
    # CMD is one of the ComposeCmd enum values (defaulting to FREE).
    # TEXT is the operator-typed message body.
    # FOR is the final-recipient callsign used only when CMD is
    # MSG_TO; for all other CMDs the field is hidden and the value
    # is ignored.
    # ``compose_focused_field`` is the focusable-field name string
    # ("compose_to", "compose_cmd", "compose_for", "compose_text",
    # "compose_send") OR None when not on the COMPOSE screen.
    # ``compose_to_heard_index`` / ``compose_for_heard_index`` are
    # set when the field's value was picked from the heard-list
    # dropdown (so the renderer can colour the field by HEARD-age);
    # None when the operator typed free-form.
    compose_to: str = ""
    compose_cmd: ComposeCmd = ComposeCmd.FREE
    compose_text: str = ""
    compose_for: str = ""
    compose_focused_field: Optional[str] = None
    compose_to_heard_index: Optional[int] = None
    compose_for_heard_index: Optional[int] = None
    # v0.0.19: set when the operator cycled to MYLOC but no GPS fix
    # was available. Renderer surfaces "NO GPS FIX PLEASE WAIT" in
    # the TX warning band. Cleared when the operator cycles to any
    # other CMD value.
    compose_myloc_no_fix: bool = False

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
        # Phase 13: heartbeat mode + ALLCALL screen state.
        self._hb_mode: HbMode = HbMode.OFF
        self._allcall_focus: int = 0
        self._hb_select_focus: int = 0
        # App.py registers a callback here that fires whenever
        # set_hb_mode is invoked with a different mode. App reconciles
        # the HeartbeatBeacon thread to match the new mode.
        self._hb_mode_change_callback: Optional[
            Callable[["HbMode"], None]
        ] = None
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
        # v0.0.10: scroll offsets for the Directed and Heard screens.
        # 0 means "show newest at the top of the visible window"; the
        # operator presses ↓ to advance into older entries. The
        # offset's upper bound is enforced by the scroll methods so
        # the renderer never receives an out-of-range value. The
        # renderer derives a "more above / more below" hint from
        # these offsets for the footer.
        self._directed_scroll_offset: int = 0
        self._heard_scroll_offset: int = 0

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
        # FOR callsign -- used only when CMD is MSG_TO. The wire is
        # "<TO> MSG TO:<FOR> <TEXT>", asking TO to hold the body for
        # FOR. The field is rendered conditionally and the field
        # cycle (Tab order) skips it when CMD is anything else.
        self._compose_for: str = ""
        # v0.0.19: set when the operator cycled to MYLOC but no GPS
        # fix was available. Renderer surfaces "NO GPS FIX PLEASE
        # WAIT". Cleared when the operator cycles to another CMD or
        # when a fix arrives and they re-select MYLOC.
        self._compose_myloc_no_fix: bool = False
        # Pointer into the heard+groups cycle so successive ↑/↓ keypresses
        # walk the list in order rather than jumping back to position 0.
        # None means "no cycle position yet" — next press lands on index 0
        # (or n-1 for ↑). Reset to None whenever the operator hand-types
        # a new TO value via begin_edit/commit.
        self._compose_to_heard_index: Optional[int] = None
        # FOR-field heard-index — same semantics as TO's, but for the
        # MSG_TO field. Reset to None when the operator hand-types.
        self._compose_for_heard_index: Optional[int] = None

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

        # Phase 19 v0.0.9: when entering EXIT_CONFIRM, always reset
        # focus to NO (index 0). Without this, a previous visit that
        # ended with focus on YES (e.g. operator considered exiting,
        # cycled to YES, then changed mind and pressed Esc) would
        # leave the focus index at YES the NEXT time the modal opens
        # — defeating the "safer default" intent.
        if self._screen is Screen.EXIT_CONFIRM:
            self._focus_index[Screen.EXIT_CONFIRM] = 0

    # ── Focus cycling ────────────────────────────────────────────────

    def cycle_focus(self) -> None:
        # Phase 14b: COMPOSE has a dynamic field cycle (4 or 5
        # fields depending on whether CMD is MSG_TO). Use the
        # helper so Tab navigation respects the conditional FOR row.
        if self._screen is Screen.COMPOSE:
            fields = _compose_focus_cycle(self._compose_cmd)
        else:
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

    # ── Phase 13: Heartbeat mode + ALLCALL screen ────────────────────

    @property
    def hb_mode(self) -> "HbMode":
        return self._hb_mode

    @property
    def allcall_focus(self) -> int:
        return self._allcall_focus

    @property
    def hb_select_focus(self) -> int:
        return self._hb_select_focus

    def set_hb_mode_change_callback(
        self, callback: Optional[Callable[["HbMode"], None]],
    ) -> None:
        """Wire app.py's beacon-lifecycle handler. Called from
        ``set_hb_mode`` whenever the mode changes. Stored, not
        invoked here. Use ``None`` to detach."""
        self._hb_mode_change_callback = callback

    def set_hb_mode(self, mode: "HbMode") -> None:
        """Change the heartbeat broadcast mode.

        No-op if ``mode`` equals the current mode (avoids spurious
        beacon-thread churn on mode-select sub-screen Enter when the
        operator picked the already-selected mode). Otherwise fires
        the registered mode-change callback (synchronously, on the
        asyncio thread) and sets the dirty flag so the renderer
        repaints HOME + ALLCALL with the new value.
        """
        if self._hb_mode is mode:
            return
        self._hb_mode = mode
        if self._hb_mode_change_callback is not None:
            try:
                self._hb_mode_change_callback(mode)
            except Exception:
                # Same rationale as arm_emergency_beacon: the state
                # mutation has already happened; a raising callback
                # shouldn't roll us back. App.py logs via its own
                # exception handler.
                pass
        self._dirty.set()

    def allcall_focus_next(self) -> None:
        """Cycle the ALLCALL screen focus down (HEARTBEAT → QUERY MSGS → CQ → wrap)."""
        self._allcall_focus = (self._allcall_focus + 1) % 3
        self._dirty.set()

    def allcall_focus_prev(self) -> None:
        """Cycle the ALLCALL screen focus up."""
        self._allcall_focus = (self._allcall_focus - 1) % 3
        self._dirty.set()

    def open_hb_mode_select(self) -> None:
        """Enter the HB_MODE_SELECT modal sub-screen.

        Initializes the sub-screen focus to the currently-active
        mode, so the operator sees "this is what's running now"
        when the sub-screen opens.
        """
        try:
            self._hb_select_focus = HB_MODES_ORDERED.index(self._hb_mode)
        except ValueError:
            self._hb_select_focus = 0
        self._screen = Screen.HB_MODE_SELECT
        self._editing_field = None
        self._edit_buffer = ""
        self._dirty.set()

    def close_hb_mode_select(self, *, commit: bool) -> None:
        """Return from HB_MODE_SELECT to ALLCALL.

        If ``commit`` is True, apply whatever mode the operator has
        the focus parked on. Otherwise just navigate back without
        changing the mode.
        """
        if self._screen is not Screen.HB_MODE_SELECT:
            return
        if commit:
            self.set_hb_mode(HB_MODES_ORDERED[self._hb_select_focus])
        self._screen = Screen.ALLCALL
        self._editing_field = None
        self._edit_buffer = ""
        self._dirty.set()

    def hb_select_focus_next(self) -> None:
        """Cycle HB_MODE_SELECT focus down (OFF → SINGLE → 20 MIN → 1 HR → wrap)."""
        self._hb_select_focus = (
            self._hb_select_focus + 1
        ) % len(HB_MODES_ORDERED)
        self._dirty.set()

    def hb_select_focus_prev(self) -> None:
        """Cycle HB_MODE_SELECT focus up."""
        self._hb_select_focus = (
            self._hb_select_focus - 1
        ) % len(HB_MODES_ORDERED)
        self._dirty.set()

    # ── Heard list / Directed list (Step 5) ──────────────────────────

    def set_heard(self, heard: tuple[HeardStation, ...]) -> None:
        """Replace the heard-list snapshot.

        Caller passes the most-recent-first slice from the message
        store; we don't sort here. The render layer respects the
        order it's given.

        Marks dirty if the list actually changed (callsign membership
        OR most-recent timestamps), so high-frequency updates of the
        same N stations don't churn the screen.

        v0.0.10 scroll behavior: clamp ``_heard_scroll_offset`` to a
        valid range if entries were dropped (HEARD_CAP rollover) or
        the list cleared on a callsign change. We do NOT reset the
        offset to 0 on every new heard sighting — operators may be
        actively reviewing older entries when a new station decodes,
        and yanking them back to the top would be jarring. (Contrast
        with directed_log which DOES auto-reset; the activity-log is
        the operator's chat surface and they want to see new traffic
        immediately.)
        """
        if heard == self._heard:
            return
        self._heard = heard
        max_off = max(0, len(heard) - 1)
        if self._heard_scroll_offset > max_off:
            self._heard_scroll_offset = max_off
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
            #
            # Phase 14b: STORE rows (unified mailbox view) — inbox +
            # STORE share the same list. We carry through ``is_stored``
            # and the destination callsign so the renderer can
            # distinguish them visually (amber color, ``→TO`` label
            # instead of FROM).
            type_str = getattr(r, "type", "")
            is_stored = (type_str == "STORE")
            recipient = (
                str(getattr(r, "to_call", "") or "")
                if is_stored else None
            ) or None
            new_messages.append(
                InboxRow(
                    id=int(getattr(r, "id")),
                    from_call=str(getattr(r, "from_call", "") or ""),
                    body=str(getattr(r, "text", "") or ""),
                    utc_iso=str(getattr(r, "utc_iso", "") or ""),
                    snr_db=getattr(r, "snr_db", None),
                    # STORE rows never start as UNREAD-styled; the
                    # operator's never going to "open and read" their
                    # own held mail. Render at FG_DIM by default so
                    # the screen draws them as quiet/secondary content.
                    is_read=(type_str in ("READ", "STORE")),
                    is_stored=is_stored,
                    recipient=recipient,
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

        v0.0.10 scroll behavior: when the snapshot grows (new entry
        arrived), reset ``_directed_scroll_offset`` to 0 so the
        operator sees the newest entry immediately. If the snapshot
        shrunk (clear() or buffer rollover), clamp to a valid range.
        """
        if entries == self._directed_log_entries:
            return
        old_len = len(self._directed_log_entries)
        new_len = len(entries)
        self._directed_log_entries = entries
        if new_len > old_len:
            # New entry appended — jump to top (newest).
            self._directed_scroll_offset = 0
        else:
            # Clamp; the offset can't point past the last entry.
            max_off = max(0, new_len - 1)
            if self._directed_scroll_offset > max_off:
                self._directed_scroll_offset = max_off
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

    # ── v0.0.10: Directed / Heard scroll offsets ─────────────────────

    def directed_scroll_up(self) -> None:
        """Scroll Directed view one row toward newer entries (offset−).

        offset 0 = top of list (newest). No-op when already at 0.
        Marks dirty only on observable change.
        """
        if self._directed_scroll_offset <= 0:
            return
        self._directed_scroll_offset -= 1
        self._dirty.set()

    def directed_scroll_down(self) -> None:
        """Scroll Directed view one row toward older entries (offset+).

        We cap at ``len(entries) - 1`` so the operator can't scroll
        past the oldest visible row. Renderers cap further based on
        their own visible-row budget.
        """
        max_off = max(0, len(self._directed_log_entries) - 1)
        if self._directed_scroll_offset >= max_off:
            return
        self._directed_scroll_offset += 1
        self._dirty.set()

    def directed_scroll_reset(self) -> None:
        """Reset Directed scroll to top (newest).

        Called whenever a new entry is appended so the operator
        always sees fresh activity without scrolling.
        """
        if self._directed_scroll_offset == 0:
            return
        self._directed_scroll_offset = 0
        self._dirty.set()

    def heard_scroll_up(self) -> None:
        """Scroll Heard list one row toward newer (more recently heard)."""
        if self._heard_scroll_offset <= 0:
            return
        self._heard_scroll_offset -= 1
        self._dirty.set()

    def heard_scroll_down(self) -> None:
        """Scroll Heard list one row toward older entries."""
        max_off = max(0, len(self._heard) - 1)
        if self._heard_scroll_offset >= max_off:
            return
        self._heard_scroll_offset += 1
        self._dirty.set()

    def heard_scroll_reset(self) -> None:
        """Reset Heard scroll to top (most recently heard)."""
        if self._heard_scroll_offset == 0:
            return
        self._heard_scroll_offset = 0
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
        empties don't matter here. Typing clears the heard-index
        marker (the operator is hand-typing now, not cycling).
        """
        self._compose_to = value
        self._compose_to_heard_index = None
        self._dirty.set()

    def compose_set_for(self, value: str) -> None:
        """Set the COMPOSE FOR field (used by MSG TO). Same semantics
        as ``compose_set_to`` — typing clears the heard-index marker."""
        self._compose_for = value
        self._compose_for_heard_index = None
        self._dirty.set()

    def compose_set_text(self, value: str) -> None:
        """Set the COMPOSE TEXT field. Called from the router on type/edit."""
        self._compose_text = value
        self._dirty.set()

    def compose_cycle_cmd(self, *, forward: bool) -> None:
        """Cycle the CMD dropdown one step.

        ``forward=True`` means down (next in COMPOSE_CMD_ORDER, wraps to
        first). ``forward=False`` means up (previous, wraps to last).
        Operators cycle this when CMD is the focused field; other
        fields don't consume up/down.

        Phase 14b: if the new CMD does not use the FOR field (i.e.
        is anything other than MSG_TO), and the current focus index
        is out of range for the smaller cycle, we clamp it back to
        CMD (index 1). This keeps the focus index sensible across
        CMD changes that remove the FOR field from the cycle.

        v0.0.20: MYLOC is a UI sugar for "compose a position
        message". When the cycle lands on MYLOC:
          - If we have a current GPS fix with position: pre-fill
            TEXT with "<lat>,<lon>" (4 decimal places, no MYLOC
            prefix) and leave CMD on MYLOC so the operator sees
            their selection. The wire builder substitutes MSG for
            MYLOC at transmit time since JS8Call has no MYLOC verb;
            the over-air result is "<TO> MSG <lat>,<lon> [more]".
          - If we have no current GPS fix: stay on MYLOC and set
            ``_compose_myloc_no_fix`` so the renderer can surface
            "NO GPS FIX PLEASE WAIT" in the TX warning area. The
            operator can cycle to another verb to dismiss.
        """
        try:
            idx = COMPOSE_CMD_ORDER.index(self._compose_cmd)
        except ValueError:
            idx = 0
        n = len(COMPOSE_CMD_ORDER)
        idx = (idx + 1) % n if forward else (idx - 1) % n
        candidate = COMPOSE_CMD_ORDER[idx]
        # v0.0.20: intercept MYLOC selection so we can pre-fill the
        # text field with the current GPS position (or show the
        # no-fix warning). MYLOC remains the visible CMD; the wire
        # builder handles the MSG substitution at transmit time.
        if candidate is ComposeCmd.MYLOC:
            self._apply_myloc_action()
            # _apply_myloc_action already set _compose_cmd to MYLOC
            # and called self._dirty.set(); return without falling
            # through to the default cycle-advance logic below.
            return
        # Default path: settle on candidate.
        self._compose_cmd = candidate
        # Clear any stale MYLOC warning when leaving MYLOC.
        if self._compose_myloc_no_fix:
            self._compose_myloc_no_fix = False
        # If we just stepped away from MSG TO, the FOR field is no
        # longer in the focus cycle -- clamp the focus index to CMD
        # (index 1) so the next Tab lands on TEXT, not in undefined
        # territory.
        if self._compose_cmd is not ComposeCmd.MSG_TO:
            cur_focus = self._focus_index.get(Screen.COMPOSE, 0)
            cycle = _compose_focus_cycle(self._compose_cmd)
            if cur_focus >= len(cycle):
                self._focus_index[Screen.COMPOSE] = 1  # park on CMD
        self._dirty.set()

    def _apply_myloc_action(self) -> None:
        """v0.0.20: MYLOC pre-fills lat/lon into the TEXT field.

        Called from ``compose_cycle_cmd`` when the cycle lands on
        ``ComposeCmd.MYLOC``. v0.0.20 redesign per operator feedback:
        MYLOC is purely a UI sugar that pre-fills the body with our
        current position so the operator doesn't have to type it.
        The over-air verb is MSG -- JS8Call has no MYLOC verb, so the
        wire builder substitutes MSG when CMD is MYLOC (see
        ``build_compose_wire``).

        Behavior:

          - If we have a current GPS fix with position (lat/lon
            present and ``has_position`` is True): pre-fill
            ``_compose_text`` with "<lat>,<lon>" formatted to 4
            decimal places (~10 m precision -- finer than the JS8
            community-default 3 decimals, per operator preference).
            No "MYLOC" prefix in the body itself; the operator can
            edit or append to the text before sending.
            ``_compose_cmd`` stays at ``MYLOC`` so the operator sees
            their selection reflected in the dropdown; only the wire
            envelope says MSG. Clear the no-fix warning.

          - If we have no current GPS fix: leave ``_compose_text``
            untouched, leave ``_compose_cmd`` at MYLOC (the cycle
            landed there), and set ``_compose_myloc_no_fix`` so the
            renderer can show "NO GPS FIX PLEASE WAIT" in the warning
            band. Operator must cycle away (up/down) to escape.

        We deliberately do NOT use the SETUP-configured grid as a
        fallback: the operator chose GPS-only explicitly.
        """
        fix = self._gps
        if fix is not None and fix.has_position:
            self._compose_text = f"{fix.lat:.4f},{fix.lon:.4f}"
            self._compose_cmd = ComposeCmd.MYLOC
            self._compose_myloc_no_fix = False
        else:
            self._compose_cmd = ComposeCmd.MYLOC
            self._compose_myloc_no_fix = True
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

    def _compose_for_cycle(self, *, forward: bool) -> None:
        """Same as _compose_to_cycle but for the FOR field.

        Used only when CMD is MSG_TO. The dropdown is the heard-list
        (no groups — the FOR field is the FINAL recipient, which is
        always a real station, not a group). Empty heard list → no-op.
        """
        dropdown = self._heard_for_compose_dropdown()
        if not dropdown:
            return
        n = len(dropdown)
        if self._compose_for_heard_index is None:
            idx = 0 if forward else (n - 1)
        else:
            i = self._compose_for_heard_index
            idx = (i + 1) % n if forward else (i - 1) % n
        self._compose_for_heard_index = idx
        self._compose_for = dropdown[idx].callsign
        self._dirty.set()

    def compose_for_cycle_heard_next(self) -> None:
        """Operator pressed ↓ on focused FOR field (MSG_TO only)."""
        self._compose_for_cycle(forward=True)

    def compose_for_cycle_heard_prev(self) -> None:
        """Operator pressed ↑ on focused FOR field (MSG_TO only)."""
        self._compose_for_cycle(forward=False)


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
        # Phase 14b: also reset FOR field + heard indices.
        self._compose_for = ""
        self._compose_to_heard_index = None
        self._compose_for_heard_index = None
        # v0.0.19: clear the MYLOC no-fix warning on reset.
        self._compose_myloc_no_fix = False
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
            populate TO. Set ``_compose_to_heard_index`` to 0 (most-
            recent slot in the filtered dropdown) when the picked
            callsign matches that slot, so the renderer can use the
            HEARD-age color.
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
        # Pre-population always corresponds to the most-recent heard
        # slot (index 0). Set the marker so the renderer can use the
        # HEARD-age color.
        dropdown = self._heard_for_compose_dropdown()
        if dropdown and dropdown[0].callsign.upper() == self._compose_to:
            self._compose_to_heard_index = 0
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

    @property
    def compose_for(self) -> str:
        return self._compose_for

    @property
    def compose_to_heard_index(self) -> Optional[int]:
        return self._compose_to_heard_index

    @property
    def compose_for_heard_index(self) -> Optional[int]:
        return self._compose_for_heard_index

    def compose_focused_field(self) -> Optional[str]:
        """Return the currently-focused COMPOSE field name, or None.

        Returns one of ``"compose_to"``, ``"compose_cmd"``,
        ``"compose_for"`` (MSG_TO only), ``"compose_text"``,
        ``"compose_send"`` when on the COMPOSE screen, else ``None``.

        Phase 14b: when CMD is MSG_TO the field cycle inserts
        ``compose_for`` between ``compose_cmd`` and ``compose_text``.
        For all other CMDs, the FOR field is hidden and skipped in
        the Tab order. The router uses this to dispatch keystrokes
        (type-to-edit on TO/FOR/TEXT, ↑/↓ on CMD, Enter on SEND).
        """
        if self._screen is not Screen.COMPOSE:
            return None
        fields = _compose_focus_cycle(self._compose_cmd)
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
            directed_scroll_offset=self._directed_scroll_offset,
            heard_scroll_offset=self._heard_scroll_offset,
            compose_to=self._compose_to,
            compose_cmd=self._compose_cmd,
            compose_text=self._compose_text,
            compose_for=self._compose_for,
            compose_focused_field=self.compose_focused_field(),
            compose_to_heard_index=self._compose_to_heard_index,
            compose_for_heard_index=self._compose_for_heard_index,
            compose_myloc_no_fix=self._compose_myloc_no_fix,
            hb_mode=self._hb_mode,
            allcall_focus=self._allcall_focus,
            hb_select_focus=self._hb_select_focus,
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
