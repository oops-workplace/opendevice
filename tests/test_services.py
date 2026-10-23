from __future__ import annotations

import hashlib
import os
import plistlib
import socket
import threading
import time
from pathlib import Path

import pytest

from opendevice.core import paths, plistio
from opendevice.core.errors import (
    AfcError,
    DeveloperModeError,
    InstallError,
    OpenDeviceError,
    ServiceError,
)
from opendevice.remote import coredevice
from opendevice.remote.rsd import RSD
from opendevice.services import afc as afc_module
from opendevice.services import amfi, diagnostics, installation, mounter, syslog, tss
from opendevice.transport.service import IpStackSocket, ServiceConnection, open_service
from opendevice.wireless import rrp

STATUS_OK = b"\x00" * 8


def _pair() -> "tuple[socket.socket, socket.socket]":
    left, right = socket.socketpair()
    left.settimeout(2.0)
    right.settimeout(2.0)
    return left, right


def _plist_bytes(obj) -> bytes:
    body = plistlib.dumps(obj)
    return len(body).to_bytes(4, "big") + body


class _Device:
    'Play the device.'

    def __init__(self, replies: list, afc: bool = False, *, echo: bool = False) -> None:
        self.client, self.server = _pair()
        self.requests: list = []
        self._thread = threading.Thread(target=self._run, args=(replies, afc, echo),
                                        daemon=True)
        self._thread.start()

    def _run(self, replies: list, afc: bool, echo: bool = False) -> None:
        if isinstance(replies, dict):
            seen: dict[str, int] = {}
            while True:
                try:
                    request = plistio.recv_plist(self.server)
                except OSError:
                    return
                self.requests.append(request)
                key = str(request.get("Command"))
                entry = replies.get(key)
                if entry is None:
                    return
                if isinstance(entry, list):
                    index = seen.get(key, 0)
                    reply = entry[min(index, len(entry) - 1)]
                    seen[key] = index + 1
                else:
                    reply = entry
                self.server.sendall(_plist_bytes(reply))
            return
        if echo:
            for reply in replies:
                try:
                    self.requests.append(plistio.recv_plist(self.server))
                except OSError:
                    return
                self.server.sendall(_plist_bytes(reply))
            return
        if afc:
            for reply in replies:
                raw = plistio.recv_exact(self.server, afc_module.HEADER_SIZE)
                _magic, entire, _this, num, operation = afc_module.HEADER.unpack(raw)
                body = plistio.recv_exact(self.server, entire - afc_module.HEADER_SIZE) \
                    if entire > afc_module.HEADER_SIZE else b""
                self.requests.append((operation, body, num))
                if isinstance(reply, int):
                    error = reply.to_bytes(8, "little")
                    self.server.sendall(
                        afc_module.build_header(afc_module.OP_STATUS, error) + error)
                else:
                    self.server.sendall(
                        afc_module.build_header(afc_module.OP_DATA, reply) + reply)
            return
        self.requests.append(plistio.recv_plist(self.server))
        for reply in replies:
            self.server.sendall(_plist_bytes(reply))

    def conn(self) -> ServiceConnection:
        return ServiceConnection(self.client, "fake")

    def close(self) -> None:
        self.client.close()
        self.server.close()


class TestAfcWire:
    def test_header_layout(self) -> None:
        '40-byte little-endian header: magic / entire / this / packet_num / operation.'
        header = afc_module.build_header(afc_module.OP_MAKE_DIR, b"/a\x00", packet_num=7)
        assert len(header) == 40
        magic, entire, this, num, op = afc_module.HEADER.unpack(header)
        assert magic == b"CFA6LPAA"
        assert (entire, this, num, op) == (43, 43, 7, afc_module.OP_MAKE_DIR)

    def test_write_this_length_counts_handle_only(self) -> None:
        "``WRITE``'s ``this_length`` only counts up to the handle (40 + 8); the data is not included."
        header = afc_module.build_header(afc_module.OP_WRITE, b"\x01" * 12, this_length=48)
        _magic, entire, this, _num, _op = afc_module.HEADER.unpack(header)
        assert entire == 52
        assert this == 48

    def test_parse_stat_types(self) -> None:
        info = afc_module.parse_stat(
            b"st_size\x0012\x00st_ifmt\x00S_IFREG\x00st_mtime\x005\x00")
        assert info["st_size"] == 12
        assert info["st_mtime"] == 5
        assert info["st_ifmt"] == "S_IFREG"


