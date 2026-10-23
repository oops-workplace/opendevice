from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Optional

from . import log as log_module
from . import paths
from .errors import DeviceBusyError

HELD: dict[str, dict[str, Any]] = {}

_MUTEX = threading.RLock()


STALE_AFTER = 30.0

BEAT_EVERY = 5.0


def read_holder(udid: Optional[str]) -> Optional[dict[str, Any]]:
    'Who currently holds this device.'
    if not udid:
        return None
    path = paths.lock_file(udid)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if not raw.strip():
        try:
            if time.time() - path.stat().st_mtime < STALE_AFTER:
                return None
        except OSError:
            return None
        _drop_broken(path)
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        _drop_broken(path)
        return None
    if not isinstance(record, dict):
        _drop_broken(path)
        return None
    try:
        if time.time() - path.stat().st_mtime > STALE_AFTER:
            log_module.get(__name__).debug('taking over the stale lock of %s (heartbeat stopped %ds ago)',
                                           udid, int(time.time() - path.stat().st_mtime))
            path.unlink()
            return None
    except OSError:
        return None
    return record


def _drop_broken(path: Any) -> None:
    'An unreadable lock file counts as no lock.'
    try:
        path.unlink()
    except OSError:
        pass


def describe(holder: dict[str, Any]) -> str:
    'Render the holder in plain words.'
    task = holder.get("task") or 'task'
    pid = holder.get("pid")
    since = float(holder.get("since") or 0)
    age = f", running for{int(time.time() - since)} s" if since else ""
    return f"{task} (PID {pid}{age})"


def assert_free(udid: Optional[str], force: bool = False, who: str = 'this command') -> None:
    'Check whether another party holds the device (short commands call this before connecting).'
    if force:
        return
    holder = read_holder(udid)
    if holder is None or int(holder.get("pid") or 0) == os.getpid():
        return
    raise DeviceBusyError(
        f"Device{udid} is held by another process: {describe(holder)}.\n  Only one long task (streaming / syslog) may hold a device at a time —{who} is busy for now.\n  • Wait until it finishes (`opendevice devices` only lists devices and is unaffected)\n  • Use another device (-d other)\n  • Force it anyway knowing they will interfere: add --force")


class DeviceLock:
    'Exclusive ownership of one device (for long tasks).'

    def __init__(self, udid: str, task: str, owner: Any = None) -> None:
        self.udid = udid
        self.task = task
        self.owner = owner
        self._token: str = ""
        self._released = False
        self._beating = False
        self.lost = False

    @classmethod
    def acquire(cls, udid: Optional[str], task: str = 'long task',
                owner: Any = None, timeout: float = 0.0,
                force: bool = False) -> "DeviceLock":
        'Take the lock; raise ``DeviceBusyError`` when another process or another caller in this process holds it.'
        if not udid:
            raise DeviceBusyError('no device UDID, cannot take the exclusive lock')
        with _MUTEX:
            mine = HELD.get(udid)
            if mine is not None:
                same = (owner is None and mine["owner"] is None) or mine["owner"] is owner
                if same:
                    existing = mine["lock"]
                    if existing.lost:
                        raise DeviceBusyError(
                            f"Device{udid} ownership was taken over by another process (--force), yielding")
                    mine["count"] += 1
                    return existing
                raise DeviceBusyError(
                    f"Device{udid} is already held by another session in this process ({mine['record'].get('task')}) — only one session may stream per device.")

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            holder = read_holder(udid)
            if holder is None:
                break
            if int(holder.get("pid") or 0) == os.getpid() and udid not in HELD:
                break
            if force:
                log_module.get(__name__).warning(
                    'forcing takeover of device %s (previous holder: %s)', udid, describe(holder))
                try:
                    paths.lock_file(udid).unlink()
                except OSError:
                    pass
                break
            if time.monotonic() >= deadline:
                raise DeviceBusyError(
                    f"Device{udid} is held by another process: {describe(holder)}.\n  Only one long task (streaming / syslog) may hold a device at a time, so this run will not steal it.\n  Retry after it finishes; `opendevice devices` still works.")
            time.sleep(0.2)

        lock = cls(udid, task, owner)
        lock._token = uuid.uuid4().hex
        record = {"udid": udid, "pid": os.getpid(), "task": task,
                  "since": time.time(), "token": lock._token}
        path = paths.lock_file(udid)
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise DeviceBusyError(
                f"Device{udid} was just taken by another process ({describe(read_holder(udid) or {})}), this run lost the race."
            ) from None
        try:
            os.write(handle, json.dumps(record, ensure_ascii=False).encode("utf-8"))
        finally:
            os.close(handle)
        with _MUTEX:
            HELD[udid] = {"count": 1, "owner": owner, "record": record, "lock": lock}
        lock._beat()
        log_module.get(__name__).info('holding device %s (%s, PID %d)', udid, task, os.getpid())
        return lock

    def _beat(self) -> None:
        'Background loop: touch the lock file mtime every :data:`BEAT_EVERY` seconds and watch whether the lock was taken over.'
        if self._beating:
            return
        self._beating = True

        def loop() -> None:
            path = paths.lock_file(self.udid)
            while self._beating and not self._released:
                try:
                    owner = int(json.loads(path.read_text(encoding="utf-8"))
                                .get("pid") or 0)
                except FileNotFoundError:
                    owner = 0
                except (OSError, ValueError):
                    owner = 0
                if owner != os.getpid():
                    self.lost = True
                    self._beating = False
                    log_module.get(__name__).warning(
                        'ownership of device %s was taken over (--force), yielding', self.udid)
                    return
                try:
                    os.utime(path, None)
                except OSError:
                    return
                time.sleep(BEAT_EVERY)

        threading.Thread(target=loop, name=f"opendevice-lock-{self.udid}",
                         daemon=True).start()

    def release(self) -> None:
        'Release one level of the lock.'
        if self._released:
            return
        with _MUTEX:
            mine = HELD.get(self.udid)
            if mine is None or mine["count"] <= 0:
                return
            mine["count"] -= 1
            if mine["count"] > 0:
                return
            self._released = True
            self._beating = False
            HELD.pop(self.udid, None)
            path = paths.lock_file(self.udid)
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(current, dict):
                    return
                stored = current.get("token")
                if stored is not None and stored != self._token:
                    return
                if int(current.get("pid") or 0) == os.getpid():
                    path.unlink()
            except (OSError, ValueError):
                pass

    def __enter__(self) -> "DeviceLock":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def held_by_us(udid: Optional[str]) -> bool:
    'Whether this process currently holds the device (for hints and debugging).'
    return bool(udid) and udid in HELD


def held_lock(udid: Optional[str]) -> Optional["DeviceLock"]:
    'The lock object this process holds for the device (``None`` when not held).'
    entry = HELD.get(udid or "")
    return entry.get("lock") if entry else None


def holders() -> dict[str, dict[str, Any]]:
    'Devices held right now (**all processes on this host**; reads lock files and clears stale ones).'
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(paths.locks_dir().glob("*.lock")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _drop_broken(path)
            continue
        try:
            if time.time() - path.stat().st_mtime > STALE_AFTER:
                _drop_broken(path)
                continue
        except OSError:
            continue
        found[str(record.get("udid") or path.stem)] = record
    return found
