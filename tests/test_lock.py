from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from opendevice.core import lock, paths
from opendevice.core.errors import DeviceBusyError

UDID = "EXAMPLE-0000000000000000"
OTHER = "00008110-001A2B3C4D5E6F70"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    'Locks are written under OPENDEVICE_HOME; real device state is untouched.'
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
    lock.HELD.clear()
    yield tmp_path
    lock.HELD.clear()


class Sleeper:
    'A real "other process": borrow its PID as the holder, and kill it to fake a stale lock.'

    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process

    @property
    def pid(self) -> int:
        return self.process.pid

    def kill(self) -> None:
        self.process.kill()
        self.process.wait(timeout=5)
        time.sleep(0.3)


@pytest.fixture
def sleeper():
    started: list[Sleeper] = []

    def start() -> Sleeper:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        item = Sleeper(process)
        started.append(item)
        time.sleep(0.4)
        return item

    yield start
    for item in started:
        try:
            if item.process.poll() is None:
                item.process.kill()
            item.process.wait(timeout=5)
        except Exception:                    # noqa: BLE001
            pass


def fake_holder(udid: str, pid: int, task: str = 'streaming') -> None:
    paths.lock_file(udid).write_text(
        json.dumps({"udid": udid, "pid": pid, "task": task, "since": time.time()}),
        encoding="utf-8")


def stop_heartbeat(udid: str) -> None:
    "Age the lock file's timestamp - simulates the holder process dying (liveness is the heartbeat, not the process)."
    path = paths.lock_file(udid)
    old = time.time() - lock.STALE_AFTER - 1
    os.utime(path, (old, old))


class TestAcquireRelease:
    def test_creates_and_removes_the_lock_file(self) -> None:
        assert lock.read_holder(UDID) is None
        held = lock.DeviceLock.acquire(UDID, task='streaming', owner="A")
        try:
            record = lock.read_holder(UDID)
            assert record is not None
            assert record["pid"] == os.getpid()
            assert record["task"] == 'streaming'
            assert paths.lock_file(UDID).is_file()
            assert lock.held_by_us(UDID)
        finally:
            held.release()
        assert lock.read_holder(UDID) is None
        assert not paths.lock_file(UDID).exists()

    def test_release_is_idempotent(self) -> None:
        held = lock.DeviceLock.acquire(UDID)
        held.release()
        held.release()
        assert lock.read_holder(UDID) is None

    def test_two_devices_are_independent(self) -> None:
        'Multi-device: holding A must not affect B.'
        first = lock.DeviceLock.acquire(UDID, task='streaming')
        second = lock.DeviceLock.acquire(OTHER, task='syslog')
        try:
            assert set(lock.holders()) == {UDID, OTHER}
        finally:
            first.release()
            second.release()
        assert lock.holders() == {}

    def test_owner_can_acquire_twice(self) -> None:
        owner = object()
        first = lock.DeviceLock.acquire(UDID, owner=owner)
        second = lock.DeviceLock.acquire(UDID, owner=owner)
        first.release()
        assert lock.held_by_us(UDID)
        second.release()
        assert not lock.held_by_us(UDID)

    def test_anonymous_owner_is_not_bypassed_by_named(self) -> None:
        first = lock.DeviceLock.acquire(UDID, task='streaming')
        try:
            with pytest.raises(DeviceBusyError, match='another session'):
                lock.DeviceLock.acquire(UDID, task='streaming', owner='other session')
        finally:
            first.release()

    def test_named_owner_is_not_bypassed_by_anonymous(self) -> None:
        first = lock.DeviceLock.acquire(UDID, task='streaming', owner=self)
        try:
            with pytest.raises(DeviceBusyError, match='another session'):
                lock.DeviceLock.acquire(UDID, task='streaming')
        finally:
            first.release()

    def test_another_owner_in_the_same_process_is_refused(self) -> None:
        'Two DeviceSessions in one process also count as repeated calls and are blocked.'
        first = lock.DeviceLock.acquire(UDID, owner='session A')
        try:
            with pytest.raises(DeviceBusyError, match='another session'):
                lock.DeviceLock.acquire(UDID, owner='session B')
        finally:
            first.release()