class TestAfcClient:
    def test_mkdir_sends_path_with_nul(self) -> None:
        device = _Device([0], afc=True)
        afc_module.Afc(device.conn()).mkdir("/PublicStaging")
        device._thread.join(2.0)
        operation, body, _num = device.requests[0]
        assert operation == afc_module.OP_MAKE_DIR
        assert body == b"/PublicStaging\x00"
        device.close()

    def test_stat_and_size(self) -> None:
        reply = b"st_size\x003\x00st_ifmt\x00S_IFREG\x00"
        device = _Device([reply, reply], afc=True)
        afc = afc_module.Afc(device.conn())
        assert afc.stat("/a")["st_size"] == 3
        assert afc.size("/a") == 3
        assert device.requests[0][0] == afc_module.OP_GET_FILE_INFO
        device.close()

    def test_error_status_raises(self) -> None:
        device = _Device([8], afc=True)
        with pytest.raises(AfcError, match="OBJECT_NOT_FOUND"):
            afc_module.Afc(device.conn()).stat("/nope")
        device.close()

    def test_missing_path_reports_false(self) -> None:
        device = _Device([8], afc=True)
        assert afc_module.Afc(device.conn()).exists("/nope") is False
        device.close()

    def test_listdir_returns_names(self) -> None:
        device = _Device([b"a\x00b\x00"], afc=True)
        assert afc_module.Afc(device.conn()).listdir("/") == ["a", "b"]
        device.close()

    def test_listdir_drops_dot_entries(self) -> None:
        'The device returns ``.`` / ``..`` in the root (seen on iOS 27); filter them out.'
        device = _Device([b".\x00..\x00DCIM\x00"], afc=True)
        assert afc_module.Afc(device.conn()).listdir("/") == ["DCIM"]
        device.close()

    def test_write_streams_chunks_with_handle(self) -> None:
        'Write a file: ``FILE_OPEN`` for a handle -> ``WRITE`` with it -> ``FILE_CLOSE``.'
        device = _Device([(3).to_bytes(8, "little"), 0, 0], afc=True)
        afc_module.Afc(device.conn()).write("/staged.ipa", b"hello")
        device._thread.join(2.0)
        operations = [item[0] for item in device.requests]
        assert operations == [afc_module.OP_FILE_OPEN, afc_module.OP_WRITE,
                              afc_module.OP_FILE_CLOSE]
        assert device.requests[1][1][:8] == (3).to_bytes(8, "little")
        assert device.requests[1][1][8:] == b"hello"
        device.close()

    def test_read_returns_file_bytes(self) -> None:
        'Read a file: stat for the size -> open a handle -> read -> close.'
        device = _Device([b"st_size\x004\x00st_ifmt\x00S_IFREG\x00",
                          (9).to_bytes(8, "little"), b"data", 0], afc=True)
        assert afc_module.Afc(device.conn()).read("/a") == b"data"
        device.close()

    def test_read_reports_progress_per_chunk(self, monkeypatch) -> None:
        'Read with on_progress: call back per chunk with the accumulated byte count (small chunks give several).'
        monkeypatch.setattr(afc_module, "CHUNK", 2)
        device = _Device([b"st_size\x004\x00st_ifmt\x00S_IFREG\x00",
                          (9).to_bytes(8, "little"), b"da", b"ta", 0], afc=True)
        seen: list = []
        got = afc_module.Afc(device.conn()).read("/a", on_progress=seen.append)
        assert got == b"data"
        assert seen == [2, 4]
        device.close()

    def test_pull_streams_to_file_with_progress(self, tmp_path, monkeypatch) -> None:
        'Pull a file: write to disk as it is read; on_progress reports accumulated bytes.'
        monkeypatch.setattr(afc_module, "CHUNK", 2)
        stat = b"st_ifmt\x00S_IFREG\x00st_size\x004\x00"
        device = _Device([stat, stat, (9).to_bytes(8, "little"), b"da", b"ta", 0],
                         afc=True)
        target = tmp_path / "out.bin"
        seen: list = []
        afc_module.Afc(device.conn()).pull("/a", str(target), on_progress=seen.append)
        assert target.read_bytes() == b"data"
        assert seen == [2, 4]
        device.close()

    def test_pull_without_progress_still_copies(self, tmp_path, monkeypatch) -> None:
        'Without on_progress: no callbacks, but the file is still pulled.'
        monkeypatch.setattr(afc_module, "CHUNK", 2)
        stat = b"st_ifmt\x00S_IFREG\x00st_size\x004\x00"
        device = _Device([stat, stat, (9).to_bytes(8, "little"), b"da", b"ta", 0],
                         afc=True)
        target = tmp_path / "out.bin"
        afc_module.Afc(device.conn()).pull("/a", str(target))
        assert target.read_bytes() == b"data"
        device.close()

    def test_push_reports_progress_per_chunk(self, tmp_path, monkeypatch) -> None:
        'Push with on_progress: call back per chunk (4 = directory missing, which exercises mkdir).'
        monkeypatch.setattr(afc_module, "CHUNK", 2)
        device = _Device([4, 0, (9).to_bytes(8, "little"), 0, 0, 0], afc=True)
        source = tmp_path / "in.bin"
        source.write_bytes(b"data")
        seen: list = []
        afc_module.Afc(device.conn()).push(str(source), "/Public/in.bin",
                                           on_progress=seen.append)
        assert seen == [2, 4]
        device.close()


class TestAfcSandbox:
    "house_arrest: a plist exchange on the **same connection** first, then it becomes that app's AFC."

    @pytest.mark.parametrize("documents,command", [
        (False, afc_module.VEND_CONTAINER),
        (True, afc_module.VEND_DOCUMENTS),
    ])
    def test_vend_command_goes_first(self, documents: bool, command: str) -> None:
        client, server = _pair()
        seen: list = []

        def fake_device() -> None:
            seen.append(plistio.recv_plist(server))
            server.sendall(_plist_bytes({"Status": "Complete"}))
            raw = plistio.recv_exact(server, afc_module.HEADER_SIZE)
            seen.append(afc_module.HEADER.unpack(raw))
            reply = b"Documents\x00Library\x00"
            server.sendall(afc_module.build_header(afc_module.OP_DATA, reply) + reply)

        thread = threading.Thread(target=fake_device, daemon=True)
        thread.start()

        class FakeLockdown:
            def start_service(self, name: str, use_ssl: object = None) -> ServiceConnection:
                assert name == afc_module.SERVICE_HOUSE_ARREST
                return ServiceConnection(client)

        with afc_module.Afc.open_app(FakeLockdown(), None, "com.example.app",
                                     documents=documents) as afc:
            assert afc.listdir("/") == ["Documents", "Library"]
        thread.join(2.0)
        assert seen[0] == {"Command": command, "Identifier": "com.example.app"}
        assert seen[1][4] == afc_module.OP_READ_DIR

    def test_status_must_be_complete(self) -> None:
        afc_module.check_vend({"Status": "Complete"})
        with pytest.raises(AfcError, match="Failure"):
            afc_module.check_vend({"Status": "Failure"})

    def test_lookup_failure_explains_itself(self) -> None:
        'Readable message when the device refuses: old versions say ``ApplicationLookupFailed``, iOS 27 says ``Installation...``.'
        for error in ("ApplicationLookupFailed", "InstallationLookupFailed"):
            with pytest.raises(AfcError, match='developer-signed'):
                afc_module.check_vend({"Error": error})


class TestAfcPathHelpers:
    def test_rel_helper(self) -> None:
        'Local layout: ``_rel`` only yields the components relative to the root.'
        assert afc_module._rel("/a/b/c", "/a") == ["b", "c"]
        assert afc_module._rel("/a", "/a") == []


