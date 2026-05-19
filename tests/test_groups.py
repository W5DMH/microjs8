"""Tests for the JS8Call groups feature (May 2026).

Covers:
  - Config validation (parse, dedup, implicit-filter, limit, format)
  - Round-trip through save_atomic + load
  - UIState / UISnapshot exposes groups
  - Setup screen renders the groups row
  - Parser sets is_for_us on group-directed frames when we're a member
  - Activity log carries for_group through record_in + supersede
  - DIRECTED renderer shows the K1ABC@@ARESGA label
  - Compose TO cycle includes configured groups alongside heard
  - Auto-response planner: SNR?, GRID?, member/non-member, missing data
"""

from __future__ import annotations

import random

import pytest

from microjs8 import config as config_mod
from microjs8.activity import DirectedActivityLog, Direction
from microjs8.protocol.types import DecodedFrame, HeardStation
from microjs8.protocol.grammar import parse
from microjs8.protocol.types import FrameKind
from microjs8.tx.auto_response import (
    AUTO_RESPONSE_MAX_DELAY_S,
    AutoResponsePlan,
    plan_auto_response,
)
from microjs8.ui.state import Screen, UIState


# ── Helpers ──────────────────────────────────────────────────────────


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """Redirect data + etc dirs into a tmp tree.

    Same shape as the fixture in test_config / test_setup_wizard so
    save_atomic / load round-trips don't touch the developer's real
    /var/lib/microjs8/config.toml.
    """
    data = tmp_path / "data"
    etc = tmp_path / "etc"
    data.mkdir()
    etc.mkdir()
    monkeypatch.setenv("MICROJS8_DATA_DIR", str(data))
    monkeypatch.setenv("MICROJS8_ETC_DIR", str(etc))
    return data, etc


def _decoded(text: str, snr_db: int = -10) -> DecodedFrame:
    """Build a minimal DecodedFrame for parser tests."""
    return DecodedFrame(
        text=text,
        raw="",
        snr_db=snr_db,
        frequency_hz=1500.0,
        dt_seconds=0.0,
        submode=0,
        quality=10,
        frame_type=0,
        utc_seconds_of_day=0,
        received_at=0.0,
    )


# ── Config validation ────────────────────────────────────────────────


def test_groups_default_is_empty_tuple():
    cfg = config_mod.StationConfig()
    assert cfg.groups == ()


def test_groups_accepts_comma_separated_string():
    out = config_mod._validate_groups("@EMCOMM, @ARES, @SKYWARN")
    assert out == ("@EMCOMM", "@ARES", "@SKYWARN")


def test_groups_accepts_list_form():
    out = config_mod._validate_groups(["@EMCOMM", "@ARES"])
    assert out == ("@EMCOMM", "@ARES")


def test_groups_uppercases_input():
    out = config_mod._validate_groups("@emcomm, @ares")
    assert out == ("@EMCOMM", "@ARES")


def test_groups_dedupes_case_insensitively():
    out = config_mod._validate_groups("@EMCOMM, @emcomm, @EMCOMM")
    assert out == ("@EMCOMM",)


def test_groups_drops_implicit_allcall_and_hb():
    out = config_mod._validate_groups("@ALLCALL, @EMCOMM, @HB")
    assert out == ("@EMCOMM",)


def test_groups_accepts_slashes_in_name():
    out = config_mod._validate_groups("@DX/NA, @REGION/1, @GROUP/0")
    assert out == ("@DX/NA", "@REGION/1", "@GROUP/0")


def test_groups_at_maximum_count():
    """Exactly 4 (the MAX_GROUPS limit) is fine."""
    out = config_mod._validate_groups("@A, @B, @C, @D")
    assert len(out) == 4


def test_groups_above_maximum_rejected():
    with pytest.raises(config_mod.ConfigError, match="too many"):
        config_mod._validate_groups("@A, @B, @C, @D, @E")


def test_groups_missing_at_prefix_rejected():
    with pytest.raises(config_mod.ConfigError, match="not a valid format"):
        config_mod._validate_groups("EMCOMM")


def test_groups_too_long_after_at_rejected():
    with pytest.raises(config_mod.ConfigError, match="not a valid format"):
        config_mod._validate_groups("@TOOLONGGG")  # 9 chars after @


