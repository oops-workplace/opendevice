from __future__ import annotations

import plistlib
import uuid
import zlib

MODE_VIDEO = 5
MODE_AUDIO = 6

DECODER_NAME = "Viceroy 1.7.0"

HOST_MODEL = "Mac15,9"
HOST_OS_VERSION = "2205.3.1"
HOST_BUILD = "25F80"

HEVC_FEATURES = "FLS;SW:1;"
AVC_FEATURES = "FLS;SW:1;"

HEVC_PAYLOAD_TYPE = 123
AVC_PAYLOAD_TYPE = 100

RES_CODEC_CAP_ID = 50115

VIDEO_BITRATE_TIERS = (
    (4074, 0, 16384),
    (0, 75_000_000, 524288),
    (0, 40_000_000, 12288),
    (16, 4100, None),
    (0, 20_000_000, 98304),
    (4, 6500, None),
    (0, 6_000_000, 131072),
    (0, 100_000_000, 1048576),
    (0, 60_000_000, 262144),
    (1, 299, None),
)

TIMESTAMP = 17137042128614416384


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _varint_fixed(value: int, width: int) -> bytes:
    'Encode as a varint padded to exactly ``width`` bytes (continuation bytes keep the value).'
    raw = bytearray(_varint(value & ((1 << (7 * width)) - 1)))
    while len(raw) < width:
        raw[-1] |= 0x80
        raw.append(0x00)
    return bytes(raw)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _f_varint(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(value)


def _f_bytes(field: int, value: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(value)) + value


def _f_string(field: int, value: str) -> bytes:
    return _f_bytes(field, value.encode("utf-8"))


def _res_entry(pair_index: int) -> bytes:
    'One ResEntry: capability items that come in pairs.'
    return _f_varint(1, 1) + _f_varint(2, pair_index) + _f_varint(3, RES_CODEC_CAP_ID) + _f_varint(4, 0)


def _codec_bank(payload_type: int, features: str, tag: int, pairs: int) -> bytes:
    'One codec library: HEVC carries 4 ResEntries, AVC carries 2.'
    body = _f_varint(1, payload_type)
    for index in range(pairs):
        body += _f_bytes(2, _res_entry(1 + (index % 2)))
    return body + _f_string(3, features) + _f_varint(4, tag)


def _blob_common(settings_field: int, settings_body: bytes) -> bytes:
    'Fixed MediaBlob fields other than settings (including the bitrate table).'
    tiers = b""
    for kind, bps, buffer_cap in VIDEO_BITRATE_TIERS:
        body = _f_varint(1, kind) + _f_varint(2, bps)
        if buffer_cap is not None:
            body += _f_varint(3, buffer_cap)
        tiers += _f_bytes(9, body)
    return (
        _f_varint(1, 1)
        + _f_varint(2, 1)
        + _f_bytes(settings_field, settings_body)
        + _f_string(6, DECODER_NAME)
        + _f_varint(8, 0)
        + tiers
        + _f_varint(13, TIMESTAMP)
        + _f_varint(14, 2)
        + _f_varint(16, 0)
        + _f_varint(18, 1)
    )


def build_media_blob_video(session_id: int, *, allow_rtcp_fb: bool = False,
                           ltrp_enabled: bool = False, fec_enabled: bool = True,
                           hevc_features: str = HEVC_FEATURES,
                           avc_features: str = AVC_FEATURES) -> bytes:
    'Video MediaBlob.'
    settings = (
        _tag(1, 0)
        + _varint_fixed(session_id, 5)
        + _f_varint(2, 1 if allow_rtcp_fb else 0)
        + _f_bytes(3, _codec_bank(HEVC_PAYLOAD_TYPE, hevc_features, 1, 4))
        + _f_bytes(3, _codec_bank(AVC_PAYLOAD_TYPE, avc_features, 14, 2))
        + _f_varint(7, 1 if ltrp_enabled else 0)
        + _f_varint(8, 63)
        + (_f_varint(10, 1) if fec_enabled else b"")
        + _f_varint(12, 1)
    )
    return _blob_common(5, settings)


def build_media_blob_audio(session_id: int) -> bytes:
    'Audio MediaBlob.'
    settings = (
        _tag(1, 0)
        + _varint_fixed(session_id, 5)
        + _f_varint(2, 0)
        + _f_varint(3, 0)
        + _f_varint(4, 24191)
        + _f_varint(5, 0)
        + _f_varint(6, 0)
    )
    return _blob_common(3, settings)


def build_remote_endpoint_info(model: str = HOST_MODEL, os_version: str = HOST_OS_VERSION,
                               build: str = HOST_BUILD) -> bytes:
    'Protobuf for ``avcMediaStreamOptionRemoteEndpointInfo``.'
    return (_f_varint(1, 0) + _f_varint(2, 1)
            + _f_string(3, model) + _f_string(4, os_version) + _f_string(5, build))


def _offer(mode: int, call_id: str, blob: bytes) -> bytes:
    return plistlib.dumps({
        "avcMediaStreamOptionRemoteEndpointInfo": build_remote_endpoint_info(),
        "avcMediaStreamNegotiatorMode": mode,
        "avcMediaStreamNegotiatorMediaBlob": zlib.compress(blob, level=9),
        "avcMediaStreamOptionCallID": call_id,
    }, fmt=plistlib.FMT_BINARY)


def build_negotiator_offer_video(call_id: str, session_id: int, **kwargs) -> bytes:
    'Video offer (bplist bytes).'
    return _offer(MODE_VIDEO, call_id, build_media_blob_video(session_id, **kwargs))


def build_negotiator_offer_audio(call_id: str, session_id: int) -> bytes:
    'Audio offer (bplist bytes).'
    return _offer(MODE_AUDIO, call_id, build_media_blob_audio(session_id))


def new_call_id() -> str:
    'A fresh ``avcMediaStreamOptionCallID``.'
    return str(uuid.uuid4()).upper()