class TestInstallation:
    def test_uninstall_request_shape(self) -> None:
        device = _Device([{"Status": "Complete", "PercentComplete": 100}])
        installation.InstallationProxy(device.conn()).uninstall("com.example.app")
        device._thread.join(2.0)
        sent = device.requests[0]
        assert sent["Command"] == "Uninstall"
        assert sent["ApplicationIdentifier"] == "com.example.app"
        device.close()

    def test_install_drains_progress_until_complete(self) -> None:
        device = _Device([
            {"Status": "CreatingStagingDirectory", "PercentComplete": 5},
            {"Status": "InstallingPackage", "PercentComplete": 60},
            {"Status": "Complete", "PercentComplete": 100},
        ])
        proxy = installation.InstallationProxy(device.conn())
        seen: list = []
        proxy._run("Install", {"PackagePath": "/PublicStaging/a.ipa", "ClientOptions": {}},
                   on_progress=lambda percent, status: seen.append((percent, status)))
        assert [item[0] for item in seen] == [5, 60, 100]
        assert device.requests[0]["PackagePath"] == "/PublicStaging/a.ipa"
        device.close()

    def test_install_error_raises(self) -> None:
        device = _Device([{"Error": "ApplicationVerificationFailed",
                           "ErrorDescription": "bad signature"}])
        proxy = installation.InstallationProxy(device.conn())
        with pytest.raises(InstallError, match="ApplicationVerificationFailed"):
            proxy._run("Install", {})
        device.close()

    def test_browse_accumulates_current_list(self) -> None:
        device = _Device([
            {"Status": "BrowsingApplications", "CurrentList": [{"CFBundleIdentifier": "a"}]},
            {"Status": "BrowsingApplications", "CurrentList": [{"CFBundleIdentifier": "b"}]},
            {"Status": "Complete", "CurrentList": []},
        ])
        apps = installation.InstallationProxy(device.conn()).apps()
        assert [app["CFBundleIdentifier"] for app in apps] == ["a", "b"]
        assert device.requests[0]["Command"] == "Browse"
        assert "CFBundleIdentifier" in device.requests[0]["ClientOptions"]["ReturnAttributes"]
        device.close()

    def test_lookup_returns_lookup_result(self) -> None:
        device = _Device([{"LookupResult": {"k": {"CFBundleIdentifier": "k"}}}])
        proxy = installation.InstallationProxy(device.conn())
        assert proxy.lookup(["k"])["k"]["CFBundleIdentifier"] == "k"
        device.close()

    def test_staging_afc_is_lazy(self) -> None:
        'No AFC and no link -> a clear error, not a silent failure.'
        left, _right = _pair()
        proxy = installation.InstallationProxy(ServiceConnection(left))
        with pytest.raises(ServiceError, match="AFC"):
            proxy._staging()


class TestDiagnostics:
    def test_restart_request(self) -> None:
        device = _Device([{"Status": "Success", "Diagnostics": {}}])
        diagnostics.DiagnosticsRelay(device.conn()).restart()
        device._thread.join(2.0)
        assert device.requests[0] == {"Request": "Restart"}
        device.close()

    def test_info_returns_diagnostics_payload(self) -> None:
        device = _Device([{"Status": "Success",
                           "Diagnostics": {"BatteryCurrentCapacity": 88}}])
        assert diagnostics.DiagnosticsRelay(device.conn()).info()["BatteryCurrentCapacity"] == 88
        device.close()

    def test_battery_uses_iopmpowersource(self) -> None:
        device = _Device([{"Status": "Success",
                           "Diagnostics": {"IORegistry": {"CurrentCapacity": 50}}}])
        relay = diagnostics.DiagnosticsRelay(device.conn())
        assert relay.battery()["CurrentCapacity"] == 50
        device._thread.join(2.0)
        assert device.requests[0]["EntryClass"] == "IOPMPowerSource"
        assert device.requests[0]["Request"] == "IORegistry"
        device.close()

    def test_failure_status_raises(self) -> None:
        device = _Device([{"Status": "Failure", "Diagnostics": {}}])
        with pytest.raises(ServiceError, match="Failure"):
            diagnostics.DiagnosticsRelay(device.conn()).info()
        device.close()


class TestSyslog:
    def test_split_lines_keeps_partial(self) -> None:
        lines, rest = syslog.split_lines(b"one\n\x00two\n\x00thr")
        assert lines == ["one", "two"]
        assert rest == b"thr"

    def test_split_lines_without_splitter(self) -> None:
        assert syslog.split_lines(b"nope") == ([], b"nope")

    def test_split_lines_skips_empty(self) -> None:
        assert syslog.split_lines(b"\n\x00\n\x00") == ([], b"")

    def test_recv_decodes_utf8(self) -> None:
        client, server = _pair()
        server.sendall('2026-01-01 00:00:00 sample log line\n\x00'.encode())
        assert syslog.SyslogRelay(ServiceConnection(client)).recv() == ['2026-01-01 00:00:00 sample log line']
        client.close()
        server.close()

    def test_recv_keeps_partial_line(self) -> None:
        client, server = _pair()
        server.sendall(b"half")
        relay = syslog.SyslogRelay(ServiceConnection(client), timeout=0.2)
        assert relay.recv() == []
        server.sendall(b" done\n\x00")
        assert relay.recv() == ["half done"]
        client.close()
        server.close()

    def test_follow_stops_on_eof(self) -> None:
        client, server = _pair()
        server.sendall(b"a\n\x00")
        server.close()
        relay = syslog.SyslogRelay(ServiceConnection(client), timeout=0.2)
        seen: list = []
        assert relay.follow(None, on_line=seen.append) == 1
        assert seen == ["a"]
        client.close()


class TestDeveloperMode:
    def test_action_codes(self) -> None:
        assert (amfi.ACTION_REVEAL, amfi.ACTION_ENABLE, amfi.ACTION_ACCEPT) == (0, 1, 2)

    def test_reveal_sends_action_zero(self) -> None:
        device = _Device([{"success": True}])

        class FakeLockdown:
            def start_service(self, name, use_ssl=None):
                assert name == amfi.SERVICE
                return device.conn()

        amfi.AmfiService(FakeLockdown()).reveal()
        device._thread.join(2.0)
        assert device.requests[0] == {"action": amfi.ACTION_REVEAL}
        device.close()

    def test_failure_raises_developer_mode_error(self) -> None:
        device = _Device([{"success": False, "error": "DeviceHasPasscodeSet"}])

        class FakeLockdown:
            def start_service(self, name, use_ssl=None):
                return device.conn()

        with pytest.raises(DeveloperModeError, match='passcode'):
            amfi.AmfiService(FakeLockdown()).request_enable()
        device.close()


_PERSONALIZED_ENTRY = {
    "BackingImage": "/private/var/db/com.example.security.cryptexd/codex.system/cryptex/"
                    "com.apple.MobileAsset.DDI/gdmg",
    "DiskImageType": "Personalized",
    "IsMounted": True,
    "MountPath": "/System/Developer",
    "PersonalizedImageType": "DeveloperDiskImage",
    "PersonalizedImageVersion": "642.16",
    "PersonalizedImageVersionInfo": {
        "BuildVersion": "1473",
        "ProductBuildVersion": "ExampleBuild1",
        "ProjectMetadata": [{"Project": "XCTest", "Version": "25228"},
                            {"Project": "CoreDevice", "Version": "642.16"}],
    },
}


_BUILD_MANIFEST = {
    "BuildIdentities": [{
        "ApBoardID": "0x6",
        "ApChipID": "0x8100",
        "Manifest": {
            "PersonalizedDMG": {"Trusted": True,
                                "Digest": hashlib.sha384(b"img").digest(),
                                "Info": {"Personalize": True}},
            "Cryptex1,GenericDmg": {"Digest": b"\x02" * 32,
                                    "Info": {"Personalize": False}},
        },
    }],
}
_IDENTIFIERS = {
    "BoardId": 0x6,
    "ChipID": 0x8100,
    "Ap,SecurityDomain": 1,
}