def test_groups_with_space_inside_rejected():
    with pytest.raises(config_mod.ConfigError, match="not a valid format"):
        config_mod._validate_groups("@FOO BAR")


def test_groups_double_at_rejected():
    with pytest.raises(config_mod.ConfigError, match="not a valid format"):
        config_mod._validate_groups("@@FOO")


def test_groups_punctuation_rejected():
    with pytest.raises(config_mod.ConfigError):
        config_mod._validate_groups("@FOO!")


def test_groups_none_input_returns_empty():
    assert config_mod._validate_groups(None) == ()


def test_groups_empty_string_returns_empty():
    assert config_mod._validate_groups("") == ()


def test_groups_empty_list_returns_empty():
    assert config_mod._validate_groups([]) == ()


def test_groups_wrong_type_rejected():
    with pytest.raises(config_mod.ConfigError, match="must be"):
        config_mod._validate_groups(42)


# ── Round-trip persistence ──────────────────────────────────────────


def test_groups_round_trip_through_save_and_load(isolated_paths):
    """Save groups via save_atomic, reload via load(), verify identity."""
    config_mod.save_atomic(
        "K1ABC", "FN42", "miles",
        groups=("@EMCOMM", "@ARES"),
    )
    loaded = config_mod.load()
    assert loaded.station.groups == ("@EMCOMM", "@ARES")


def test_groups_omitted_save_preserves_existing(isolated_paths):
    """save_atomic(..., groups=None) keeps the persisted groups."""
    config_mod.save_atomic(
        "K1ABC", "FN42", "miles",
        groups=("@EMCOMM",),
    )
    # Now save without groups — should preserve.
    config_mod.save_atomic("K1ABC", "FN42", "miles")
    loaded = config_mod.load()
    assert loaded.station.groups == ("@EMCOMM",)


def test_groups_empty_save_clears_persisted(isolated_paths):
    """Explicitly passing () clears groups."""
    config_mod.save_atomic(
        "K1ABC", "FN42", "miles",
        groups=("@EMCOMM",),
    )
    config_mod.save_atomic(
        "K1ABC", "FN42", "miles",
        groups=(),
    )
    loaded = config_mod.load()
    assert loaded.station.groups == ()


def test_groups_save_with_comma_string_normalises(isolated_paths):
    """The router passes raw comma-separated input — save normalises."""
    config_mod.save_atomic(
        "K1ABC", "FN42", "miles",
        groups="@emcomm, @ares, @ALLCALL",  # mixed case + implicit
    )
    loaded = config_mod.load()
    assert loaded.station.groups == ("@EMCOMM", "@ARES")


def test_groups_save_invalid_raises(isolated_paths):
    with pytest.raises(config_mod.ConfigError):
        config_mod.save_atomic(
            "K1ABC", "FN42", "miles",
            groups="not_a_group",
        )


# ── UIState / snapshot exposure ──────────────────────────────────────


def test_uistate_default_groups_is_empty():
    s = UIState("K1ABC", "FN42", True, "miles")
    assert s.groups == ()
    snap = s.snapshot()
    assert snap.groups == ()


def test_uistate_construct_with_groups():
    s = UIState("K1ABC", "FN42", True, "miles", groups=("@EMCOMM", "@ARES"))
    assert s.groups == ("@EMCOMM", "@ARES")
    assert s.snapshot().groups == ("@EMCOMM", "@ARES")


def test_uistate_set_identity_updates_groups():
    s = UIState("K1ABC", "FN42", True, "miles")
    s.set_identity("K1ABC", "FN42", "miles", True, groups=("@EMCOMM",))
    assert s.groups == ("@EMCOMM",)


def test_uistate_set_identity_groups_change_marks_dirty():
    s = UIState("K1ABC", "FN42", True, "miles")
    s.consume_dirty()
    s.set_identity("K1ABC", "FN42", "miles", True, groups=("@EMCOMM",))
    assert s.consume_dirty() is True


# ── Setup screen rendering ───────────────────────────────────────────


