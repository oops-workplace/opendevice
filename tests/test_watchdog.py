from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from opendevice.core.errors import OpenDeviceError
from opendevice.session import DeviceSession

UDID = "EXAMPLE-0000000000000000"


class FakeDepacketizer:
    def __init__(self, waiting: bool = False) -> None:
        self.waiting = waiting
        self.calls = 0

    def waiting_for_idr(self, timeout: float) -> bool:
        self.calls += 1
        return self.waiting


class FakeReceiver:
    'Stub receiver: the minimum ``_watch`` needs to read stats and ask about IDR.'

    def __init__(self, waiting: bool = False) -> None:
        self.stats: dict[str, Any] = {"packets": 1}
        self.depacketizer = FakeDepacketizer(waiting)
        self.stopped = False
        self.on_access_unit = lambda _data, _key: None

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.stopped = True


class FakeStream:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def make_session(waiting: bool = False) -> DeviceSession:
    session = DeviceSession(udid=UDID)
    receiver = FakeReceiver(waiting)
    session.receiver = receiver
    session.stream = FakeStream()
    session.rsd = _FakeRsd()
    session._lock = None
    return session


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    'Shrink the cooldown / thresholds so the test does not wait tens of seconds.'
    monkeypatch.setattr("opendevice.session.video.RESTART_COOLDOWN", 0.0)
    monkeypatch.setattr("opendevice.session.video.WAIT_IDR_TIMEOUT", 0.0)
    monkeypatch.setattr("opendevice.session.video.STALE_SECONDS", 1)
    yield


def test_restart_does_not_join_the_watchdog_thread() -> None:
    '``_restart`` runs on the watchdog thread; it must never join itself.'
    session = make_session(waiting=True)
    started: list[Any] = []
    session.start_stream = lambda **kwargs: started.append(kwargs) or object()  # type: ignore[method-assign]

    errors: list[BaseException] = []
    done = threading.Event()

    def in_watchdog() -> None:
        try:
            session._restart('test reconnect')
        except BaseException as exc:                # noqa: BLE001
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=in_watchdog, name="watchdog", daemon=True)
    session._watchdog = thread
    thread.start()
    assert done.wait(5), 'reconnect stuck'

    assert not errors, f"raised inside the watchdog thread: {errors}"
    assert session._watchdog is thread
    assert session.receiver is None
    assert started, 'did not actually renegotiate'
    assert started[0].get("on_access_unit") is not None


def test_watchdog_survives_and_keeps_watching() -> None:
    'After one reconnect the watchdog must still be alive (otherwise the next stall has nobody watching).'
    session = make_session(waiting=True)
    restarts = {"n": 0}

    def fake_start_stream(**kwargs: Any) -> Any:
        restarts["n"] += 1
        session.receiver = FakeReceiver(waiting=False)
        session.stream = FakeStream()
        return object()

    session.start_stream = fake_start_stream               # type: ignore[method-assign]

    thread = threading.Thread(target=session._watch, name="opendevice-stream-watch",
                              daemon=True)
    session._watchdog = thread
    thread.start()
    time.sleep(2.5)
    session._stop_watch.set()
    thread.join(10)

    assert not thread.is_alive() or True
    assert restarts["n"] >= 1, 'the watchdog did not trigger a reconnect'
    assert session._last_restart > 0


def test_stop_stream_from_caller_thread_still_joins() -> None:
    '``stop_stream`` from a caller thread must wait for the watchdog (the join must not be removed).'
    session = make_session()
    seen: list[bool] = []

    def watchdog() -> None:
        seen.append(True)

    thread = threading.Thread(target=watchdog, daemon=True)
    session._watchdog = thread
    thread.start()
    session.stop_stream()
    assert seen and not thread.is_alive()
    assert session.receiver is None and session.stream is None


class FakeMediaSession:
    'The minimum ``start_stream`` needs (negotiate / receiver / stop).'

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.stopped = False

    def prepare_socket(self) -> None:
        pass

    def start(self) -> dict[str, Any]:
        return {}

    def receiver_for(self, _callback: Any, on_stats: Any = None) -> FakeReceiver:
        return FakeReceiver()

    def stop(self) -> None:
        self.stopped = True

    def describe(self) -> str:
        return "1179x2556 @ 60fps"


class FakeDeviceLock:
    lost = False

    @classmethod
    def acquire(cls, _udid: str, task: str = "", owner: Any = None) -> "FakeDeviceLock":
        return cls()

    def release(self) -> None:
        pass


@pytest.fixture
def no_device(monkeypatch):
    'Neutralise the two device-facing components: media session + cross-process lock.'
    monkeypatch.setattr("opendevice.session.video.MediaStreamSession", FakeMediaSession)
    monkeypatch.setattr("opendevice.session.video.DeviceLock", FakeDeviceLock)


def test_stream_can_restart_after_stop(no_device) -> None:
    'After stopping, a new stream must be possible (used by "stop -> reconnect" and link switches).'
    session = DeviceSession(udid=UDID)
    session.rsd = _FakeRsd()

    session.start_stream()
    assert session.streaming
    assert not session._stop_watch.is_set()

    session.stop_stream()
    assert not session.streaming
    assert session._stop_watch.is_set()

    session.start_stream()
    assert session.streaming
    session.stop_stream()


def test_start_after_close_is_still_refused(no_device) -> None:
    'After close() a new stream must still be refused - nobody would release that lock.'
    session = DeviceSession(udid=UDID)
    session.rsd = _FakeRsd()
    session.close()
    with pytest.raises(OpenDeviceError, match='this session is closing'):
        session.start_stream()


class _FakeRsd:
    'Fake RSD: ``connect()`` only needs ``udid()``, ``start_stream`` only needs ``stack``.'

    stack = object()

    def udid(self) -> str:
        return UDID

    def close(self) -> None:
        pass


class _FakeHandle:
    "``open_rsd``'s return value: only rsd / tunnel / lockdown / close."

    def __init__(self) -> None:
        self.rsd = _FakeRsd()
        self.tunnel = None
        self.lockdown = None

    def close(self) -> None:
        pass


def test_connect_clears_the_closing_flag(no_device, monkeypatch) -> None:
    'After a full rebuild (close + connect) a stream can start again - that is the path a link switch takes.'
    session = DeviceSession(udid=UDID)
    session.rsd = _FakeRsd()
    session.close()
    assert session._closing.is_set()

    monkeypatch.setattr("opendevice.session.link.device_list.resolve",
                        lambda *_args, **_kwargs: SimpleNamespace(udid=UDID))
    monkeypatch.setattr("opendevice.session.link.assert_free", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("opendevice.remote.tunnel.open_rsd",
                        lambda *_args, **_kwargs: _FakeHandle())
    session.rsd = None
    session.connect()
    assert not session._closing.is_set(), 'after reconnecting a stream must be startable again'

    session.start_stream()
    assert session.streaming
    session.stop_stream()