class _FakeRsd:
    'A fake RSD carrying only the service table (version parsing needs no real tunnel).'

    def __init__(self, services: dict) -> None:
        self.services = services
        self.core_device_version = None


def _bare_rsd(services: dict | None = None) -> RSD:
    "An RSD without a tunnel - only to check ``service_entry``'s error text (``__new__`` skips the handshake)."
    rsd = RSD.__new__(RSD)
    rsd.peer_info = {"Services": services or {}}
    rsd.ddi_probe = None
    return rsd


class TestMounter:
    def test_lookup_present(self) -> None:
        device = _Device([{"ImagePresent": True, "ImageSignature": b"sig"}])
        assert mounter.MobileImageMounter(device.conn()).lookup() == b"sig"
        device.close()

    def test_lookup_absent(self) -> None:
        device = _Device([{"ImagePresent": False}])
        assert mounter.MobileImageMounter(device.conn()).lookup() is None
        device.close()

    def test_signature_list_takes_first(self) -> None:
        device = _Device([{"ImagePresent": True, "ImageSignature": [b"sig", b"other"]}])
        assert mounter.MobileImageMounter(device.conn()).lookup() == b"sig"
        device.close()

    def test_developer_mode_status(self) -> None:
        device = _Device([{"DeveloperModeStatus": True}])
        assert mounter.MobileImageMounter(device.conn()).developer_mode_status() is True
        device.close()

    def test_developer_mode_status_unsupported(self) -> None:
        device = _Device([{"Status": "Success"}])
        assert mounter.MobileImageMounter(device.conn()).developer_mode_status() is None
        device.close()

    def test_nonce(self) -> None:
        device = _Device([{"PersonalizationNonce": b"\x01\x02"}])
        assert mounter.MobileImageMounter(device.conn()).nonce() == b"\x01\x02"
        device.close()

    def test_mounted_path_and_core_device_version(self) -> None:
        'Mount path and CoreDevice version both come from the entry - read the version live, do not hardcode it.'
        device = _Device([{"EntryList": [_PERSONALIZED_ENTRY]},
                          {"EntryList": [_PERSONALIZED_ENTRY]},
                          {"EntryList": [{"DiskImageType": "Developer", "IsMounted": True,
                                          "MountPath": "/Developer"}]}], echo=True)
        service = mounter.MobileImageMounter(device.conn())
        assert service.mounted_path() == "/System/Developer"
        assert service.core_device_version() == "642.16"
        assert service.mounted_path(mounter.IMAGE_DEVELOPER) == "/Developer"
        device.close()

    def test_core_device_version_falls_back_to_image_version(self) -> None:
        entry = {"DiskImageType": "Personalized", "IsMounted": True,
                 "MountPath": "/System/Developer", "PersonalizedImageVersion": "700.1"}
        device = _Device([{"EntryList": [entry]}])
        assert mounter.MobileImageMounter(device.conn()).core_device_version() == "700.1"
        device.close()

    def test_unmount_sends_mount_path(self) -> None:
        device = _Device([{"Status": "Success"}])
        path = mounter.MobileImageMounter(device.conn()).unmount("/System/Developer")
        device._thread.join(2.0)
        assert device.requests[0] == {"Command": "UnmountImage",
                                      "MountPath": "/System/Developer"}
        assert path == "/System/Developer"
        device.close()

    def test_unmount_without_path_asks_devices_first(self) -> None:
        'Without a path, unmount the image currently mounted (asks CopyDevices first).'
        device = _Device([{"EntryList": [_PERSONALIZED_ENTRY]}, {"Status": "Success"}],
                         echo=True)
        path = mounter.MobileImageMounter(device.conn()).unmount()
        assert device.requests[0] == {"Command": "CopyDevices"}
        assert device.requests[1] == {"Command": "UnmountImage",
                                      "MountPath": "/System/Developer"}
        assert path == "/System/Developer"
        device.close()

    def test_unmount_nothing_mounted(self) -> None:
        device = _Device([{"EntryList": []}])
        with pytest.raises(ServiceError, match='nothing to unmount'):
            mounter.MobileImageMounter(device.conn()).unmount()
        device.close()

    def test_unmount_translates_launchd_error(self) -> None:
        'When the device refuses the unmount (DDI launchd jobs cannot be unloaded), explain it and point at the reboot.'
        device = _Device([{"Error": "InternalError",
                           "DetailedError": 'Failed to unmount /System/Developer: '
                                            'Error Domain=com.apple.MobileStorage.ErrorDomain '
                                            'Code=-2 "Failed to unload launchd jobs."'}])
        with pytest.raises(ServiceError) as info:
            mounter.MobileImageMounter(device.conn()).unmount("/System/Developer")
        text = str(info.value)
        assert 'reboot' in text and "devmode enable" in text
        device.close()

    def test_upload_image_wire(self) -> None:
        'ReceiveBytes: plist header -> ReceiveBytesAck -> **raw bytes** -> Complete.'
        server, client = _pair()
        seen: dict = {}

        def run() -> None:
            seen["head"] = plistio.recv_plist(server)
            plistio.send_plist(server, {"Status": "ReceiveBytesAck"})
            seen["body"] = plistio.recv_exact(server, 5)
            plistio.send_plist(server, {"Status": "Complete"})

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        mounter.MobileImageMounter(ServiceConnection(client)).upload_image(
            b"12345", b"sig", mounter.IMAGE_PERSONALIZED)
        thread.join(2.0)
        assert seen["head"] == {"Command": "ReceiveBytes", "ImageType": "Personalized",
                                "ImageSize": 5, "ImageSignature": b"sig"}
        assert seen["body"] == b"12345"
        client.close()
        server.close()

    def test_upload_image_rejects_bad_ack(self) -> None:
        device = _Device([{"Status": "Failure"}])
        with pytest.raises(ServiceError, match='ReceiveBytes refused'):
            mounter.MobileImageMounter(device.conn()).upload_image(b"1", b"sig")
        device.close()

    def test_mount_image_translates_dev_mode_off(self) -> None:
        device = _Device([{"Error": "Failure",
                           "DetailedError": "Developer mode is not enabled"}])
        with pytest.raises(ServiceError, match='Developer mode'):
            mounter.MobileImageMounter(device.conn()).mount_image(b"sig")
        device.close()

    def test_mount_refuses_when_already_mounted(self) -> None:
        device = _Device([{"EntryList": [_PERSONALIZED_ENTRY]}])
        with pytest.raises(ServiceError, match='already mounted'):
            mounter.MobileImageMounter(device.conn()).mount(image=b"x")
        device.close()

    def test_mount_needs_an_image_when_cache_is_empty(self, tmp_path, monkeypatch) -> None:
        'No usable image in the cache: the error must name the three pieces and where to get them.'
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
        device = _Device({
            "CopyDevices": {"EntryList": []},
            "QueryDeveloperModeStatus": {"DeveloperModeStatus": True},
            "QueryPersonalizationIdentifiers": {
                "PersonalizationIdentifiers": _IDENTIFIERS},
        })
        with pytest.raises(FileNotFoundError, match="BuildManifest.plist"):
            mounter.MobileImageMounter(device.conn()).mount()
        device.close()

    def test_mount_without_manifest_needs_the_build_manifest(self, tmp_path,
                                                             monkeypatch) -> None:
        'No cached signature -> TSS, and TSS cannot work without BuildManifest.plist: name the missing file.'
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
        device = _Device({
            "CopyDevices": {"EntryList": []},
            "QueryDeveloperModeStatus": {"DeveloperModeStatus": True},
            "QueryPersonalizationIdentifiers": {
                "PersonalizationIdentifiers": _IDENTIFIERS},
            "QueryPersonalizationManifest": {"Status": "Success"},
        })
        with pytest.raises(FileNotFoundError, match="BuildManifest.plist"):
            mounter.MobileImageMounter(device.conn()).mount(image=b"img")
        device.close()

    def test_mount_signs_via_tss(self, tmp_path, monkeypatch) -> None:
        'No cache means signing now: identity -> nonce -> TSS ticket -> push image -> mount with the trust cache.'
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
        paths.ensure(paths.ddi_dir())
        (paths.ddi_dir() / "BuildManifest.plist").write_bytes(
            plistlib.dumps(_BUILD_MANIFEST))
        (paths.ddi_dir() / "Image.trustcache").write_bytes(b"tc")

        device = _Device({
            "CopyDevices": [{"EntryList": []},
                            {"EntryList": [_PERSONALIZED_ENTRY]}],
            "QueryDeveloperModeStatus": {"DeveloperModeStatus": True},
            "QueryPersonalizationIdentifiers": {
                "PersonalizationIdentifiers": _IDENTIFIERS},
            "QueryPersonalizationManifest": {"Status": "Success"},
            "QueryNonce": {"PersonalizationNonce": b"\x01" * 32},
        })
        monkeypatch.setattr(tss, "img4_ticket",
                            lambda request, timeout=30.0: b"TICKET")
        seen: dict = {}
        monkeypatch.setattr(
            mounter.MobileImageMounter, "upload_image",
            lambda self, image, signature, image_type="Personalized":
                seen.update(signature=signature))
        monkeypatch.setattr(
            mounter.MobileImageMounter, "mount_image",
            lambda self, signature, image_type="Personalized", extras=None:
                seen.update(signature=signature, extras=dict(extras or {})))

        path = mounter.MobileImageMounter(device.conn()).mount(image=b"img", ecid=0x1234)
        assert path == "/System/Developer"
        assert seen["signature"] == b"TICKET"
        assert seen["extras"]["ImageTrustCache"] == b"tc"
        device.close()

    def test_manifest_error_means_no_cache(self) -> None:
        '"No cached signature" arrives as InternalError: translate it to None, do not raise.'
        device = _Device({"QueryPersonalizationManifest": {
            "Error": "InternalError",
            "DetailedError": "Failed to copy personalization manifest. "
                             "Failed to find a cached manifest."}})
        assert mounter.MobileImageMounter(device.conn()).personalization_manifest() is None
        device.close()

    def test_stale_manifest_means_no_cache(self) -> None:
        'The device caches another image: swapping builds always trips this, so treat it as "no cache".'
        device = _Device({"QueryPersonalizationManifest": {
            "Error": "InternalError",
            "DetailedError": "Failed to copy personalization manifest. "
                             "Failed to retrieve personalization manifest: "
                             "Existing manifest's digest does not match."}})
        assert mounter.MobileImageMounter(device.conn()).personalization_manifest() is None
        device.close()

    def test_mount_signs_via_tss_after_manifest_error(self, tmp_path, monkeypatch) -> None:
        'Real behaviour: "no cache" is an InternalError (then it drops the link) - still go through TSS.'
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
        paths.ensure(paths.ddi_dir())
        (paths.ddi_dir() / "BuildManifest.plist").write_bytes(
            plistlib.dumps(_BUILD_MANIFEST))
        (paths.ddi_dir() / "Image.trustcache").write_bytes(b"tc")

        device = _Device({
            "CopyDevices": [{"EntryList": []},
                            {"EntryList": [_PERSONALIZED_ENTRY]}],
            "QueryDeveloperModeStatus": {"DeveloperModeStatus": True},
            "QueryPersonalizationIdentifiers": {
                "PersonalizationIdentifiers": _IDENTIFIERS},
            "QueryPersonalizationManifest": {
                "Error": "InternalError",
                "DetailedError": "Failed to find a cached manifest."},
            "QueryNonce": {"PersonalizationNonce": b"\x01" * 32},
        })
        monkeypatch.setattr(tss, "img4_ticket",
                            lambda request, timeout=30.0: b"TICKET")
        monkeypatch.setattr(
            mounter.MobileImageMounter, "upload_image",
            lambda self, image, signature, image_type="Personalized": None)
        monkeypatch.setattr(
            mounter.MobileImageMounter, "mount_image",
            lambda self, signature, image_type="Personalized", extras=None: None)

        path = mounter.MobileImageMounter(device.conn()).mount(image=b"img", ecid=0x1234)
        assert path == "/System/Developer"
        device.close()

    def test_mount_refuses_without_ecid(self, tmp_path, monkeypatch) -> None:
        'TSS needs the ECID - say so instead of signing with an empty one.'
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
        paths.ensure(paths.ddi_dir())
        (paths.ddi_dir() / "BuildManifest.plist").write_bytes(
            plistlib.dumps(_BUILD_MANIFEST))
        device = _Device({
            "CopyDevices": {"EntryList": []},
            "QueryDeveloperModeStatus": {"DeveloperModeStatus": True},
            "QueryPersonalizationIdentifiers": {
                "PersonalizationIdentifiers": _IDENTIFIERS},
            "QueryPersonalizationManifest": {"Status": "Success"},
        })
        with pytest.raises(ServiceError, match="ECID"):
            mounter.MobileImageMounter(device.conn()).mount(image=b"img")
        device.close()

    def test_mount_refuses_mismatched_dmg(self, tmp_path, monkeypatch) -> None:
        'A dmg and BuildManifest that do not match: stop locally instead of a cryptic mount failure later.'
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
        paths.ensure(paths.ddi_dir())
        (paths.ddi_dir() / "BuildManifest.plist").write_bytes(
            plistlib.dumps(_BUILD_MANIFEST))
        device = _Device({
            "CopyDevices": {"EntryList": []},
            "QueryDeveloperModeStatus": {"DeveloperModeStatus": True},
            "QueryPersonalizationIdentifiers": {
                "PersonalizationIdentifiers": _IDENTIFIERS},
            "QueryPersonalizationManifest": {"Status": "Success"},
        })
        with pytest.raises(ServiceError, match='do not match'):
            mounter.MobileImageMounter(device.conn()).mount(image=b"other-build.dmg")
        device.close()

    def test_cached_image_finds_recursively(self, tmp_path, monkeypatch) -> None:
        'A dmg dropped in the image cache root is found too (no product/OS sub-directory needed).'
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
        paths.ensure(paths.ddi_dir())
        (paths.ddi_dir() / "iOS_DDI.dmg").write_bytes(b"dmg")
        assert mounter.cached_image() is not None
        assert mounter.cached_image() == paths.ddi_dir() / "iOS_DDI.dmg"