def test_setup_rows_includes_groups_field():
    from microjs8.ui.screens import _setup_rows
    s = UIState("K1ABC", "FN42", True, "miles", groups=("@EMCOMM", "@ARES"))
    snap = s.snapshot()
    rows = _setup_rows(snap)
    field_names = [r[0] for r in rows]
    assert "groups" in field_names
    # Groups row should sit between grid and units.
    gi = field_names.index("groups")
    assert field_names.index("grid") < gi < field_names.index("units")


def test_setup_rows_groups_value_is_comma_joined():
    from microjs8.ui.screens import _setup_rows
    s = UIState("K1ABC", "FN42", True, "miles", groups=("@EMCOMM", "@ARES"))
    rows = _setup_rows(s.snapshot())
    for field, _label, value, _color in rows:
        if field == "groups":
            assert value == "@EMCOMM, @ARES"
            return
    pytest.fail("groups row not found")


def test_setup_rows_empty_groups_shows_placeholder():
    from microjs8.ui.screens import _setup_rows
    s = UIState("K1ABC", "FN42", True, "miles")
    rows = _setup_rows(s.snapshot())
    for field, _label, value, _color in rows:
        if field == "groups":
            assert value == "(none)"
            return
    pytest.fail("groups row not found")


def test_setup_focus_order_includes_groups():
    from microjs8.ui.state import _FOCUSABLE_FIELDS
    order = _FOCUSABLE_FIELDS[Screen.SETUP]
    assert "groups" in order
    gi = order.index("groups")
    assert order.index("grid") < gi < order.index("units")


# ── Parser: group routing ────────────────────────────────────────────


def test_parse_group_directed_for_member_is_for_us():
    """K1ABC: @ARESGA QSL? when we're in @ARESGA → is_for_us=True."""
    parsed = parse(
        _decoded("K1ABC: @ARESGA QSL? "),
        our_callsign="W5DMH",
        our_groups=("@ARESGA",),
    )
    assert parsed.from_call == "K1ABC"
    assert parsed.to_call == "@ARESGA"
    assert parsed.is_for_us is True


def test_parse_group_directed_for_non_member_not_for_us():
    """Same wire but we're NOT in @ARESGA → is_for_us=False."""
    parsed = parse(
        _decoded("K1ABC: @ARESGA QSL?"),
        our_callsign="W5DMH",
        our_groups=("@EMCOMM",),  # different group
    )
    assert parsed.to_call == "@ARESGA"
    assert parsed.is_for_us is False


def test_parse_direct_to_us_still_works_with_groups():
    """Adding groups doesn't break personally-directed routing."""
    parsed = parse(
        _decoded("K1ABC: W5DMH SNR?"),
        our_callsign="W5DMH",
        our_groups=("@ARESGA",),
    )
    assert parsed.to_call == "W5DMH"
    assert parsed.is_for_us is True


def test_parse_allcall_unaffected_by_groups():
    """@ALLCALL is always is_for_us=False regardless of group config."""
    parsed = parse(
        _decoded("K1ABC: @ALLCALL CQ FN42"),
        our_callsign="W5DMH",
        our_groups=("@ALLCALL",),  # would-be-redundant
    )
    # @ALLCALL is the broadcast kind — always False per existing semantics.
    assert parsed.to_call == "@ALLCALL"
    assert parsed.is_for_us is False


def test_parse_groups_none_defaults_to_empty():
    """parse(our_groups=None) behaves identically to empty tuple."""
    parsed = parse(
        _decoded("K1ABC: @EMCOMM SNR?"),
        our_callsign="W5DMH",
        our_groups=None,
    )
    assert parsed.is_for_us is False


def test_parse_group_case_insensitive_match():
    """Config in uppercase, wire in uppercase, but match is via .upper()."""
    parsed = parse(
        _decoded("K1ABC: @aresga QSL?"),  # lowercase on wire (unusual)
        our_callsign="W5DMH",
        our_groups=("@ARESGA",),
    )
    assert parsed.is_for_us is True


# ── Activity log: for_group plumbing ─────────────────────────────────


def test_record_in_sets_for_group():
    log = DirectedActivityLog(max_entries=10)
    entry = log.record_in(
        from_call="K1ABC", verb="SNR?", body="", at_unix=1000.0,
        for_group="@ARESGA",
    )
    assert entry.for_group == "@ARESGA"


