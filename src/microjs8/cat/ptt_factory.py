"""Factory for picking the right PTT service for a given radio.

Returns a service that exposes the same public interface regardless
of CAT vs RTS-PTT — ``ptt_on`` / ``ptt_off`` / ``ptt_kick`` /
``set_ptt_max_hold`` / ``is_connected`` / ``start`` / ``stop`` /
optional ``get_frequency_hz`` / ``set_frequency_hz``.

Decision logic (one branch per radio category):

  * ``cat_required = True`` (QDX, G90+DigiRig, all CI-V Icoms):
    use ``CatService``. rigctld is launched by systemd via the
    launcher script (``microjs8-rigctld-launcher``) which inspects
    the same RadioDef to choose its rigctld arguments. This service
    just connects to localhost:4532. Whether PTT is asserted via a
    CAT command or via RTS toggle is hidden inside rigctld — the
    service doesn't know or care.

  * ``cat_required = False`` (DigiRig RTS-only): use
    ``RtsPttService``. No rigctld. Direct pyserial RTS toggle.

The radio's ``preferred_serial_path`` (e.g. ``/dev/digirig``) is
used by the RtsPttService to find the port. The udev rules in
``udev/99-microjs8-digirig.rules`` create that stable name.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional, Protocol

from microjs8.cat.radios import RadioDef
from microjs8.cat.rts_ptt_service import RtsPttService
from microjs8.cat.service import CatService

_log = logging.getLogger(__name__)


# Status callback type — same shape as CatService and RtsPttService
# expose, so the factory's return type can be unified.
StatusCallback = Callable[[bool], None]


class PttService(Protocol):
    """Common protocol both CatService and RtsPttService satisfy.

    The TX backend codes against this protocol rather than the
    concrete class. This is what lets QDX and DigiRig coexist
    without separate TX paths.

    Note: ``get_frequency_hz`` / ``set_frequency_hz`` are part of
    the protocol but RtsPttService implements them as no-ops
    (returning None / False). Callers that need real CAT should
    check ``radio.cat_required`` upstream.
    """

    @property
    def is_connected(self) -> bool: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def ptt_on(self) -> bool: ...
    def ptt_off(self) -> bool: ...
    def ptt_kick(self) -> None: ...
    def set_ptt_max_hold(self, seconds: float) -> None: ...
    def get_frequency_hz(self) -> Optional[int]: ...
    def set_frequency_hz(self, hz: int) -> bool: ...


def build_ptt_service(
    radio: RadioDef,
    *,
    on_status_change: Optional[StatusCallback] = None,
) -> PttService:
    """Construct (but do NOT start) a PTT service for ``radio``.

    Parameters
    ----------
    radio : RadioDef
        From ``microjs8.cat.radios.get_radio(...)``.
    on_status_change : optional callback
        Fires when the underlying connection state changes.

    Returns
    -------
    A service implementing the ``PttService`` protocol. Caller must
    call ``start()`` to begin connection / open the port.

    Raises
    ------
    ValueError
        If the radio's configuration is internally inconsistent
        (e.g. RTS-PTT requested but no preferred_serial_path).
    """
    if radio.cat_required:
        # CatService talks to rigctld which is launched by systemd
        # with whatever args the launcher script picked for THIS
        # radio. We just connect to localhost:4532 — the same as
        # before for the QDX, and the same for G90+DigiRig (rigctld
        # handles the RTS-PTT internally for that case).
        _log.info(
            "PTT factory: %s → CatService (rigctld at localhost)",
            radio.id,
        )
        return CatService(on_status_change=on_status_change)

    # cat_required is False — radio has no CAT, just RTS-PTT.
    if not radio.preferred_serial_path:
        raise ValueError(
            f"radio {radio.id!r} has cat_required=False but no "
            f"preferred_serial_path — cannot determine which port "
            f"to toggle for RTS-PTT"
        )
    _log.info(
        "PTT factory: %s → RtsPttService (port=%s)",
        radio.id, radio.preferred_serial_path,
    )
    return RtsPttService(
        serial_port=radio.preferred_serial_path,
        baudrate=radio.baud_rate,
        on_status_change=on_status_change,
    )
