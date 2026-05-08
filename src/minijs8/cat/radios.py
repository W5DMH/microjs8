"""Built-in radio definitions.

Mirrors the data shape from the operator-supplied radios.json file —
hamlib model, baud rate, PTT method, CAT delays — but ships embedded
in the package so we never depend on a runtime file lookup. Adding
support for a new radio is a code change, not a config change. That's
the right tradeoff for an appliance: fewer surprises, fewer "why
doesn't my radio work" debug threads.

The operator selects a radio via ``[radio] id = "..."`` in
``config.toml``. Default is the QDX since that's our reference
hardware. Three profiles ship today:

  - ``qdx`` — QRP Labs QDX (CAT for control + PTT, builtin USB audio)
  - ``xiegu-g90-digirig`` — Xiegu G90 over DigiRig (CAT for control,
    RTS-PTT on the same DigiRig serial port)
  - ``digirig-rts-only`` — DigiRig + arbitrary radio (FM walkie, uSDX,
    TRX-DUO, anything with a data port but no CAT). RTS-PTT only,
    no CAT control.

Why three rather than a JSON file: embedded radios mean validation
happens at code-load time (typos = ImportError, not silent fallback),
and we get type-checking for free. Operators with unsupported radios
file an issue — adding support is a 5-line code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RadioDef:
    """One supported radio configuration.

    Fields are organized into three groups:

      * **Identity & display** — id, display_name, description
      * **CAT control** — hamlib_id, baud_rate, cat_required (when
        False, the daemon doesn't even try to launch rigctld)
      * **PTT control** — ptt_method ("CAT" / "RTS"), ptt_on_delay_ms,
        ptt_off_delay_ms, tx_pipeline_latency_ms
      * **Audio binding** — requires_external_audio,
        audio_card_substring (used by the audio device picker to
        pick the right sound card; the QDX's "Transceiver" vs
        DigiRig's "Device")
      * **Serial port hints** — preferred_serial_path (stable
        symlink we can fall back on if the operator hasn't run
        the udev rules yet)
    """

    # Identity / display
    id: str
    display_name: str
    description: str

    # CAT control
    hamlib_id: int
    baud_rate: int
    # When False, the daemon does NOT start rigctld and skips CatService
    # entirely. PTT goes through the RtsPttService (direct pyserial
    # toggle of the serial port's RTS line). Used for radios with no
    # CAT capability — FM walkies via DigiRig is the canonical case.
    cat_required: bool

    # PTT control
    ptt_method: str  # "CAT" or "RTS"
    ptt_on_delay_ms: int
    ptt_off_delay_ms: int
    # End-to-end audio pipeline latency from "we call stream.start()"
    # to "first DAC sample emerges" — the unobservable component of
    # the OS → ALSA → USB → radio chain. Subtracted from the silence
    # budget in transmit_frame() so modulation actually arrives on-air
    # at slot+500 ms. JS8Call's Modulator::start() doesn't expose this
    # directly because Qt's QAudioOutput in pull-mode hides the
    # buffer-fill startup latency. Empirical: measure on real hardware
    # once with this set to 0, observe JS8Call's auto-tune offset on a
    # calibrated remote receiver, set this to the magnitude of that
    # offset. Default 0 = no compensation (safe; matches old behavior).
    tx_pipeline_latency_ms: int = 0

    # Audio binding
    # If True, the audio picker prefers a separate USB sound card
    # (e.g. DigiRig's CM108) over the radio's built-in audio. The
    # match is by audio_card_substring against ALSA device names.
    requires_external_audio: bool = False
    # Substring matched against ALSA card names (e.g. "Transceiver"
    # for the QDX, "Device" for the DigiRig CM108). When None, the
    # audio picker uses defaults (built-in USB sound card detection).
    audio_card_substring: Optional[str] = None

    # Serial port hints
    # Stable /dev path the udev rules create. The launcher script
    # (and the RtsPttService) prefer this over auto-detection so
    # that ports stay consistent across reboots / re-plugs.
    preferred_serial_path: Optional[str] = None


# ── Defined radios ──────────────────────────────────────────────────


QDX = RadioDef(
    id="qdx",
    display_name="QRP Labs QDX",
    description=(
        "QRP Labs QDX — 5W HF digital transceiver. Built-in USB audio + "
        "CAT via Kenwood TS-480 emulation."
    ),
    # Hamlib model 2028 = Kenwood TS-480, the emulation the QDX firmware
    # implements. Verified against hamlib 4.5.x on Bookworm with
    # `rigctl --list | grep TS-480`. Earlier hamlib versions used
    # different numbers — verify when migrating distributions.
    hamlib_id=2028,
    baud_rate=9600,
    cat_required=True,
    ptt_method="CAT",
    # The QDX has a solid-state PTT (no relay) so settle time is short.
    # 50 ms covers the USB serial round-trip and any internal mode-
    # switch latency without eating into our 500 ms slot budget.
    # On-air testing of multi-frame bursts on Pi Zero 2W showed
    # alignment failures with 150 ms settle; 50 ms recovered margin.
    ptt_on_delay_ms=50,
    ptt_off_delay_ms=100,
    requires_external_audio=False,
    audio_card_substring="Transceiver",
    preferred_serial_path="/dev/serial/by-id/usb-QRP_Labs_QDX_Transceiver-if00",
    # Audio pipeline latency — subtracted from the silence prefix in
    # tx_backend.transmit_frame() so modulation actually arrives on
    # air at slot+500ms.
    #
    # How this 200 ms was derived (W5DMH bench, May 2026):
    #   1. Fresh-image QDX setup, tx_pipeline_latency_ms=0.
    #   2. Reference station (NTP-disciplined laptop running JS8Call,
    #      stable to ~10ms drift) read off the DT column for our
    #      heartbeat TX over a 5-minute window:
    #         204, 204, 214, 180, 209, 180, 180 ms
    #      Mean ~196 ms, range 180-214 (spread 34 ms). Tight cluster.
    #      Positive DT = arrived late.
    #   3. tx_pipeline_latency_ms=200 directs the alignment math to
    #      transmit ~200 ms earlier in real time, landing the next
    #      TX at ~-4 ms (essentially on the boundary, well within
    #      the residual ±15 ms USB-scheduling jitter).
    #
    # Why the QDX needs ~2x the G90's 100 ms compensation:
    # the QDX appears to do more internal DSP buffering before its
    # modulator — likely a couple of FFT frames at the 12 kHz
    # internal rate, which lines up with ~200 ms. The G90 path has
    # the external DigiRig CM108 (small ALSA buffer) but is then
    # an analog audio path through to the modulator, so less
    # total buffering despite the extra hop.
    #
    # Re-measurement is appropriate if the QDX firmware changes
    # (different DSP buffer depths) or the host changes (Pi 4 vs
    # Pi Zero 2W has slightly different USB latency).
    tx_pipeline_latency_ms=200,
)


XIEGU_G90_DIGIRIG = RadioDef(
    id="xiegu-g90-digirig",
    display_name="Xiegu G90 + DigiRig",
    description=(
        "Xiegu G90 HF transceiver via DigiRig Mobile interface. "
        "CAT and PTT both go through the DigiRig CP2102 USB-serial "
        "bridge. PTT via RTS line; CAT (frequency / mode) via the "
        "same port at 19200 baud. Audio routed through DigiRig's "
        "CM108 USB sound card (NOT the G90's USB audio)."
    ),
    # Hamlib model 3088 = Xiegu G90.
    # Note: radios.json had 3087 historically, but per the official
    # Hamlib supported-radios list (verified 2026), 3087 is the X6100
    # and 3088 is the G90. The difference matters — using the wrong
    # backend produces command-rejected errors.
    hamlib_id=3088,
    baud_rate=19200,
    cat_required=True,
    ptt_method="RTS",
    # G90 has a relay-based PTT chain through the DigiRig optoisolator;
    # 300 ms gives generous settle for both the RTS-driven optoisolator
    # and the radio itself (manual recommends 200-300 ms for digital
    # modes). Trades slot budget for reliability — the operator can
    # tune this down if their setup is faster.
    ptt_on_delay_ms=300,
    ptt_off_delay_ms=200,
    # External audio: DigiRig presents itself as a CM108-based USB
    # sound card with the ALSA name "Device". Audio MUST go through
    # DigiRig (not the G90's USB), which is why requires_external_audio
    # is True.
    requires_external_audio=True,
    audio_card_substring="Device",
    # Stable path created by the DigiRig udev rule.
    preferred_serial_path="/dev/digirig",
    # Audio pipeline latency — subtracted from the silence prefix in
    # tx_backend.transmit_frame() so modulation actually arrives on
    # air at slot+500ms.
    #
    # How this 100 ms was derived (W5DMH bench, May 2026):
    #   1. Fresh-image G90 + DigiRig setup, tx_pipeline_latency_ms=0.
    #   2. Reference station (NTP-disciplined laptop running JS8Call,
    #      stable to ~10ms drift) read off the DT column for our
    #      heartbeat TX over a 5-minute window:
    #         99, 99, 115, 99, 120, 99, 125 ms
    #      Mean ~108 ms, very stable. Positive DT = arrived late.
    #   3. tx_pipeline_latency_ms=100 directs the alignment math to
    #      transmit ~100 ms earlier in real time, landing the next
    #      TX at +99 - 100 = -1 ms (essentially on the boundary).
    #
    # Why a constant works: the latency is dominated by USB→ALSA→radio
    # pipeline depth, which is set at stream-open time and doesn't
    # vary per-frame. Frame-to-frame jitter is small (~25 ms). A
    # single offset corrects the bias without chasing jitter.
    #
    # Re-measurement is appropriate if the audio chain changes —
    # different DigiRig firmware, different cable, different host
    # (Pi 4 vs Pi Zero 2W has slightly different USB latency).
    tx_pipeline_latency_ms=100,
)


DIGIRIG_RTS_ONLY = RadioDef(
    id="digirig-rts-only",
    display_name="DigiRig + Unknown Radio",
    description=(
        "DigiRig Mobile with RTS-PTT only — no CAT control. Use for "
        "FM walkie-talkies, uSDX, TRX-DUO, or any radio with a data "
        "port but no CAT. Frequency / mode setup is operator-managed "
        "on the radio's front panel."
    ),
    # No CAT — but we still keep these fields so the dataclass is
    # populated. The factory ignores hamlib_id / baud_rate when
    # cat_required is False.
    hamlib_id=1,  # 1 = Hamlib's "Dummy" rig (unused here)
    baud_rate=9600,
    cat_required=False,
    ptt_method="RTS",
    # FM walkie radios typically have slow PTT chains; 300 ms covers
    # both the optoisolator settle and any mode-switching the radio
    # needs to do on TX.
    ptt_on_delay_ms=300,
    ptt_off_delay_ms=200,
    requires_external_audio=True,
    audio_card_substring="Device",  # DigiRig CM108
    preferred_serial_path="/dev/digirig",
    # Audio pipeline latency — subtracted from the silence prefix in
    # tx_backend.transmit_frame() so modulation actually arrives on
    # air at slot+500ms.
    #
    # How this 90 ms was derived (W5DMH bench, May 2026):
    #   1. Pi Zero 2W + DigiRig + a generic FM walkie, default 0.
    #   2. Reference station (NTP-disciplined laptop running JS8Call,
    #      stable to ~10ms drift) read off the DT column for our
    #      heartbeat TX over a 5-minute window:
    #         84, 89, 89, 94, 89 ms
    #      Mean ~89 ms, range 84-94 (spread 10 ms). Tight cluster.
    #      Positive DT = arrived late.
    #   3. tx_pipeline_latency_ms=90 directs the alignment math to
    #      transmit ~90 ms earlier in real time, landing the next
    #      TX at ~-1 ms (essentially on the boundary).
    #
    # Caveat: this is a CHASSIS PROFILE used with any RTS-only radio
    # (different FM walkies, uSDX, TRX-DUO, etc.). The latency is
    # dominated by the DigiRig CM108 sound card pipeline (which is
    # constant), with a small additional component from the radio's
    # modulator (~10-30 ms typical). The G90 path (also CM108)
    # measured 100 ms — within 10 ms of this, confirming the CM108
    # is the dominant component. Operators using a different walkie
    # may see DT in the 60-120 ms range with this 90 ms value, all
    # well inside JS8's ±2.5 s tolerance — no need to re-tune
    # unless the operator wants slot-perfect alignment for their
    # specific rig.
    tx_pipeline_latency_ms=90,
)


_ALL_RADIOS: tuple[RadioDef, ...] = (
    QDX,
    XIEGU_G90_DIGIRIG,
    DIGIRIG_RTS_ONLY,
)


def get_radio(radio_id: str) -> RadioDef:
    """Return the RadioDef for the given id, or raise KeyError.

    The config layer validates the id at load time, so a KeyError
    here means a programmer / config bug (probably a typo in
    config.toml). Listing all known ids in the message helps the
    operator self-correct.
    """
    for radio in _ALL_RADIOS:
        if radio.id == radio_id:
            return radio
    known = ", ".join(r.id for r in _ALL_RADIOS)
    raise KeyError(
        f"unknown radio id {radio_id!r}; known: {known}"
    )


def known_radio_ids() -> tuple[str, ...]:
    """List of all radio ids (for config validation, UI dropdowns)."""
    return tuple(r.id for r in _ALL_RADIOS)