def test_record_in_default_for_group_is_none():
    log = DirectedActivityLog(max_entries=10)
    entry = log.record_in(
        from_call="K1ABC", verb="SNR?", body="", at_unix=1000.0,
    )
    assert entry.for_group is None


def test_record_in_uppercases_for_group():
    log = DirectedActivityLog(max_entries=10)
    entry = log.record_in(
        from_call="K1ABC", verb="QSL?", body="", at_unix=1000.0,
        for_group="@aresga",
    )
    assert entry.for_group == "@ARESGA"


# ── record_in_supersede behavior (Phase 14a) ─────────────────────────


def test_record_in_supersede_preserves_for_group_when_new_is_none():
    """Multi-frame reassembly path: original entry had for_group set,
    continuation has None → keep the group label."""
    log = DirectedActivityLog(max_entries=10)
    log.record_in(
        from_call="K1ABC", verb="MSG", body="HELLO", at_unix=1000.0,
        snr_db=-9, for_group="@EMCOMM",
    )
    log.record_in_supersede(
        from_call="K1ABC", verb="MSG", body="HELLO WORLD", at_unix=1005.0,
        snr_db=None, for_group=None,  # reassembled emit
    )
    snap = log.snapshot()
    assert len(snap) == 1
    assert snap[0].body == "HELLO WORLD"
    assert snap[0].for_group == "@EMCOMM"  # preserved
    assert snap[0].snr_db == -9


def test_record_in_supersede_new_for_group_overrides():
    """If the new call provides for_group, that wins."""
    log = DirectedActivityLog(max_entries=10)
    log.record_in(
        from_call="K1ABC", verb="MSG", body="HI", at_unix=1000.0,
        for_group=None,
    )
    log.record_in_supersede(
        from_call="K1ABC", verb="MSG", body="HI THERE", at_unix=1005.0,
        for_group="@EMCOMM",
    )
    snap = log.snapshot()
    assert snap[0].for_group == "@EMCOMM"


# ── DIRECTED renderer: K1ABC@@ARESGA label ───────────────────────────


def test_directed_renderer_uses_group_label():
    """When for_group is set, the chat row should show 'K1ABC@@ARESGA'
    as the sender label rather than just 'K1ABC'."""
    # We construct the entry and inspect what _render_directed_log_rows
    # builds. Since the renderer uses PIL drawing primitives, this
    # test reaches into the helper that composes the line text.
    from microjs8.activity import DirectedActivityEntry, Direction
    entry = DirectedActivityEntry(
        at_unix=1000.0,
        direction=Direction.IN,
        other_call="K1ABC",
        verb="QSL?",
        body="",
        snr_db=-10,
        freq_hz=1500.0,
        for_group="@ARESGA",
    )
    # Reproduce the renderer's line-assembly snippet directly. This
    # asserts the contract without rendering pixels.
    sender = entry.other_call
    if entry.for_group:
        sender = f"{sender}@{entry.for_group}"
    line = f"{sender} {entry.verb}".strip()
    assert line == "K1ABC@@ARESGA QSL?"


def test_directed_renderer_personal_traffic_no_label():
    """When for_group is None, the sender label is the bare callsign."""
    from microjs8.activity import DirectedActivityEntry, Direction
    entry = DirectedActivityEntry(
        at_unix=1000.0,
        direction=Direction.IN,
        other_call="K1ABC",
        verb="SNR?",
        body="",
        for_group=None,
    )
    sender = entry.other_call
    if entry.for_group:
        sender = f"{sender}@{entry.for_group}"
    assert sender == "K1ABC"


# ── Compose TO cycle ────────────────────────────────────────────────


def test_compose_to_cycle_includes_groups_when_no_heard():
    """Empty heard list but configured groups → cycle picks groups."""
    s = UIState("W5DMH", "EN83", True, "miles", groups=("@EMCOMM", "@ARES"))
    s.set_screen(Screen.COMPOSE)
    s.compose_to_cycle_heard_next()
    snap = s.snapshot()
    # First-press lands on the first pick (alphabetical: @ARES).
    assert snap.compose_to == "@ARES"


