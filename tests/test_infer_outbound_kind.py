"""Tests for ``infer_outbound_kind`` — the verb-based classifier that
keeps outbound queries from accidentally entering WAIT_ACK and
looping forever.

This is the safety net for the on-air bug observed in the bench-test
log: an outbound ``"KD8PGB QUERY MSGS"`` was queued with kind=DIRECTED
by some manual test path. The scheduler put it in WAIT_ACK, no ACK
ever came (correctly — JS8Call doesn't ACK queries), and we
retransmitted every 90 s for three attempts before abandoning.

The classifier guarantees that ANY caller (manual tooling, future
Compose UI, CLI helpers, REPL sessions) gets the right kind
automatically, even if they forget to specify one.

Coverage groups:

  1. Buffered-MSG verbs → DIRECTED (recipient auto-ACKs)
  2. Query / notification verbs → REPLY (no ACK expected)
  3. Edge cases: empty, whitespace-only, no verb, unknown verb
  4. Case-insensitivity (JS8Call upper-cases on TX)
  5. Multi-word "MSG TO:" handling (must beat plain MSG check)
"""

from __future__ import annotations

import pytest

from microjs8.tx.queue import OutboundKind, infer_outbound_kind


# ── Buffered MSG / MSG TO: → DIRECTED ────────────────────────────


def test_msg_with_body_is_directed():
    """Plain MSG followed by body → DIRECTED. Recipient's JS8Call
    auto-ACKs; we want the scheduler to track that ACK and only
    mark DELIVERED when it arrives."""
    assert infer_outbound_kind("KD8PGB MSG HELLO WORLD") == OutboundKind.DIRECTED


def test_msg_with_long_body_is_directed():
    """Multi-word MSG bodies stay DIRECTED."""
    text = "KD8PGB MSG This is a longer message body with many words"
    assert infer_outbound_kind(text) == OutboundKind.DIRECTED


def test_msg_to_is_directed():
    """MSG TO:<recipient> body → DIRECTED. Same auto-ACK semantics
    as plain MSG."""
    text = "KD8PGB MSG TO:KC1WDO Please pass this along"
    assert infer_outbound_kind(text) == OutboundKind.DIRECTED


def test_msg_to_with_tight_colon_is_directed():
    """No space between TO: and the recipient — JS8Call's standard form."""
    text = "KD8PGB MSG TO:K1ABC short body"
    assert infer_outbound_kind(text) == OutboundKind.DIRECTED


# ── Query / notification verbs → REPLY ──────────────────────────


def test_query_msgs_is_reply():
    """The exact bug from the on-air log: outbound QUERY MSGS must
    be classified as REPLY, NOT DIRECTED. The recipient's reply is
    YES/NO + held-msg-id, never an ACK — putting this in WAIT_ACK
    creates the retransmit loop."""
    assert infer_outbound_kind("KD8PGB QUERY MSGS") == OutboundKind.REPLY


def test_query_msg_id_is_reply():
    """Asking for a specific held message returns the body, not an ACK."""
    assert infer_outbound_kind("KD8PGB QUERY MSG 5") == OutboundKind.REPLY


def test_query_call_is_reply():
    """QUERY CALL <other> asks if the recipient can hear <other>; the
    response is a HEARING reply, not an ACK."""
    assert infer_outbound_kind("KD8PGB QUERY CALL N0XYZ") == OutboundKind.REPLY


def test_ack_is_reply():
    """Outbound ACK is the canonical fire-and-forget — JS8Call NEVER
    ACKs an ACK. This is the original loop-bug fix from the prior
    session, now also guaranteed by the classifier."""
    assert infer_outbound_kind("KC1WDO ACK") == OutboundKind.REPLY


def test_no_reply_is_reply():
    """'<asker> NO' — our reply when QUERY MSGS finds nothing held."""
    assert infer_outbound_kind("KD8PGB NO") == OutboundKind.REPLY


def test_yes_reply_is_reply():
    """'<asker> YES MSG ID <n>' — peer's reply when they have mail
    for the asker. We don't typically originate this verb (it's an
    inbound reply for us), but for completeness the classifier
    handles it as REPLY."""
    assert infer_outbound_kind("KD8PGB YES MSG ID 5") == OutboundKind.REPLY


def test_snr_question_is_reply():
    """SNR? is a query — answer is the SNR reading, not an ACK."""
    assert infer_outbound_kind("KD8PGB SNR?") == OutboundKind.REPLY


def test_snr_answer_is_reply():
    """And our outbound SNR answer back is also REPLY (terminal)."""
    assert infer_outbound_kind("KD8PGB SNR -8") == OutboundKind.REPLY


