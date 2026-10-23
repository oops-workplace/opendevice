from __future__ import annotations

from typing import List, Optional

from opendevice.media.rtp import HEVC_IDR

VPS, SPS, PPS = 32, 33, 34
IDR_TYPES = HEVC_IDR


def nal_type(nal: bytes) -> int:
    return (nal[0] >> 1) & 0x3F


def split_annexb(data: bytes) -> List[bytes]:
    """Split on 00 00 00 01 / 00 00 01 start codes; return NALs without the start codes."""
    out: List[bytes] = []
    start = -1
    i = 0
    size = len(data)
    while i < size - 3:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            if start >= 0:
                out.append(data[start:i])
            start = i + 3
            i += 3
            continue
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 0 and data[i + 3] == 1:
            if start >= 0:
                out.append(data[start:i])
            start = i + 4
            i += 4
            continue
        i += 1
    if start >= 0:
        out.append(data[start:])
    return out


def is_keyframe(data: bytes) -> bool:
    """Does this access unit contain an IDR / CRA."""
    return any(nal_type(n) in IDR_TYPES for n in split_annexb(data))


def _rbsp(nal: bytes) -> bytes:
    """Strip the 2-byte NAL header and emulation-prevention bytes (00 00 03 -> 00 00)."""
    out = bytearray()
    i = 2
    while i < len(nal):
        if i + 2 < len(nal) and nal[i] == 0 and nal[i + 1] == 0 and nal[i + 2] == 3:
            out.extend(nal[i:i + 2])
            i += 3
        else:
            out.append(nal[i])
            i += 1
    return bytes(out)


def codec_string_from_sps(sps_nal: bytes) -> Optional[str]:
    """SPS NAL -> WebCodecs codec string."""
    try:
        rb = _rbsp(sps_nal)
        pos = 0

        def read_bits(n: int) -> int:
            nonlocal pos
            v = 0
            for _ in range(n):
                v = (v << 1) | ((rb[pos >> 3] >> (7 - (pos & 7))) & 1)
                pos += 1
            return v

        read_bits(4)
        read_bits(3)
        read_bits(1)
        profile_space = read_bits(2)
        tier_flag = read_bits(1)
        profile_idc = read_bits(5)
        pcf = read_bits(32)
        cif = read_bits(48)
        level_idc = read_bits(8)
    except Exception:                     # noqa: BLE001 - give up on a truncated SPS
        return None

    rev = 0
    x = pcf
    for _ in range(32):
        rev = (rev << 1) | (x & 1)
        x >>= 1
    ps_char = "ABCD"[profile_space] if profile_space else ""
    tier_char = "H" if tier_flag else "L"
    cif_hex = f"{cif:012X}"
    while len(cif_hex) > 2 and cif_hex.endswith("00"):
        cif_hex = cif_hex[:-2]
    return f"hev1.{ps_char}{profile_idc}.{rev:X}.{tier_char}{level_idc}.{cif_hex}"