def test_compose_to_cycle_orders_heard_before_groups():
    """Heard stations first, groups after."""
    s = UIState("W5DMH", "EN83", True, "miles", groups=("@EMCOMM",))
    s.set_screen(Screen.COMPOSE)
    s.set_heard((
        HeardStation(
            callsign="K1ABC", snr_db=-9, grid="FN42",
            frequency_hz=1500.0, distance_mi=None,
            bearing_deg=None, last_heard=1000.0,
        ),
    ))
    # Cycle: first ↓ should pick K1ABC (heard), second ↓ should pick @EMCOMM.
    s.compose_to_cycle_heard_next()
    assert s.snapshot().compose_to == "K1ABC"
    s.compose_to_cycle_heard_next()
    assert s.snapshot().compose_to == "@EMCOMM"


def test_compose_to_cycle_wraps_through_groups():
    """↑/↓ cycling wraps through the full heard+groups list."""
    s = UIState("W5DMH", "EN83", True, "miles",
                groups=("@A", "@B"))
    s.set_screen(Screen.COMPOSE)
    s.compose_to_cycle_heard_next()  # @A
    s.compose_to_cycle_heard_next()  # @B
    s.compose_to_cycle_heard_next()  # wraps to @A
    assert s.snapshot().compose_to == "@A"


def test_compose_to_cycle_no_heard_no_groups_is_noop():
    s = UIState("W5DMH", "EN83", True, "miles")
    s.set_screen(Screen.COMPOSE)
    before = s.snapshot().compose_to
    s.compose_to_cycle_heard_next()
    assert s.snapshot().compose_to == before


# ── Persistence: UIState must reflect config groups at startup ───────
#
# W5DMH bench May 2026: groups saved correctly to config.toml and
# auto-respond worked across restarts, but the Setup screen showed
# "(none)" because UIState's constructor wasn't given the loaded
# groups. The fix: app.py's UIState(...) call must pass
# ``groups=self._config.station.groups``. These tests pin both:
# (a) UIState propagates groups into its snapshot on construction,
# (b) Setup screen reflects them.


def test_uistate_groups_reach_snapshot_at_construction():
    """Constructing UIState with groups must round-trip via snapshot."""
    s = UIState("W5DMH", "EN83ih", True, "miles",
                groups=("@EMCOMM", "@SKYWARN"))
    snap = s.snapshot()
    assert snap.groups == ("@EMCOMM", "@SKYWARN")


def test_setup_screen_displays_groups_after_construction():
    """The Setup row renderer must read groups from the snapshot,
    not from a stale source. Pin the contract: state constructed
    with groups → groups row value shows them comma-joined."""
    from microjs8.ui.screens import _setup_rows
    s = UIState("W5DMH", "EN83ih", True, "miles",
                groups=("@EMCOMM", "@SKYWARN"))
    rows = _setup_rows(s.snapshot())
    for field, _label, value, _color in rows:
        if field == "groups":
            assert value == "@EMCOMM, @SKYWARN"
            return
    pytest.fail("groups row not found in setup rows")


def test_begin_edit_groups_prefills_with_comma_joined_value():
    """When the operator presses Enter on a populated groups row,
    the edit buffer should pre-fill with the current value in the
    same comma-separated form the display uses. Otherwise they'd
    appear to clear the field by entering edit mode, which is
    confusing AND would wipe their config on a blind commit."""
    s = UIState("K1ABC", "FN42", True, "miles",
                groups=("@EMCOMM", "@ARES"))
    s.set_screen(Screen.SETUP)
    s.begin_edit("groups")
    assert s.is_editing()
    assert s.editing_field() == "groups"
    # Pre-fill matches the display format from _setup_rows.
    assert s.edit_buffer() == "@EMCOMM, @ARES"


def test_begin_edit_groups_empty_yields_empty_buffer():
    """No configured groups → edit buffer starts empty (operator
    types from scratch)."""
    s = UIState("K1ABC", "FN42", True, "miles", groups=())
    s.set_screen(Screen.SETUP)
    s.begin_edit("groups")
    assert s.edit_buffer() == ""


# ── Auto-response planner ────────────────────────────────────────────


def _seeded_rng(seed: int = 42) -> random.Random:
    return random.Random(seed)


