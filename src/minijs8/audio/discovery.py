"""Audio device discovery — find the radio's USB sound card.

Per Step 5 design (your call to use udev-style discovery), we identify
the radio's USB sound card by ALSA device-name substring. For most
radios that's the radio itself ("QRP Labs QDX Transceiver" → match
"Transceiver"); for DigiRig-based setups it's the DigiRig's CM108
("Device" — yes, that's actually the C-Media CM108 ALSA name).

The function returns the *sounddevice index* of the device, since
``sounddevice.InputStream(device=...)`` takes either a name or an
integer index. Indices are stable for the duration of the process
lifetime (once enumerated, they don't shift) but can change between
boots, which is why we re-discover on every daemon start.

Radio-specific selection
------------------------

When a ``preferred_card_substring`` is supplied (e.g. ``"Device"``
for DigiRig-based radios, ``"Transceiver"`` for the QDX), we match
that FIRST. This matters when both the radio AND DigiRig are on the
USB bus simultaneously — without a preference, we'd grab whichever
came up first and could end up routing audio through the wrong
device. With a preference, the operator's intent (set by which
radio is selected in config) wins.

If no preferred match is found within the timeout, we fall back to
the legacy known-devices list (QDX, DigiRig, generic CM108) — that
keeps single-radio setups working with no config and matches the
behavior shipped before this commit.

Boot race
---------

USB sound cards take 1-2 s after enumeration to be registered with
ALSA, and another fraction of a second before PortAudio sees them.
On Bookworm, ``sound.target`` fires the moment ALSA's init scripts
complete — BEFORE USB sound cards have necessarily registered. If
our daemon starts within that window, ``sd.query_devices()`` may
report no input devices at all.

We tolerate this by retrying discovery for up to ``DISCOVERY_TIMEOUT_S``
seconds, with PortAudio re-init between attempts so each query gets
a fresh enumeration. In practice the QDX is found within 2-3 attempts
(every 1 s) on a cold boot.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

_log = logging.getLogger(__name__)


# How long to keep retrying device discovery before giving up.
# 10 seconds covers the worst observed cold-boot case on Pi Zero 2W.
DISCOVERY_TIMEOUT_S = 10.0
# How long to wait between attempts.
DISCOVERY_RETRY_S = 1.0


# Known radio audio devices — fallback list when no
# preferred_card_substring is supplied (or no preferred match found).
# Order matters: first match wins. We list radios with built-in audio
# first, then the generic interfaces.
KNOWN_RADIO_DEVICES = (
    {"name_substr": "Transceiver", "label": "QRP Labs QDX"},
    {"name_substr": "QDX", "label": "QRP Labs QDX (legacy match)"},
    {"name_substr": "DigiRig", "label": "DigiRig Mobile"},
    {"name_substr": "Device", "label": "DigiRig CM108 / generic"},
    {"name_substr": "C-Media", "label": "C-Media USB Audio"},
)


class RadioDeviceNotFound(Exception):
    """No recognized radio audio device is plugged in."""


def find_radio_input_device(
    *,
    timeout_s: float = DISCOVERY_TIMEOUT_S,
    preferred_card_substring: Optional[str] = None,
    preferred_card_label: Optional[str] = None,
) -> tuple[int, str]:
    """Return (sounddevice_index, descriptive_label) for the radio.

    Parameters
    ----------
    timeout_s : float
        How long to keep retrying before raising RadioDeviceNotFound.
    preferred_card_substring : optional str
        Case-insensitive substring matched against ALSA device names
        FIRST. When the operator selects a specific radio in config
        (e.g. ``xiegu-g90-digirig``), we want the matching audio card
        ("Device" — the CM108 in the DigiRig) and NOT the radio's own
        USB audio (which exists on the G90 but isn't what we want for
        a DigiRig-based setup). Passing this overrides the legacy
        first-match-wins behavior.
    preferred_card_label : optional str
        Human-readable label paired with ``preferred_card_substring``.
        Falls back to ``preferred_card_substring`` if not supplied.

    Behavior:
      1. Each attempt enumerates input devices.
      2. If ``preferred_card_substring`` is given and matches, return
         that — even if the legacy list also has a match.
      3. Otherwise, return the first match from KNOWN_RADIO_DEVICES.
      4. Retry every ``DISCOVERY_RETRY_S`` until ``timeout_s`` elapses.

    Raises RadioDeviceNotFound on timeout. Caller should log loudly
    and refuse to start the audio pipeline rather than silently fail.
    """
    deadline = time.monotonic() + timeout_s
    attempt = 0
    last_seen: list[str] = []

    while True:
        attempt += 1
        result, last_seen = _try_find_device(
            preferred_card_substring=preferred_card_substring,
            preferred_card_label=preferred_card_label,
        )
        if result is not None:
            if attempt > 1:
                _log.info(
                    "audio device discovery succeeded on attempt %d "
                    "(%.1fs after start)",
                    attempt, timeout_s - (deadline - time.monotonic()),
                )
            return result

        if time.monotonic() >= deadline:
            break
        # Brief sleep before retrying — gives the USB stack time to
        # finish enumeration and ALSA to register the card.
        time.sleep(DISCOVERY_RETRY_S)

    # Final failure — log all input devices we DID see, for diagnosis.
    _log.error(
        "no recognized radio audio device found after %.1fs and %d "
        "attempts. Input devices seen on the last query:\n%s",
        timeout_s, attempt,
        "\n".join(last_seen) if last_seen else "  (none)",
    )
    detail = (
        f"; preferred substring was {preferred_card_substring!r}"
        if preferred_card_substring else ""
    )
    raise RadioDeviceNotFound(
        f"no recognized radio audio device detected{detail}"
    )


def _try_find_device(
    *,
    preferred_card_substring: Optional[str],
    preferred_card_label: Optional[str],
) -> tuple[Optional[tuple[int, str]], list[str]]:
    """One pass through PortAudio's device list.

    Returns (result_or_None, list_of_input_device_descriptions).

    PortAudio caches its device list in process state; to pick up
    newly-arrived USB sound cards we need to terminate and re-init.
    sounddevice exposes this via _terminate / _initialize.
    """
    # Lazy import so host-side tests don't pull in PortAudio.
    import sounddevice as sd  # type: ignore[import-not-found]

    # Force a fresh PortAudio enumeration. Without this, devices that
    # appeared AFTER the first sd import are invisible.
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        # If the lifecycle hooks aren't available on this sounddevice
        # version, fall back to a single query. Worst case we don't
        # see late-arriving devices, but the discovery loop's outer
        # retries still help.
        _log.debug(
            "sounddevice _terminate/_initialize failed", exc_info=True,
        )

    devices = sd.query_devices()
    available: list[str] = []
    preferred_match: Optional[tuple[int, str]] = None
    legacy_match: Optional[tuple[int, str]] = None

    for i, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) < 1:
            continue
        name = dev.get("name", "")
        available.append(
            f"  [{i}] {name!r} ({dev.get('max_input_channels')}ch)"
        )

        # Preferred match takes precedence — operator told us which
        # card to use via radio config.
        if (
            preferred_match is None
            and preferred_card_substring is not None
            and preferred_card_substring.lower() in name.lower()
        ):
            label = preferred_card_label or preferred_card_substring
            _log.info(
                "audio device found (preferred): index=%d name=%r "
                "matched=%s",
                i, name, label,
            )
            preferred_match = (i, label)
            # Don't break — keep building the available list for the
            # diagnostic in case we need it.

        # Legacy first-match list — used when the preferred substring
        # doesn't match anything (or wasn't supplied at all).
        if legacy_match is None:
            for known in KNOWN_RADIO_DEVICES:
                if known["name_substr"].lower() in name.lower():
                    legacy_match = (i, known["label"])
                    break

    if preferred_match is not None:
        return preferred_match, available
    if legacy_match is not None:
        _log.info(
            "audio device found (fallback): index=%d label=%s",
            legacy_match[0], legacy_match[1],
        )
        return legacy_match, available
    return None, available
