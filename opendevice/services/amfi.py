from __future__ import annotations

import time
from typing import Any, Callable, Optional

from ..core.errors import DeveloperModeError, OpenDeviceError
from ..transport.lockdown import LockdownClient
from ..transport.service import ServiceConnection, open_service
from ..transport.usbmux import list_devices
from .base import Service

SERVICE = "com.apple.amfi.lockdown"

ACTION_REVEAL = 0
ACTION_ENABLE = 1
ACTION_ACCEPT = 2


class AmfiService(Service):
    'Developer mode switch (USB / lockdown only).'

    NAME = SERVICE
    RSD_NAME = None

    def __init__(self, lockdown: Any) -> None:
        super().__init__(None)
        self.lockdown = lockdown

    @classmethod
    def open(cls, lockdown: Any = None, rsd: Any = None) -> "AmfiService":
        if lockdown is None:
            raise OpenDeviceError('developer mode is USB-only: the device has no Wi-Fi variant of this service')
        return cls(lockdown)

    def _action(self, action: int) -> dict[str, Any]:
        conn: ServiceConnection = open_service(self.lockdown, None, self.NAME)
        try:
            self.log.debug("AMFI action %d", action)
            response = conn.request({"action": action})
        finally:
            conn.close()
        if not isinstance(response, dict) or not response.get("success"):
            raise DeveloperModeError(
                f"AMFI action {action} failed: {response!r}"
                ' (developer mode cannot be enabled while the device has a passcode)')
        return response

    def reveal(self) -> None:
        'Reveal the developer mode switch in Settings.'
        self._action(ACTION_REVEAL)

    def request_enable(self) -> None:
        'Request developer mode.'
        self._action(ACTION_ENABLE)

    def accept_after_restart(self) -> None:
        'Confirm after the reboot (without it the request does not count).'
        self._action(ACTION_ACCEPT)

    def enable(self, wait: bool = True, timeout: float = 300.0,
               on_status: Optional[Callable[[str], None]] = None) -> LockdownClient:
        'Enable developer mode: request -> wait for the device to reboot -> confirm.'
        udid = getattr(self.lockdown, "device", None)
        udid = getattr(udid, "udid", None)
        self.request_enable()
        if not wait:
            return self.lockdown

        _say(on_status, 'device is rebooting...')
        self._wait_reboot(udid, timeout, on_status)
        fresh = LockdownClient(udid)
        fresh.start_session()
        self.lockdown = fresh
        self.accept_after_restart()
        _say(on_status, 'developer mode enabled')
        return fresh

    def _wait_reboot(self, udid: Optional[str], timeout: float,
                     on_status: Optional[Callable[[str], None]]) -> None:
        'Wait for the device to disappear from usbmux and come back (it is gone while rebooting).'
        deadline = time.monotonic() + timeout
        gone = False
        while time.monotonic() < deadline:
            present = any(not udid or device.matches_udid(udid)
                          for device in _devices())
            if not present:
                gone = True
            elif gone:
                _say(on_status, 'device is back, confirming...')
                return
            time.sleep(2.0)
        raise DeveloperModeError(f"timed out waiting for the device to reboot ({timeout:.0f}s)")


def _devices() -> list[Any]:
    try:
        return list_devices() or []
    except Exception:                            # noqa
        return []


def _say(on_status: Optional[Callable[[str], None]], text: str) -> None:
    if on_status is not None:
        on_status(text)


def developer_mode_status(lockdown: Any = None, rsd: Any = None) -> Optional[bool]:
    'Whether developer mode is enabled.'
    from .mounter import MobileImageMounter

    return MobileImageMounter.open(lockdown, rsd).developer_mode_status()