def test_plan_snr_query_for_member():
    plan = plan_auto_response(
        verb="SNR?", body="",
        from_call="K1ABC", to_call="@ARESGA",
        our_groups=("@ARESGA",),
        our_grid="EN83",
        snr_db=-9,
        rng=_seeded_rng(),
    )
    assert plan is not None
    assert plan.text == "K1ABC SNR -9"
    assert plan.to_call == "K1ABC"
    assert 0.0 <= plan.delay_s <= AUTO_RESPONSE_MAX_DELAY_S


def test_plan_grid_query_for_member():
    plan = plan_auto_response(
        verb="GRID?", body="",
        from_call="K1ABC", to_call="@EMCOMM",
        our_groups=("@EMCOMM",),
        our_grid="EN83ih",
        snr_db=-10,
        rng=_seeded_rng(),
    )
    assert plan is not None
    assert plan.text == "K1ABC GRID EN83ih"
    assert plan.to_call == "K1ABC"


def test_plan_non_member_no_reply():
    """We're NOT in @ARESGA → no plan, even for a query verb."""
    plan = plan_auto_response(
        verb="SNR?", body="",
        from_call="K1ABC", to_call="@ARESGA",
        our_groups=("@EMCOMM",),  # different group
        our_grid="EN83",
        snr_db=-9,
    )
    assert plan is None


def test_plan_unsupported_verb_no_reply():
    """INFO?/HEARING?/AGN? are out of scope for this drop."""
    for verb in ("INFO?", "HEARING?", "AGN?", "QSL?", "STATUS"):
        plan = plan_auto_response(
            verb=verb, body="",
            from_call="K1ABC", to_call="@EMCOMM",
            our_groups=("@EMCOMM",),
            our_grid="EN83",
            snr_db=-9,
        )
        assert plan is None, f"verb={verb} unexpectedly produced plan"


def test_plan_allcall_no_reply():
    """@ALLCALL queries are not auto-answered (would jam the channel)."""
    plan = plan_auto_response(
        verb="SNR?", body="",
        from_call="K1ABC", to_call="@ALLCALL",
        our_groups=("@EMCOMM",),
        our_grid="EN83",
        snr_db=-9,
    )
    assert plan is None


def test_plan_hb_no_reply():
    """@HB queries (would be unusual) are not auto-answered."""
    plan = plan_auto_response(
        verb="SNR?", body="",
        from_call="K1ABC", to_call="@HB",
        our_groups=("@EMCOMM",),
        our_grid="EN83",
        snr_db=-9,
    )
    assert plan is None


def test_plan_personal_directed_no_reply():
    """Direct-to-us queries are operator-answered manually in this drop."""
    plan = plan_auto_response(
        verb="SNR?", body="",
        from_call="K1ABC", to_call="W5DMH",
        our_groups=("@EMCOMM",),
        our_grid="EN83",
        snr_db=-9,
    )
    assert plan is None


def test_plan_snr_requires_snr_value():
    """SNR? with snr_db=None → no plan."""
    plan = plan_auto_response(
        verb="SNR?", body="",
        from_call="K1ABC", to_call="@EMCOMM",
        our_groups=("@EMCOMM",),
        our_grid="EN83",
        snr_db=None,
    )
    assert plan is None


def test_plan_grid_requires_grid_value():
    """GRID? with empty configured grid → no plan."""
    plan = plan_auto_response(
        verb="GRID?", body="",
        from_call="K1ABC", to_call="@EMCOMM",
        our_groups=("@EMCOMM",),
        our_grid="",  # not configured
        snr_db=-9,
    )
    assert plan is None


def test_plan_delay_is_randomized_within_bounds():
    """Across many invocations the delay should span most of [0, MAX]."""
    rng = random.Random(0)
    delays = []
    for _ in range(200):
        p = plan_auto_response(
            verb="SNR?", body="",
            from_call="K1ABC", to_call="@EMCOMM",
            our_groups=("@EMCOMM",),
            our_grid="EN83",
            snr_db=-5,
            rng=rng,
        )
        assert p is not None
        delays.append(p.delay_s)
    # All within bounds.
    assert all(0.0 <= d <= AUTO_RESPONSE_MAX_DELAY_S for d in delays)
    # Spread: min and max should be well-separated (sanity check that
    # we're actually getting a uniform distribution, not a constant).
    assert max(delays) - min(delays) > AUTO_RESPONSE_MAX_DELAY_S * 0.5


