from __future__ import annotations

import time

from ..ax import ax as ax_module
from ..core.errors import OpenDeviceError
from ..input import hid as hid_module
from ..input import pasteboard as pasteboard_module


class ControlsMixin:
    'Input injection (part of ``DeviceSession``; do not instantiate alone).'

    def _require_stream(self, what: str) -> None:
        'Touch / keyboard need a running stream.'
        if self.receiver is None:
            raise OpenDeviceError(
                f"{what} needs a running media stream (they share the auth gate); "
                "call start_stream() first"
            )

    @property
    def touch(self) -> hid_module.UniversalHID:
        'Touch / keyboard client (created lazily).'
        if self._hid is None:
            self._hid = hid_module.UniversalHID(self._require_rsd()).connect()
        return self._hid

    @property
    def board(self) -> pasteboard_module.PasteboardClient:
        'Pasteboard client (created lazily).'
        if self._board is None:
            self._board = pasteboard_module.PasteboardClient(self._require_rsd()).connect()
        return self._board

    def tap(self, x: float, y: float) -> None:
        'Tap. ``x`` / ``y`` are normalized 0..1 coordinates.'
        self._require_stream('touch')
        self.touch.tap(x, y)

    def move(self, x: float, y: float) -> None:
        'Move the pointer only (hover), no press. Same coordinates as :meth:`move_cursor` (panel space).'
        self._require_stream('pointer hover')
        self.touch.move(*self._panel_to_display(x, y))

    def touch_event(self, phase: str, x: float, y: float) -> None:
        'One step of a **live touch sequence**.'
        self._require_stream('touch')
        phase = (phase or "move").strip().lower()
        if phase == "up":
            self.touch.touch_up(hid_module.clamp01(x), hid_module.clamp01(y))
        else:
            self.touch.touch_down(hid_module.clamp01(x), hid_module.clamp01(y))

    def move_cursor(self, x: float, y: float) -> None:
        'Move the **on-device cursor** (the grey dot on the real screen) to normalized (x, y).'
        self._require_stream('cursor')
        self.touch.move_cursor(*self._panel_to_display(x, y))

    def swipe(self, start: tuple[float, float], end: tuple[float, float],
              steps: int = 16, duration: float = 0.35) -> None:
        'Swipe.'
        self._require_stream('touch')
        self.touch.swipe(start, end, steps=steps, duration=duration)

    def touch_down(self, x: float, y: float) -> None:
        self._require_stream('touch')
        self.touch.touch_down(x, y)

    def touch_up(self, x: float, y: float) -> None:
        self._require_stream('touch')
        self.touch.touch_up(x, y)

    def press_button(self, name: str = "home", hold: float = 0.08) -> tuple[int, int]:
        'Press a hardware button (``home`` etc.). Does **not** need a stream.'
        if self._indigo is None:
            self._indigo = hid_module.IndigoHID(self._require_rsd()).connect()
        return self._indigo.press(name, hold=hold)

    def type_text(self, text: str, delay: float = 0.02) -> int:
        'Type character by character (ASCII only).'
        self._require_stream('keyboard')
        return self.touch.type_text(text, delay=delay)

    def press_key(self, name: str) -> bool:
        'Press a function key (``enter`` / ``backspace`` / ...).'
        self._require_stream('keyboard')
        return self.touch.press(name)

    def paste(self) -> None:
        'Send Cmd+V once (does not touch the pasteboard).'
        self._require_stream('keyboard')
        self.touch.paste()

    def send_text(self, text: str, approve: bool = True,
                  pasteboard_name: str = pasteboard_module.GENERAL,
                  verify: bool = False) -> str:
        'Send a whole string into the focused input field (any script, emoji included).'
        self._require_stream('text input')
        self.board.set_text(text, pasteboard_name)
        back = self.board.get_text(pasteboard_name) if verify else ""

        time.sleep(0.4)
        self.touch.paste()
        if approve:
            time.sleep(0.5)
            self.approve_paste()
        return back

    def approve_paste(self, attempts: int = 4) -> bool:
        'Dismiss the iOS "Allow Paste" prompt.'
        for _ in range(attempts):
            try:
                elements = self.elements()
            except Exception:                      # noqa: BLE001
                time.sleep(0.5)
                continue
            target = (ax_module.find(elements, "Allow Paste")
                      or ax_module.find(elements, "Paste"))
            if target is not None and target.center is not None:
                return self.tap_element(target)
            time.sleep(0.7)
        return False
