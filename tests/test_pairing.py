from __future__ import annotations

import plistlib
import types
from pathlib import Path

import pytest

from opendevice.core.errors import NotPairedError
from opendevice.transport import pairing, usbmux

_RECORD = {
    "HostID": "HOST-1",
    "SystemBUID": "BUID-1",
    "HostCertificate": b"cert",
    "HostPrivateKey": b"key",
}


class _FakeMux:
    'Play usbmuxd: ``ReadPairRecord`` only knows the devices placed in ``records``.'

    def __init__(self, records: dict, attached: "list[str]" = None) -> None:
        self._records = records
        self._attached = attached or sorted(records)

    def __enter__(self) -> "_FakeMux":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def list_devices(self) -> list:
        return [types.SimpleNamespace(devid=index + 1, udid=udid, connection_type="USB")
                for index, udid in enumerate(self._attached)]

    def read_pair_record(self, udid: str) -> bytes:
        data = self._records.get(udid)
        return plistlib.dumps(data) if data else b""


def _install_fake_mux(monkeypatch, records: dict, attached: "list[str]" = None) -> None:
    monkeypatch.setattr(usbmux, "Mux", lambda *args, **kwargs: _FakeMux(records, attached))


def _point_dirs(monkeypatch, directory: Path) -> None:
    monkeypatch.setattr(pairing, "DIRS", {"win32": [str(directory)],
                                          "darwin": [str(directory)],
                                          "linux": [str(directory)]})


class TestRecordFromFile:
    def test_reads_the_first_complete_record(self, tmp_path, monkeypatch) -> None:
        (tmp_path / "EXAMPLE-0001.plist").write_bytes(__import__("plistlib").dumps(_RECORD))
        _point_dirs(monkeypatch, tmp_path)
        record = pairing.PairingRecord.load()
        assert record.udid == "EXAMPLE-0001"
        assert record.host_private_key == b"key"

    def test_skips_record_without_keys(self, tmp_path, monkeypatch) -> None:
        'Without HostPrivateKey it is not "paired"; do not use it for TLS.'
        (tmp_path / "nope.plist").write_bytes(plistlib.dumps({"HostID": "H"}))
        _point_dirs(monkeypatch, tmp_path)
        _install_fake_mux(monkeypatch, {})
        with pytest.raises(NotPairedError, match='Trust This Computer'):
            pairing.PairingRecord.load()


class TestFallbackToUsbmux:
    def test_empty_directory_asks_usbmuxd(self, tmp_path, monkeypatch) -> None:
        'Missing from the system directory (common on macOS) -> ask usbmuxd.'
        _point_dirs(monkeypatch, tmp_path / "missing")
        _install_fake_mux(monkeypatch, {"EXAMPLE-0002": _RECORD})
        record = pairing.PairingRecord.load("EXAMPLE-0002")
        assert record.udid == "EXAMPLE-0002"
        assert record.host_certificate == b"cert"
        assert record.path is None

    def test_eperm_on_the_file_falls_through(self, tmp_path, monkeypatch) -> None:
        'A file that exists but cannot be read (macOS ``/var/db/lockdown`` is EPERM) must still fall back'

        class _EpermPlist:
            'make only ``load`` fail while ``loads`` keeps working (on 3.14 loads itself depends on load'

            def __init__(self) -> None:
                self.loads = plistlib.loads
                self.dumps = plistlib.dumps

            def load(self, _handle):
                raise PermissionError(13, "Permission denied")

        (tmp_path / "EXAMPLE00-0003.plist").write_bytes(plistlib.dumps(_RECORD))
        _point_dirs(monkeypatch, tmp_path)
        monkeypatch.setattr(pairing, "plistlib", _EpermPlist())
        _install_fake_mux(monkeypatch, {"EXAMPLE00-0003": _RECORD})
        assert pairing.PairingRecord.load("EXAMPLE00-0003").udid == "EXAMPLE00-0003"

    def test_unlistable_directory_is_skipped(self, tmp_path, monkeypatch) -> None:
        'A directory that refuses listing (``drwx-----x``) must also keep going instead of raising.'
        def boom(_self, _pattern):
            raise PermissionError(13, "Permission denied")

        _point_dirs(monkeypatch, tmp_path)
        monkeypatch.setattr(Path, "glob", boom)
        _install_fake_mux(monkeypatch, {"EXAMPLE00-0004": _RECORD})
        assert pairing.PairingRecord.load("EXAMPLE00-0004").udid == "EXAMPLE00-0004"

    def test_without_udid_uses_the_first_attached_device(self, tmp_path,
                                                         monkeypatch) -> None:
        _point_dirs(monkeypatch, tmp_path / "missing")
        _install_fake_mux(monkeypatch, {"EXAMPLE00-0005": _RECORD},
                          attached=["EXAMPLE00-0005", "EXAMPLE00-0006"])
        assert pairing.PairingRecord.load().udid == "EXAMPLE00-0005"

    def test_usbmux_cannot_help_either(self, tmp_path, monkeypatch) -> None:
        'With neither source: raise a readable message (USB / trust hints), never a traceback.'
        _point_dirs(monkeypatch, tmp_path / "missing")
        monkeypatch.setattr(usbmux, "Mux", lambda *args, **kwargs: _FakeMux({}))
        with pytest.raises(NotPairedError, match='Trust This Computer'):
            pairing.PairingRecord.load("EXAMPLE00-0007")

    def test_usbmux_unreachable_is_not_an_error(self, tmp_path, monkeypatch) -> None:
        'If the fallback itself raises, treat it as "none"; the same readable message comes out.'
        def boom(*_args, **_kwargs):
            raise OSError("no usbmuxd")

        _point_dirs(monkeypatch, tmp_path / "missing")
        monkeypatch.setattr(usbmux, "Mux", boom)
        with pytest.raises(NotPairedError):
            pairing.PairingRecord.load("EXAMPLE00-0008")