class TestResolveCoreDeviceVersion:
    'The CoreDevice version must be **read live** (from the mounted entry); fall back only when unreadable.'

    def test_uses_rsd_cache(self) -> None:
        rsd = _FakeRsd({mounter.SERVICE_RSD: {}})
        rsd.core_device_version = "700.1"
        assert mounter.resolve_core_device_version(rsd=rsd) == "700.1"

    def test_defaults_when_no_shim(self) -> None:
        'Without that shim on the RSD (old firmware / no image) do not connect pointlessly; use the fallback.'
        rsd = _FakeRsd({})
        assert mounter.resolve_core_device_version(rsd=rsd) == \
            coredevice.DEFAULT_CORE_DEVICE_VERSION
        assert rsd.core_device_version == coredevice.DEFAULT_CORE_DEVICE_VERSION

    def test_defaults_without_anything(self) -> None:
        assert mounter.resolve_core_device_version() == coredevice.DEFAULT_CORE_DEVICE_VERSION


def _make_image(tmp_path, build: str, board: str = "0x6", chip: str = "0x8100",
                types: tuple = ("ExampleProduct,1",)) -> Path:
    'Create one image in the cache (three pieces + manifest) for the picking / mounting cases.'
    directory = paths.ensure(paths.ddi_dir(None, build))
    (directory / "Image.dmg").write_bytes(b"dmg")
    (directory / "Image.dmg.trustcache").write_bytes(b"tc")
    (directory / "BuildManifest.plist").write_bytes(plistlib.dumps({
        "ProductBuildVersion": build,
        "SupportedProductTypes": list(types),
        "BuildIdentities": [{"ApBoardID": board, "ApChipID": chip, "Manifest": {}}],
    }))
    return directory


