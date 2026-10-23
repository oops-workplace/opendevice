from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

MAGIC = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

TYPE_DATA = 0x00
TYPE_HEADERS = 0x01
TYPE_RST_STREAM = 0x03
TYPE_SETTINGS = 0x04
TYPE_PING = 0x06
TYPE_GOAWAY = 0x07
TYPE_WINDOW_UPDATE = 0x08

FLAG_ACK = 0x01
FLAG_END_STREAM = 0x01
FLAG_END_HEADERS = 0x04

SETTINGS_MAX_CONCURRENT_STREAMS = 0x03
SETTINGS_INITIAL_WINDOW_SIZE = 0x04

MAX_FRAME = 16384


@dataclass
class Frame:
    type: int
    flags: int
    stream_id: int
    payload: bytes

    @property
    def is_data(self) -> bool:
        return self.type == TYPE_DATA


def frame(kind: int, flags: int, stream_id: int, payload: bytes = b"") -> bytes:
    return (len(payload).to_bytes(3, "big") + bytes([kind, flags])
            + struct.pack(">I", stream_id & 0x7FFFFFFF) + payload)


def settings_frame(pairs: Optional[list[tuple[int, int]]] = None, ack: bool = False) -> bytes:
    payload = b""
    for key, value in pairs or []:
        payload += struct.pack(">HI", key, value)
    return frame(TYPE_SETTINGS, FLAG_ACK if ack else 0, 0, payload)


def window_update(stream_id: int, increment: int) -> bytes:
    return frame(TYPE_WINDOW_UPDATE, 0, stream_id, struct.pack(">I", increment))


def headers_frame(stream_id: int) -> bytes:
    return frame(TYPE_HEADERS, FLAG_END_HEADERS, stream_id)


def data_frame(stream_id: int, payload: bytes) -> bytes:
    return frame(TYPE_DATA, 0, stream_id, payload)


def iter_frames(buffer: bytes) -> tuple[list[Frame], bytes]:
    'Split complete frames out of the buffer, returning (frames, remaining bytes).'
    frames: list[Frame] = []
    pos = 0
    while True:
        if len(buffer) - pos < 9:
            break
        length = int.from_bytes(buffer[pos:pos + 3], "big")
        if len(buffer) - pos < 9 + length:
            break
        kind = buffer[pos + 3]
        flags = buffer[pos + 4]
        stream_id = struct.unpack(">I", buffer[pos + 5:pos + 9])[0] & 0x7FFFFFFF
        payload = buffer[pos + 9:pos + 9 + length]
        frames.append(Frame(kind, flags, stream_id, payload))
        pos += 9 + length
    return frames, buffer[pos:]
