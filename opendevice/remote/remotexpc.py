from __future__ import annotations

from typing import Any

from ..core.errors import OpenDeviceError
from . import http2, xpc
from .ipstack import TcpConnection

ROOT_CHANNEL = 1
REPLY_CHANNEL = 3

REMOTE_XPC_VERSION_FLAGS = 0x0100000000000006
MESSAGING_PROTOCOL_VERSION = 7


class RemoteXPCConnection:
    'A RemoteXPC connection (backed by one TCP stream).'

    def __init__(self, conn: TcpConnection) -> None:
        self.conn = conn
        self.next_message_id = {ROOT_CHANNEL: 0, REPLY_CHANNEL: 0}
        self._buffer = b""

    def handshake(self, timeout: float = 10.0) -> None:
        self.conn.send(http2.MAGIC)
        self.conn.send(http2.settings_frame([
            (http2.SETTINGS_MAX_CONCURRENT_STREAMS, 100),
            (http2.SETTINGS_INITIAL_WINDOW_SIZE, 16 * 1024 * 1024),
        ]))
        self.conn.send(http2.window_update(0, 16 * 1024 * 1024 - 65535))
        self.conn.send(http2.headers_frame(ROOT_CHANNEL))

        self.send_request({})
        self.conn.send(http2.headers_frame(REPLY_CHANNEL))
        self.conn.send(http2.data_frame(ROOT_CHANNEL, xpc.envelope(0x0201)))
        self.conn.send(http2.data_frame(
            REPLY_CHANNEL, xpc.envelope(xpc.FLAG_ALWAYS_SET | xpc.FLAG_INIT_HANDSHAKE)))
        self.next_message_id[REPLY_CHANNEL] += 1

        deadline = _deadline(timeout)
        while not _expired(deadline):
            for item in self._read_frames(1.0):
                if item.type == http2.TYPE_SETTINGS and not item.flags & http2.FLAG_ACK:
                    self.conn.send(http2.settings_frame(ack=True))
                    return
                if item.type == http2.TYPE_GOAWAY:
                    raise OpenDeviceError('device sent GOAWAY')
        raise OpenDeviceError('RemoteXPC handshake timed out')

    def send_request(self, obj: dict[str, Any], wanting_reply: bool = False) -> None:
        message_id = self.next_message_id[ROOT_CHANNEL]
        payload = xpc.wrap(obj, message_id, wanting_reply)
        for offset in range(0, len(payload), http2.MAX_FRAME):
            chunk = payload[offset:offset + http2.MAX_FRAME]
            self.conn.send(http2.data_frame(ROOT_CHANNEL, chunk))
        self.next_message_id[ROOT_CHANNEL] = (message_id + 1) | 1

    def receive_response(self, timeout: float = 10.0) -> dict[str, Any]:
        'Read one XPC response with a body (keep accumulating across DATA frames).'
        deadline = _deadline(timeout)
        buffer = b""
        while not _expired(deadline):
            for item in self._read_frames(1.0):
                if not item.is_data or not item.payload:
                    continue
                buffer += item.payload
                try:
                    obj, _message_id, _flags = xpc.unwrap(buffer)
                except ValueError:
                    continue
                if obj:
                    return obj
                buffer = b""
        raise OpenDeviceError('timed out waiting for the XPC response')

    def send_receive(self, obj: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        self.send_request(obj, wanting_reply=True)
        return self.receive_response(timeout)

    def close(self) -> None:
        self.conn.close()

    def _read_frames(self, timeout: float) -> list[http2.Frame]:
        chunk = self.conn.recv(timeout)
        if chunk:
            self._buffer += chunk
        frames, self._buffer = http2.iter_frames(self._buffer)
        return frames


def _deadline(timeout: float) -> float:
    import time

    return time.monotonic() + timeout


def _expired(deadline: float) -> bool:
    import time

    return time.monotonic() > deadline


def handshake_message(peer_uuid: Any) -> dict[str, Any]:
    'The RSD Handshake message body.'
    return {
        "MessageType": "Handshake",
        "MessagingProtocolVersion": xpc.XpcUInt64Type(MESSAGING_PROTOCOL_VERSION),
        "UUID": peer_uuid,
        "Properties": {
            "RemoteXPCVersionFlags": xpc.XpcUInt64Type(REMOTE_XPC_VERSION_FLAGS),
            "SensitivePropertiesVisible": True,
        },
        "Services": {},
    }
