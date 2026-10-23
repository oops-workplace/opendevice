from __future__ import annotations


def width(text: str) -> int:
    'Columns this string occupies in a terminal (wide characters count as 2).'
    return sum(2 if _wide(ord(ch)) else 1 for ch in text)


def pad(text: str, columns: int) -> str:
    'Pad on the right up to ``columns`` columns (leave it if already that wide).'
    return text + " " * max(0, columns - width(text))


def clip(text: str, columns: int) -> str:
    'Clip to at most ``columns`` columns (append ``…`` when clipped).'
    if width(text) <= columns:
        return text
    kept: list[str] = []
    used = 0
    for ch in text:
        step = 2 if _wide(ord(ch)) else 1
        if used + step > columns - 1:
            break
        kept.append(ch)
        used += step
    return "".join(kept) + "…"


def _wide(code: int) -> bool:
    'Unicode East Asian wide / fullwidth ranges (an approximation, not a full UAX#11 implementation).'
    return (0x1100 <= code <= 0x115F or 0x2E80 <= code <= 0xA4CF
            or 0xAC00 <= code <= 0xD7A3 or 0xF900 <= code <= 0xFAFF
            or 0xFE30 <= code <= 0xFE6F or 0xFF00 <= code <= 0xFF60
            or 0xFFE0 <= code <= 0xFFE6 or 0x20000 <= code <= 0x3FFFD)