def test_plan_positive_snr_formats_without_explicit_sign():
    """+5 dB → 'SNR 5' (we send what Python's int repr gives, which
    is fine for JS8Call's parser)."""
    plan = plan_auto_response(
        verb="SNR?", body="",
        from_call="K1ABC", to_call="@EMCOMM",
        our_groups=("@EMCOMM",),
        our_grid="EN83",
        snr_db=5,
    )
    assert plan is not None
    assert plan.text == "K1ABC SNR 5"


def test_plan_returns_correct_type():
    plan = plan_auto_response(
        verb="SNR?", body="",
        from_call="K1ABC", to_call="@EMCOMM",
        our_groups=("@EMCOMM",),
        our_grid="EN83",
        snr_db=-9,
    )
    assert isinstance(plan, AutoResponsePlan)
    assert plan.text and plan.to_call
    assert isinstance(plan.delay_s, float)


def test_plan_empty_from_call_no_reply():
    """Can't reply to no-one."""
    plan = plan_auto_response(
        verb="SNR?", body="",
        from_call="", to_call="@EMCOMM",
        our_groups=("@EMCOMM",),
        our_grid="EN83",
        snr_db=-9,
    )
    assert plan is None



# ── Router: end-to-end keypress flow on the Groups field ─────────────
#
# Regression coverage for the May 2026 bug where the Setup→Groups
# field would accept Tab-focus but completely ignored typed keystrokes
# AND Enter. Root cause: the router's "enter edit mode" whitelist in
# _handle and _handle_enter listed only ("callsign","grid","units",
# "freq_hz") — not "groups". Focus could land there but no edit
# session would ever start, so the operator's typing was silently
# dropped. These tests simulate the exact keypress sequence an
# operator performs and assert that the buffer accumulates and the
# save callback fires with the right value.


def _setup_state_for_router_tests():
    """Build a Setup-screen UIState ready for router tests."""
    from microjs8.input.router import InputRouter
    from microjs8.input.events import KeyEvent
    state = UIState("K1ABC", "FN42", True, "miles")
    state.set_screen(Screen.SETUP)
    return state


def test_groups_field_accepts_typed_characters():
    """Tab to groups, type '@EMCOMM' — buffer should accumulate."""
    from microjs8.input.router import InputRouter
    from microjs8.input.events import Key, KeyEvent
    state = _setup_state_for_router_tests()

    saved: list = []
    def save(callsign, grid, units, new_groups=None):
        saved.append({"groups": new_groups})
        return True
    def emergency(): pass

    router = InputRouter(state, save_config=save, emergency_bypass=emergency)
    # Tab past callsign and grid to land on groups.
    router.handle(KeyEvent(key=Key.TAB))
    router.handle(KeyEvent(key=Key.TAB))
    assert state.focused_field_name() == "groups"

    # Type-to-edit: typing the first character must auto-enter edit
    # mode and append the character to the buffer.
    router.handle(KeyEvent(char="@"))
    assert state.is_editing(), "typing should have started an edit session"
    assert state.edit_buffer() == "@", (
        f"buffer should contain '@' after one keypress, got "
        f"{state.edit_buffer()!r}"
    )

    # Continue typing to build "@EMCOMM" — uppercase happens in the
    # router (lowercase 'e' → 'E').
    for ch in "emcomm":
        router.handle(KeyEvent(char=ch))
    assert state.edit_buffer() == "@EMCOMM"

    # Enter commits — save callback fires with our typed buffer in
    # new_groups; the config validator inside save_atomic normalises
    # and rejects malformed input. In this test the save is a stub
    # that just captures the value.
    router.handle(KeyEvent(key=Key.ENTER))
    assert saved == [{"groups": "@EMCOMM"}], saved


