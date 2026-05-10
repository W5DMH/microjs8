"""Tests for microjs8.audio.discovery.

The retry-on-cold-boot path needs explicit coverage. We don't actually
open PortAudio; we monkey-patch ``sounddevice.query_devices`` and the
``_terminate``/``_initialize`` lifecycle hooks so we can simulate
"USB audio took 3 seconds to enumerate" deterministically.
"""

from __future__ import annotations

import time

import pytest

from microjs8.audio import discovery


@pytest.fixture
def fake_sd(monkeypatch):
    """Inject a fake `sounddevice` module with controllable device list.

    Returns a mutable holder; tests set ``holder["devices"]`` to the
    list to return on the next ``query_devices()`` call. Calls between
    sets re-use the previous list. Each ``_terminate``+``_initialize``
    cycle increments ``holder["init_count"]`` so we can assert the
    discovery loop is forcing fresh enumerations.
    """
    holder = {"devices": [], "init_count": 0}

    class _FakeSd:
        # Match the public surface our code uses.
        @staticmethod
        def query_devices():
            return list(holder["devices"])

        @staticmethod
        def _terminate():
            holder["init_count"] += 1

        @staticmethod
        def _initialize():
            pass

    # The discovery module imports sounddevice lazily inside the function
    # body, so we patch sys.modules.
    import sys
    monkeypatch.setitem(sys.modules, "sounddevice", _FakeSd)
    return holder


# ── Immediate-success path ───────────────────────────────────────────


def test_finds_qdx_on_first_try(fake_sd, monkeypatch):
    fake_sd["devices"] = [
        {"name": "QDX Transceiver: USB Audio", "max_input_channels": 1},
    ]
    # Speed up the test — no need for the production 1s retry interval.
    monkeypatch.setattr(discovery, "DISCOVERY_RETRY_S", 0.01)
    idx, label = discovery.find_radio_input_device(timeout_s=1.0)
    assert idx == 0
    assert "QDX" in label


def test_finds_digirig(fake_sd, monkeypatch):
    fake_sd["devices"] = [
        {"name": "Some other thing", "max_input_channels": 1},
        {"name": "DigiRig Mobile USB Audio", "max_input_channels": 1},
    ]
    monkeypatch.setattr(discovery, "DISCOVERY_RETRY_S", 0.01)
    idx, label = discovery.find_radio_input_device(timeout_s=1.0)
    assert idx == 1
    assert "DigiRig" in label


def test_skips_output_only_devices(fake_sd, monkeypatch):
    """A device with 0 input channels must be skipped even if name matches."""
    fake_sd["devices"] = [
        {"name": "QDX Transceiver: USB Audio (output)", "max_input_channels": 0},
        {"name": "QDX Transceiver: USB Audio (input)", "max_input_channels": 1},
    ]
    monkeypatch.setattr(discovery, "DISCOVERY_RETRY_S", 0.01)
    idx, _ = discovery.find_radio_input_device(timeout_s=1.0)
    assert idx == 1


# ── Retry path (the boot-race fix) ───────────────────────────────────


def test_retries_when_no_device_initially(monkeypatch):
    """First N calls return no devices; later call shows QDX. Discovery
    must succeed without giving up.

    This is the smoking-gun regression test for the boot-race bug
    where systemd starts our daemon before USB audio enumeration
    completes."""
    monkeypatch.setattr(discovery, "DISCOVERY_RETRY_S", 0.05)

    calls = {"n": 0}

    class _RaceySd:
        @staticmethod
        def query_devices():
            calls["n"] += 1
            if calls["n"] < 3:
                return []  # USB device not yet enumerated
            return [
                {"name": "QDX Transceiver: USB Audio", "max_input_channels": 1},
            ]

        @staticmethod
        def _terminate():
            pass

        @staticmethod
        def _initialize():
            pass

    import sys
    monkeypatch.setitem(sys.modules, "sounddevice", _RaceySd)

    idx, label = discovery.find_radio_input_device(timeout_s=2.0)
    assert idx == 0
    assert calls["n"] >= 3, "should have retried at least 3 times"


def test_raises_when_timeout_elapses_with_no_device(fake_sd, monkeypatch):
    fake_sd["devices"] = []  # never appears
    monkeypatch.setattr(discovery, "DISCOVERY_RETRY_S", 0.05)
    t0 = time.monotonic()
    with pytest.raises(discovery.RadioDeviceNotFound):
        discovery.find_radio_input_device(timeout_s=0.3)
    elapsed = time.monotonic() - t0
    # Should respect the timeout — not give up immediately, but not
    # hang well past it either.
    assert 0.25 < elapsed < 1.0