class TestDdiImages:
    'One image covers a family of models: several may coexist, picked by device before mounting.'

    @staticmethod
    def _make(tmp_path, build: str, board: str = "0x6", chip: str = "0x8100",
              types: tuple = ("ExampleProduct,1",)) -> Path:
        return _make_image(tmp_path, build, board=board, chip=chip, types=types)

    def test_order_is_coverage_then_freshness(self, tmp_path, monkeypatch) -> None:
        "Ordering ignores the build string (Apple's numbering is not monotonic): wider model coverage first, ties go to the newer entry."
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
        self._make(tmp_path, "ExampleBuild1")
        newer = self._make(tmp_path, "ExampleBuild9")
        os.utime(newer / "BuildManifest.plist", (time.time() + 10, time.time() + 10))
        found = mounter.list_images()
        assert [item.build for item in found] == ["ExampleBuild9", "ExampleBuild1"]
        assert found[0].trustcache is not None

        wide = self._make(tmp_path, "27A1000a", types=tuple(f"iPhone{i},1" for i in range(40)))
        os.utime(wide / "BuildManifest.plist", (time.time() - 100, time.time() - 100))
        assert mounter.list_images()[0].build == "27A1000a"

    def test_picks_the_first_that_fits(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
        self._make(tmp_path, "ExampleBuild1")
        newer = self._make(tmp_path, "ExampleBuild9")
        os.utime(newer / "BuildManifest.plist", (time.time() + 10, time.time() + 10))
        picked = mounter.pick_image("ExampleProduct,1", _IDENTIFIERS)
        assert picked is not None and picked.build == "ExampleBuild9"

    def test_skips_what_does_not_cover_this_device(self, tmp_path, monkeypatch) -> None:
        'Images whose model is not supported or whose identity cannot be picked must be skipped, not forced.'
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
        self._make(tmp_path, "ExampleBuild1", types=("iPad14,2",))
        self._make(tmp_path, "ExampleBuild5", board="0xFF", chip="0x9999")
        assert mounter.pick_image("ExampleProduct,1", _IDENTIFIERS) is None

    def test_still_finds_the_only_usable_one(self, tmp_path, monkeypatch) -> None:
        'When only one works, use it, not merely the newest.'
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
        good = self._make(tmp_path, "ExampleBuild1")
        self._make(tmp_path, "ExampleBuild9", board="0xFF", chip="0x9999")
        picked = mounter.pick_image("ExampleProduct,1", _IDENTIFIERS)
        assert picked is not None and picked.image.parent == good


class TestMountFallback:
    'With several usable: if one fails to mount, try the next; a specific one can also be requested.'

    @staticmethod
    def _device() -> "_Device":
        return _Device({
            "CopyDevices": [{"EntryList": []},
                            {"EntryList": [_PERSONALIZED_ENTRY]}],
            "QueryDeveloperModeStatus": {"DeveloperModeStatus": True},
            "QueryPersonalizationIdentifiers": {
                "PersonalizationIdentifiers": _IDENTIFIERS},
            "QueryPersonalizationManifest": {"Status": "Success"},
            "QueryNonce": {"PersonalizationNonce": b"\x01" * 32},
        })

    @staticmethod
    def _stub(monkeypatch, ticket, seen: dict) -> None:
        monkeypatch.setattr(tss, "img4_ticket", ticket)
        monkeypatch.setattr(
            mounter.MobileImageMounter, "upload_image",
            lambda self, image, signature, image_type="Personalized":
                seen.update(signature=signature))
        monkeypatch.setattr(
            mounter.MobileImageMounter, "mount_image",
            lambda self, signature, image_type="Personalized", extras=None: None)

    def test_falls_back_to_the_next_image(self, tmp_path, monkeypatch) -> None:
        'When the first cannot be signed (Apple says it does not match) -> try the next instead of failing.'
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
        _make_image(tmp_path, "ExampleBuild1")
        newer = _make_image(tmp_path, "ExampleBuild9")
        os.utime(newer / "BuildManifest.plist", (time.time() + 10, time.time() + 10))

        calls = {"n": 0}

        def ticket(request, timeout=30.0) -> bytes:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OpenDeviceError(
                    "Apple TSS rejected this request: isn't eligible for the requested build")
            return b"TICKET"

        seen: dict = {}
        self._stub(monkeypatch, ticket, seen)
        device = self._device()
        path = mounter.MobileImageMounter(device.conn()).mount(ecid=0x1234)
        assert path == "/System/Developer"
        assert calls["n"] == 2
        assert seen["signature"] == b"TICKET"
        device.close()

    def test_requested_build_is_used(self, tmp_path, monkeypatch) -> None:
        'A requested image is used even if it is not first in the ordering.'
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
        _make_image(tmp_path, "ExampleBuild1")
        newer = _make_image(tmp_path, "ExampleBuild9")
        os.utime(newer / "BuildManifest.plist", (time.time() + 10, time.time() + 10))

        notes: list[str] = []
        seen: dict = {}
        self._stub(monkeypatch, lambda request, timeout=30.0: b"TICKET", seen)
        device = self._device()
        mounter.MobileImageMounter(device.conn()).mount(ddi_build="ExampleBuild1",
                                                        ecid=0x1234,
                                                        on_status=notes.append)
        assert any("ExampleBuild1" in note for note in notes)
        assert not any("ExampleBuild9" in note for note in notes)
        device.close()


class TestFetchImage:
    'Fetch an image from a custom source (a local directory / share, or an HTTP directory).'

    @staticmethod
    def _source(tmp_path, build: str = "ExampleBuild1", *, bad_sum: bool = False,
                digest: "bytes | None" = None) -> Path:
        directory = tmp_path / "source" / build
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "Image.dmg").write_bytes(b"dmg")
        (directory / "Image.dmg.trustcache").write_bytes(b"tc")
        manifest: dict = {
            "ProductBuildVersion": build,
            "SupportedProductTypes": ["ExampleProduct,1"],
            "BuildIdentities": [{"ApBoardID": "0x6", "ApChipID": "0x8100",
                                 "Manifest": {}}],
        }
        if digest is not None:
            manifest["BuildIdentities"][0]["Manifest"]["PersonalizedDMG"] = {
                "Digest": digest}
        (directory / "BuildManifest.plist").write_bytes(plistlib.dumps(manifest))
        lines = [f"{hashlib.sha256((directory / n).read_bytes()).hexdigest()}  {n}"
                 for n in mounter.DDI_FILES]
        if bad_sum:
            lines[0] = "0" * 64 + "  Image.dmg"
        (directory / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return directory.parent

    def test_fetches_from_a_local_dir(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "home"))
        source = self._source(tmp_path)
        item = mounter.fetch_image("ExampleBuild1", base=str(source))
        assert item.build == "ExampleBuild1"
        assert (paths.ddi_dir(None, "ExampleBuild1") / "Image.dmg").read_bytes() == b"dmg"
        assert item.trustcache is not None

    def test_bad_checksum_is_rejected(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "home"))
        source = self._source(tmp_path, bad_sum=True)
        with pytest.raises(ServiceError, match='failed verification'):
            mounter.fetch_image("ExampleBuild1", base=str(source))

    def test_mismatched_source_is_rejected(self, tmp_path, monkeypatch) -> None:
        'A dmg and manifest from the source that do not match: stop while fetching, not at mount time.'
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "home"))
        source = self._source(tmp_path, digest=hashlib.sha384(b"another-build").digest())
        with pytest.raises(ServiceError, match='do not match'):
            mounter.fetch_image("ExampleBuild1", base=str(source))

    def test_fetch_defaults_to_the_project_mirror(self, tmp_path, monkeypatch) -> None:
        'With no --url and no env var, fall back to DEFAULT_DDI_URL instead of refusing to fetch.'
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "home"))
        monkeypatch.delenv(mounter.DDI_URL_ENV, raising=False)
        seen: list[str] = []

        def fake_read(base: str, build: str, name: str, token, optional: bool = False) -> bytes:
            seen.append(base)
            raise RuntimeError("sentinel")

        monkeypatch.setattr(mounter, "_read_source", fake_read)
        with pytest.raises(RuntimeError, match="sentinel"):
            mounter.fetch_image("ExampleBuild1")
        assert seen == [mounter.DEFAULT_DDI_URL]


