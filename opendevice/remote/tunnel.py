from __future__ import annotations

import json
import socket
import struct
from dataclasses import dataclass
from typing import Any, Optional

from ..core import log as log_module
from ..core.errors import TunnelError
from ..transport.lockdown import LockdownClient
from ..transport.service import ServiceConnection

log = log_module.get(__name__)

SERVICE = "com.apple.internal.devicecompute.CoreDeviceProxy"
MAGIC = b"CDTunnel"
LOOPBACK_HEADER = b"\x00\x00\x86\xdd"
IPV6_HEADER = 40
REQUESTED_MTU = 16000

RESULT_KEY = "type"


@dataclass
class TunnelInfo:
    'Handshake result.'
    host_address: str
    device_address: str
    rsd_port: int
    mtu: int
    raw: dict[str, Any]

    def __str__(self) -> str:
        return (f"Tunnel device {self.device_address} <-> host {self.host_address}  "
                f"mtu={self.mtu}  RSD port {self.rsd_port}")


class SocketTunnel:
    'A tunnel that runs over a socket / TLS stream.'

    def __init__(self) -> None:
        self._sock: Optional[socket.socket] = None
        self.info: Optional[TunnelInfo] = None

    def _sock_or_raise(self) -> socket.socket:
        'The tunnel socket.'
        if self._sock is None:
            raise TunnelError('tunnel has no socket yet')
        return self._sock

    def send_packet(self, packet: bytes) -> None:
        self._sock_or_raise().sendall(packet)

    def recv_packet(self) -> bytes:
        sock = self._sock_or_raise()
        head = _recv_exact(sock, 4)
        if head == LOOPBACK_HEADER:
            head = _recv_exact(sock, 4)
        if (head[0] >> 4) != 6:
            raise TunnelError(f"not an IPv6 packet: {head.hex()}")
        header = head + _recv_exact(sock, IPV6_HEADER - 4)
        length = struct.unpack(">H", header[4:6])[0]
        return header + _recv_exact(sock, length)

    def set_timeout(self, timeout: Optional[float]) -> None:
        'Set the socket timeout.'
        if self._sock is not None:
            self._sock.settimeout(timeout)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


class Tunnel(SocketTunnel):
    'A **USB** tunnel (CoreDeviceProxy).'

    def __init__(self, lockdown: LockdownClient) -> None:
        super().__init__()
        self.lockdown = lockdown
        self.service: Optional[ServiceConnection] = None

    def start(self) -> TunnelInfo:
        self.service = self.lockdown.start_service(SERVICE)
        self._sock = self.service.sock
        self.set_timeout(15.0)
        sock = self._require_socket()
        sock.sendall(_packet({"type": "clientHandshakeRequest", "mtu": REQUESTED_MTU}))
        response = json.loads(_read_packet(sock))
        if response.get(RESULT_KEY) not in ("serverHandshakeResponse", None):
            raise TunnelError(f"unexpected tunnel handshake: {response}")
        self.info = _info_from_handshake(response)
        log.info('USB tunnel ready: %s <-> %s  mtu=%d  RSD port %d',
                 self.info.host_address, self.info.device_address,
                 self.info.mtu, self.info.rsd_port)
        return self.info

    def _require_socket(self) -> socket.socket:
        if self._sock is None:
            raise TunnelError('tunnel has not been started yet')
        return self._sock

    def close(self) -> None:
        service, self.service = self.service, None
        self._sock = None
        if service is not None:
            try:
                service.close()
            except Exception:                 # noqa: BLE001
                pass


class WifiTunnel(SocketTunnel):
    'A **Wi-Fi** tunnel (RemotePairing).'

    def __init__(self, sock: socket.socket, info: TunnelInfo) -> None:
        super().__init__()
        self._sock = sock
        self.info = info


def _info_from_handshake(response: dict[str, Any]) -> TunnelInfo:
    'Read tunnel parameters from the ``CDTunnel`` clientHandshakeResponse.'
    client = response.get("clientParameters") or {}
    info = TunnelInfo(
        host_address=str(client.get("address", "")),
        device_address=str(response.get("serverAddress", "")),
        rsd_port=int(response.get("serverRSDPort", 0)),
        mtu=int(client.get("mtu", REQUESTED_MTU)),
        raw=response,
    )
    if not info.host_address or not info.rsd_port:
        raise TunnelError(f"tunnel handshake is missing fields: {response}")
    return info


