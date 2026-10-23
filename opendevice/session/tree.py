from __future__ import annotations

import time
from typing import Optional

from ..ax import ax as ax_module
from ..ax.ax import AXElement


class TreeMixin:
    'Control tree (part of ``DeviceSession``; do not instantiate alone).'

    def elements(self, timeout: float = 15.0, attempts: int = 3) -> list[AXElement]:
        "Fetch the current screen's controls (label + exact frame)."
        usable: list[AXElement] = []
        best: list[AXElement] = []
        for attempt in range(attempts):
            wedged = False
            try:
                with ax_module.AXClient(self._require_rsd()) as client:
                    usable = [el for el in client.fetch(timeout) if el.frame]
                    self.ax_last_len = client.last_len
                    wedged = bool(client.stale)
            except Exception:                      # noqa: BLE001
                usable = []
            if wedged:
                self._ax_wedged = True
            if usable:
                best = usable
                if not wedged:
                    self._ax_wedged = False
                # A lone window element is the transitional dump some apps push first.
                if not (len(usable) == 1 and getattr(usable[0], "kind_code", None) == 1):
                    if self.on_status:
                        self.on_status(f"controls{len(usable)}")
                    return usable
            if wedged:
                break
            if attempt + 1 < attempts:
                time.sleep(0.3)
        return best or usable

    def find(self, text: str, exact: bool = False) -> Optional[AXElement]:
        'Find a control by text (fetches again each time).'
        return ax_module.find(self.elements(), text, exact=exact)

    def tap_element(self, element: AXElement) -> bool:
        'Tap the center of a control (points are converted to 0..1).'
        center = element.center
        if center is None:
            return False
        width, height = self.screen_points()
        self.tap(center[0] / width, center[1] / height)
        return True

    def tap_text(self, text: str, exact: bool = False) -> bool:
        'Find a control by text and tap it (``False`` when not found).'
        element = self.find(text, exact=exact)
        if element is None:
            return False
        return self.tap_element(element)
