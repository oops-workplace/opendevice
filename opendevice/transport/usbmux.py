from __future__ import annotations

import os
import plistlib
import socket
import struct
import sys
from dataclasses import dataclass
from typing import Any, Optional

from ..core import log as log_module
from ..core.errors import MuxError

VERSION_PLIST = 1
MSG_RESULT = 1
MSG_CONNECT = 2
MSG_PLIST = 8

RESULT_OK = 0

ITUNES_HOST = ("127.0.0.1", 27015)

MUX_SOCKET = "/var/run/usbmuxd"
DARWIN_SOCKET = MUX_SOCKET

LOCKDOWN_PORT = 62078


@dataclass
class MuxDevice:
    'A device attached to this host.'
    devid: int
    udid: str
    connection_type: str = ""
    properties: Optional[dict[str, Any]] = None

    def __str__(self) -> str:
        return f"{self.udid} ({self.connection_type}) devid={self.devid}"

    @property
    def is_usb(self) -> bool:
        'Whether it is attached over USB (``ConnectionType == "USB"``).'
        return str(self.connection_type).upper() == "USB"

    def matches_udid(self, udid: Optional[str]) -> bool:
        'Whether this is the given device.'
        return not udid or self.udid.lower() == str(udid).lower()


def normalise_address(address: Any) -> Any:
    'Parse a ``"host:port"`` string into (host, port).'
    if isinstance(address, str) and ":" in address:
        host, _, port = address.rpartition(":")
        return (host, int(port))
    return address


def default_address() -> Any:
    'usbmuxd address: use the Unix socket on macOS / Linux when present.'
    if sys.platform in ("darwin", "linux") and os.path.exists(MUX_SOCKET):
        return MUX_SOCKET
    return ITUNES_HOST


class Mux:
    'A usbmux control connection.'

    def __init__(self, address: Any = None) -> None:
        self.log = log_module.get(__name__)
        self.address = normalise_address(address if address is not None
                                         else default_address())
        self._sock = self._open()
        self._tag = 1
        self.log.debug('connected to usbmuxd %s', self.address)

    def _open(self) -> socket.socket:
        family = socket.AF_UNIX if isinstance(self.address, str) else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(10.0)
            sock.connect(self.address)
        except BaseException:
            sock.close()
            raise
        return sock

    def _send(self, message: int, payload: bytes = b"") -> None:
        body = struct.pack("<III", VERSION_PLIST, message, self._tag) + payload
        self._sock.sendall((len(body) + 4).to_bytes(4, "little") + body)
        self._tag += 1

    def _recv_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self._sock.recv(size - len(data))
            if not chunk:
                raise MuxError('usbmux connection closed')
            data += chunk
        return bytes(data)

    def _recv(self) -> tuple[int, bytes]:
        size = struct.unpack("<L", self._recv_exact(4))[0]
        if size < 12:
            raise MuxError(f"bad usbmux frame length: {size}")
        body = self._recv_exact(size - 4)
        version, message, _tag = struct.unpack("<III", body[:12])
        return message, body[12:]

    def _plist_request(self, obj: dict[str, Any]) -> dict[str, Any]:
        self.log.debug("→ %s", log_module.redact(obj))
        self._send(MSG_PLIST, plistlib.dumps(obj, fmt=plistlib.FMT_XML))
        message, payload = self._recv()
        if message != MSG_PLIST:
            raise MuxError(f"expected a PLIST response, got message={message}")
        response = plistlib.loads(payload) if payload else {}
        self.log.debug("← %s", log_module.redact(response))
        return response

    def read_buid(self) -> str:
        return str(self._plist_request({"MessageType": "ReadBUID"}).get("BUID", ""))

    def read_pair_record(self, udid: str) -> bytes:
        "Ask usbmuxd for this device's pairing record (``ReadPairRecord``), returning raw plist bytes."
        response = self._plist_request({
            "MessageType": "ReadPairRecord",
            "PairRecordID": udid,
        })
        return response.get("PairRecordData", b"") or b""

    def list_devices(self) -> list[MuxDevice]:
        response = self._plist_request({"MessageType": "ListDevices"})
        devices = []
        for item in response.get("DeviceList", []) or []:
            props = item.get("Properties", {}) or {}
            devices.append(MuxDevice(
                devid=int(item.get("DeviceID", 0)),
                udid=str(props.get("SerialNumber", "")),
                connection_type=str(props.get("ConnectionType", "")),
                properties=props,
            ))
        return devices

    def connect(self, devid: int, port: int) -> socket.socket:
        'Turn this control connection into a data channel to the device port and return the socket.'
        response = self._plist_request({
            "MessageType": "Connect",
            "DeviceID": devid,
            "PortNumber": socket.htons(port),
        })
        number = int(response.get("Number", -1))
        if number != RESULT_OK:
            raise MuxError(f"connect to device port {port} failed, Number={number}")
        self._sock.settimeout(None)
        return self._sock

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "Mux":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def list_devices(address: Any = None) -> list[MuxDevice]:
    with Mux(address) as mux:
        return mux.list_devices()


def connect_port(devid: int, port: int, address: Any = None) -> socket.socket:
    'Open a fresh usbmux connection to a device port (CONNECT is terminal, so one connection per call).'
    mux = Mux(address)
    try:
        return mux.connect(devid, port)
    except BaseException:
        mux.close()
        raise
