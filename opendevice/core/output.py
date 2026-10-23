from __future__ import annotations

import json
import sys
from typing import Any, Iterable, NoReturn, Optional, Sequence

from .console import pad, width
from .errors import OpenDeviceError

GAP = "  "


def line(text: str = "") -> None:
    'One line of result (stdout).'
    print(text)


def note(text: str) -> None:
    'Hint (stderr): explains the next step or what a value means.'
    print(text, file=sys.stderr)


def hint(text: str) -> None:
    'Same as :func:`note`; pick one by tone.'
    note(text)


def warn(text: str) -> None:
    'Warning (stderr): execution can continue, but the result may be unexpected.'
    print(f"warning: {text}", file=sys.stderr)


def die(message: str) -> NoReturn:
    'Fail; the entry point turns it into exit code 1.'
    raise OpenDeviceError(message)


def json_out(value: Any) -> None:
    'The single ``--json`` output path: stable indent, no unicode escaping, non-JSON values become strings.'
    line(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]],
          indent: int = 0, aligns: Optional[Sequence[str]] = None) -> str:
    'Plain-text table: two-space gap, one header row, columns that are entirely empty are dropped.'
    cells = [["" if item is None else str(item) for item in row] for row in rows]
    for row in cells:
        row.extend([""] * (len(headers) - len(row)))
    keep = [index for index in range(len(headers))
            if any(row[index] for row in cells)]
    if not keep:
        keep = list(range(len(headers)))
    widths = {index: max([width(headers[index])] +
                         [width(row[index]) for row in cells]) for index in keep}
    sides = list(aligns or [])
    sides.extend("l" * (len(headers) - len(sides)))

    def render(row: Sequence[str]) -> str:
        return GAP.join(_fit(row[index], widths[index], sides[index])
                        for index in keep).rstrip()

    lines = [render([str(title) for title in headers])]
    lines.extend(render(row) for row in cells)
    prefix = " " * indent
    return "\n".join(prefix + text for text in lines)


def size(count: int) -> str:
    'Human-readable byte size: ``3208077`` → ``3.1 MB``.'
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _fit(text: str, columns: int, side: str) -> str:
    'Pad to ``columns`` columns: ``"r"`` pads on the left (numbers).'
    spaces = " " * max(0, columns - width(text))
    return f"{spaces}{text}" if side == "r" else pad(text, columns)


def pairs(items: Iterable[tuple[str, Any]], indent: int = 2,
          separator: str = ": ") -> str:
    'Two-column key/value output (``key: value``, keys aligned by display width).'
    made = [(str(key), "" if value is None else str(value)) for key, value in items]
    columns = max((width(key) for key, _value in made), default=0)
    prefix = " " * indent
    return "\n".join(f"{prefix}{pad(key, columns)}{separator}{value}"
                     for key, value in made)
