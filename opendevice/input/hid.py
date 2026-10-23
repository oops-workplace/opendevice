from __future__ import annotations

import struct
import threading
import time
from typing import Any, Optional, TypeVar

from ..core.errors import OpenDeviceError
from ..remote.remotexpc import RemoteXPCConnection
from ..remote.rsd import RSD

SERVICE_UNIVERSAL_HID = "com.apple.coredevice.hid.universalhidservice"
SERVICE_INDIGO = "com.apple.coredevice.hid.indigo"

FEATURE_UNIVERSAL_HID = "com.apple.coredevice.feature.remote.universalhidservice"
FEATURE_HID_BUTTON = "com.apple.coredevice.feature.remote.hid.button"

HANDLE_TOUCHSCREEN = 257
HANDLE_VIRTUAL_KEYBOARD = 512
HANDLE_SCREEN_BUTTONS = 1026
HANDLE_GESTURE_CURSOR = 1281

COORD_MAX = 0xFFFF
FP = 65536.0


MACH_TICKS_PER_SEC = 24_000_000


def _timestamp() -> int:
    'Device-side timestamp: **Mach ticks**, not nanoseconds.'
    return time.monotonic_ns() * MACH_TICKS_PER_SEC // 1_000_000_000


def touch_report(x: float, y: float, down: bool = True,
                 timestamp: Optional[int] = None) -> bytes:
    '58-byte touch report. ``x`` / ``y`` are normalized 0..1 coordinates.'
    payload = bytearray(58)
    payload[0] = 0x09
    payload[1] = 0x01
    payload[2] = 0x05
    payload[3] = 0xC2 if down else 0x02
    struct.pack_into("<H", payload, 4, max(0, min(COORD_MAX, int(round(x * COORD_MAX)))))
    struct.pack_into("<H", payload, 6, max(0, min(COORD_MAX, int(round(y * COORD_MAX)))))
    payload[40] = 0x02
    struct.pack_into("<Q", payload, 45, _timestamp() if timestamp is None else timestamp)
    return bytes(payload)


def clamp01(value: float) -> float:
    'Clamp to [0, 1]: out-of-range values wrap in 16.16 fixed point (the cursor jumps around).'
    value = float(value)
    if value != value:
        return 0.0
    return 0.0 if value <= 0 else (1.0 if value >= 1 else value)


_clamp01 = clamp01


def pointer_report(x: float, y: float, timestamp: Optional[int] = None) -> bytes:
    '19-byte pointer (hover) report.'
    fixed_x = int(round(x * FP)) & 0xFFFFFFFF
    fixed_y = int(round(y * FP)) & 0xFFFFFFFF
    return struct.pack("<BIIH Q", 0x13, fixed_x, fixed_y, 0x0000,
                       _timestamp() if timestamp is None else timestamp)


