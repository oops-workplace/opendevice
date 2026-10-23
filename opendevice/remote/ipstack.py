from __future__ import annotations

import random
import socket
import struct
import threading
import time
from typing import Any, Optional

from ..core.errors import OpenDeviceError

FIN, SYN, RST, PSH, ACK = 0x01, 0x02, 0x04, 0x08, 0x10
IPV6_HEADER = 40
TCP_HEADER = 20

MAX_POOLED_PACKETS = 512


def _checksum(src: str, dst: str, segment: bytes, protocol: int = 6) -> int:
    'Standard checksum over the IPv6 pseudo header + segment (TCP=6 / UDP=17).'
    head = (socket.inet_pton(socket.AF_INET6, src)
            + socket.inet_pton(socket.AF_INET6, dst)
            + struct.pack(">II", len(segment), protocol))
    data = head + segment
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for index in range(0, len(data), 2):
        total += (data[index] << 8) | data[index + 1]
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def build_tcp(src_port: int, dst_port: int, seq: int, ack: int, flags: int,
              payload: bytes = b"", window: int = 65535,
              options: bytes = b"") -> bytes:
    offset = (TCP_HEADER + len(options)) // 4
    head = struct.pack(">HHIIBBHHH", src_port, dst_port, seq, ack,
                       offset << 4, flags, window, 0, 0)
    return head + options + payload


def build_segment(src_ip: str, dst_ip: str, src_port: int, dst_port: int,
                  seq: int, ack: int, flags: int, payload: bytes = b"",
                  window: int = 65535, options: bytes = b"") -> bytes:
    'A TCP segment with a correct checksum (it needs the IPv6 pseudo header, so both addresses are required).'
    segment = build_tcp(src_port, dst_port, seq, ack, flags, payload, window, options)
    checksum = _checksum(src_ip, dst_ip, segment)
    return segment[:16] + struct.pack(">H", checksum) + segment[18:]


def parse_tcp(segment: bytes) -> dict[str, Any]:
    (src_port, dst_port, seq, ack, offset, flags,
     window, _check, _urgent) = struct.unpack(">HHIIBBHHH", segment[:TCP_HEADER])
    data_offset = (offset >> 4) * 4
    return {
        "src_port": src_port, "dst_port": dst_port, "seq": seq, "ack": ack,
        "flags": flags, "window": window, "payload": segment[data_offset:],
    }


def build_udp(src_port: int, dst_port: int, payload: bytes,
              src_ip: str, dst_ip: str) -> bytes:
    'A UDP datagram with a correct checksum.'
    segment = struct.pack(">HHHH", src_port, dst_port, 8 + len(payload), 0) + payload
    checksum = _checksum(src_ip, dst_ip, segment, protocol=17)
    return segment[:6] + struct.pack(">H", checksum) + segment[8:]


def _udp_ports(packet: bytes) -> Optional[tuple[int, int]]:
    'Parse (source port, destination port) from a UDP packet inside an IPv6 packet.'
    if len(packet) < IPV6_HEADER + 8:
        return None
    if (packet[0] >> 4) != 6 or packet[6] != 17:
        return None
    src, dst = struct.unpack(">HH", packet[IPV6_HEADER:IPV6_HEADER + 4])
    return src, dst


class UdpEndpoint:
    'A UDP endpoint on the stack.'

    def __init__(self, stack: "IpStack", port: int = 0) -> None:
        self.stack = stack
        self.port = port or random.randint(40000, 60000)
        self._timeout = 1.0
        self._queue: list[tuple[bytes, Any]] = []

    def settimeout(self, seconds: float) -> None:
        self._timeout = seconds

    def recvfrom(self, bufsize: int = 8192):
        deadline = time.monotonic() + max(self._timeout, 0.01)
        while True:
            if self._queue:
                data, addr = self._queue.pop(0)
                return data[:bufsize], addr
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout('timed out waiting for a UDP packet')
            packet = self.stack.recv_packet(remaining, accept=self._wants)
            if packet is None:
                continue
            ports = _udp_ports(packet)
            if ports is None:
                continue
            self._queue.append((packet[IPV6_HEADER + 8:],
                                (self.stack.device_ip, ports[0])))

    def sendto(self, data: bytes, address) -> int:
        dst_ip, dst_port = address
        segment = build_udp(self.port, dst_port, data, self.stack.host_ip, dst_ip)
        self.stack.send_ipv6(dst_ip, segment, next_header=17)
        return len(data)

    def close(self) -> None:
        self._queue.clear()

    def _wants(self, packet: bytes) -> bool:
        ports = _udp_ports(packet)
        return ports is not None and ports[1] == self.port


