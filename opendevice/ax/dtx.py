from __future__ import annotations

import plistlib
import struct
import time
from typing import Any, Optional

from ..core.errors import OpenDeviceError
from ..remote.ipstack import TcpConnection

MAGIC = 0x1F3D5B79
FRAGMENT_HEADER_SIZE = 32
PAYLOAD_HEADER_SIZE = 16
MAX_FRAGMENT_SIZE = 128 * 1024

OK, DATA, DISPATCH, OBJECT, ERROR, BARRIER = 0, 1, 2, 3, 4, 5

FLAG_EXPECTS_REPLY = 1 << 0

P_NULL, P_STRING, P_BUFFER, P_INT32, P_INT64, P_DOUBLE = 10, 1, 2, 3, 6, 9
P_DICT = 0xF0
P_DICT_MAGIC = 0x1F0

DEFAULT_CAPABILITIES: dict[str, Any] = {
    "com.apple.private.DTXBlockCompression": 0,
    "com.apple.private.DTXConnection": 1,
}

#: How many times one ``pump`` may skip garbage before giving up.
MAX_RESYNC = 8


class FramingError(OpenDeviceError):
    'The stream is no longer on a fragment boundary (a message was dropped or truncated).'


def archive_object(obj: Any) -> bytes:
    'Encode Python objects into an NSKeyedArchive (binary plist).'
    if isinstance(obj, (str, bool, int, float)):
        objects: list[Any] = ["$null", obj]
    elif isinstance(obj, dict):
        keys: list[Any] = []
        values: list[Any] = []
        for key, value in obj.items():
            keys.append(key)
            values.append(value)
        root: dict[str, Any] = {"$class": plistlib.UID(2)}
        root["NS.keys"] = [plistlib.UID(3 + i * 2) for i in range(len(keys))]
        root["NS.objects"] = [plistlib.UID(4 + i * 2) for i in range(len(values))]
        objects = ["$null", root, {"$classes": ["NSDictionary"],
                                   "$classname": "NSDictionary"}]
        for index in range(len(keys)):
            objects.append(keys[index])
            objects.append(values[index])
    else:
        raise OpenDeviceError(f"cannot archive this type: {type(obj).__name__}")
    return plistlib.dumps({
        "$archiver": "NSKeyedArchiver",
        "$version": 100000,
        "$top": {"root": plistlib.UID(1)},
        "$objects": objects,
    }, fmt=plistlib.FMT_BINARY)


def unarchive_objects(data: bytes) -> list[Any]:
    'Fetch the ``$objects`` table of an NSKeyedArchive (elements resolve by UID themselves).'
    return plistlib.loads(data)["$objects"]


class PInt32(int):
    'Send as Int32 (otherwise it gets archived as an NSKeyedArchive).'


class PInt64(int):
    'Send as Int64.'


class PStr(str):
    'Send as a bare string.'


class PBuf(bytes):
    'Send as a raw buffer (already encoded bytes).'


class PNull:
    'NULL placeholder (always the aux dict key).'

    def __repr__(self) -> str:
        return "PNull"


PNULL = PNull()


def _encode_primitive(value: Any) -> bytes:
    'Encode one primitive (including the type code).'
    if value is PNULL or isinstance(value, PNull):
        return struct.pack("<I", P_NULL)
    if isinstance(value, PStr):
        raw = str(value).encode("utf-8")
        return struct.pack("<II", P_STRING, len(raw)) + raw
    if isinstance(value, PBuf):
        raw = bytes(value)
        return struct.pack("<II", P_BUFFER, len(raw)) + raw
    if isinstance(value, PInt32):
        return struct.pack("<Ii", P_INT32, int(value))
    if isinstance(value, PInt64):
        return struct.pack("<Iq", P_INT64, int(value))
    if isinstance(value, float):
        return struct.pack("<Id", P_DOUBLE, value)
    raise OpenDeviceError(f"unknown primitive: {type(value).__name__}")


def _coerce_argument(arg: Any) -> Any:
    'Non-primitive arguments are wrapped as PBuf(NSKeyedArchive).'
    if arg is PNULL or isinstance(arg, (PNull, PStr, PBuf, PInt32, PInt64)):
        return arg
    if arg is None:
        return PBuf(b"")
    if isinstance(arg, float):
        return arg
    return PBuf(archive_object(arg))


def build_aux(args: Any = ()) -> bytes:
    'Encode an argument list into the aux dict: ``{PNULL: [args...]}``; empty when there are none.'
    if not args:
        return b""
    body = bytearray()
    for arg in args:
        body += _encode_primitive(PNULL)
        body += _encode_primitive(_coerce_argument(arg))
    return (struct.pack("<IIQ", P_DICT_MAGIC, 0, len(body)) + bytes(body))


