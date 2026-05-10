"""microjs8.audio — USB audio capture + playback (Step 5 + 6)."""

from microjs8.audio.capture import (
    AudioCapture,
    CAPTURE_SAMPLE_RATE,
    RX_BUFFER_SECONDS,
    RX_BUFFER_SIZE,
    RX_SAMPLE_RATE,
)
from microjs8.audio.discovery import RadioDeviceNotFound, find_radio_input_device
from microjs8.audio.playback import (
    AudioPlayback,
    PlaybackError,
    TX_OUTPUT_CHANNELS,
    TX_OUTPUT_RATE,
    TX_SOURCE_RATE,
    TX_UPSAMPLE_FACTOR,
)

__all__ = [
    "AudioCapture",
    "AudioPlayback",
    "CAPTURE_SAMPLE_RATE",
    "PlaybackError",
    "RX_BUFFER_SECONDS",
    "RX_BUFFER_SIZE",
    "RX_SAMPLE_RATE",
    "RadioDeviceNotFound",
    "TX_OUTPUT_CHANNELS",
    "TX_OUTPUT_RATE",
    "TX_SOURCE_RATE",
    "TX_UPSAMPLE_FACTOR",
    "find_radio_input_device",
]