class TcpConnection:
    'A TCP connection: connect, send (wait for ACK, retransmit on timeout), receive, close.'

    def __init__(self, stack: "IpStack", port: int, timeout: float = 8.0) -> None:
        self.stack = stack
        self.port = port
        self.timeout = timeout
        self.sport = random.randint(40000, 60000)
        self.dport = port
        self.seq = random.randint(0, 0xFFFFFFFF)
        self.peer_seq = 0
        self.peer_ack = 0
        self.rxbuf = bytearray()
        self.closed = False
        self.retransmits = 0

    def _segment(self, flags: int, payload: bytes = b"", options: bytes = b"") -> bytes:
        return build_segment(self.stack.host_ip, self.stack.device_ip,
                             self.sport, self.dport, self.seq, self.peer_seq,
                             flags, payload, options=options)

    def connect(self) -> "TcpConnection":
        mss = min(self.stack.mss, 1460)
        syn = self._segment(SYN, options=struct.pack("!BBH", 2, 4, mss))
        deadline = time.monotonic() + self.timeout
        attempts = 0
        while attempts < 6 and time.monotonic() < deadline:
            self.stack.send_ipv6(self.stack.device_ip, syn)
            end = time.monotonic() + 1.5
            while time.monotonic() < end:
                if self._pump(0.3):
                    if self.peer_seq and self.peer_ack == self.seq + 1:
                        self.seq += 1
                        self.stack.send_ipv6(self.stack.device_ip, self._ack_segment())
                        return self
                if self.closed:
                    raise OpenDeviceError('connection reset')
            attempts += 1
        raise OpenDeviceError(f"TCP handshake timed out (device {self.stack.device_ip}:{self.dport})")

    def send(self, data: bytes) -> None:
        'Send data: split by MSS, wait for an ACK per chunk, retransmit on timeout.'
        if not data:
            return
        limit = max(512, min(self.stack.mss, 16384))
        for offset in range(0, len(data), limit):
            self._send_chunk(data[offset:offset + limit])

    def _send_chunk(self, data: bytes) -> None:
        segment = self._segment(PSH | ACK, data)
        end_of_data = self.seq + len(data)
        for _attempt in range(6):
            if _attempt:
                self.retransmits += 1
            self.stack.send_ipv6(self.stack.device_ip, segment)
            end = time.monotonic() + 2.0
            while time.monotonic() < end:
                self._pump(0.3)
                if self.peer_ack >= end_of_data:
                    self.seq = end_of_data
                    return
                if self.closed:
                    raise OpenDeviceError('connection closed')
        raise OpenDeviceError('no ACK received for sent data')

    def recv(self, timeout: float = 2.0) -> bytes:
        'Receive one chunk of data.'
        end = time.monotonic() + timeout
        while not self.rxbuf and time.monotonic() < end:
            self._pump(0.2)
            if self.closed:
                break
        if not self.rxbuf:
            return b""
        data = bytes(self.rxbuf)
        self.rxbuf.clear()
        return data

    def recv_until(self, predicate, timeout: float = 15.0) -> bytes:
        'Receive until predicate(accumulated bytes) is true (used to wait for a full response).'
        buffer = bytearray()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            chunk = self.recv(0.5)
            if chunk:
                buffer += chunk
                if predicate(bytes(buffer)):
                    return bytes(buffer)
            if self.closed and not chunk:
                break
        return bytes(buffer)

    def close(self) -> None:
        if self.closed:
            return
        try:
            self.stack.send_ipv6(self.stack.device_ip, self._segment(FIN | ACK))
        except OpenDeviceError:
            pass
        self.closed = True

    def _ack_segment(self) -> bytes:
        return self._segment(ACK)

    def _wants(self, packet: bytes) -> bool:
        'Whether this IPv6 packet is a TCP segment of this connection.'
        if len(packet) < IPV6_HEADER + TCP_HEADER:
            return False
        if (packet[0] >> 4) != 6 or packet[6] != 6:
            return False
        payload_len = struct.unpack(">H", packet[4:6])[0]
        if len(packet) < IPV6_HEADER + payload_len:
            return False
        segment = packet[IPV6_HEADER:IPV6_HEADER + payload_len]
        src_port, dst_port = struct.unpack(">HH", segment[:4])
        return dst_port == self.sport and src_port == self.dport

    def _pump(self, timeout: float) -> bool:
        'Read one packet and handle it.'
        packet = self.stack.recv_packet(timeout, accept=self._wants)
        if packet is None:
            return False
        payload_len = struct.unpack(">H", packet[4:6])[0]
        segment = packet[IPV6_HEADER:IPV6_HEADER + payload_len]
        tcp = parse_tcp(segment)
        if tcp["flags"] & RST:
            self.closed = True
            return True
        if tcp["flags"] & ACK:
            self.peer_ack = tcp["ack"]
        if tcp["flags"] & SYN:
            self.peer_seq = tcp["seq"] + 1
        payload = tcp["payload"]
        if payload and tcp["seq"] >= self.peer_seq:
            self.rxbuf += payload
            self.peer_seq = tcp["seq"] + len(payload)
            self.stack.send_ipv6(self.stack.device_ip, self._ack_segment())
        if tcp["flags"] & FIN:
            self.closed = True
        return True


