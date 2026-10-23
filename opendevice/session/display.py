from __future__ import annotations

import time
from typing import Any, Optional

from ..core.errors import OpenDeviceError
from ..remote.coredevice import SERVICE_DEVICE_INFO, CoreDeviceClient

ROTATION_TTL = 1.0


class DisplayMixin:
    'Device info and display geometry (part of ``DeviceSession``; do not instantiate alone).'

    @property
    def product_type(self) -> Optional[str]:
        return self.rsd.product_type if self.rsd else None

    @property
    def product_version(self) -> Optional[str]:
        return self.rsd.product_version if self.rsd else None

    def display_info(self) -> dict[str, Any]:
        'Raw ``getdisplayinfo`` result (cached).'
        if self._display is None:
            client = CoreDeviceClient(self._require_rsd(), SERVICE_DEVICE_INFO)
            self._display = dict(client.get_display_info() or {})
        return self._display

    @property
    def screen_px(self) -> Optional[tuple[int, int]]:
        '**Pixel** size of the main display (built-in screen).'
        main = self._main_display()
        if main is None:
            return None
        size = main.get("nativeSize") or (main.get("currentMode") or {}).get("size") or []
        if len(size) >= 2:
            return int(size[0]), int(size[1])
        return None

    def screen_points(self) -> tuple[float, float]:
        '**Point** size of the main display.'
        main = self._main_display()
        if main is None:
            raise OpenDeviceError('the display info has no built-in screen')
        size = main.get("nativeSize") or (main.get("currentMode") or {}).get("size") or []
        scale = float(main.get("pointScale")
                      or (main.get("currentMode") or {}).get("preferredUIScale") or 1) or 1
        if len(size) >= 2 and size[0] and size[1]:
            return float(size[0]) / scale, float(size[1]) / scale
        raise OpenDeviceError('cannot read the main screen size')

    def display_rotation(self, refresh: bool = False) -> str:
        '**Current** main display rotation: ``rot0`` / ``rot90`` / ``rot180`` / ``rot270``.'
        if refresh:
            self._display = None
        main = self._main_display()
        return str((main or {}).get("currentOrientation") or "rot0")

    def cursor_rotation(self) -> str:
        'Display orientation used for folding the cursor.'
        now = time.monotonic()
        if now - self._rotation_at < ROTATION_TTL:
            return self._rotation_value
        try:
            self._rotation_value = self.display_rotation(refresh=True)
        except Exception:                          # noqa
            self._rotation_value = "rot0"
        self._rotation_at = now
        return self._rotation_value

    def _panel_to_display(self, x: float, y: float) -> tuple[float, float]:
        'Normalized panel space -> normalized display space.'
        rotation = self.cursor_rotation()
        if rotation == "rot90":
            return 1.0 - y, x
        if rotation == "rot270":
            return y, 1.0 - x
        if rotation == "rot180":
            return 1.0 - x, 1.0 - y
        return x, y

    def _main_display(self) -> Optional[dict[str, Any]]:
        displays = self.display_info().get("displays") or []
        for display in displays:
            if display.get("primary") and not display.get("external"):
                return display
        for display in displays:
            if not display.get("external"):
                return display
        return None

    def describe(self) -> str:
        'One-line summary.'
        if not self.connected:
            return 'not connected'
        tunnel = str(self.info) if self.info is not None else ""
        return (f"{self.product_type or '?'} iOS {self.product_version or '?'}  "
                f"{self.screen_px or '?'}\n  {tunnel}")