def _packet(obj: dict[str, Any]) -> bytes:
    body = json.dumps(obj).encode()
    return MAGIC + len(body).to_bytes(2, "big") + body


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise TunnelError('tunnel connection closed')
        data += chunk
    return bytes(data)


def _read_packet(sock: socket.socket) -> bytes:
    'Read the JSON body of one CDTunnel control message.'
    head = _recv_exact(sock, len(MAGIC))
    if head != MAGIC:
        raise TunnelError(f"not a CDTunnel message: {head!r}")
    size = int.from_bytes(_recv_exact(sock, 2), "big")
    return _recv_exact(sock, size)


def handshake_cdtunnel(sock: socket.socket, mtu: int = REQUESTED_MTU) -> TunnelInfo:
    'Run the CDTunnel handshake over an already connected stream (used by Wi-Fi).'
    sock.sendall(_packet({"type": "clientHandshakeRequest", "mtu": mtu}))
    response = json.loads(_read_packet(sock))
    if response.get(RESULT_KEY) not in ("serverHandshakeResponse", None):
        raise TunnelError(f"unexpected tunnel handshake: {response}")
    return _info_from_handshake(response)


@dataclass
class TunnelHandle:
    'A live tunnel: use ``.rsd``; :meth:`close` tears the link down in a fixed order.'

    rsd: Any
    tunnel: Optional[Any] = None
    lockdown: Optional[LockdownClient] = None
    backend: str = "userspace"

    def close(self) -> None:
        for step in (lambda: self.rsd.close() if self.rsd is not None else None,
                     lambda: self.tunnel.close() if self.tunnel is not None else None,
                     lambda: getattr(self.rsd, "stack", None) and self.rsd.stack.close(),
                     lambda: self.lockdown.close() if self.lockdown is not None else None):
            try:
                step()
            except Exception:                   # noqa
                pass
        self.rsd = self.tunnel = self.lockdown = None


def _close_all(rsd: Any = None, tunnel: Any = None, lockdown: Any = None) -> None:
    'Clean up a half-built link after a **mid-way** failure (same order as :meth:`TunnelHandle.close`).'
    for closer in (rsd, tunnel,
                   getattr(rsd, "stack", None) if rsd is not None else None,
                   lockdown):
        if closer is None:
            continue
        try:
            closer.close()
        except Exception:                   # noqa
            pass


def open_rsd(udid: Optional[str] = None, backend: str = "auto",
             mux_address: Any = None, timeout: float = 120.0) -> TunnelHandle:
    'Open a tunnel and connect RSD.'
    from .rsd import RSD

    backend = "userspace" if backend == "auto" else backend

    if backend == "userspace":
        lockdown = LockdownClient(udid, mux_address)
        tunnel: Any = None
        rsd: Any = None
        try:
            lockdown.start_session()
            tunnel = Tunnel(lockdown)
            info = tunnel.start()
            rsd = RSD(tunnel, info, threaded=True)
            rsd.connect()
        except BaseException:
            _close_all(rsd, tunnel, lockdown)
            raise
        return TunnelHandle(rsd=rsd, tunnel=tunnel, lockdown=lockdown, backend=backend)

    if backend == "network":
        try:
            from ..wireless.wifi import open_wifi_tunnel
        except ImportError as exc:
            raise TunnelError(
                "the Wi-Fi link needs cryptography + zeroconf (USB does not):\n"
                "  pip install cryptography zeroconf\n"
                f"(original error: {exc})"
            ) from exc
        tunnel: Any = None
        rsd: Any = None
        try:
            tunnel, info = open_wifi_tunnel(udid=udid, timeout=timeout)
            rsd = RSD(tunnel, info, threaded=True)
            rsd.connect()
        except BaseException:
            _close_all(rsd, tunnel, None)
            raise
        return TunnelHandle(rsd=rsd, tunnel=tunnel, lockdown=None, backend=backend)

    raise TunnelError(f"Tunnel backend {backend!r} does not exist; use \'userspace\' (USB) or \'network\' (Wi-Fi)")
