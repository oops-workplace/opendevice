from __future__ import annotations

import struct
import uuid
from typing import Any

MAGIC_WRAPPER = 0x29B00B92
MAGIC_PAYLOAD = 0x42133742
PROTOCOL_VERSION = 5

FLAG_ALWAYS_SET = 0x00000001
FLAG_DATA_PRESENT = 0x00000100
FLAG_WANTING_REPLY = 0x00010000
FLAG_INIT_HANDSHAKE = 0x00400000

T_NULL = 0x00001000
T_BOOL = 0x00002000
T_INT64 = 0x00003000
T_UINT64 = 0x00004000
T_DOUBLE = 0x00005000
T_DATA = 0x00008000
T_STRING = 0x00009000
T_UUID = 0x0000A000
T_ARRAY = 0x0000E000
T_DICT = 0x0000F000


class XpcInt64Type(int):
    'Mark as signed 64-bit (otherwise non-negative integers encode as uint64).'


class XpcUInt64Type(int):
    'Mark as unsigned 64-bit.'


def _pad(buf: bytearray) -> None:
    'Pad the buffer to 4-byte alignment (all variable-length XPC fields are aligned).'
    while len(buf) % 4:
        buf.append(0)


def _encode_object(value: Any) -> bytes:
    buf = bytearray()

    if value is None:
        buf += struct.pack("<I", T_NULL)

    elif isinstance(value, bool):
        buf += struct.pack("<II", T_BOOL, 1 if value else 0)

    elif isinstance(value, XpcInt64Type):
        buf += struct.pack("<Iq", T_INT64, int(value))

    elif isinstance(value, XpcUInt64Type):
        buf += struct.pack("<IQ", T_UINT64, int(value) & 0xFFFFFFFFFFFFFFFF)

    elif isinstance(value, int):
        if value < 0:
            buf += struct.pack("<Iq", T_INT64, value)
        else:
            buf += struct.pack("<IQ", T_UINT64, value)

    elif isinstance(value, float):
        buf += struct.pack("<Id", T_DOUBLE, value)

    elif isinstance(value, (bytes, bytearray)):
        data = bytes(value)
        buf += struct.pack("<II", T_DATA, len(data)) + data
        _pad(buf)

    elif isinstance(value, uuid.UUID):
        buf += struct.pack("<I", T_UUID) + value.bytes

    elif isinstance(value, str):
        raw = value.encode("utf-8") + b"\x00"
        buf += struct.pack("<II", T_STRING, len(raw)) + raw
        _pad(buf)

    elif isinstance(value, (list, tuple)):
        body = struct.pack("<I", len(value))
        for item in value:
            body += _encode_object(item)
        buf += struct.pack("<II", T_ARRAY, len(body)) + body

    elif isinstance(value, dict):
        body = struct.pack("<I", len(value))
        for key, item in value.items():
            raw = str(key).encode("utf-8") + b"\x00"
            entry = bytearray(raw)
            _pad(entry)
            entry += _encode_object(item)
            body += bytes(entry)
        buf += struct.pack("<II", T_DICT, len(body)) + body

    else:
        raise TypeError(f"cannot encode this type: {type(value)!r}")

    return bytes(buf)


def wrap(obj: dict[str, Any], message_id: int = 0, wanting_reply: bool = False) -> bytes:
    'Wrap a dict into a RemoteXPC envelope.'
    flags = FLAG_ALWAYS_SET
    if obj:
        flags |= FLAG_DATA_PRESENT
    if wanting_reply:
        flags |= FLAG_WANTING_REPLY
    body = struct.pack("<Q", message_id)
    if obj:
        body += struct.pack("<II", MAGIC_PAYLOAD, PROTOCOL_VERSION) + _encode_object(obj)
    return struct.pack("<IIQ", MAGIC_WRAPPER, flags, len(body) - 8) + body


def envelope(flags: int, message_id: int = 0, payload: bytes = b"") -> bytes:
    'Bare envelope (control frame for the handshake: no message body).'
    body = struct.pack("<Q", message_id)
    return struct.pack("<IIQ", MAGIC_WRAPPER, flags, len(body) - 8) + body


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, size: int) -> bytes:
        if self.pos + size > len(self.data):
            raise ValueError('truncated XPC data')
        chunk = self.data[self.pos:self.pos + size]
        self.pos += size
        return chunk

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def align(self) -> None:
        self.pos += (-self.pos) % 4


def _decode_object(reader: _Reader) -> Any:
    kind = reader.u32()
    if kind == T_NULL:
        return None
    if kind == T_BOOL:
        return bool(reader.u32())
    if kind == T_INT64:
        return struct.unpack("<q", reader.take(8))[0]
    if kind == T_UINT64:
        return struct.unpack("<Q", reader.take(8))[0]
    if kind == T_DOUBLE:
        return struct.unpack("<d", reader.take(8))[0]
    if kind == T_DATA:
        size = reader.u32()
        data = reader.take(size)
        reader.align()
        return data
    if kind == T_STRING:
        size = reader.u32()
        raw = reader.take(size)
        reader.align()
        return raw.rstrip(b"\x00").decode("utf-8", "replace")
    if kind == T_UUID:
        return uuid.UUID(bytes=reader.take(16))
    if kind == T_ARRAY:
        size = reader.u32()
        end = reader.pos + size
        count = reader.u32()
        items = [_decode_object(reader) for _ in range(count)]
        reader.pos = end
        return items
    if kind == T_DICT:
        size = reader.u32()
        end = reader.pos + size
        count = reader.u32()
        result = {}
        for _ in range(count):
            key = bytearray()
            while True:
                byte = reader.take(1)
                if byte == b"\x00":
                    break
                key += byte
            reader.align()
            result[key.decode("utf-8", "replace")] = _decode_object(reader)
        reader.pos = end
        return result
    raise ValueError(f"unknown XPC type: {kind:#x}")


def unwrap(data: bytes) -> tuple[dict[str, Any], int, int]:
    'Decode an envelope, returning (dict, message_id, flags).'
    if len(data) < 16:
        raise ValueError('envelope too short')
    magic, flags, length = struct.unpack("<IIQ", data[:16])
    if magic != MAGIC_WRAPPER:
        raise ValueError(f"not an XPC envelope: {magic:#x}")
    body = data[16:16 + length + 8]
    if len(body) < 8:
        return {}, 0, flags
    message_id = struct.unpack("<Q", body[:8])[0]
    rest = body[8:]
    if len(rest) < 8:
        return {}, message_id, flags
    payload_magic, _version = struct.unpack("<II", rest[:8])
    if payload_magic != MAGIC_PAYLOAD:
        return {}, message_id, flags
    return _decode_object(_Reader(rest[8:])), message_id, flags
