from __future__ import annotations

import argparse
from typing import Any, Optional

from ..core.errors import OpenDeviceError
from ..core.lock import DeviceLock, assert_free
from ..remote import tunnel as tunnel_module
from ..transport import devices
from ..transport.devices import DeviceEntry
from ..transport.lockdown import LockdownClient
from ..transport.service import ServiceConnection, open_service


def resolve_device(args: argparse.Namespace) -> DeviceEntry:
    'Select the target device from ``-d``.'
    return devices.resolve(getattr(args, "device", None), getattr(args, "mux", None))


class Link:
    'An established link (USB or Wi-Fi).'

    def __init__(self, kind: str, device: Optional[DeviceEntry] = None,
                 lockdown: Optional[LockdownClient] = None,
                 tunnel: Any = None, rsd: Any = None, handle: Any = None) -> None:
        self.kind = kind
        self.device = device
        self.lockdown = lockdown
        self.tunnel = tunnel
        self.rsd = rsd
        self.handle = handle
        self._lock: Optional[DeviceLock] = None

    @property
    def udid(self) -> Optional[str]:
        device_udid = getattr(self.device, "udid", None)
        if device_udid:
            return device_udid
        value = getattr(self.rsd, "udid", None)
        return value() if callable(value) else value

    def service(self, name: str, rsd_name: Optional[str] = None) -> ServiceConnection:
        'Get a lockdown-style service (USB via lockdown, Wi-Fi via the shim port).'
        return open_service(self.lockdown, self.rsd, name, rsd_name)

    def __str__(self) -> str:
        return f"{self.kind}:{self.udid or '?'}"

    def wire_core_device(self) -> None:
        'Prepare CoreDevice metadata: **protocol version** + a **live diagnostic probe** for missing services.'
        if self.rsd is None:
            return
        from ..services import mounter as mounter_module

        mounter_module.ensure_mounted(lockdown=self.lockdown, rsd=self.rsd)
        self.rsd.core_device_version = mounter_module.resolve_core_device_version(
            lockdown=self.lockdown, rsd=self.rsd)
        self.rsd.ddi_probe = lambda: mounter_module.ddi_state(
            lockdown=self.lockdown, rsd=self.rsd)

    def hold(self, task: str, force: bool = False) -> None:
        'Hold the device until :meth:`close`.'
        udid = self.udid
        if self._lock is None and udid:
            self._lock = DeviceLock.acquire(udid, task=task, force=force)

    def close(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        handle, self.handle = self.handle, None
        if handle is not None:
            try:
                handle.close()
            except Exception:                     # noqa
                pass
            self.rsd = self.tunnel = self.lockdown = None
            return
        for item in (self.rsd, self.tunnel, self.lockdown):
            if item is not None:
                try:
                    item.close()
                except Exception:                     # noqa
                    pass

    def __enter__(self) -> "Link":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def open_link(args: argparse.Namespace, need_rsd: bool = False,
              usb_only: bool = False, task: str = "") -> Link:
    'Connect according to ``--link``.'
    wanted = getattr(args, "link", None) or "auto"
    if usb_only and wanted == "wifi":
        raise OpenDeviceError('this command only works over USB: the device has no Wi-Fi variant of it')
    if usb_only:
        wanted = "usb"

    if wanted in ("auto", "usb"):
        try:
            device = resolve_device(args)
        except OpenDeviceError:
            if wanted == "usb" or getattr(args, "device", None):
                raise
            return _open_wifi_link(args)
        if task:
            assert_free(device.udid, force=bool(getattr(args, "force", False)),
                        who=f"`{getattr(args, 'command', '')}`")
        mux = getattr(args, "mux", None)
        if need_rsd:
            handle = tunnel_module.open_rsd(device.udid, backend="userspace",
                                            mux_address=mux)
            link = Link("usb", device=device, lockdown=handle.lockdown,
                        tunnel=handle.tunnel, rsd=handle.rsd, handle=handle)
            link.wire_core_device()
        else:
            lockdown = LockdownClient(device.udid, mux)
            lockdown.start_session()
            link = Link("usb", device=device, lockdown=lockdown)
        if task:
            link.hold(task, force=bool(getattr(args, "force", False)))
        return link

    return _open_wifi_link(args, task)


def _open_wifi_link(args: argparse.Namespace, task: str = "") -> Link:
    'Wi-Fi: bonjour + RemotePairing -> CDTunnel -> RSD (needs 3.13+ and cryptography).'
    udid = getattr(args, "device", None)
    if task:
        assert_free(udid, force=bool(getattr(args, "force", False)), who='this command')
    handle = tunnel_module.open_rsd(udid, backend="network")
    link = Link("wifi", rsd=handle.rsd, tunnel=handle.tunnel, handle=handle)
    link.wire_core_device()
    if task:
        link.hold(task, force=bool(getattr(args, "force", False)))
    return link
