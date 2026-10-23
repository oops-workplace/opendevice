from __future__ import annotations

import os
import plistlib
import ssl
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..core.errors import NotPairedError
from ..core.paths import LOCKDOWN_DIRS

DIRS = LOCKDOWN_DIRS


@dataclass
class PairingRecord:
    udid: str
    host_id: str = ""
    system_buid: str = ""
    host_certificate: bytes = b""
    host_private_key: bytes = b""
    device_certificate: bytes = b""
    root_certificate: bytes = b""
    escrow_bag: bytes = b""
    path: Optional[str] = None
    _temp_files: list[str] = field(default_factory=list)
    _context: Optional[ssl.SSLContext] = field(default=None, repr=False, compare=False)

    @classmethod
    def load(cls, udid: Optional[str] = None) -> "PairingRecord":
        'Load the pairing record of one device.'
        for directory in DIRS.get(sys.platform) or []:
            base = Path(directory)
            if not base.is_dir():
                continue
            if udid:
                candidates = [base / f"{udid}.plist"]
            else:
                try:
                    candidates = sorted(p for p in base.glob("*.plist")
                                        if p.name != "SystemConfiguration.plist")
                except OSError:
                    continue
            for path in candidates:
                if not path.is_file():
                    continue
                try:
                    with open(path, "rb") as handle:
                        data = plistlib.load(handle)
                except OSError:
                    continue
                if not data.get("HostCertificate") or not data.get("HostPrivateKey"):
                    continue
                return cls(
                    udid=udid or path.stem,
                    host_id=str(data.get("HostID", "")),
                    system_buid=str(data.get("SystemBUID", "")),
                    host_certificate=data.get("HostCertificate", b""),
                    host_private_key=data.get("HostPrivateKey", b""),
                    device_certificate=data.get("DeviceCertificate", b""),
                    root_certificate=data.get("RootCertificate", b""),
                    escrow_bag=data.get("EscrowBag", b""),
                    path=str(path),
                )
        record = cls._load_from_usbmux(udid)
        if record is not None:
            return record
        raise NotPairedError(
            f"No pairing record for {udid or 'any device'}. Unlock the device and tap \"Trust This Computer\" over USB.\nRecord directories: {' / '.join(DIRS.get(sys.platform) or []) or '(unknown)'}"
        )

    @classmethod
    def _load_from_usbmux(cls, udid: Optional[str]) -> Optional["PairingRecord"]:
        'Ask usbmuxd for the pairing record (``ReadPairRecord``).'
        from . import usbmux as usbmux_module

        target = udid or ""
        try:
            with usbmux_module.Mux() as mux:
                if not target:
                    devices = mux.list_devices()
                    if not devices:
                        return None
                    target = devices[0].udid
                raw = mux.read_pair_record(target)
        except Exception:                            # noqa
            return None
        try:
            data = plistlib.loads(raw) if raw else {}
        except Exception:                            # noqa: BLE001
            return None
        if not data.get("HostCertificate") or not data.get("HostPrivateKey"):
            return None
        return cls(
            udid=target,
            host_id=str(data.get("HostID", "")),
            system_buid=str(data.get("SystemBUID", "")),
            host_certificate=data.get("HostCertificate", b""),
            host_private_key=data.get("HostPrivateKey", b""),
            device_certificate=data.get("DeviceCertificate", b""),
            root_certificate=data.get("RootCertificate", b""),
            escrow_bag=data.get("EscrowBag", b""),
        )

    def ssl_context(self) -> ssl.SSLContext:
        'Client context used by lockdownd / services that need SSL.'
        if self._context is None:
            certfile = self._write_temp("cert", self.host_certificate
                                        + b"\n" + self.device_certificate)
            keyfile = self._write_temp("key", self.host_private_key)
            try:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                context.load_cert_chain(certfile=certfile, keyfile=keyfile)
            finally:
                self.cleanup()
            self._context = context
        return self._context

    def _write_temp(self, tag: str, data: bytes) -> str:
        'Write a PEM into the package\'s temp directory (0600, unpredictable name).'
        from ..core import paths

        directory = paths.ensure(paths.tmp_dir())
        path = directory / f"pair-{tag}-{uuid.uuid4().hex}.pem"
        payload = data if data.endswith(b"\n") else data + b"\n"
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(handle, payload)
        finally:
            os.close(handle)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self._temp_files.append(str(path))
        return str(path)

    def cleanup(self) -> None:
        for name in self._temp_files:
            try:
                os.unlink(name)
            except OSError:
                pass
        self._temp_files.clear()
