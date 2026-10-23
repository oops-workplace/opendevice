from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Callable, Optional, TypeVar

from ..core.errors import OpenDeviceError
from ..core.lock import DeviceLock, assert_free
from ..remote.coredevice import SERVICE_DEVICE_INFO, CoreDeviceClient
from ..services import mounter as mounter_module
from ..transport import devices as device_list

if TYPE_CHECKING:
    from ..input import hid as hid_module
    from ..input import pasteboard as pasteboard_module
    from ..media.stream import MediaStreamSession
    from ..remote.rsd import RSD
    from ..remote.tunnel import Tunnel, TunnelInfo
    from ..transport.lockdown import LockdownClient

LINK_BACKENDS = {"usb": "userspace", "wifi": "network"}

_SessionT = TypeVar("_SessionT", bound="LinkMixin")


class LinkMixin:
    'Connect / close / switch link (part of ``DeviceSession``; do not instantiate alone).'

    def __init__(self, udid: Optional[str] = None, mux_address: Any = None,
                 display_id: int = 1, session_timeout: int = 3600,
                 rsd: Optional[RSD] = None, link: str = "auto",
                 force: bool = False) -> None:
        self.udid = udid
        self.mux_address = mux_address
        self.display_id = display_id
        self.session_timeout = session_timeout
        self.force = force
        self.link = link
        self.rsd = rsd
        self.info: Optional[TunnelInfo] = getattr(rsd, "info", None)

        self.lockdown: Optional[LockdownClient] = None
        self.tunnel: Optional[Tunnel] = None
        self.device: Optional[Any] = None

        self.stream: Optional[MediaStreamSession] = None
        self.receiver: Any = None
        self._watchdog: Optional[threading.Thread] = None
        self._stop_watch = threading.Event()
        self._closing = threading.Event()
        self._last_restart = 0.0

        self._hid: Optional[hid_module.UniversalHID] = None
        self._indigo: Optional[hid_module.IndigoHID] = None
        self._board: Optional[pasteboard_module.PasteboardClient] = None

        self._handle: Optional[Any] = None

        self._owns_link = False

        self._ax_wedged = False

        self._display: Optional[dict[str, Any]] = None
        self._rotation_at = 0.0
        self._rotation_value = "rot0"

        self.ax_last_len = 0

        self.core_device_version: Optional[str] = None

        self.on_error: Optional[Callable[[str], None]] = None
        self.on_status: Optional[Callable[[str], None]] = None

        self._lock: Optional[DeviceLock] = None

    def connect(self: _SessionT) -> _SessionT:
        'Connect to the device: lockdown -> CoreDeviceProxy tunnel -> RSD.'
        if self.rsd is not None:
            return self
        self._owns_link = True
        self._closing.clear()

        if self.link != "network":
            self.device = device_list.resolve(self.udid, self.mux_address)
            self.udid = self.device.udid

        assert_free(self.udid, force=self.force, who="opendevice")

        from ..remote.tunnel import open_rsd

        self._handle = open_rsd(self.udid, backend=self.link)
        self.rsd = self._handle.rsd
        self.tunnel = self._handle.tunnel
        self.lockdown = self._handle.lockdown
        self.info = getattr(self.rsd, "info", None)

        device_udid = self.rsd.udid()
        if device_udid:
            self.udid = device_udid

        mounter_module.ensure_mounted(lockdown=self.lockdown, rsd=self.rsd,
                                      on_status=self.on_status)
        self.core_device_version = mounter_module.resolve_core_device_version(
            lockdown=self.lockdown, rsd=self.rsd)
        self.rsd.ddi_probe = lambda: mounter_module.ddi_state(
            lockdown=self.lockdown, rsd=self.rsd)
        return self

    def close(self) -> None:
        'Tear everything down (stream -> injection clients -> RSD -> tunnel -> lockdown).'
        self._closing.set()
        self.stop_stream()

        if self._lock is not None:
            self._lock.release()
            self._lock = None

        for client in (self._hid, self._indigo, self._board):
            try:
                if client is not None:
                    client.close()
            except Exception:                      # noqa: BLE001
                pass
        self._hid = self._indigo = self._board = None

        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except Exception:                      # noqa: BLE001
                pass
            self.rsd = self.tunnel = self.lockdown = None
        else:
            if self.rsd is not None:
                try:
                    self.rsd.close()
                except Exception:                  # noqa: BLE001
                    pass
                self.rsd = None
            if self.tunnel is not None:
                try:
                    self.tunnel.close()
                except Exception:                  # noqa: BLE001
                    pass
                self.tunnel = None
            if self.lockdown is not None:
                try:
                    self.lockdown.close()
                except Exception:                  # noqa: BLE001
                    pass
                self.lockdown = None

    def __enter__(self: _SessionT) -> _SessionT:
        return self.connect()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _require_rsd(self) -> RSD:
        if self.rsd is None:
            raise OpenDeviceError('not connected yet, call connect()')
        return self.rsd

    def switch_link(self, kind: str) -> bool:
        'Switch the link (``"usb"`` / ``"wifi"``) and **rebuild the entire link**; returns whether it changed.'
        kind = (kind or "").strip().lower()
        backend = LINK_BACKENDS.get(kind)
        if backend is None:
            raise ValueError(f"the link must be usb or wifi, got {kind!r}")
        if not self._owns_link:
            raise OpenDeviceError('the link was injected by the caller (rsd=); it must switch it itself')
        current = "userspace" if self.link == "auto" else self.link
        if backend == current:
            return False
        old = self.link
        on_access_unit, on_stats = self._stream_callbacks()
        was_streaming = self.receiver is not None
        try:
            self.link = backend
            self.rebuild_link()
            return True
        except Exception:                          # noqa: BLE001
            self.link = old
            try:
                self.rebuild_link()
            except Exception:                      # noqa: BLE001
                pass
            raise
        finally:
            if was_streaming:
                self._start_stream_quietly(on_access_unit, on_stats)

    def link_state(self) -> dict[str, Any]:
        '**Measured** link state (not the policy): which one is used, USB present, can switch.'
        backend = self.link if self._owns_link else "injected"
        return {
            "backend": backend,
            "kind": "wifi" if backend == "network" else "usb",
            "usb_attached": self.usb_attached(),
            "switchable": self._owns_link and backend in ("auto", "userspace", "network"),
        }

    def usb_attached(self) -> Optional[bool]:
        'Is the device attached over USB (asks usbmux)? ``None`` when unknown.'
        if not self.udid:
            return None
        try:
            from ..transport.usbmux import list_devices

            devices = list_devices() or []
        except Exception:                          # noqa: BLE001
            return None
        return any(device.is_usb and device.matches_udid(self.udid)
                   for device in devices)

    def is_alive(self) -> bool:
        'Whether the link is still usable.'
        if self.rsd is None:
            return False
        try:
            CoreDeviceClient(self.rsd, SERVICE_DEVICE_INFO).get_display_info()
            return True
        except Exception:                          # noqa: BLE001
            return False

    @property
    def connected(self) -> bool:
        'Whether the link is up.'
        return self.rsd is not None
