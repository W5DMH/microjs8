"""microjs8.modem — GFSK8 wrapper integration (Step 5 + 6)."""

from microjs8.modem.decoder import DecodeThread, JS8_SLOT_SECONDS
from microjs8.modem.encoder import (
    DEFAULT_AUDIO_FREQ_HZ,
    EncoderError,
    SUBMODE_FAST,
    SUBMODE_NORMAL,
    SUBMODE_SLOW,
    SUBMODE_TURBO,
    SUBMODE_ULTRA,
    TX_LEVEL_FRAC,
    TX_SAMPLE_RATE,
    encode_message,
)

__all__ = [
    "DEFAULT_AUDIO_FREQ_HZ",
    "DecodeThread",
    "EncoderError",
    "JS8_SLOT_SECONDS",
    "SUBMODE_FAST",
    "SUBMODE_NORMAL",
    "SUBMODE_SLOW",
    "SUBMODE_TURBO",
    "SUBMODE_ULTRA",
    "TX_LEVEL_FRAC",
    "TX_SAMPLE_RATE",
    "encode_message",
]