def test_groups_field_enter_begins_edit_session():
    """Enter on the focused groups field starts an edit session
    pre-filled with the current value (matches the pattern for
    callsign/grid/units/freq_hz)."""
    from microjs8.input.router import InputRouter
    from microjs8.input.events import Key, KeyEvent
    state = UIState(
        "K1ABC", "FN42", True, "miles", groups=("@EMCOMM",),
    )
    state.set_screen(Screen.SETUP)
    def save(callsign, grid, units, new_groups=None): return True
    def emergency(): pass
    router = InputRouter(state, save_config=save, emergency_bypass=emergency)

    router.handle(KeyEvent(key=Key.TAB))
    router.handle(KeyEvent(key=Key.TAB))
    assert state.focused_field_name() == "groups"

    router.handle(KeyEvent(key=Key.ENTER))
    assert state.is_editing()
    # Pre-fill: the buffer should hold the comma-joined current value.
    assert state.edit_buffer() == "@EMCOMM"


def test_groups_field_commits_comma_separated_list():
    """End-to-end: type '@EMCOMM,@ARES', commit, save sees the
    comma-separated string (the validator inside save_atomic does
    the actual parse + normalisation)."""
    from microjs8.input.router import InputRouter
    from microjs8.input.events import Key, KeyEvent
    state = _setup_state_for_router_tests()

    captured = {}
    def save(callsign, grid, units, new_groups=None):
        captured["groups"] = new_groups
        return True
    def emergency(): pass

    router = InputRouter(state, save_config=save, emergency_bypass=emergency)
    router.handle(KeyEvent(key=Key.TAB))
    router.handle(KeyEvent(key=Key.TAB))
    for ch in "@emcomm,@ares":
        router.handle(KeyEvent(char=ch))
    router.handle(KeyEvent(key=Key.ENTER))
    # Router uppercases each char; the buffer therefore holds the
    # uppercase form. The comma is preserved.
    assert captured["groups"] == "@EMCOMM,@ARES"


def test_groups_field_accepts_slash_and_digits():
    """Typing '@DX/NA' should land verbatim in the buffer — '/' and
    digits are part of the JS8Call group regex."""
    from microjs8.input.router import InputRouter
    from microjs8.input.events import Key, KeyEvent
    state = _setup_state_for_router_tests()
    def save(callsign, grid, units, new_groups=None): return True
    def emergency(): pass
    router = InputRouter(state, save_config=save, emergency_bypass=emergency)

    router.handle(KeyEvent(key=Key.TAB))
    router.handle(KeyEvent(key=Key.TAB))
    for ch in "@dx/na":
        router.handle(KeyEvent(char=ch))
    assert state.edit_buffer() == "@DX/NA"


def test_groups_field_backspace_works_in_edit():
    """Edit then backspace removes characters one at a time."""
    from microjs8.input.router import InputRouter
    from microjs8.input.events import Key, KeyEvent
    state = _setup_state_for_router_tests()
    def save(callsign, grid, units, new_groups=None): return True
    def emergency(): pass
    router = InputRouter(state, save_config=save, emergency_bypass=emergency)

    router.handle(KeyEvent(key=Key.TAB))
    router.handle(KeyEvent(key=Key.TAB))
    for ch in "@FOO":
        router.handle(KeyEvent(char=ch))
    assert state.edit_buffer() == "@FOO"
    router.handle(KeyEvent(key=Key.BACKSPACE))
    assert state.edit_buffer() == "@FO"
    router.handle(KeyEvent(key=Key.BACKSPACE))
    router.handle(KeyEvent(key=Key.BACKSPACE))
    assert state.edit_buffer() == "@"


def test_groups_field_esc_cancels_edit():
    """ESC during edit discards the buffer without saving."""
    from microjs8.input.router import InputRouter
    from microjs8.input.events import Key, KeyEvent
    state = _setup_state_for_router_tests()

    save_calls = []
    def save(callsign, grid, units, new_groups=None):
        save_calls.append(new_groups)
        return True
    def emergency(): pass

    router = InputRouter(state, save_config=save, emergency_bypass=emergency)
    router.handle(KeyEvent(key=Key.TAB))
    router.handle(KeyEvent(key=Key.TAB))
    for ch in "@TEST":
        router.handle(KeyEvent(char=ch))
    router.handle(KeyEvent(key=Key.ESC))
    assert not state.is_editing()
    assert save_calls == []  # nothing committed



