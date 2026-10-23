from __future__ import annotations

import posixpath
import struct
from typing import Any, Callable, Iterator, Optional

from ..core.errors import AfcError
from ..core.plistio import recv_exact, sendall
from ..transport.service import ServiceConnection, open_service
from .base import Service

MAGIC = b"CFA6LPAA"
HEADER = struct.Struct("<8sQQQQ")
HEADER_SIZE = HEADER.size

SERVICE = "com.apple.afc"
SERVICE_RSD = "com.apple.afc.shim.remote"

SERVICE_HOUSE_ARREST = "com.apple.mobile.house_arrest"
SERVICE_HOUSE_ARREST_RSD = "com.apple.mobile.house_arrest.shim.remote"
VEND_CONTAINER = "VendContainer"
VEND_DOCUMENTS = "VendDocuments"

OP_STATUS = 0x01
OP_DATA = 0x02
OP_READ_DIR = 0x03
OP_REMOVE_PATH = 0x08
OP_MAKE_DIR = 0x09
OP_GET_FILE_INFO = 0x0A
OP_GET_DEV_INFO = 0x0B
OP_FILE_OPEN = 0x0D
OP_READ = 0x0F
OP_WRITE = 0x10
OP_FILE_CLOSE = 0x14
OP_RENAME_PATH = 0x18
OP_FILE_SET_SIZE = 0x15

MODE_RDONLY = 0x01
MODE_RW = 0x02
MODE_WRONLY = 0x03

OP_NAMES = {value: name for name, value in list(globals().items())
            if name.startswith("OP_") and isinstance(value, int)}

CHUNK = 1024 * 1024

ERRORS = {
    0: "SUCCESS", 1: "UNKNOWN_ERROR", 2: "OP_HEADER_INVALID", 3: "NO_RESOURCES",
    4: "READ_ERROR", 5: "WRITE_ERROR", 6: "UNKNOWN_PACKET_TYPE", 7: "INVALID_ARG",
    8: "OBJECT_NOT_FOUND", 9: "OBJECT_IS_DIR", 10: "PERM_DENIED",
    11: "SERVICE_NOT_CONNECTED", 12: "OP_TIMEOUT", 13: "TOO_MUCH_DATA",
    14: "END_OF_DATA", 15: "OP_NOT_SUPPORTED", 16: "OBJECT_EXISTS", 17: "OBJECT_BUSY",
    18: "NO_SPACE_LEFT", 20: "IO_ERROR", 21: "OP_INTERRUPTED", 22: "OP_IN_PROGRESS",
    23: "INTERNAL_ERROR", 30: "MUX_ERROR", 31: "NO_MEMORY", 32: "NOT_ENOUGH_DATA",
    33: "DIR_NOT_EMPTY",
}
NOT_FOUND = (4, 8)


def build_header(operation: int, payload: bytes = b"", packet_num: int = 0,
                 this_length: Optional[int] = None) -> bytes:
    'Build a 40-byte header (pure function, testable without a device).'
    entire = HEADER_SIZE + len(payload)
    return HEADER.pack(MAGIC, entire, this_length or entire, packet_num, operation)


def parse_stat(raw: bytes) -> dict[str, Any]:
    '``GET_FILE_INFO`` response: a NUL-separated list of key/value pairs.'
    parts = raw.decode("utf-8", "replace").split("\x00")[:-1]
    info: dict[str, Any] = dict(zip(parts[::2], parts[1::2]))
    for key in ("st_size", "st_blocks", "st_nlink", "st_mtime", "st_birthtime"):
        if key in info:
            info[key] = int(info[key])
    return info


def _cstr(path: str) -> bytes:
    return path.encode("utf-8") + b"\x00"


LOOKUP_FAILED = ("ApplicationLookupFailed", "InstallationLookupFailed")


def check_vend(response: Any) -> None:
    'house_arrest reply: only ``Status: Complete`` yields the session (pure function, testable without a device).'
    if not isinstance(response, dict):
        raise AfcError(f"house_arrest reply is not a dict: {response!r}")
    error = response.get("Error")
    if error in LOOKUP_FAILED:
        raise AfcError(
            f"the device will not hand over this app's container ({error}): "
            "house_arrest only works for developer-signed apps (get-task-allow)"
        )
    if error:
        raise AfcError(f"house_arrest: {error}")
    status = response.get("Status")
    if status and status != "Complete":
        raise AfcError(f"house_arrest returned {status}")


