from __future__ import annotations

from typing import Any, Optional, TypeVar

from ..core.errors import OpenDeviceError
from ..remote.remotexpc import RemoteXPCConnection
from ..remote.rsd import RSD

SERVICE_PASTEBOARD = "com.apple.coredevice.pasteboardservice"
GENERAL = "general"

TEXT_UTIS: list[str] = [
    "public.utf8-plain-text",
    "public.plain-text",
    "public.text",
]

_PasteboardT = TypeVar("_PasteboardT", bound="PasteboardClient")


class PasteboardClient:
    'Device pasteboard client.'

    def __init__(self, rsd: RSD, service_name: str = SERVICE_PASTEBOARD) -> None:
        self.rsd = rsd
        self.service_name = service_name
        self.xpc: Optional[RemoteXPCConnection] = None

    def connect(self: _PasteboardT) -> _PasteboardT:
        port = self.rsd.service_port(self.service_name)
        self.xpc = RemoteXPCConnection(self.rsd.stack.connect(port))
        self.xpc.handshake()
        return self

    def close(self) -> None:
        if self.xpc is not None:
            self.xpc.close()
            self.xpc = None

    def __enter__(self: _PasteboardT) -> _PasteboardT:
        return self.connect()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _command(self, payload: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
        if self.xpc is None:
            self.connect()
        if self.xpc is None:
            raise OpenDeviceError('pasteboard connection was not established (connect() failed)')
        try:
            reply = self.xpc.send_receive(payload, timeout)
        except Exception:                       # noqa
            self.close()
            self.connect()
            if self.xpc is None:
                raise OpenDeviceError('pasteboard reconnect failed (the device link is gone)')
            reply = self.xpc.send_receive(payload, timeout)
        if not isinstance(reply, dict):
            raise ValueError(f"pasteboard returned a non-dict: {reply!r}")
        return reply

    def set_text(self, text: str, pasteboard_name: str = GENERAL) -> dict[str, Any]:
        'Write ``text`` into the device pasteboard and return its ``SET_REPLY``.'
        raw = text.encode("utf-8")
        items = [{
            "types": list(TEXT_UTIS),
            "data": {uti: {"data": raw} for uti in TEXT_UTIS},
        }]
        return self._command({
            "command": "SET",
            "pasteboardName": pasteboard_name,
            "items": items,
            "sourceMetadata": None,
        })

    def get_text(self, pasteboard_name: str = GENERAL) -> str:
        'Read the text back out of the pasteboard (for verification).'
        reply = self._command({
            "command": "PULL",
            "pasteboardName": pasteboard_name,
            "dataPolicy": {"promiseSecondary": {}},
        })
        items = (reply.get("pasteboard") or {}).get("items") or []
        for item in items:
            for uti in TEXT_UTIS:
                raw = ((item.get("data") or {}).get(uti) or {}).get("data")
                if raw:
                    if isinstance(raw, bytes):
                        return raw.decode("utf-8", "replace")
                    return str(raw)
        return ""
