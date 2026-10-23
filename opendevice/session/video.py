from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from ..core import log as log_module
from ..core.errors import OpenDeviceError
from ..core.lock import DeviceLock
from ..media.stream import MediaStreamSession

RESTART_COOLDOWN = 8.0

WAIT_IDR_TIMEOUT = 1.5

STALE_SECONDS = 24

REFRESH_MARGIN = 300.0


class VideoMixin:
    'Media stream and watchdog (part of ``DeviceSession``; do not instantiate alone).'

    @property
    def streaming(self) -> bool:
        return self.receiver is not None

    def start_stream(self,
                     on_access_unit: Optional[Callable[[bytes, bool], None]] = None,
                     on_stats: Optional[Callable[[dict[str, Any]], None]] = None,
                     auto_restart: bool = True) -> MediaStreamSession:
        'Negotiate and start **continuous** streaming.'
        if self._closing.is_set():
            raise OpenDeviceError('this session is closing, cannot start a stream')

        if self.receiver is not None:
            if self.stream is None:
                raise OpenDeviceError('a receiver exists but the media session is gone (inconsistent state)')
            return self.stream

        claimed = False
        if self._lock is None:
            self._lock = DeviceLock.acquire(self.udid, task='streaming', owner=self)
            claimed = True

        session: Any = None
        started = False
        try:
            rsd = self._require_rsd()
            session = MediaStreamSession(rsd, rsd.stack, udid=self.udid,
                                         display_id=self.display_id,
                                         timeout=self.session_timeout)
            session.prepare_socket()
            session.start()
            started = True
            self.stream = session

            self.receiver = session.receiver_for(
                on_access_unit or (lambda _au, _key: None), on_stats=on_stats)
            self.receiver.start()

            self.reset_clients()
        except BaseException:
            self.stream = None
            self.receiver = None
            if started and session is not None:
                try:
                    session.stop()
                except Exception:           # noqa
                    pass
            if claimed and self._lock is not None:
                self._lock.release()
                self._lock = None
            raise

        if self.on_status:
            self.on_status(session.describe())

        if auto_restart:
            self._stop_watch.clear()
            if self._watchdog is None or not self._watchdog.is_alive():
                self._watchdog = threading.Thread(target=self._watch, daemon=True,
                                                  name="opendevice-stream-watch")
                self._watchdog.start()
        return session

    def stop_stream(self) -> None:
        'Stop the stream (and the watchdog that would renegotiate) and release the device.'
        self._stop_watch.set()
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None and watchdog is not threading.current_thread():
            watchdog.join(timeout=3.0)
            if watchdog.is_alive():
                log_module.get(__name__).warning(
                    'the watchdog thread did not exit within 3 s (probably stuck reconnecting); finishing anyway')
        self._teardown_stream()

        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def _teardown_stream(self) -> None:
        'Tear down only the receiver and the media session: no watchdog, no lock.'
        for part in (self.receiver, self.stream):
            try:
                if part is not None:
                    part.stop()
            except Exception:                      # noqa: BLE001
                pass
        self.receiver = None
        self.stream = None

    def request_keyframe(self) -> bool:
        'Send an RTCP PLI so the device emits a key frame right away.'
        if self.stream is None:
            return False
        return self.stream.request_keyframe()

    def reset_clients(self) -> None:
        'Drop **every client bound to the session** (touch / buttons / pasteboard); rebuilt on next use.'
        for client in (self._hid, self._indigo, self._board):
            try:
                if client is not None:
                    client.close()
            except Exception:                      # noqa: BLE001
                pass
        self._hid = self._indigo = self._board = None
        self._ax_wedged = False
        self.ax_last_len = 0

    def rebuild_link(self) -> bool:
        'Rebuild the **whole** link: stream -> clients -> RSD -> tunnel -> lockdown -> reconnect.'
        if not self._owns_link:
            return False
        on_access_unit, on_stats = self._stream_callbacks()
        was_streaming = self.receiver is not None
        self.close()
        self._display = None
        self.connect()
        if was_streaming:
            self._start_stream_quietly(on_access_unit, on_stats)
        return True

    def _stream_callbacks(self):
        'The callbacks currently on the receiver.'
        receiver = self.receiver
        if receiver is None:
            return None, None
        return receiver.on_access_unit, receiver.on_stats

    def _start_stream_quietly(self, on_access_unit, on_stats=None) -> None:
        'Bring the stream back.'
        if self.streaming or self._closing.is_set():
            return
        try:
            self.start_stream(on_access_unit=on_access_unit, on_stats=on_stats)
        except Exception as exc:                   # noqa: BLE001
            log_module.get(__name__).warning('restarting the stream failed (the link may be unusable): %s', exc)

    @property
    def ax_wedged(self) -> bool:
        'Whether the device-side AX session is wedged (truncated dumps, same every retry).'
        return self._ax_wedged

    @property
    def stream_stats(self) -> dict[str, Any]:
        return dict(self.receiver.stats) if self.receiver is not None else {}

    def _watch(self) -> None:
        'Watchdog: renegotiate when key frames stop / the stream breaks / **the session nears its lifetime**.'
        last_packets = 0
        stale = 0
        watched_stream: Any = None
        stream_started = 0.0
        while not self._stop_watch.is_set():
            time.sleep(1)

            if self._lock is not None and self._lock.lost:
                self._give_up('another process took the device over (--force); this side stopped streaming')
                return

            receiver = self.receiver
            if receiver is None:
                watched_stream = None
                continue

            if receiver is not watched_stream:
                watched_stream = receiver
                stream_started = time.monotonic()
                stale, last_packets = 0, receiver.stats.get("packets", 0)

            cooled_down = (time.monotonic() - self._last_restart) >= RESTART_COOLDOWN

            due = max(self.session_timeout - REFRESH_MARGIN, self.session_timeout * 0.5)
            if time.monotonic() - stream_started >= due and cooled_down:
                self._restart('media session nears its lifetime, renewing...')
                stale, last_packets = 0, self.stream_stats.get("packets", 0)
                continue

            if cooled_down and receiver.depacketizer.waiting_for_idr(WAIT_IDR_TIMEOUT):
                self._restart('key frames were lost, renegotiating...')
                stale, last_packets = 0, self.stream_stats.get("packets", 0)
                continue

            packets = receiver.stats.get("packets", 0)
            if packets == last_packets:
                stale += 1
                if stale >= STALE_SECONDS and cooled_down:
                    self._restart('the stream broke, reconnecting...')
                    stale, last_packets = 0, self.stream_stats.get("packets", 0)
                    continue
            else:
                stale = 0
            last_packets = packets

    def _give_up(self, message: str) -> None:
        'Another process took over the device with ``--force``: release the stream and the lock.'
        self._stop_watch.set()
        self._teardown_stream()
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        if self.on_error:
            self.on_error(message)

    def _restart(self, message: str) -> None:
        'Renegotiate (**only call from the watchdog thread**).'
        self._last_restart = time.monotonic()
        if self.on_error:
            self.on_error(message)
        callback = getattr(self.receiver, "on_access_unit", None)
        self._teardown_stream()
        time.sleep(1.0)
        if self._closing.is_set() or self._stop_watch.is_set():
            return
        try:
            self.start_stream(on_access_unit=callback)
        except Exception as exc:                   # noqa: BLE001
            if self.on_error:
                self.on_error(f"reconnect failed: {exc}")