def parse_aux(data: bytes) -> list[Any]:
    'Decode the aux dict and return the argument list.'
    if not data:
        return []
    magic, _unknown, body_len = struct.unpack_from("<IIQ", data, 0)
    if magic & 0xFF != P_DICT:
        raise OpenDeviceError(f"aux is not a primitive dict (magic {magic:#x})")
    args: list[Any] = []
    offset = 16
    end = 16 + body_len
    while offset < end:
        _key, offset = _decode_primitive(data, offset)
        value, offset = _decode_primitive(data, offset)
        args.append(value)
    return args


def _decode_primitive(data: bytes, offset: int) -> tuple[Any, int]:
    (type_code,) = struct.unpack_from("<I", data, offset)
    offset += 4
    kind = type_code & 0xFF
    if kind == P_NULL:
        return PNULL, offset
    if kind == P_STRING:
        (length,) = struct.unpack_from("<I", data, offset)
        offset += 4
        return data[offset:offset + length].decode("utf-8", "replace"), offset + length
    if kind == P_BUFFER:
        (length,) = struct.unpack_from("<I", data, offset)
        offset += 4
        return data[offset:offset + length], offset + length
    if kind == P_INT32:
        (value,) = struct.unpack_from("<i", data, offset)
        return value, offset + 4
    if kind == P_INT64:
        (value,) = struct.unpack_from("<q", data, offset)
        return value, offset + 8
    if kind == P_DOUBLE:
        (value,) = struct.unpack_from("<d", data, offset)
        return value, offset + 8
    raise OpenDeviceError(f"unknown primitive type {type_code:#x}")


class DTXMessage:
    'A fully assembled DTX message.'

    def __init__(self, msg_type: int, identifier: int = 0, conversation_index: int = 0,
                 channel_code: int = 0, transport_flags: int = 0,
                 aux_data: bytes = b"", payload_data: bytes = b"") -> None:
        self.type = msg_type
        self.identifier = identifier
        self.conversation_index = conversation_index
        self.channel_code = channel_code
        self.transport_flags = transport_flags
        self.aux_data = aux_data
        self.payload_data = payload_data

    @property
    def aux(self) -> list[Any]:
        return parse_aux(self.aux_data)

    @property
    def payload(self) -> Any:
        'Decode the payload (NSKeyedArchive); return the bytes unchanged when it cannot be decoded.'
        if not self.payload_data:
            return None
        try:
            return _unarchive(self.payload_data)
        except Exception:
            return self.payload_data

    @property
    def name(self) -> str:
        value = self.payload
        return value if isinstance(value, str) else ""

    def body(self) -> bytes:
        'Message body = header + aux + payload.'
        header = struct.pack("<BBBBIII", self.type, 0, 0, 0,
                             len(self.aux_data),
                             len(self.aux_data) + len(self.payload_data),
                             0)
        return header + self.aux_data + self.payload_data

    def __repr__(self) -> str:
        return (f"<DTXMessage {_TYPE_NAMES.get(self.type, self.type)} "
                f"i{self.identifier}.{self.conversation_index} c{self.channel_code} "
                f"aux={len(self.aux_data)}B payload={len(self.payload_data)}B>")


_TYPE_NAMES = {OK: "OK", DATA: "DATA", DISPATCH: "DISPATCH", OBJECT: "OBJECT",
               ERROR: "ERROR", BARRIER: "BARRIER"}


def _unarchive(data: bytes) -> Any:
    'Decode an NSKeyedArchive back into Python values (scalars / strings / arrays / dicts).'
    objects = unarchive_objects(data)
    return _resolve(objects, objects[1])


def _resolve(objects: list[Any], value: Any) -> Any:
    if isinstance(value, plistlib.UID):
        value = objects[value.data]
    if isinstance(value, dict):
        class_name = _class_name(objects, value)
        if class_name and "Auth" in class_name:
            pass
        if class_name in ("NSString", "NSMutableString"):
            return value.get("NS.string")
        if class_name in ("NSDictionary", "NSMutableDictionary"):
            keys = [_resolve(objects, k) for k in (value.get("NS.keys") or [])]
            vals = [_resolve(objects, v) for v in (value.get("NS.objects") or [])]
            return dict(zip(keys, vals))
        if class_name and "Array" in class_name:
            return [_resolve(objects, i) for i in (value.get("NS.objects") or [])]
        if class_name in ("NSNumber",):
            return value.get("NS.number")
        return {k: _resolve(objects, v) for k, v in value.items() if not k.startswith("$")}
    if isinstance(value, (list, tuple)):
        return [_resolve(objects, i) for i in value]
    return value