class TestEnsureImage:
    'Mounting downloads the image on demand: cache first, source only when there is nothing usable.'

    def test_cache_is_used_without_network(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "home"))
        _make_image(tmp_path, "ExampleBuild1")
        monkeypatch.setattr(mounter, "fetch_image",
                            lambda *a, **kw: pytest.fail("must not download when the cache has one"))
        items = mounter.ensure_image("ExampleProduct,1")
        assert [item.build for item in items] == ["ExampleBuild1"]

    def test_empty_cache_downloads(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "home"))
        seen: list[tuple] = []

        def fake_fetch(build: str, base=None, token=None, **kw):
            seen.append((build, base))
            return mounter.DdiImage(build=build, image=_make_image(tmp_path, build) / "Image.dmg",
                                    manifest=paths.ddi_dir(None, build) / "BuildManifest.plist")

        monkeypatch.setattr(mounter, "fetch_image", fake_fetch)
        monkeypatch.setattr(mounter, "remote_builds", lambda base=None, token=None: ["ExampleBuild1"])
        items = mounter.ensure_image("ExampleProduct,1")
        assert [item.build for item in items] == ["ExampleBuild1"]
        assert seen == [("ExampleBuild1", mounter.DEFAULT_DDI_URL)]

    def test_download_failure_returns_nothing(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "home"))

        def fake_fetch(*a, **kw):
            raise OpenDeviceError("offline")

        monkeypatch.setattr(mounter, "fetch_image", fake_fetch)
        monkeypatch.setattr(mounter, "remote_builds", lambda base=None, token=None: ["ExampleBuild1"])
        assert mounter.ensure_image("ExampleProduct,1") == []

    def test_remote_builds_reads_index(self, tmp_path) -> None:
        (tmp_path / "index.json").write_text('{"builds": ["ExampleBuild9", "ExampleBuild1"]}',
                                             encoding="utf-8")
        assert mounter.remote_builds(str(tmp_path)) == ["ExampleBuild9", "ExampleBuild1"]

    def test_remote_builds_falls_back(self, tmp_path) -> None:
        assert mounter.remote_builds(str(tmp_path)) == [mounter.DEFAULT_DDI_BUILD]


