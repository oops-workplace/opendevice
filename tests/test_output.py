from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout

import pytest

from opendevice.core import console, output
from opendevice.core.errors import OpenDeviceError


def _column_of(text: str, needle: str) -> int:
    'Which column ``needle`` starts at in this line (display width, not a character index).'
    return console.width(text[:text.index(needle)])


def test_table_aligns_by_display_width() -> None:
    'Wide characters take two columns: padding by len() skews the next column.'
    text = output.table(['name', 'value'], [['ＡＢＣ', "a"], ["ab", "z"]])
    head, first, second = text.splitlines()
    assert head.split() == ['name', 'value']
    assert _column_of(head, 'value') == _column_of(first, "a") == _column_of(second, "z")


def test_table_drops_columns_that_are_empty_everywhere() -> None:
    text = output.table(["PID", 'name', 'note'], [[1, "wechat", ""], [2, "maps", ""]])
    assert text.splitlines()[0].split() == ["PID", 'name']


def test_table_right_aligns_numbers() -> None:
    lines = output.table(["PID", 'path'], [[7, "/a"], [1234, "/b"]],
                         aligns=("r", "l")).splitlines()
    assert lines[1].index("/a") == lines[2].index("/b")


def test_pairs_line_up_the_keys() -> None:
    'Wide keys take two columns, so values must start at the same column.'
    first, second = output.pairs([('short', 1), ('a very long key', 2)]).splitlines()
    assert _column_of(first, "1") == _column_of(second, "2")


def test_json_out_is_machine_readable(capsys: pytest.CaptureFixture[str]) -> None:
    output.json_out({'ＡＢＣ': [1, b"raw"], "n": None})
    payload = json.loads(capsys.readouterr().out)
    assert payload == {'ＡＢＣ': [1, "b'raw'"], "n": None}


def test_line_goes_to_stdout_and_note_to_stderr() -> None:
    ours, theirs = io.StringIO(), io.StringIO()
    with redirect_stdout(ours), redirect_stderr(theirs):
        output.line('result')
        output.note('hint')
        output.warn('careful')
    assert ours.getvalue() == 'result\n'
    assert 'hint' in theirs.getvalue() and 'warning: careful' in theirs.getvalue()


def test_die_raises_so_the_entry_point_can_set_the_exit_code() -> None:
    with pytest.raises(OpenDeviceError, match='no device found'):
        output.die('no device found')
