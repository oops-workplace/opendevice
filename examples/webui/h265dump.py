from __future__ import annotations

import queue
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .hevc import SPS, codec_string_from_sps, nal_type, split_annexb

QUEUE_MAX = 240
FLUSH_EVERY = 60


class H265Dumper:
    """Write access units to file while broadcasting (is-key, bytes) to subscribers."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path
        self.bytes_written = 0
        self.access_units = 0
        self.keyframes = 0
        self.started_at: Optional[float] = None
        self._fh = None
        self._subs: List["queue.Queue[Tuple[bool, bytes]]"] = []
        self._lock = threading.Lock()
        self._sps: Optional[bytes] = None
        self._codec: Optional[str] = None

    def start(self) -> None:
        if self.path:
            self._fh = open(self.path, "wb")
        self.started_at = time.time()

    def stop(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                except Exception:                 # noqa: BLE001
                    pass
                self._fh.close()
            self._fh = None
            self._subs.clear()

    def feed(self, data: bytes, is_key: bool = False) -> None:
        if not data:
            return
        with self._lock:
            if self._fh is not None:
                self._fh.write(data)
                if self.access_units % FLUSH_EVERY == 0:
                    self._fh.flush()
            self.bytes_written += len(data)
            self.access_units += 1
            self.keyframes += 1 if is_key else 0
            if is_key:
                self._remember_codec(data)
            for q in self._subs:
                if q.full():
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                q.put_nowait((is_key, data))

    def _remember_codec(self, data: bytes) -> None:
        """A keyframe carries VPS/SPS/PPS, so the codec string can be derived here."""
        if self._codec:
            return
        for nal in split_annexb(data):
            if nal_type(nal) == SPS:
                self._sps = nal
                self._codec = codec_string_from_sps(nal)
                break

    def subscribe(self) -> "queue.Queue[Tuple[bool, bytes]]":
        q: "queue.Queue[Tuple[bool, bytes]]" = queue.Queue(maxsize=QUEUE_MAX)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[Tuple[bool, bytes]]") -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    @property
    def codec(self) -> Optional[str]:
        """The codec string for WebCodecs."""
        return self._codec

    def stats(self) -> Dict[str, Any]:
        elapsed = (time.time() - self.started_at) if self.started_at else 0.0
        return {
            "path": self.path,
            "bytes": self.bytes_written,
            "access_units": self.access_units,
            "keyframes": self.keyframes,
            "codec": self._codec,
            "seconds": round(elapsed, 1),
            "kbps": round(self.bytes_written * 8 / 1000 / elapsed) if elapsed > 1 else 0,
        }