class IpStack:
    'Use the tunnel as a NIC: send and receive IPv6 packets, open TCP connections.'

    def __init__(self, tunnel, host_ip: str, device_ip: str, mtu: int = 16000,
                 threaded: bool = False) -> None:
        self.tunnel = tunnel
        self.host_ip = host_ip
        self.device_ip = device_ip
        self.mtu = mtu
        self.mss = mtu - IPV6_HEADER - TCP_HEADER
        self._pool: list[bytes] = []
        self._threaded = threaded
        self._closed = False
        self._cond = threading.Condition()
        self._send_lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        if threaded:
            self._reader = threading.Thread(target=self._reader_loop, daemon=True,
                                            name="opendevice-tunnel-rx")
            self._reader.start()

    def _reader_loop(self) -> None:
        'Reader thread that owns the socket.'
        try:
            self.tunnel.set_timeout(None)
        except OSError:
            return
        while not self._closed:
            try:
                packet = self.tunnel.recv_packet()
            except Exception:              # noqa
                break
            with self._cond:
                self._pool.append(packet)
                if len(self._pool) > MAX_POOLED_PACKETS:
                    del self._pool[:len(self._pool) - MAX_POOLED_PACKETS]
                self._cond.notify_all()
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def send_ipv6(self, dst: str, payload: bytes, next_header: int = 6) -> None:
        header = struct.pack(">IHBB", 6 << 28, len(payload), next_header, 255)
        packet = header + socket.inet_pton(socket.AF_INET6, self.host_ip)
        packet += socket.inet_pton(socket.AF_INET6, dst)
        with self._send_lock:
            self.tunnel.send_packet(packet + payload)

    def _read_packet(self, timeout: float):
        self.tunnel.set_timeout(timeout)
        try:
            return self.tunnel.recv_packet()
        except (socket.timeout, TimeoutError):
            return None
        except OSError:
            return None

    def recv_packet(self, timeout: float = 0.5, accept=None):
        'Receive one IPv6 packet.'
        if self._threaded:
            deadline = time.monotonic() + timeout
            with self._cond:
                while True:
                    for index, packet in enumerate(self._pool):
                        if accept is None or accept(packet):
                            return self._pool.pop(index)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or self._closed:
                        return None
                    self._cond.wait(remaining)

        deadline = time.monotonic() + timeout
        while True:
            for index, packet in enumerate(self._pool):
                if accept is None or accept(packet):
                    return self._pool.pop(index)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            packet = self._read_packet(remaining)
            if packet is None:
                continue
            if accept is None or accept(packet):
                return packet
            self._pool.append(packet)

    def close(self) -> None:
        'Stop the reader thread (the socket is closed by :class:`opendevice.remote.tunnel.Tunnel`).'
        self._closed = True
        with self._cond:
            self._cond.notify_all()
        if self._reader is not None:
            self._reader.join(timeout=2.0)
            self._reader = None

    def open_udp(self, port: int = 0) -> "UdpEndpoint":
        'Open a UDP endpoint on the stack (used by RTP / RTCP).'
        return UdpEndpoint(self, port)

    def connect(self, port: int, timeout: float = 8.0) -> TcpConnection:
        conn = TcpConnection(self, port, timeout=timeout)
        return conn.connect()
