from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Any, Callable, Optional

START_CODE = b"\x00\x00\x00\x01"

HEVC_AP = 48
HEVC_FU = 49
HEVC_VPS, HEVC_SPS, HEVC_PPS = 32, 33, 34
HEVC_IDR = (19, 20, 21)

DEFAULT_PAYLOAD_TYPE = 100

RTCP_PLI = 206


def parse_rtp(packet: bytes) -> Optional[dict[str, Any]]:
    'Parse one RTP packet (including the extension header).'
    if len(packet) < 12:
        return None
    cc = packet[0] & 0x0F
    has_ext = bool(packet[0] & 0x10)
    offset = 12 + cc * 4
    if has_ext:
        if len(packet) < offset + 4:
            return None
        ext_words = struct.unpack(">H", packet[offset + 2:offset + 4])[0]
        offset += 4 + ext_words * 4
    if offset > len(packet):
        return None
    seq, timestamp, ssrc = struct.unpack(">HII", packet[2:12])
    return {
        "marker": bool(packet[1] & 0x80),
        "payload_type": packet[1] & 0x7F,
        "seq": seq,
        "timestamp": timestamp,
        "ssrc": ssrc,
        "payload": packet[offset:],
    }


def send_pli(sock: Any, address: Any, local_ssrc: int, remote_ssrc: int) -> bool:
    'Send an RTCP PLI so the device emits an IDR (required when a viewer joins mid-stream).'
    if not (local_ssrc and remote_ssrc):
        return False
    try:
        packet = struct.pack("!BBHII", 0x81, RTCP_PLI, 2,
                             local_ssrc & 0xFFFFFFFF, remote_ssrc & 0xFFFFFFFF)
        sock.sendto(packet, address)
        return True
    except OSError:
        return False


class Depacketizer:
    'Reassemble RTP payloads into Annex-B and track whether a full key frame has arrived.'

    def __init__(self) -> None:
        self._fu_buffer = bytearray()
        self._fu_active = False
        self.saw_parameter_sets = False
        self.saw_idr = False
        self.au_is_key = False
        self._invalidated_at: Optional[float] = None

    def ready(self) -> bool:
        'Only feed the decoder once parameter sets and an IDR are both present.'
        return self.saw_parameter_sets and self.saw_idr

    def invalidate(self) -> None:
        'After loss / a stall / a stream swap, go back to waiting for a full key frame.'
        self._fu_buffer = bytearray()
        self._fu_active = False
        self.saw_parameter_sets = False
        self.saw_idr = False
        self._invalidated_at = time.monotonic()

    def waiting_for_idr(self, seconds: float = 1.5) -> bool:
        'Whether it has been **stuck** waiting for a key frame for more than ``seconds`` (a freshly started stream does not count).'
        if self.saw_idr:
            return False
        return (self._invalidated_at is not None
                and (time.monotonic() - self._invalidated_at) > seconds)

    def take_au_key(self) -> bool:
        'Take and clear the "this access unit is a key frame" flag.'
        key, self.au_is_key = self.au_is_key, False
        return key

    @staticmethod
    def _nal_type(data: bytes) -> int:
        return (data[0] >> 1) & 0x3F

    def _note(self, nal_type: int) -> None:
        if nal_type in (HEVC_VPS, HEVC_SPS, HEVC_PPS):
            self.saw_parameter_sets = True
        elif nal_type in HEVC_IDR:
            self.saw_idr = True
            self.au_is_key = True
            self._invalidated_at = None

    def feed(self, payload: bytes) -> bytes:
        'Feed one RTP payload and return decoder-ready Annex-B (may be empty).'
        if len(payload) < 2:
            return b""
        nal_type = self._nal_type(payload)

        if nal_type == HEVC_AP:
            out = bytearray()
            offset = 2
            while offset + 2 <= len(payload):
                size = struct.unpack(">H", payload[offset:offset + 2])[0]
                offset += 2
                if size == 0 or offset + size > len(payload):
                    break
                nal = payload[offset:offset + size]
                if len(nal) >= 2:
                    self._note(self._nal_type(nal))
                out += START_CODE + nal
                offset += size
            return bytes(out)

        if nal_type == HEVC_FU:
            if len(payload) < 3:
                return b""
            fu_header = payload[2]
            start = bool(fu_header & 0x80)
            end = bool(fu_header & 0x40)
            real_type = fu_header & 0x3F
            self._note(real_type)
            if start:
                first = (payload[0] & 0x81) | (real_type << 1)
                self._fu_buffer = bytearray([first, payload[1]])
                self._fu_active = True
            if not self._fu_active:
                return b""
            self._fu_buffer += payload[3:]
            if end:
                data = START_CODE + bytes(self._fu_buffer)
                self._fu_buffer = bytearray()
                self._fu_active = False
                return data
            return b""

        self._note(nal_type)
        return START_CODE + payload


class VideoReceiver:
    'Receive RTP from a UDP endpoint and call back per access unit (split on the marker bit).'

    def __init__(self, endpoint: Any, on_access_unit: Callable[[bytes, bool], None],
                 payload_type: Optional[int] = None,
                 on_stats: Optional[Callable[[dict[str, Any]], None]] = None) -> None:
        self.endpoint = endpoint
        self.on_access_unit = on_access_unit
        self.on_stats = on_stats
        self.payload_type = payload_type or DEFAULT_PAYLOAD_TYPE
        self.depacketizer = Depacketizer()
        self.stats: dict[str, Any] = {
            "packets": 0, "bytes": 0, "frames": 0, "first_packet": None,
            "lost": 0, "reordered": 0, "dropped": 0, "ssrcs": 0,
        }
        self._ssrc: Optional[int] = None
        self._ssrcs: dict[int, int] = {}
        self._last_seq: Optional[int] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None

    def _run(self) -> None:
        pending = bytearray()
        while not self._stop.is_set():
            try:
                packet, _addr = self.endpoint.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break

            info = parse_rtp(packet)
            if info is None:
                continue
            if info["payload_type"] != self.payload_type:
                self.stats["dropped"] += 1
                continue
            if not info["payload"]:
                continue

            self._ssrcs[info["ssrc"]] = self._ssrcs.get(info["ssrc"], 0) + 1
            self.stats["ssrcs"] = len(self._ssrcs)

            if self._ssrc is not None and info["ssrc"] != self._ssrc:
                self.depacketizer.invalidate()
                pending = bytearray()
            elif self._last_seq is not None:
                gap = (info["seq"] - self._last_seq) & 0xFFFF
                if gap > 0x8000:
                    self.stats["reordered"] += 1
                elif gap > 1:
                    self.stats["lost"] += gap - 1
                    self.depacketizer.invalidate()
                    pending = bytearray()
            self._ssrc = info["ssrc"]
            self._last_seq = info["seq"]

            self.stats["packets"] += 1
            self.stats["bytes"] += len(packet)
            if self.stats["first_packet"] is None:
                self.stats["first_packet"] = time.time()

            pending += self.depacketizer.feed(info["payload"])

            if info["marker"] and pending:
                if self.depacketizer.ready():
                    self.stats["frames"] += 1
                    self.stats["last_au_size"] = len(pending)
                    self.on_access_unit(bytes(pending), self.depacketizer.take_au_key())
                else:
                    self.depacketizer.take_au_key()
                    self.stats["skipped"] = self.stats.get("skipped", 0) + 1
                pending = bytearray()

            if self.on_stats and self.stats["packets"] % 120 == 0:
                self.on_stats(self.stats)
