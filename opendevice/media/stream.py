from __future__ import annotations

import uuid
from typing import Any, Callable, Optional

from ..core.errors import OpenDeviceError
from ..remote import xpc
from ..remote.coredevice import (
    A_MEDIASTREAM_START,
    A_MEDIASTREAM_STOP,
    SERVICE_DISPLAY,
    CoreDeviceClient,
)
from ..remote.ipstack import UdpEndpoint
from ..remote.rsd import RSD
from . import media, rtp

VIDEO_MODE = media.MODE_VIDEO
CLIENT_SUPPORTED_FEATURES = 140
DEFAULT_SESSION_TIMEOUT = 3600


def _dyn(value: Any) -> dict[str, Any]:
    "Mercury's ``DynamicCodable`` wrapper."
    if isinstance(value, bool):
        return {"bool": value}
    if isinstance(value, int):
        return {"int": xpc.XpcInt64Type(value)}
    if isinstance(value, uuid.UUID):
        return {"uuid": value}
    return {"string": str(value)}


class MediaStreamSession:
    'One video streaming session.'

    def __init__(self, rsd: RSD, stack: Any, udid: Optional[str] = None,
                 display_id: int = 1, timeout: int = DEFAULT_SESSION_TIMEOUT) -> None:
        self.rsd = rsd
        self.stack = stack
        self.udid = udid
        self.display_id = display_id
        self.timeout = timeout
        self.session_id = uuid.uuid4()
        self.offer = b""
        self.answer: Any = b""
        self.stream_config: dict[str, Any] = {}
        self.receiver: dict[str, Any] = {}
        self.sender: dict[str, Any] = {}
        self.payload_type: Optional[int] = None
        self.local_ssrc = 0
        self.remote_ssrc = 0
        self.source_port = 0
        self.endpoint: Optional[UdpEndpoint] = None
        self._client: Optional[CoreDeviceClient] = None

    def prepare_socket(self) -> UdpEndpoint:
        'Open the UDP endpoints on the stack first (must happen before mediastreamstart).'
        endpoint = self.stack.open_udp()
        self.endpoint = endpoint
        return endpoint

    def start(self) -> dict[str, Any]:
        'Negotiate and make the device start streaming; returns ``streamConfig``.'
        if not self.offer:
            self.offer = media.build_negotiator_offer_video(
                media.new_call_id(), self.session_id.int & 0xFFFFFFFF)

        if self._client is None:
            self._client = CoreDeviceClient(self.rsd, SERVICE_DISPLAY)
            self._client.connect()

        payload = {
            "options": {
                "avcMediaStreamOptionClientSessionID": _dyn(self.session_id),
                "AVCMediaStreamNegotiatorTransportProtocolType": _dyn(2),
                "AVCMediaStreamNegotiatorAccessNetworkType": _dyn(1),
                "CoreDeviceVideoDisplayMode": _dyn("DisplayByID"),
                "VideoStreamForDisplayID": _dyn(self.display_id),
            },
            "receiverIP": self.stack.host_ip,
            "receiverPort": xpc.XpcUInt64Type(self.endpoint.port if self.endpoint else 0),
            "senderIP": self.stack.device_ip,
            "direction": "output",
            "sessionEventChannel": uuid.uuid4(),
            "clientSupportedFeatures": xpc.XpcUInt64Type(CLIENT_SUPPORTED_FEATURES),
            "type": "video",
            "timeout": xpc.XpcUInt64Type(self.timeout),
            "negotiatorOffer": self.offer,
        }

        try:
            result = self._client.invoke(A_MEDIASTREAM_START, payload, timeout=25)
        except OpenDeviceError as exc:
            text = str(exc)
            if "Remote control requires" in text or "9021" in text:
                raise OpenDeviceError(
                    "this device's OS does not support media streaming. Device replied: "
                    + text[:160] + '\n  - screen mirroring + touch injection need a recent system (media stream sessions only exist on the newest ones);\n  - other commands still work on older systems: deviceinfo / get / applist / afc / install / screenshot / power / syslog / devmode do not need the stream.') from None
            raise
        connection = result.get("connection", {}) or {}
        self.answer = result.get("negotiatorAnswer", b"")
        self.stream_config = connection.get("streamConfig", {}) or {}
        self.receiver = connection.get("receiver", {}) or {}
        self.sender = connection.get("sender", {}) or {}
        if self.receiver.get("port") and self.endpoint is not None:
            self.endpoint.port = int(self.receiver["port"])
        self.payload_type = int(self.stream_config.get("RxPayloadType") or 0) or None
        self.local_ssrc = int(self.stream_config.get("RemoteSSRC") or 0)
        self.remote_ssrc = int(self.stream_config.get("LocalSSRC") or 0)
        self.source_port = int(self.stream_config.get("SourcePort") or 0)
        return self.stream_config

    def receiver_for(self, on_access_unit: Callable[[bytes, bool], None],
                     on_stats: Optional[Callable[[dict[str, Any]], None]] = None) -> rtp.VideoReceiver:
        'Create a receiver for the negotiated PT (RTCP shares the port, so it must be filtered).'
        if self.endpoint is None:
            raise OpenDeviceError('call prepare_socket() first')
        return rtp.VideoReceiver(self.endpoint, on_access_unit,
                                 payload_type=self.payload_type, on_stats=on_stats)

    def request_keyframe(self) -> bool:
        'Send an RTCP PLI to request an IDR (the device sends one at stream setup, deltas afterwards).'
        if self.endpoint is None:
            return False
        address = (self.sender.get("ipv6Address") or self.stack.device_ip,
                   self.source_port or int(self.sender.get("port") or 0))
        return rtp.send_pli(self.endpoint, address, self.local_ssrc, self.remote_ssrc)

    def stop(self) -> None:
        'Stop the stream and make the device tear the session down.'
        if self._client is not None:
            try:
                self._client.invoke(A_MEDIASTREAM_STOP, {"stopAll": True}, timeout=5)
            except Exception:                # noqa
                pass
            try:
                self._client.close()
            except Exception:                # noqa: BLE001
                pass
            self._client = None
        if self.endpoint is not None:
            self.endpoint.close()
            self.endpoint = None

    @property
    def width(self) -> int:
        return int(self.stream_config.get("CustomWidth", 1216))

    @property
    def height(self) -> int:
        return int(self.stream_config.get("CustomHeight", 2624))

    @property
    def fps(self) -> int:
        return int(self.stream_config.get("Framerate", 60))

    def describe(self) -> str:
        config = self.stream_config
        return (f"{self.width}x{self.height} @ {self.fps}fps (RTP payload type {config.get('RxPayloadType')})")