def keyboard_report(usages: Any = (), timestamp: Optional[int] = None) -> bytes:
    '39-byte keyboard report.'
    bitmap = bytearray(30)
    for usage in usages:
        if 0 <= usage < 240:
            bitmap[usage // 8] |= 1 << (usage % 8)
    stamp = (time.monotonic_ns() & ((1 << 48) - 1)) if timestamp is None else timestamp
    return bytes([0x01]) + bytes(bitmap) + stamp.to_bytes(6, "little") + b"\x00\x00"


def parse_touch_report(report: bytes) -> dict[str, Any]:
    'Decode a 58-byte touch report back into fields (for debugging).'
    if len(report) != 58:
        raise ValueError(f"touch report should be 58 bytes, got {len(report)}")
    x, y = struct.unpack("<HH", report[4:8])
    return {
        "down": report[3] == 0xC2,
        "x": x / COORD_MAX,
        "y": y / COORD_MAX,
        "timestamp": struct.unpack("<Q", report[45:53])[0],
    }


KEY_A, KEY_Z = 0x04, 0x1D
KEY_1, KEY_0 = 0x1E, 0x27
KEY_ENTER, KEY_ESC, KEY_BACKSPACE, KEY_TAB, KEY_SPACE = 0x28, 0x29, 0x2A, 0x2B, 0x2C
KEY_MINUS, KEY_EQUAL, KEY_LBRACKET, KEY_RBRACKET = 0x2D, 0x2E, 0x2F, 0x30
KEY_BACKSLASH, KEY_SEMICOLON, KEY_APOSTROPHE = 0x31, 0x33, 0x34
KEY_GRAVE, KEY_COMMA, KEY_DOT, KEY_SLASH = 0x35, 0x36, 0x37, 0x38
KEY_RIGHT, KEY_LEFT, KEY_DOWN, KEY_UP = 0x4F, 0x50, 0x51, 0x52
KEY_LEFT_SHIFT, KEY_LEFT_GUI = 0xE1, 0xE3

SPECIAL_KEYS: dict[str, int] = {
    "enter": KEY_ENTER, "return": KEY_ENTER,
    "backspace": KEY_BACKSPACE, "delete": KEY_BACKSPACE,
    "tab": KEY_TAB, "space": KEY_SPACE, "esc": KEY_ESC, "escape": KEY_ESC,
    "up": KEY_UP, "down": KEY_DOWN, "left": KEY_LEFT, "right": KEY_RIGHT,
}


def _build_ascii_map() -> dict[str, tuple[int, bool]]:
    'Character -> (usage, needs shift). US-layout ASCII only; other characters require the pasteboard.'
    table: dict[str, tuple[int, bool]] = {}
    for index, char in enumerate("abcdefghijklmnopqrstuvwxyz"):
        usage = KEY_A + index
        table[char] = (usage, False)
        table[char.upper()] = (usage, True)
    for index, char in enumerate("1234567890"):
        table[char] = (KEY_1 + index, False)
    for char, usage in (("!", KEY_1), ("@", KEY_1 + 1), ("#", KEY_1 + 2),
                        ("$", KEY_1 + 3), ("%", KEY_1 + 4), ("^", KEY_1 + 5),
                        ("&", KEY_1 + 6), ("*", KEY_1 + 7), ("(", KEY_1 + 8),
                        (")", KEY_1 + 9)):
        table[char] = (usage, True)
    table.update({
        " ": (KEY_SPACE, False), "\t": (KEY_TAB, False), "\n": (KEY_ENTER, False),
        "-": (KEY_MINUS, False), "_": (KEY_MINUS, True),
        "=": (KEY_EQUAL, False), "+": (KEY_EQUAL, True),
        "[": (KEY_LBRACKET, False), "{": (KEY_LBRACKET, True),
        "]": (KEY_RBRACKET, False), "}": (KEY_RBRACKET, True),
        "\\": (KEY_BACKSLASH, False), "|": (KEY_BACKSLASH, True),
        ";": (KEY_SEMICOLON, False), ":": (KEY_SEMICOLON, True),
        "'": (KEY_APOSTROPHE, False), '"': (KEY_APOSTROPHE, True),
        "`": (KEY_GRAVE, False), "~": (KEY_GRAVE, True),
        ",": (KEY_COMMA, False), "<": (KEY_COMMA, True),
        ".": (KEY_DOT, False), ">": (KEY_DOT, True),
        "/": (KEY_SLASH, False), "?": (KEY_SLASH, True),
    })
    return table


ASCII_TO_HID = _build_ascii_map()

PAGE_CONSUMER = 12
BUTTONS: dict[str, tuple[int, int]] = {
    "home": (PAGE_CONSUMER, 64),
    "power": (PAGE_CONSUMER, 48),
    "volumeUp": (PAGE_CONSUMER, 233),
    "volumeDown": (PAGE_CONSUMER, 234),
    "mute": (PAGE_CONSUMER, 226),
}

LONG_PRESS: dict[str, tuple[str, float]] = {
    "siri": ("power", 1.5),
}

MIN_HOLD = 0.05

STATE_DOWN, STATE_UP = 1, 2


_ServiceT = TypeVar("_ServiceT", bound="_RemoteXPCService")


class _RemoteXPCService:
    'Open a RemoteXPC service connection by name (shared by both HID services).'

    SERVICE_NAME = ""

    def __init__(self, rsd: RSD, service_name: Optional[str] = None) -> None:
        self.rsd = rsd
        self.service_name = service_name or self.SERVICE_NAME
        self.xpc: Optional[RemoteXPCConnection] = None
        self._io_lock = threading.Lock()

    def connect(self: _ServiceT) -> _ServiceT:
        if not self.rsd.uses_remote_xpc(self.service_name):
            raise OpenDeviceError(f"{self.service_name} is not a RemoteXPC service")
        port = self.rsd.service_port(self.service_name)
        self.xpc = RemoteXPCConnection(self.rsd.stack.connect(port))
        self.xpc.handshake()
        return self

    def close(self) -> None:
        if self.xpc is not None:
            self.xpc.close()
            self.xpc = None

    def __enter__(self: _ServiceT) -> _ServiceT:
        return self.connect()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _send(self, message: dict[str, Any]) -> None:
        'Send one-way.'
        with self._io_lock:
            if self.xpc is None:
                self.connect()
            if self.xpc is None:
                raise OpenDeviceError('HID connection was not established (connect() failed)')
            try:
                self.xpc.send_request(message, wanting_reply=False)
                return
            except Exception:           # noqa
                self.close()
                self.connect()
            self.xpc.send_request(message, wanting_reply=False)

    def _try_send(self, message: dict[str, Any]) -> bool:
        'Try once and give up on failure (no reconnect, no raise).'
        if self.xpc is None:
            return False
        try:
            with self._io_lock:
                self.xpc.send_request(message, wanting_reply=False)
            return True
        except Exception:                 # noqa: BLE001
            return False


class UniversalHID(_RemoteXPCService):
    'universalhidservice.'

    SERVICE_NAME = SERVICE_UNIVERSAL_HID

    def __init__(self, rsd: RSD) -> None:
        super().__init__(rsd, SERVICE_UNIVERSAL_HID)
        self.touchscreen_id = HANDLE_TOUCHSCREEN
        self.keyboard_id = HANDLE_VIRTUAL_KEYBOARD
        self.services: dict[Any, dict[str, Any]] = {}
        self.cursor_min_interval = 0.03
        self._last_cursor_at = 0.0

    def _send_message(self, operation: str, arguments: dict[str, Any],
                      handle: Optional[int]) -> None:
        from ..remote import xpc

        self._send({
            "messageType": "Request",
            "payload": {operation: dict(arguments,
                                        _1=xpc.XpcUInt64Type(
                                            self.touchscreen_id if handle is None else handle))},
            "featureIdentifier": FEATURE_UNIVERSAL_HID,
        })

    def send(self, report: bytes, handle: Optional[int] = None) -> None:
        '``{"send": {"_0": report, "_1": handle}}``.'
        self._send_message("send", {"_0": report}, handle)

    def send_once(self, report: bytes, handle: Optional[int] = None) -> bool:
        'Try once and give up on failure (no reconnect, no raise).'
        from ..remote import xpc

        return self._try_send({
            "messageType": "Request",
            "payload": {"send": {"_0": report,
                                 "_1": xpc.XpcUInt64Type(
                                     self.touchscreen_id if handle is None else handle)}},
            "featureIdentifier": FEATURE_UNIVERSAL_HID,
        })

    def reset_gesture_state(self, handle: Optional[int] = None) -> None:
        from ..remote import xpc

        self._send({
            "messageType": "Request",
            "payload": {"resetGestureState": {"_0": xpc.XpcUInt64Type(
                self.touchscreen_id if handle is None else handle)}},
            "featureIdentifier": FEATURE_UNIVERSAL_HID,
        })

    def list_services(self, timeout: float = 10.0) -> dict[str, Any]:
        'Ask the device which HID surfaces it has.'
        probe = UniversalHID(self.rsd)
        try:
            probe.connect()
            if probe.xpc is None:
                raise OpenDeviceError('HID probe connection was not established (connect() failed)')
            response = probe.xpc.send_receive({
                "messageType": "Request",
                "payload": {"connectedServices": {}},
                "featureIdentifier": FEATURE_UNIVERSAL_HID,
            }, timeout)
        finally:
            probe.close()
        self.services = {}
        for item in response.get("connectedServices", []) or []:
            key = (item.get("PrimaryUsagePage"), item.get("PrimaryUsage"))
            self.services[key] = item
        touch = self.services.get((13, 4))
        if touch and touch.get("_ServiceID"):
            self.touchscreen_id = int(touch["_ServiceID"])
        for item in response.get("connectedServices", []) or []:
            product = str(item.get("Product") or "")
            if "keyboard" in product.lower():
                self.keyboard_id = int(item.get("_ServiceID") or HANDLE_VIRTUAL_KEYBOARD)
                break
        return response

    def move(self, x: float, y: float) -> None:
        'Report position only (hover), no press.'
        self.send(pointer_report(x, y))

    def move_cursor(self, x: float, y: float, throttle: bool = True) -> bool:
        'Drive only the **on-device cursor** (the grey dot on the real screen); no touch events.'
        now = time.monotonic()
        if throttle and now - self._last_cursor_at < self.cursor_min_interval:
            return False
        self._last_cursor_at = now
        return self.send_once(pointer_report(_clamp01(x), _clamp01(y)),
                              HANDLE_GESTURE_CURSOR)

    def touch_down(self, x: float, y: float, handle: Optional[int] = None) -> None:
        self.send(touch_report(x, y, True), handle)

    def touch_up(self, x: float, y: float, handle: Optional[int] = None) -> None:
        self.send(touch_report(x, y, False), handle)

    def tap(self, x: float, y: float, settle: float = 0.06) -> None:
        'Tap: press and release at the same position.'
        self.touch_down(x, y)
        time.sleep(settle)
        self.touch_up(x, y)
        self.reset_gesture_state()

    def swipe(self, start: tuple[float, float], end: tuple[float, float],
              steps: int = 16, duration: float = 0.35) -> None:
        'Swipe: a run of presses at advancing positions, then one release.'
        for index in range(steps + 1):
            ratio = index / steps
            self.touch_down(start[0] + (end[0] - start[0]) * ratio,
                            start[1] + (end[1] - start[1]) * ratio)
            time.sleep(duration / max(steps, 1))
        self.touch_up(end[0], end[1])
        self.reset_gesture_state()

    def send_usages(self, usages: Any = ()) -> None:
        'Send a keyboard report to the device keyboard surface (the report replaces the set of held keys).'
        self.send(keyboard_report(usages), self.keyboard_id)

    def send_keyboard(self, service_id: int, usages: Any = ()) -> None:
        'Send a keyboard report to a **specific** surface.'
        self.send(keyboard_report(usages), service_id)

    def list_connected_services(self, timeout: float = 10.0) -> dict[str, Any]:
        'Alias of ``list_services`` returning the raw response dict.'
        return self.list_services(timeout)

    def press_key(self, usage: int, shift: bool = False) -> None:
        'Press one usage (down + up).'
        self.send_usages([KEY_LEFT_SHIFT, usage] if shift else [usage])
        self.send_usages([])

    def press(self, name: str) -> bool:
        'Press a special key (names in :data:`SPECIAL_KEYS`).'
        usage = SPECIAL_KEYS.get(name.lower())
        if usage is None:
            return False
        self.press_key(usage)
        return True

    def type_text(self, text: str, delay: float = 0.02) -> int:
        'Type character by character; returns the number sent (non-ASCII characters are skipped).'
        if text.lower() in SPECIAL_KEYS:
            return 1 if self.press(text) else 0
        sent = 0
        for char in text:
            mapped = ASCII_TO_HID.get(char)
            if mapped is None:
                continue
            self.press_key(mapped[0], mapped[1])
            sent += 1
            if delay:
                time.sleep(delay)
        return sent

    def paste(self) -> None:
        'Send Cmd+V (**three separate reports**).'
        usage = ASCII_TO_HID.get("v")
        if usage is None:
            return
        self.send_usages([KEY_LEFT_GUI])
        time.sleep(0.12)
        self.send_usages([KEY_LEFT_GUI, usage[0]])
        time.sleep(0.12)
        self.send_usages([])


class IndigoHID(_RemoteXPCService):
    'hid.indigo.'

    SERVICE_NAME = SERVICE_INDIGO

    def __init__(self, rsd: RSD) -> None:
        super().__init__(rsd, SERVICE_INDIGO)

    def send_state(self, state: int, usage_page: int, usage_code: int) -> None:
        '``messageType`` is the **event type**, and all three integers must be UINT64.'
        from ..remote import xpc

        self._send({
            "messageType": "IndigoButtonEvent",
            "payload": {
                "state": xpc.XpcUInt64Type(state),
                "usagePage": xpc.XpcUInt64Type(usage_page),
                "usageCode": xpc.XpcUInt64Type(usage_code),
            },
            "featureIdentifier": FEATURE_HID_BUTTON,
        })

    def press(self, name: str = "home", hold: float = 0.08,
              settle: float = 0.35) -> tuple[int, int]:
        'Press one button.'
        if isinstance(name, str) and name in LONG_PRESS:
            name, minimum = LONG_PRESS[name]
            hold = max(hold, minimum)
        hold = max(hold, MIN_HOLD)
        if isinstance(name, str) and "," in name:
            left, right = name.split(",", 1)
            page, code = int(left), int(right)
        elif name not in BUTTONS:
            raise KeyError(f"unknown button {name!r}; known: {sorted(BUTTONS) + sorted(LONG_PRESS)}")
        else:
            page, code = BUTTONS[name]
        self.send_state(STATE_DOWN, page, code)
        time.sleep(hold)
        self.send_state(STATE_UP, page, code)
        time.sleep(settle)
        return page, code
