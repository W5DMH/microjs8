"""Auto-respond to JS8Call group queries.

JS8Call's AUTO feature responds automatically to certain query verbs
addressed to a group the station belongs to. The intent is that a net
controller can issue ``@ARESGA SNR?`` and every group member's station
replies with the SNR at which it received the controller — a quick
roll-call without the controller polling each member.

This module implements that for two query verbs:

  - ``SNR?``  → reply with our SNR of the asker's transmission
  - ``GRID?`` → reply with our configured grid square

Other JS8Call query verbs (``INFO?``, ``HEARING?``, ``AGN?``) are out of
scope for this drop. They depend on state we don't yet track:

  - ``INFO?``    needs a configurable INFO string (not in our config)
  - ``HEARING?`` needs a heard-list scan with recency window
  - ``AGN?``     needs last-TX replay state

Direct (non-group) queries are not handled here — they're still answered
manually by the operator via the COMPOSE screen. Adding direct
auto-respond is a one-line conditional change if we want it later.

# Collision avoidance

A net controller polling ``@ARESGA SNR?`` triggers replies from every
group member at the same instant. Without spreading those replies
across time, every responder would TX in the next slot and step on
each other — the whole point of the broadcast SNR poll defeated.

We follow JS8Call's lead: each station picks a uniform random delay in
``[0, 30]`` seconds before submitting its reply to the outbound queue.
30 s is two JS8 normal slots (15 s each), which spreads ~10 group
members across enough TX windows that most replies land cleanly.

The randomization happens via ``loop.call_later`` — non-blocking, runs
on the asyncio thread. Cancellation isn't needed: if the operator
changes their mind they can manually clear the outbound queue.

# Skip conditions

  - Sender is us (impossible in practice — we never decode our own
    transmissions — but defensive)
  - We have no configured grid (for GRID?) — silent skip rather than
    advertising an empty grid
  - We aren't actually in the group (already filtered upstream by
    ``parsed.is_for_us``; this check is belt-and-braces)
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Optional

_log = logging.getLogger(__name__)


# Maximum randomized delay before submitting the auto-respond to the
# outbound queue. Spreads replies from group members across multiple
# JS8 slots so they don't collide. Two normal-mode slots (15 s each)
# is a comfortable spread for up to ~10 group members.
AUTO_RESPONSE_MAX_DELAY_S: float = 30.0


@dataclass(frozen=True)
class AutoResponsePlan:
    """The decision output of ``plan_auto_response``.

    ``text`` is the wire string (without the from-envelope; the modem
    prepends ``OURCALL: `` automatically). ``to_call`` is the asker's
    callsign — replies go DIRECTLY back to them, NOT to the group.
    Per JS8Call convention, group-query replies are personal — a net
    controller wants to know each station's SNR, not have the group
    name repeated in every reply.

    ``delay_s`` is the randomized submission delay (0..MAX). The
    caller schedules a ``loop.call_later(delay_s, enqueue_fn)``.
    """

    text: str
    to_call: str
    delay_s: float


def plan_auto_response(
    *,
    verb: str,
    body: str,
    from_call: str,
    to_call: str,
    our_groups: tuple[str, ...],
    our_grid: str,
    snr_db: Optional[int],
    rng: Optional[random.Random] = None,
) -> Optional[AutoResponsePlan]:
    """Decide whether to auto-respond to a parsed directed frame.

    Returns an ``AutoResponsePlan`` if the frame is an SNR?/GRID? query
    addressed to a group we belong to, else None. The caller is
    responsible for actually scheduling and enqueuing.

    Parameters
    ----------
    verb : str
        Uppercase first token of the frame body, e.g. "SNR?" / "GRID?".
    body : str
        Remainder of the frame body after the verb (unused today;
        accepted for forward compatibility).
    from_call : str
        Sender's callsign (the asker we'll reply TO).
    to_call : str
        The original frame's TO field — starts with '@' for groups.
        Must be in ``our_groups`` for the function to plan a reply.
    our_groups : tuple of str
        Operator-configured group memberships (uppercase, '@' prefix).
    our_grid : str
        Operator-configured grid square. Empty string means "not
        configured" — we skip GRID? replies in that case.
    snr_db : Optional[int]
        SNR of the asker's frame. Required for SNR? replies; None
        means we have no SNR data (shouldn't happen for a decoded
        frame, but defensive).
    rng : Optional[random.Random]
        Random generator for the delay. Tests pass a seeded Random
        for determinism; production passes None to use the module
        default (``random.uniform``).

    Returns
    -------
    AutoResponsePlan if a reply should be queued; None otherwise.
    """
    if not from_call:
        return None  # can't reply to no-one
    if not to_call or not to_call.startswith("@"):
        return None  # not a group-addressed frame
    target_group = to_call.upper()
    if target_group in ("@ALLCALL", "@HB"):
        return None  # implicit broadcasts aren't queries we answer
    # Defensive: confirm we're actually in the group. Upstream
    # routing should have filtered this already, but if a caller
    # plumbs the wrong addresses through we'd rather skip than reply.
    if target_group not in {g.upper() for g in our_groups}:
        return None

    verb_upper = (verb or "").upper()

    if verb_upper == "SNR?":
        if snr_db is None:
            # No SNR measurement available — shouldn't occur for a
            # successfully decoded frame, but skip rather than reply
            # with garbage. The asker will move on.
            return None
        # Format matches JS8Call protocol: SNR values are signed
        # integers in dB, formatted with explicit sign for the
        # negative case ("SNR -09") and just a number for positive
        # ("SNR 5"). gfsk8's wire encoder handles both.
        text = f"{from_call} SNR {snr_db}"
    elif verb_upper == "GRID?":
        grid = (our_grid or "").strip()
        if not grid:
            # We have no grid to advertise. Skip rather than send
            # an empty or placeholder grid that would mislead
            # callers tracking station locations.
            return None
        text = f"{from_call} GRID {grid}"
    else:
        # Not a verb we handle this drop. INFO?, HEARING?, AGN? all
        # fall through here. Future drops can add cases.
        return None

    # Randomized delay to avoid collisions when multiple group
    # members respond to the same query simultaneously.
    if rng is None:
        delay = random.uniform(0.0, AUTO_RESPONSE_MAX_DELAY_S)
    else:
        delay = rng.uniform(0.0, AUTO_RESPONSE_MAX_DELAY_S)

    return AutoResponsePlan(text=text, to_call=from_call, delay_s=delay)
