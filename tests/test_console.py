from __future__ import annotations

from opendevice.core import console


def test_width_counts_wide_characters_twice() -> None:
    assert console.width("abc") == 3
    assert console.width('ＡＢＣ') == 6
    assert console.width("abcdefghij") == 10


def test_pad_fills_up_to_the_asked_columns() -> None:
    assert console.pad('name', 6) == 'name' + " " * 2
    assert console.pad("toolong", 3) == "toolong"
