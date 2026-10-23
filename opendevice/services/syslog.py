from __future__ import annotations

import socket
import time
from typing import Any, Callable, Iterator, Optional

from ..transport.service import ServiceConnection, open_service
from .base import Service

SERVICE = "com.apple.syslog_relay"
SERVICE_RSD = "com.apple.syslog_relay.shim.remote"

SPLITTER = b"\n\x00"
CHUNK = 8192


def split_lines(buffer: bytes) -> tuple[list[str], bytes]:
    'Split the buffer into complete lines plus the trailing partial one (pure function, testable without a device).'
    parts = buffer.split(SPLITTER)
    rest = parts.pop()
    return [part.decode("utf-8", "replace") for part in parts if part], rest


class SyslogRelay(Service):
    'Device system log (synchronous stream).'

    NAME = SERVICE
    RSD_NAME = SERVICE_RSD

    def __init__(self, conn: ServiceConnection, timeout: float = 1.0) -> None:
        super().__init__(conn)
        self.timeout = timeout
        self.eof = False
        self._buffer = b""

    @classmethod
    def open(cls, lockdown: Any = None, rsd: Any = None, timeout: float = 1.0,
             ) -> "SyslogRelay":
        return cls(open_service(lockdown, rsd, cls.NAME, cls.RSD_NAME), timeout)

    def recv(self) -> list[str]:
        'Receive a small chunk and return the complete lines inside it (empty list when nothing arrived).'
        sock = self.conn.sock
        try:
            sock.settimeout(self.timeout)
            chunk = sock.recv(CHUNK)
        except (socket.timeout, TimeoutError):
            return []
        except OSError:
            self.eof = True
            return []
        if not chunk:
            if hasattr(sock, "sendall"):
                self.eof = True
            return []
        self._buffer += chunk
        lines, self._buffer = split_lines(self._buffer)
        return lines

    @property
    def closed(self) -> bool:
        if self.eof:
            return True
        conn = getattr(self.conn.sock, "conn", None)
        return bool(getattr(conn, "closed", False))

    def lines(self) -> Iterator[str]:
        'Yield log lines until the connection drops.'
        while not self.closed:
            for line in self.recv():
                yield line

    def follow(self, seconds: Optional[float] = None,
               on_line: Optional[Callable[[str], None]] = None) -> int:
        'Collect for ``seconds`` (``None`` = until the link drops) and return the line count.'
        deadline = None if seconds is None else time.monotonic() + seconds
        count = 0
        while not self.closed and (deadline is None or time.monotonic() < deadline):
            for line in self.recv():
                count += 1
                if on_line is not None:
                    on_line(line)
        return count


def connect(lockdown: Any = None, rsd: Any = None) -> SyslogRelay:
    'Open a syslog_relay over the available link.'
    return SyslogRelay.open(lockdown, rsd)
