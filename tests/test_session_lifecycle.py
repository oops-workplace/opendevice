from __future__ import annotations

import types
from typing import Any

import pytest

from opendevice.session import DeviceSession


class _El:
    'Minimal control stub: ``elements()`` only needs ``frame``.'

    def __init__(self, frame=(0.0, 0.0, 100.0, 200.0)) -> None:
        self.frame = frame


class _FakeAX:
    '``AXClient`` stub: ``stale`` / ``last_len`` are the two entries of the wedge check.'

    def __init__(self, rsd: object, service_name: str = "") -> None:
        self.rsd = rsd
        self.last_len = 0
        self.stale = False
        self.fetched: list[_El] = []

    def __enter__(self) -> "_FakeAX":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def fetch(self, timeout: float = 15.0, fresh: bool = True) -> list[_El]:
        return self.fetched


def _fake_ax(monkeypatch, elements: list[_El], last_len: int, stale: bool) -> None:
    from opendevice.ax import ax as ax_module

    def make(rsd: object, service_name: str = "") -> _FakeAX:
        client = _FakeAX(rsd, service_name)
        client.fetched = elements
        client.last_len = last_len
        client.stale = stale
        return client

    monkeypatch.setattr(ax_module, "AXClient", make)


def test_elements_keeps_a_good_dump(monkeypatch) -> None:
    'Controls that were fetched must be returned - do not drop them on the last line over a wrong attribute name.'
    _fake_ax(monkeypatch, [_El()], last_len=25000, stale=False)
    session = DeviceSession(rsd=object())

    got = session.elements()

    assert len(got) == 1
    assert session.ax_last_len == 25000
    assert session.ax_wedged is False


def test_elements_flags_a_wedged_session(monkeypatch) -> None:
    'A truncated dump (~1.7 KB) means the device AX session is wedged; every retry looks the same, and it must be reported.'
    _fake_ax(monkeypatch, [], last_len=1700, stale=True)
    session = DeviceSession(rsd=object())

    assert session.elements() == []
    assert session.ax_wedged is True


def test_ax_client_really_has_the_stale_property() -> None:
    'The attribute is named ``stale`` - it used to say ``is_stale``, which does not exist,'
    from opendevice.ax import ax as ax_module

    assert isinstance(getattr(ax_module.AXClient, "stale", None), property)
    assert not hasattr(ax_module.AXClient, "is_stale")


def test_reset_clients_drops_every_session_client() -> None:
    'Renegotiation = a new session: touch / **buttons** / pasteboard must all be dropped (kept ones are dead connections).'
    session = DeviceSession(rsd=object())
    stub = lambda: types.SimpleNamespace(close=lambda: None)   # noqa: E731
    session._hid, session._indigo, session._board = stub(), stub(), stub()
    session._ax_wedged = True
    session.ax_last_len = 1700

    session.reset_clients()

    assert (session._hid, session._indigo, session._board) == (None, None, None)
    assert session.ax_wedged is False
    assert session.ax_last_len == 0


def test_connect_checks_the_device_lock_before_building_a_tunnel(monkeypatch) -> None:
    '"Check nobody holds it before connecting" and "take the lock when streaming" are **two halves of one policy**,'
    import opendevice.session as session_module
    from opendevice.remote import tunnel as tunnel_module
    from opendevice.session import link as link_module

    seen: dict[str, Any] = {}

    class _Rsd:
        def udid(self) -> str:
            return "UDID"

        def close(self) -> None:
            pass

    monkeypatch.setattr(link_module.device_list, "resolve",
                        lambda selector=None, mux=None: types.SimpleNamespace(
                            udid=selector or "UDID"))
    monkeypatch.setattr(link_module, "assert_free",
                        lambda udid, force=False, who="": seen.update(
                            udid=udid, force=force))
    monkeypatch.setattr(tunnel_module, "open_rsd",
                        lambda udid=None, backend="auto", mux_address=None,
                        timeout=120.0: tunnel_module.TunnelHandle(rsd=_Rsd()))

    session_module.DeviceSession(udid="UDID").connect()
    assert seen == {"udid": "UDID", "force": False}

    seen.clear()
    session_module.DeviceSession(udid="UDID", force=True).connect()
    assert seen == {"udid": "UDID", "force": True}


def test_rebuild_link_defers_when_the_caller_owns_the_tunnel() -> None:
    'The link was injected by the caller (``rsd=``): the tunnel is theirs; this must not and cannot tear it down.'
    session = DeviceSession(rsd=object())
    assert session.rebuild_link() is False


def test_rebuild_link_tears_down_before_reconnecting(monkeypatch) -> None:
    'A self-built link: a full rebuild means **tear down first**, then connect (the reverse stacks two links).'
    session = DeviceSession(udid="test-udid")
    calls: list[str] = []
    monkeypatch.setattr(session, "close", lambda: calls.append("close"))
    monkeypatch.setattr(session, "connect", lambda: calls.append("connect") or session)
    session._owns_link = True

    assert session.rebuild_link() is True
    assert calls == ["close", "connect"]


@pytest.mark.parametrize("rotation,want", [
    ("rot0", (0.25, 0.5)),
    ("rot90", (0.5, 0.25)),
    ("rot270", (0.5, 0.75)),
    ("rot180", (0.75, 0.5)),
])
def test_panel_to_display_folds_by_rotation(monkeypatch, rotation: str,
                                           want: tuple[float, float]) -> None:
    "The cursor is 90 degrees off in landscape when panel space is not folded into the device's display space."
    session = DeviceSession(udid="test-udid")
    monkeypatch.setattr(session, "cursor_rotation", lambda: rotation)

    assert session._panel_to_display(0.25, 0.5) == pytest.approx(want)


def test_move_cursor_and_move_both_fold(monkeypatch) -> None:
    'Both hover entries (visible cursor / plain pointer) use the same pointer report and must both be folded.'
    sent: list[tuple[str, tuple[float, float]]] = []

    class _Touch:
        def move_cursor(self, x: float, y: float) -> bool:
            sent.append(("cursor", (x, y)))
            return True

        def move(self, x: float, y: float) -> None:
            sent.append(("pointer", (x, y)))

    session = DeviceSession(udid="test-udid")
    session._hid = _Touch()
    monkeypatch.setattr(session, "_require_stream", lambda what: None)
    monkeypatch.setattr(session, "cursor_rotation", lambda: "rot270")

    session.move_cursor(0.25, 0.5)
    session.move(0.25, 0.5)

    assert sent == [("cursor", (0.5, 0.75)), ("pointer", (0.5, 0.75))]