class TestOtherProcess:
    def test_live_foreign_holder_is_refused(self, sleeper) -> None:
        other = sleeper()
        fake_holder(UDID, other.pid, task='streaming')
        with pytest.raises(DeviceBusyError) as excinfo:
            lock.DeviceLock.acquire(UDID, task='streaming')
        message = str(excinfo.value)
        assert f"PID {other.pid}" in message and 'streaming' in message
        assert "opendevice devices" in message

    def test_assert_free_refuses_but_force_passes(self, sleeper) -> None:
        fake_holder(UDID, sleeper().pid, task='syslog')
        with pytest.raises(DeviceBusyError, match="--force"):
            lock.assert_free(UDID, who="`info`")
        lock.assert_free(UDID, force=True)

    def test_assert_free_passes_for_our_own_lock(self) -> None:
        held = lock.DeviceLock.acquire(UDID, task='streaming')
        try:
            lock.assert_free(UDID)
        finally:
            held.release()
        lock.assert_free(UDID)
        lock.assert_free(None)

    def test_stale_lock_is_taken_over(self, sleeper) -> None:
        'A lock left by a stopped heartbeat must not hold the device forever - the next caller takes over.'
        other = sleeper()
        fake_holder(UDID, other.pid, task='streaming')
        other.kill()
        stop_heartbeat(UDID)

        held = lock.DeviceLock.acquire(UDID, task='streaming')
        try:
            assert lock.read_holder(UDID)["pid"] == os.getpid()
        finally:
            held.release()

    def test_timeout_waits_until_the_heartbeat_stops(self, sleeper) -> None:
        'With a timeout, poll and wait: once the other heartbeat stops, the lock is taken over.'
        other = sleeper()
        fake_holder(UDID, other.pid, task='streaming')
        threading.Thread(target=lambda: (time.sleep(0.6), other.kill(),
                                         stop_heartbeat(UDID)),
                         daemon=True).start()
        held = lock.DeviceLock.acquire(UDID, task='streaming', timeout=5.0)
        try:
            assert lock.held_by_us(UDID)
        finally:
            held.release()


class TestShortVersusLong:
    'CLI policy: short commands ignore the lock, long tasks take it.'

    @staticmethod
    def args(**overrides) -> argparse.Namespace:
        values = {"device": UDID, "mux": None, "link": "usb", "force": False,
                  "command": "deviceinfo"}
        values.update(overrides)
        return argparse.Namespace(**values)

    @pytest.fixture
    def link_env(self, monkeypatch):
        'Fake out device picking and lockdown, so only the lock logic is tested.'
        from opendevice.cli import common
        from opendevice.transport.devices import DeviceEntry

        monkeypatch.setattr(common, "resolve_device",
                            lambda args: DeviceEntry(udid=UDID, usb=True, devid=1))

        class FakeLockdown:
            def __init__(self, udid=None, mux_address=None) -> None:
                self.device = SimpleNamespace(udid=udid)

            def start_session(self) -> None:
                pass

            def close(self) -> None:
                pass

        monkeypatch.setattr(common, "LockdownClient", FakeLockdown)
        return common

    def test_short_command_is_not_blocked(self, link_env, sleeper) -> None:
        fake_holder(UDID, sleeper().pid, task='streaming')
        link = link_env.open_link(self.args())
        try:
            assert link.udid == UDID
            assert not lock.held_by_us(UDID)
        finally:
            link.close()

    def test_long_command_is_blocked(self, link_env, sleeper) -> None:
        fake_holder(UDID, sleeper().pid, task='streaming')
        with pytest.raises(DeviceBusyError, match='held by another process'):
            link_env.open_link(self.args(), task='streaming')

    def test_long_command_with_force_takes_over(self, link_env, sleeper) -> None:
        "--force allows taking over someone else's lock (the two streams will fight; the caller's call)."
        other = sleeper()
        fake_holder(UDID, other.pid, task='streaming')
        link = link_env.open_link(self.args(force=True), task='streaming')
        try:
            held = lock.holders()[UDID]
            assert held["pid"] == os.getpid()
            assert held["task"] == 'streaming'
        finally:
            link.close()
        assert lock.holders() == {}
        other.kill()

    def test_release_does_not_delete_someone_elses_lock(self, sleeper) -> None:
        "After a forced takeover, the previous holder's release must not delete the new holder's lock."
        original = lock.DeviceLock.acquire(UDID, task='streaming', owner=' old')
        other = sleeper()
        fake_holder(UDID, other.pid, task='streaming')
        original.release()
        assert paths.lock_file(UDID).is_file()
        other.kill()

    def test_two_long_tasks_do_not_share(self, link_env, sleeper) -> None:
        'A second long task must also be blocked while another process holds it.'
        other = sleeper()
        fake_holder(UDID, other.pid, task='streaming')
        with pytest.raises(DeviceBusyError):
            link_env.open_link(self.args(), task='syslog')
        other.kill()