def test_info_query_is_reply():
    assert infer_outbound_kind("KD8PGB INFO?") == OutboundKind.REPLY


def test_info_answer_is_reply():
    assert infer_outbound_kind("KD8PGB INFO 14.078 MHz") == OutboundKind.REPLY


def test_grid_query_is_reply():
    assert infer_outbound_kind("KD8PGB GRID?") == OutboundKind.REPLY


def test_grid_answer_is_reply():
    assert infer_outbound_kind("KD8PGB GRID FN42") == OutboundKind.REPLY


def test_status_is_reply():
    assert infer_outbound_kind("KD8PGB STATUS") == OutboundKind.REPLY


def test_hearing_is_reply():
    assert infer_outbound_kind("KD8PGB HEARING K1ABC N0XYZ") == OutboundKind.REPLY


def test_unknown_verb_is_reply():
    """A verb the classifier doesn't recognize — REPLY is the safe
    default. It won't loop, and the only cost is missing ACK-tracking
    for whatever this turns out to be (nothing critical at this layer)."""
    assert infer_outbound_kind("KD8PGB FOOBAR something") == OutboundKind.REPLY


# ── Edge cases ───────────────────────────────────────────────────


def test_empty_text_is_reply():
    """Empty text: REPLY by default. The downstream enqueue will fail
    on a blank text anyway, but we shouldn't raise on input we don't
    recognize."""
    assert infer_outbound_kind("") == OutboundKind.REPLY


def test_whitespace_only_is_reply():
    """Pure whitespace strips to empty → REPLY."""
    assert infer_outbound_kind("   \t  ") == OutboundKind.REPLY


def test_recipient_only_is_reply():
    """Just a callsign with no verb — malformed but classify as REPLY
    so it doesn't loop."""
    assert infer_outbound_kind("KD8PGB") == OutboundKind.REPLY


def test_recipient_with_trailing_space_is_reply():
    """Trailing whitespace stripped before parsing."""
    assert infer_outbound_kind("KD8PGB  ") == OutboundKind.REPLY


# ── Case insensitivity ─────────────────────────────────────────


def test_lowercase_msg_is_directed():
    """JS8Call upper-cases on TX, but defensive: our classifier matches
    case-insensitively so it can't be tricked by lowercase test
    fixtures or operator lowercase entry."""
    assert infer_outbound_kind("kd8pgb msg hello") == OutboundKind.DIRECTED


def test_mixed_case_msg_to_is_directed():
    assert infer_outbound_kind("Kd8Pgb Msg To:Kc1Wdo body") == OutboundKind.DIRECTED


def test_lowercase_query_msgs_is_reply():
    assert infer_outbound_kind("kd8pgb query msgs") == OutboundKind.REPLY


# ── MSG vs MSG TO: precedence ────────────────────────────────────


def test_msg_with_to_prefix_text_is_still_directed():
    """Text like "KD8PGB MSG TOWER STATUS" — the body starts with
    "TOWER" not "TO:", so it's a plain MSG, not MSG TO:. Either
    way, both classify as DIRECTED — nothing to disambiguate. This
    test guards against a future regression where the MSG-TO:
    detection might over-trigger."""
    assert infer_outbound_kind("KD8PGB MSG TOWER STATUS") == OutboundKind.DIRECTED


def test_msg_to_word_in_body_is_still_directed():
    """The literal word "to" inside a MSG body shouldn't trigger
    MSG-TO: classification — both classify DIRECTED, but the
    branch taken matters for future maintainers."""
    assert infer_outbound_kind("KD8PGB MSG send to KC1WDO") == OutboundKind.DIRECTED


# ── The on-air canary ────────────────────────────────────────────


def test_canary_on_air_query_msgs_does_not_loop():
    """Direct regression test for the exact on-air observation:

    The bench-test log showed:
        "queued outbound id=57 ... text='KD8PGB QUERY MSGS'"
        ...
        "wait_ack: outbound id=57 (1 frame(s) sent, awaiting ACK)"
        ...
        "retrying outbound id=57 (no ACK in 104.7s)"
        x3 → "abandoned outbound id=57"

    With the classifier in place and ``kind=None`` on enqueue,
    the kind comes out as REPLY — and the scheduler's REPLY branch
    transitions SENDING→DELIVERED instead of SENDING→WAIT_ACK,
    breaking the loop.

    If this test ever fails, the classifier has drifted and the
    on-air loop will return.
    """
    assert infer_outbound_kind("KD8PGB QUERY MSGS") == OutboundKind.REPLY, (
        "outbound QUERY MSGS classified as DIRECTED — WAIT_ACK loop "
        "will return on-air. Check infer_outbound_kind in tx/queue.py."
    )