def test_raises_when_only_unrecognized_devices_present(fake_sd, monkeypatch):
    """Some other USB audio device is plugged in but it's not a known radio."""
    fake_sd["devices"] = [
        {"name": "Built-in Microphone", "max_input_channels": 2},
        {"name": "USB Generic Mic", "max_input_channels": 1},
    ]
    monkeypatch.setattr(discovery, "DISCOVERY_RETRY_S", 0.05)
    with pytest.raises(discovery.RadioDeviceNotFound):
        discovery.find_radio_input_device(timeout_s=0.3)


def test_re_enumerates_portaudio_each_attempt(fake_sd, monkeypatch):
    """Each retry should call _terminate + _initialize so PortAudio
    sees newly-arrived USB devices. Without this, PortAudio's cached
    device list would never refresh."""
    fake_sd["devices"] = []
    monkeypatch.setattr(discovery, "DISCOVERY_RETRY_S", 0.02)
    with pytest.raises(discovery.RadioDeviceNotFound):
        discovery.find_radio_input_device(timeout_s=0.1)
    # We expect at least 2 init cycles (initial + 1 retry).
    assert fake_sd["init_count"] >= 2


# ── preferred_card_substring (radio-specific selection) ─────────────


def test_preferred_substring_wins_over_legacy_match(fake_sd, monkeypatch):
    """When both a 'Transceiver' card AND a 'Device' card are present,
    a preferred substring of 'Device' must pick the DigiRig CM108
    even though 'Transceiver' would have won under the legacy list.

    This is the G90+DigiRig case: the G90 has its own USB audio
    we DON'T want, and the DigiRig CM108 IS what we want."""
    fake_sd["devices"] = [
        # G90's built-in USB audio (we want to ignore this).
        {"name": "Xiegu G90 USB Audio Transceiver", "max_input_channels": 1},
        # DigiRig CM108 (this is the one we want).
        {"name": "USB PnP Audio Device", "max_input_channels": 1},
    ]
    monkeypatch.setattr(discovery, "DISCOVERY_RETRY_S", 0.01)
    idx, label = discovery.find_radio_input_device(
        timeout_s=1.0,
        preferred_card_substring="Device",
        preferred_card_label="DigiRig CM108",
    )
    assert idx == 1, (
        f"preferred substring 'Device' should have picked index 1 "
        f"(DigiRig CM108), got {idx}"
    )
    assert label == "DigiRig CM108"


def test_preferred_substring_falls_back_to_legacy_when_no_match(
    fake_sd, monkeypatch,
):
    """When the preferred substring doesn't match anything, fall back
    to the legacy KNOWN_RADIO_DEVICES list. Don't fail just because
    the operator's card preference happens to be missing."""
    fake_sd["devices"] = [
        {"name": "QDX Transceiver: USB Audio", "max_input_channels": 1},
    ]
    monkeypatch.setattr(discovery, "DISCOVERY_RETRY_S", 0.01)
    idx, label = discovery.find_radio_input_device(
        timeout_s=1.0,
        preferred_card_substring="DigiRig",  # not present
        preferred_card_label="DigiRig",
    )
    # Falls back to legacy list — finds the QDX.
    assert idx == 0
    assert "QDX" in label


def test_preferred_substring_none_keeps_legacy_behavior(
    fake_sd, monkeypatch,
):
    """When preferred_card_substring is None (default), behave
    identically to the pre-DigiRig version."""
    fake_sd["devices"] = [
        {"name": "QDX Transceiver: USB Audio", "max_input_channels": 1},
    ]
    monkeypatch.setattr(discovery, "DISCOVERY_RETRY_S", 0.01)
    idx, label = discovery.find_radio_input_device(timeout_s=1.0)
    assert idx == 0
    assert "QDX" in label


def test_preferred_substring_case_insensitive(fake_sd, monkeypatch):
    """Substring matching should be case-insensitive — operators
    shouldn't have to worry about exact ALSA capitalization."""
    fake_sd["devices"] = [
        {"name": "USB PnP Audio DEVICE", "max_input_channels": 1},
    ]
    monkeypatch.setattr(discovery, "DISCOVERY_RETRY_S", 0.01)
    idx, _ = discovery.find_radio_input_device(
        timeout_s=1.0,
        preferred_card_substring="device",  # lowercase
        preferred_card_label="DigiRig",
    )
    assert idx == 0