class TestRobustness:
    def test_broken_lock_file_is_ignored(self) -> None:
        paths.lock_file(UDID).write_text('{ not json', encoding="utf-8")
        assert lock.read_holder(UDID) is None
        held = lock.DeviceLock.acquire(UDID)
        held.release()

    def test_lock_file_name_is_filesystem_safe(self) -> None:
        weird = "a:b/c\\d e"
        path = paths.lock_file(weird)
        assert path.name.endswith(".lock")
        assert not set(path.name) & set(":/\\ ")

    def test_holders_cleans_dead_entries(self, sleeper) -> None:
        other = sleeper()
        fake_holder(UDID, other.pid)
        assert set(lock.holders()) == {UDID}
        other.kill()
        stop_heartbeat(UDID)
        assert lock.holders() == {}
        assert not paths.lock_file(UDID).exists()

    def test_heartbeat_keeps_the_lock_fresh(self) -> None:
        'The holder touches the lock file in the background, so a stopped heartbeat means a stale lock.'
        held = lock.DeviceLock.acquire(UDID, task='streaming')
        try:
            path = paths.lock_file(UDID)
            first = path.stat().st_mtime
            time.sleep(lock.BEAT_EVERY + 1.0)
            assert path.stat().st_mtime > first
        finally:
            held.release()

    def test_forced_takeover_is_noticed_by_the_loser(self, sleeper) -> None:
        "After a --force takeover the previous holder's heartbeat must notice and set ``lost``."
        held = lock.DeviceLock.acquire(UDID, task='streaming')
        try:
            assert held.lost is False
            fake_holder(UDID, sleeper().pid, task='streaming')
            deadline = time.time() + lock.BEAT_EVERY + 3
            while not held.lost and time.time() < deadline:
                time.sleep(0.2)
            assert held.lost, 'the heartbeat did not notice the takeover'
        finally:
            held.release()

    def test_held_lock_is_discoverable(self) -> None:
        held = lock.DeviceLock.acquire(UDID, task='streaming')
        try:
            assert lock.held_lock(UDID) is held
        finally:
            held.release()
        assert lock.held_lock(UDID) is None

    def test_stale_heartbeat_is_taken_over(self) -> None:
        held = lock.DeviceLock.acquire(UDID, task='streaming')
        held.release()
        fake_holder(UDID, os.getpid(), task='streaming')
        old = time.time() - lock.STALE_AFTER - 10
        os.utime(paths.lock_file(UDID), (old, old))
        assert lock.read_holder(UDID) is None
