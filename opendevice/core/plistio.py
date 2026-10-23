from __future__ import annotations

import plistlib
import socket
from typing import Any, Optional

from . import log as log_module

log = log_module.get(__name__)


def sendall(sock: Any, data: bytes) -> None:
    'Send the whole buffer.'
    (getattr(sock, "sendall", None) or sock.send)(data)


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError(f"connection closed (still needed {size - len(data)} bytes)")
        data += chunk
    return bytes(data)


def send_plist(sock: socket.socket, obj: Any) -> None:
    body = plistlib.dumps(obj, fmt=plistlib.FMT_XML)
    sendall(sock, len(body).to_bytes(4, "big") + body)
    log.debug("→ %s", log_module.redact(obj))


def recv_plist(sock: socket.socket) -> Any:
    size = int.from_bytes(recv_exact(sock, 4), "big")
    value = plistlib.loads(recv_exact(sock, size)) if size else None
    log.debug("← %s", log_module.redact(value))
    return value


def request(sock: socket.socket, obj: Any, timeout: Optional[float] = None) -> Any:
    'Send one request and read one response.'
    if timeout is not None:
        sock.settimeout(timeout)
    send_plist(sock, obj)
    return recv_plist(sock)
