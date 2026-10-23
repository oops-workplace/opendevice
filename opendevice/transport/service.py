from __future__ import annotations

import socket
from typing import Any, Optional

from ..core import plistio
from ..core.errors import ServiceError


class ServiceConnection:
    'A plist service connection, optionally over TLS.'

    def __init__(self, sock: Any, name: str = "") -> None:
        self.sock = sock
        self.name = name

    @classmethod
    def connect(cls, sock: socket.socket, ssl_context=None, name: str = "",
                timeout: float = 10.0) -> "ServiceConnection":
        try:
            sock.settimeout(timeout)
            if ssl_context is not None:
                sock = ssl_context.wrap_socket(sock)
        except BaseException:
            try:
                sock.close()
            except OSError:
                pass
            raise
        return cls(sock, name)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "ServiceConnection":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def send(self, obj: Any) -> None:
        plistio.send_plist(self.sock, obj)

    def recv(self) -> Any:
        return plistio.recv_plist(self.sock)

    def request(self, obj: Any, timeout: Optional[float] = None) -> Any:
        if timeout is not None:
            self.sock.settimeout(timeout)
        self.send(obj)
        return self.recv()

    def check(self, response: Any, expect: str) -> dict[str, Any]:
        'Validate the response.'
        if not isinstance(response, dict):
            raise ServiceError(f"{self.name}: response is not a dict: {response!r}")
        error = response.get("Error")
        if error:
            raise ServiceError(f"{self.name}: {error}")
        if expect and response.get("Request") != expect:
            raise ServiceError(f"{self.name}: expected {expect}, got {response.get('Request')!r}")
        return response


class IpStackSocket:
    'Wrap :class:`~opendevice.remote.ipstack.TcpConnection` to present a socket-like interface.'

    def __init__(self, conn: Any, timeout: float = 10.0) -> None:
        self.conn = conn
        self.timeout = timeout
        self._buffer = bytearray()

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def sendall(self, data: bytes) -> None:
        self.conn.send(data)

    def recv(self, size: int) -> bytes:
        while len(self._buffer) < size and not self.conn.closed:
            chunk = self.conn.recv(min(0.5, max(0.05, self.timeout)))
            if not chunk:
                break
            self._buffer += chunk
        if not self._buffer:
            return b""
        data = bytes(self._buffer[:size])
        del self._buffer[:len(data)]
        return data

    def close(self) -> None:
        self.conn.close()


def connect_rsd(rsd: Any, name: str, timeout: float = 10.0) -> ServiceConnection:
    'A lockdown-style service over RSD: connect the port, then run the RSDCheckin handshake (see docs/PROTOCOL.md 4).'
    conn = rsd.stack.connect(rsd.service_port(name), timeout=timeout)
    connection = ServiceConnection(IpStackSocket(conn, timeout), name)
    try:
        response = connection.request({
            "Label": "opendevice", "ProtocolVersion": "2", "Request": "RSDCheckin",
        })
        if not isinstance(response, dict) or response.get("Request") != "RSDCheckin":
            raise ServiceError(f"{name}: bad RSDCheckin ack: {response!r}")
        response = connection.recv()
        if not isinstance(response, dict) or response.get("Request") != "StartService":
            raise ServiceError(f"{name}: bad StartService ack: {response!r}")
        if response.get("Error"):
            raise ServiceError(f"{name}: {response['Error']}")
    except BaseException:
        connection.close()
        raise
    return connection


def open_service(lockdown: Any = None, rsd: Any = None, name: str = "",
                 rsd_name: Optional[str] = None) -> ServiceConnection:
    'Open a service over the available link: USB whenever lockdown exists.'
    if lockdown is not None:
        return lockdown.start_service(name)
    if rsd is not None and rsd_name:
        return connect_rsd(rsd, rsd_name)
    if rsd is not None:
        raise ServiceError(f"{name} has no RSD variant; this service is USB-only")
    raise ServiceError(f"{name}: neither lockdown nor RSD is available")