class Afc(Service):
    'The filesystem of one device (synchronous).'

    NAME = SERVICE
    RSD_NAME = SERVICE_RSD

    def __init__(self, conn: ServiceConnection) -> None:
        super().__init__(conn)
        self.conn: ServiceConnection = conn
        self._packet_num = 0

    @classmethod
    def open_app(cls, lockdown: Any = None, rsd: Any = None, bundle_id: str = "",
                 documents: bool = False) -> "Afc":
        'Enter an app sandbox (house_arrest); the AFC root becomes its container root.'
        conn = open_service(lockdown, rsd, SERVICE_HOUSE_ARREST, SERVICE_HOUSE_ARREST_RSD)
        try:
            check_vend(conn.request({
                "Command": VEND_DOCUMENTS if documents else VEND_CONTAINER,
                "Identifier": bundle_id,
            }))
        except BaseException:
            conn.close()
            raise
        return cls(conn)

    def _request(self, operation: int, payload: bytes = b"",
                 this_length: Optional[int] = None) -> bytes:
        'Send one operation and return the response payload.'
        sock = self.conn.sock
        sendall(sock, build_header(operation, payload, self._packet_num, this_length) + payload)
        self._packet_num += 1

        raw = recv_exact(sock, HEADER_SIZE)
        magic, entire, _this, _num, op = HEADER.unpack(raw)
        if magic != MAGIC:
            raise AfcError(f"unrecognized response header: {magic!r}")
        body = recv_exact(sock, entire - HEADER_SIZE) if entire > HEADER_SIZE else b""
        if op == OP_STATUS:
            status = int.from_bytes(body[:8], "little") if len(body) >= 8 else 0
            if status:
                raise AfcError(f"{OP_NAMES.get(operation, operation)}: "
                               f"{ERRORS.get(status, status)}")
            return b""
        return body

    def stat(self, path: str) -> dict[str, Any]:
        'File attributes (``st_size`` / ``st_ifmt`` / ``st_mtime`` etc.).'
        return parse_stat(self._request(OP_GET_FILE_INFO, _cstr(path)))

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
        except AfcError:
            return False
        return True

    def is_dir(self, path: str) -> bool:
        return str(self.stat(path).get("st_ifmt", "")).upper() == "S_IFDIR"

    def size(self, path: str) -> int:
        return int(self.stat(path).get("st_size", 0))

    def device_info(self) -> dict[str, Any]:
        '``GET_DEV_INFO``: total and free space.'
        return parse_stat(self._request(OP_GET_DEV_INFO, b""))

    def listdir(self, path: str) -> list[str]:
        'Names in a directory (**filtering out** ``.`` / ``..``, in device order).'
        raw = self._request(OP_READ_DIR, _cstr(path))
        return [name for name in raw.decode("utf-8", "replace").split("\x00")
                if name and name not in (".", "..")]

    def mkdir(self, path: str) -> None:
        self._request(OP_MAKE_DIR, _cstr(path))

    def mkdir_p(self, path: str) -> None:
        'Create directories level by level (existing ones are skipped).'
        parts = [part for part in path.split("/") if part]
        current = ""
        for part in parts:
            current = f"{current}/{part}"
            if not self.exists(current):
                self.mkdir(current)

    def remove(self, path: str) -> None:
        'Delete a file or an **empty** directory (no recursion).'
        self._request(OP_REMOVE_PATH, _cstr(path))

    def remove_tree(self, path: str) -> None:
        'Delete recursively.'
        if self.is_dir(path):
            for name in self.listdir(path):
                self.remove_tree(posixpath.join(path, name))
        self.remove(path)

    def rename(self, source: str, target: str) -> None:
        self._request(OP_RENAME_PATH, _cstr(source) + _cstr(target))

    def walk(self, path: str) -> Iterator[tuple[str, list[str], list[str]]]:
        'Same ``(dir, subdirs, files)`` shape as :func:`os.walk`.'
        dirs: list[str] = []
        files: list[str] = []
        for name in self.listdir(path):
            full = posixpath.join(path, name)
            (dirs if self.is_dir(full) else files).append(name)
        yield path, dirs, files
        for name in dirs:
            yield from self.walk(posixpath.join(path, name))

    def open_file(self, path: str, mode: int = MODE_RDONLY) -> int:
        'Open a file and return the handle.'
        raw = self._request(OP_FILE_OPEN, struct.pack("<Q", mode) + _cstr(path))
        return int.from_bytes(raw[:8], "little")

    def close_file(self, handle: int) -> None:
        self._request(OP_FILE_CLOSE, struct.pack("<Q", handle))

    def read_handle(self, handle: int, size: int) -> bytes:
        return self._request(OP_READ, struct.pack("<QQ", handle, size))

    def write_handle(self, handle: int, data: bytes) -> None:
        for offset in range(0, len(data), CHUNK):
            chunk = data[offset:offset + CHUNK]
            self._request(OP_WRITE, struct.pack("<Q", handle) + chunk, this_length=48)

    def truncate(self, handle: int, size: int = 0) -> None:
        self._request(OP_FILE_SET_SIZE, struct.pack("<QQ", handle, size))

    def read(self, path: str, size: Optional[int] = None,
             on_progress: Optional[Callable[[int], None]] = None) -> bytes:
        'Read the whole file (``st_size`` bytes by default).'
        total = self.size(path) if size is None else size
        handle = self.open_file(path, MODE_RDONLY)
        try:
            data = bytearray()
            while len(data) < total:
                chunk = self.read_handle(handle, min(CHUNK, total - len(data)))
                if not chunk:
                    break
                data += chunk
                if on_progress is not None:
                    on_progress(len(data))
            return bytes(data)
        finally:
            self.close_file(handle)

    def write(self, path: str, data: bytes) -> None:
        'Write the whole file (create if missing, truncate if present).'
        handle = self.open_file(path, MODE_WRONLY)
        try:
            self.write_handle(handle, data)
        finally:
            self.close_file(handle)

    def stream_from(self, path: str, fileobj: Any,
                    on_progress: Optional[Callable[[int], None]] = None) -> int:
        'Stream a local file object into the device (used for app uploads); returns the byte count.'
        handle = self.open_file(path, MODE_WRONLY)
        written = 0
        try:
            while True:
                chunk = fileobj.read(CHUNK)
                if not chunk:
                    break
                self.write_handle(handle, chunk)
                written += len(chunk)
                if on_progress is not None:
                    on_progress(written)
        finally:
            self.close_file(handle)
        return written

    def pull(self, remote: str, local: str,
             on_progress: Optional[Callable[[int], None]] = None) -> None:
        'Copy a device file or directory to this host (recursive, written as it is read).'
        self._pull(remote, local, on_progress, [0])

    def _pull(self, remote: str, local: str,
              on_progress: Optional[Callable[[int], None]], written: list[int]) -> None:
        'Recursive body; ``written`` accumulates bytes across files (a one-element list so recursion can share it).'
        if self.is_dir(remote):
            self._mkdir_local(local)
            for _folder, dirs, files in self.walk(remote):
                for name in files:
                    self._pull(posixpath.join(_folder, name),
                               posixpath.join(local, *_rel(_folder, remote), name),
                               on_progress, written)
                for name in dirs:
                    self._mkdir_local(posixpath.join(local, *_rel(_folder, remote), name))
            return
        self._mkdir_local(posixpath.dirname(local))
        total = self.size(remote)
        handle = self.open_file(remote, MODE_RDONLY)
        try:
            with open(local, "wb") as target:
                done = 0
                while done < total:
                    chunk = self.read_handle(handle, min(CHUNK, total - done))
                    if not chunk:
                        break
                    target.write(chunk)
                    done += len(chunk)
                    if on_progress is not None:
                        written[0] += len(chunk)
                        on_progress(written[0])
        finally:
            self.close_file(handle)

    def push(self, local: str, remote: str,
             on_progress: Optional[Callable[[int], None]] = None) -> None:
        'Push a local file or directory to the device (recursive, sent as it is read).'
        self._push(local, remote, on_progress, [0])

    def _push(self, local: str, remote: str,
              on_progress: Optional[Callable[[int], None]], written: list[int]) -> None:
        'Recursive body; ``written`` accumulates bytes across files (a one-element list so recursion can share it).'
        import os

        if os.path.isdir(local):
            self.mkdir_p(remote)
            for name in os.listdir(local):
                self._push(os.path.join(local, name), posixpath.join(remote, name),
                           on_progress, written)
            return
        self.mkdir_p(posixpath.dirname(remote))
        with open(local, "rb") as handle:
            base = written[0]

            def report(count: int) -> None:
                written[0] = base + count
                if on_progress is not None:
                    on_progress(written[0])

            self.stream_from(remote, handle, on_progress=report)

    @staticmethod
    def _mkdir_local(path: str) -> None:
        import os

        if path:
            os.makedirs(path, exist_ok=True)


def _rel(folder: str, root: str) -> list[str]:
    'The path components of ``folder`` relative to ``root``.'
    rest = folder[len(root):].strip("/")
    return rest.split("/") if rest else []


def connect(lockdown: Any = None, rsd: Any = None, bundle_id: Optional[str] = None,
            documents: bool = False) -> Afc:
    'Open an AFC session over the available link.'
    if bundle_id:
        return Afc.open_app(lockdown, rsd, bundle_id, documents=documents)
    return Afc.open(lockdown, rsd)