def _class_name(objects: list[Any], obj: dict[str, Any]) -> Optional[str]:
    'Read ``$classname`` of an NSKeyedArchive object.'
    cls = obj.get("$class")
    index = cls.data if isinstance(cls, plistlib.UID) else None
    if index is not None and isinstance(objects[index], dict):
        name = objects[index].get("$classname")
        return name if isinstance(name, str) else None
    return None


class _Fragmenter:
    'Assemble one multi-fragment message.'

    def __init__(self, total: int, count: int) -> None:
        self.total = total
        self.expect = count - 1
        self.buffer = bytearray()
        self.meta: Optional[tuple[int, int, int, int]] = None

    def add(self, chunk: bytes) -> None:
        self.buffer += chunk

    def done(self) -> bool:
        return len(self.buffer) >= self.total


class DTXConnection:
    'A DTX connection (over raw TCP).'

    def __init__(self, conn: TcpConnection) -> None:
        self.conn = conn
        self._buffer = b""
        self._next_identifier = 1
        self._fragmenters: dict[int, _Fragmenter] = {}
        self.replies: dict[int, DTXMessage] = {}
        self.incoming: list[DTXMessage] = []
        self.supported_identifiers: Optional[dict[str, Any]] = None
        self.stats: dict[str, int] = {"sent": 0, "received": 0, "dropped": 0, "resync": 0}

    def handshake(self, timeout: float = 15.0) -> dict[str, Any]:
        'Exchange ``_notifyOfPublishedCapabilities:``.'
        self.send_dispatch(0, "_notifyOfPublishedCapabilities:",
                           (DEFAULT_CAPABILITIES,), expects_reply=False)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in self.pump(1.0):
                if (message.type == DISPATCH
                        and message.name == "_notifyOfPublishedCapabilities:"):
                    args = message.aux
                    caps = args[0] if args and isinstance(args[0], dict) else {}
                    self.supported_identifiers = caps
                    return caps
        raise OpenDeviceError('DTX capability handshake timed out')

    def send_dispatch(self, channel_code: int, selector: str, args: Any = (),
                      expects_reply: bool = True) -> int:
        'Send a DISPATCH (selector in the payload, arguments in aux).'
        message = DTXMessage(
            DISPATCH,
            channel_code=channel_code,
            transport_flags=FLAG_EXPECTS_REPLY if expects_reply else 0,
            aux_data=build_aux(args),
            payload_data=archive_object(selector),
        )
        self._send(message)
        return message.identifier

    def send_reply_ok(self, channel_code: int, identifier: int,
                      conversation_index: int) -> None:
        'Reply with an empty OK (required for dispatches carrying EXPECTS_REPLY).'
        self._send(DTXMessage(OK, identifier=identifier,
                              conversation_index=conversation_index,
                              channel_code=channel_code))

    def _send(self, message: DTXMessage) -> None:
        if message.identifier == 0:
            message.identifier = self._next_identifier
            self._next_identifier += 1
        wire_code = (message.channel_code if message.conversation_index % 2 == 0
                     else -message.channel_code)
        body = message.body()
        total = len(body)

        if total <= MAX_FRAGMENT_SIZE:
            self.conn.send(_fragment_header(0, 1, total, message.identifier,
                                            message.conversation_index, wire_code,
                                            message.transport_flags) + body)
        else:
            count = (total + MAX_FRAGMENT_SIZE - 1) // MAX_FRAGMENT_SIZE + 1
            self.conn.send(_fragment_header(0, count, total, message.identifier,
                                            message.conversation_index, wire_code,
                                            message.transport_flags))
            offset = 0
            for index in range(1, count):
                chunk = body[offset:offset + MAX_FRAGMENT_SIZE]
                offset += len(chunk)
                self.conn.send(_fragment_header(index, count, len(chunk),
                                                message.identifier,
                                                message.conversation_index,
                                                wire_code,
                                                message.transport_flags) + chunk)
        self.stats["sent"] += 1

    def invoke(self, selector: str, args: Any = (), channel_code: int = 0,
               expects_reply: bool = True, timeout: float = 15.0,
               ignore_errors: bool = False) -> Any:
        'Invoke a selector on a channel and wait for the reply (OBJECT -> value, OK -> None).'
        identifier = self.send_dispatch(channel_code, selector, args, expects_reply)
        if not expects_reply:
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if identifier in self.replies:
                message = self.replies.pop(identifier)
                if message.type == ERROR:
                    if ignore_errors:
                        return None
                    raise OpenDeviceError(f"{selector} failed: {message.payload!r}")
                return message.payload if message.type == OBJECT else None
            self.pump(1.0)
        raise OpenDeviceError(f"timed out waiting for the reply of {selector}")

    def pump(self, timeout: float = 1.0) -> list[DTXMessage]:
        'Read one round, dispatch assembled messages into :attr:`replies` / :attr:`incoming`, and return the ones that arrived in this round.'
        chunk = self.conn.recv(timeout)
        if chunk:
            self._buffer += chunk
        messages: list[DTXMessage] = []
        resyncs = 0
        while True:
            try:
                parsed = _take_message(self._buffer, self._fragmenters)
            except FramingError:
                if resyncs >= MAX_RESYNC or not self._resync():
                    raise
                resyncs += 1
                continue
            if parsed is None:
                break
            self._buffer, message = parsed
            if message is None:
                continue
            self.stats["received"] += 1
            messages.append(message)
            self._dispatch(message)
        return messages

    def _resync(self) -> bool:
        'Skip to the next fragment boundary, dropping half-assembled messages.'
        index = self._buffer.find(MAGIC.to_bytes(4, "little"), 1)
        if index <= 0:
            return False
        self._buffer = self._buffer[index:]
        self._fragmenters.clear()
        self.stats["resync"] += 1
        return True

    def drain(self, timeout: float, match=None) -> list[DTXMessage]:
        'Read until a message matching ``match`` arrives (or timeout), returning everything received.'
        collected: list[DTXMessage] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in self.pump(1.0):
                collected.append(message)
                if match is not None and match(message):
                    return collected
        return collected

    def _dispatch(self, message: DTXMessage) -> None:
        'Route messages by conversation_index into replies or device-initiated events.'
        if message.conversation_index % 2 == 0:
            message.channel_code = -message.channel_code
        is_reply = (message.conversation_index != 0
                    and message.type in (OK, OBJECT, ERROR))
        if is_reply:
            if message.identifier not in self.replies:
                self.replies[message.identifier] = message
            else:
                self.stats["dropped"] += 1
            return
        self.incoming.append(message)
        if message.transport_flags & FLAG_EXPECTS_REPLY:
            self.send_reply_ok(message.channel_code, message.identifier,
                               message.conversation_index + 1)

    def close(self) -> None:
        self.conn.close()