class TestTss:
    'The two pure signing helpers (no network).'

    @staticmethod
    def _identity() -> dict:
        return tss.pick_build_identity(_BUILD_MANIFEST, _IDENTIFIERS)

    def test_picks_identity_by_board_and_chip(self) -> None:
        assert self._identity()["ApChipID"] == "0x8100"

    def test_no_matching_identity_is_an_error(self) -> None:
        with pytest.raises(OpenDeviceError, match="BuildIdentity"):
            tss.pick_build_identity(_BUILD_MANIFEST, {"BoardId": 0x99, "ChipID": 0x99})

    def test_request_carries_nonce_ecid_and_only_personalized_entries(self) -> None:
        request = tss.personalized_request(_IDENTIFIERS, self._identity(),
                                           b"\x07" * 32, ecid=0x1234)
        assert request["ApNonce"] == b"\x07" * 32
        assert request["ApECID"] == 0x1234
        assert request["@ApImg4Ticket"] is True
        assert "PersonalizedDMG" in request
        assert "Cryptex1,GenericDmg" not in request


class TestDdiHint:
    'A missing DDI-backed service must explain why and how to fix it (static part in remote, live part in the probe).'

    def test_static_hint_for_ddi_services(self) -> None:
        rsd = _bare_rsd()
        with pytest.raises(OpenDeviceError) as info:
            rsd.service_entry("com.apple.coredevice.displayservice")
        text = str(info.value)
        assert 'does not provide this service' in text
        assert 'personalized disk image' in text and "devmode status" in text

    def test_no_hint_for_other_services(self) -> None:
        rsd = _bare_rsd()
        with pytest.raises(OpenDeviceError) as info:
            rsd.service_entry("com.apple.afc")
        assert str(info.value) == 'device does not provide this service: com.apple.afc'

    def test_probe_replaces_static_hint(self) -> None:
        'When the probe has a live answer it wins - the static fallback stops repeating.'
        rsd = _bare_rsd()
        rsd.ddi_probe = lambda: '  image not mounted (developer mode is on) - everything lives in the image.'
        with pytest.raises(OpenDeviceError) as info:
            rsd.service_entry("com.apple.coredevice.displayservice")
        text = str(info.value)
        assert 'image not mounted' in text
        assert "devmode status" not in text

    def test_broken_probe_never_masks_the_error(self) -> None:
        def boom() -> str:
            raise RuntimeError('the probe itself raised')

        rsd = _bare_rsd()
        rsd.ddi_probe = boom
        with pytest.raises(OpenDeviceError) as info:
            rsd.service_entry("com.apple.coredevice.displayservice")
        assert 'personalized disk image' in str(info.value)


class FakeXpc:
    def __init__(self, output) -> None:
        self.output = output
        self.requests: list = []

    def send_receive(self, request, timeout=10.0):
        self.requests.append(request)
        return {"CoreDevice.output": self.output}

    def close(self) -> None:
        pass


class TestCoreDeviceCalls:
    @staticmethod
    def _client(service: str, output) -> "tuple[coredevice.CoreDeviceClient, FakeXpc]":
        client = coredevice.CoreDeviceClient(None, service)
        fake = FakeXpc(output)
        client.xpc = fake
        return client, fake

    def test_capture_screenshot_uses_action(self) -> None:
        client, fake = self._client(coredevice.SERVICE_SCREEN_CAPTURE, {"image": b"PNG"})
        assert client.capture_screenshot() == b"PNG"
        request = fake.requests[0]
        assert request["CoreDevice.actionIdentifier"] == coredevice.A_CAPTURE_SCREENSHOT
        assert request["CoreDevice.input"] == {"displayUniqueID": None, "requestedFormat": "png"}
        assert request["CoreDevice.CoreDeviceDDIProtocolVersion"] == 2

    def test_launch_application_uses_feature(self) -> None:
        client, fake = self._client(coredevice.SERVICE_APP, {"pid": 42})
        client.launch_application("com.example.app", arguments=["-x"], kill_existing=False)
        request = fake.requests[0]
        assert request["CoreDevice.featureIdentifier"] == coredevice.F_LAUNCH_APPLICATION
        assert request["CoreDevice.input"]["applicationSpecifier"] == {
            "bundleIdentifier": {"_0": "com.example.app"}}
        options = request["CoreDevice.input"]["options"]
        assert options["arguments"] == ["-x"]
        assert options["terminateExisting"] is False

    def test_screenshot_output_missing_raises(self) -> None:
        client, _fake = self._client(coredevice.SERVICE_SCREEN_CAPTURE, {"nope": 1})
        with pytest.raises(Exception, match="capturescreenshot"):
            client.capture_screenshot()


class TestPlumbing:
    def test_plistio_sendall_falls_back_to_send(self) -> None:
        "Objects without ``sendall`` (IpStack's TcpConnection) must still be able to send."
        class OnlySend:
            def __init__(self) -> None:
                self.chunks: list = []

            def send(self, data) -> None:
                self.chunks.append(data)

        sock = OnlySend()
        plistio.send_plist(sock, {"Request": "QueryType"})
        body = sock.chunks[0]
        assert int.from_bytes(body[:4], "big") == len(body) - 4
        assert body[4:].startswith(b"<?xml")

    def test_ipstack_socket_buffers_extra(self) -> None:
        'The user-space stack may hand over more than asked: the extra bytes stay for next time.'
        class FakeConn:
            closed = False

            def __init__(self) -> None:
                self.chunks = [b"0123456789"]

            def recv(self, timeout) -> bytes:
                return self.chunks.pop(0) if self.chunks else b""

            def send(self, data) -> None:
                pass

            def close(self) -> None:
                pass

        sock = IpStackSocket(FakeConn(), timeout=0.1)
        assert sock.recv(4) == b"0123"
        assert sock.recv(4) == b"4567"
        assert sock.recv(4) == b"89"

    def test_open_service_prefers_lockdown(self) -> None:
        class FakeLockdown:
            def start_service(self, name, use_ssl=None):
                return ("lockdown", name)

        assert open_service(FakeLockdown(), None, "com.apple.afc") == \
            ("lockdown", "com.apple.afc")

    def test_open_service_without_rsd_name_raises(self) -> None:
        class FakeRsd:
            pass

        with pytest.raises(ServiceError, match='USB-only'):
            open_service(None, FakeRsd(), amfi.SERVICE)

    def test_host_identity_is_stable_and_branded(self) -> None:
        assert rrp._host_serial() == "opendevice"
        assert rrp._host_serial() == rrp._host_serial()
        assert len(rrp._host_alt_irk()) == 16
        address, mac = rrp._host_bt_address()
        assert len(mac) == 6 and address.count(":") == 5
        assert rrp.HOST_MODEL == "opendevice"