def _fragment_header(index: int, count: int, data_size: int, identifier: int,
                     conversation_index: int, channel_code: int, flags: int) -> bytes:
    return struct.pack("<IIHHIIIiI", MAGIC, FRAGMENT_HEADER_SIZE, index, count,
                       data_size, identifier, conversation_index, channel_code, flags)


def _take_message(buffer: bytes,
                  fragmenters: dict[int, _Fragmenter]) -> Optional[tuple[bytes, Optional[DTXMessage]]]:
    'Split one message out of the buffer, returning (remaining bytes, message or None).'
    if len(buffer) < FRAGMENT_HEADER_SIZE:
        return None
    magic, header_size, index, count, data_size, identifier, conv, channel, flags = \
        struct.unpack_from("<IIHHIIIiI", buffer, 0)
    if magic != MAGIC:
        raise FramingError(f"bad DTX fragment magic: {magic:#x}")
    body_len = 0 if (index == 0 and count > 1) else data_size
    total_len = header_size + body_len
    if len(buffer) < total_len:
        return None
    body = buffer[header_size:total_len]
    rest = buffer[total_len:]

    if count == 1:
        return rest, _parse_message(body, identifier, conv, channel, flags)

    if index == 0:
        fragmenter = _Fragmenter(data_size, count)
        fragmenter.meta = (identifier, conv, channel, flags)
        fragmenters[identifier] = fragmenter
        return rest, None

    fragmenter = fragmenters.get(identifier)
    if fragmenter is None:
        raise FramingError(f"received a fragment for unknown message {identifier}")
    fragmenter.add(body)
    if not fragmenter.done():
        return rest, None
    del fragmenters[identifier]
    meta = fragmenter.meta or (identifier, conv, channel, flags)
    return rest, _parse_message(bytes(fragmenter.buffer), *meta)


def _parse_message(body: bytes, identifier: int, conv: int, channel: int,
                   flags: int) -> DTXMessage:
    msg_type, _a, _b, _c, aux_size, _total, _flags = struct.unpack_from("<BBBBIII", body, 0)
    return DTXMessage(
        msg_type,
        identifier=identifier,
        conversation_index=conv,
        channel_code=channel,
        transport_flags=flags,
        aux_data=body[PAYLOAD_HEADER_SIZE:PAYLOAD_HEADER_SIZE + aux_size],
        payload_data=body[PAYLOAD_HEADER_SIZE + aux_size:],
    )
