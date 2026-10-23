from __future__ import annotations

from typing import Any

import pytest

from opendevice.cli.device import devices_table
from opendevice.core import console, paths
from opendevice.core.errors import MuxError, OpenDeviceError
from opendevice.transport import devices, usbmux

UDID_A = "EXAMPLE-0000000000000000"
UDID_B = "00008110-001A2B3C4D5E6F70"


def fake_device(udid: str, usb: bool = True, devid: int = 1, **props):
    connection = "USB" if usb else "Network"
    properties = {"SerialNumber": udid, "ConnectionType": connection}
    properties.update(props)
    return usbmux.MuxDevice(devid=devid, udid=udid, connection_type=connection,
                            properties=properties)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    "Cache and records land in OPENDEVICE_HOME; the user's real data is untouched."
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
    return tmp_path


@pytest.fixture
def roster(monkeypatch):
    def install(entries: list[Any]) -> list[Any]:
        monkeypatch.setattr(usbmux, "list_devices", lambda address=None: list(entries))
        return entries
    return install


class TestCollect:
    def test_merges_usb_and_network_records(self, roster) -> None:
        'The USB and Network records of one device merge into one entry, USB half wins.'
        roster([fake_device(UDID_A, usb=False, devid=9),
                fake_device(UDID_A, usb=True, devid=7, DeviceName='my device')])
        entries = devices.collect()
        assert len(entries) == 1
        entry = entries[0]
        assert entry.usb and entry.network and entry.devid == 7
        assert entry.name == 'my device'

    def test_two_physical_devices(self, roster) -> None:
        roster([fake_device(UDID_A), fake_device(UDID_B, usb=False, devid=4)])
        entries = devices.collect()
        assert [item.udid for item in entries] == [UDID_A, UDID_B]
        assert entries[1].link == "Network"


class TestResolve:
    def test_single_device_is_used_automatically(self, roster) -> None:
        roster([fake_device(UDID_A)])
        assert devices.resolve().udid == UDID_A

    def test_no_device_message_mentions_itunes(self, roster) -> None:
        roster([])
        with pytest.raises(MuxError, match="iTunes"):
            devices.resolve()

    def test_multiple_devices_need_a_clue(self, roster) -> None:
        roster([fake_device(UDID_A), fake_device(UDID_B)])
        with pytest.raises(MuxError) as excinfo:
            devices.resolve()
        message = str(excinfo.value)
        assert "-d" in message
        assert UDID_A in message and UDID_B in message

    def test_explicit_udid(self, roster) -> None:
        roster([fake_device(UDID_A), fake_device(UDID_B)])
        assert devices.resolve(UDID_B).udid == UDID_B

    def test_prefix_matches_one(self, roster) -> None:
        roster([fake_device(UDID_A), fake_device(UDID_B)])
        assert devices.resolve("00008110").udid == UDID_B

    def test_prefix_ambiguous(self, roster) -> None:
        roster([fake_device("EXAMPLE00-AAA"), fake_device("EXAMPLE00-BBB")])
        with pytest.raises(MuxError, match='be more specific'):
            devices.resolve("EXAMPLE00")

    def test_unknown_selector(self, roster) -> None:
        roster([fake_device(UDID_A)])
        with pytest.raises(MuxError, match='No device matching'):
            devices.resolve("ffff")


class TestSessionSelection:
    def test_device_session_accepts_prefix_like_the_cli(self, monkeypatch) -> None:
        '``DeviceSession(udid="1234567890ABCDEF")`` must be as lenient as the CLI, not full-UDID only.'
        from opendevice import session as session_module
        from opendevice.session import link as link_module

        seen: dict[str, Any] = {}

        class FakeLockdown:
            def __init__(self, udid=None, mux_address=None) -> None:
                seen["lockdown"] = udid

            def start_session(self) -> None:
                pass

            def close(self) -> None:
                pass

        class FakeTunnel:
            def __init__(self, lockdown) -> None:
                pass

            def start(self):
                return "info"

            def close(self) -> None:
                pass

        class FakeRsd:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def connect(self) -> None:
                pass

            def udid(self):
                return UDID_A

            def close(self) -> None:
                pass

        monkeypatch.setattr(devices, "resolve",
                            lambda selector=None, mux=None: devices.DeviceEntry(
                                udid=UDID_A, usb=True, devid=1))

        from opendevice.remote import tunnel as tunnel_module

        def fake_open_rsd(udid=None, backend="auto", mux_address=None,
                          timeout=120.0) -> Any:
            seen["lockdown"] = udid
            return tunnel_module.TunnelHandle(
                rsd=FakeRsd(), tunnel=FakeTunnel(None), lockdown=FakeLockdown(udid))

        monkeypatch.setattr(tunnel_module, "open_rsd", fake_open_rsd)
        monkeypatch.setattr(link_module, "assert_free",
                            lambda udid, force=False, who="": None)

        device = session_module.DeviceSession(udid="EXAMPLE00").connect()
        try:
            assert seen["lockdown"] == UDID_A
            assert device.udid == UDID_A
            assert device.device is not None and device.device.udid == UDID_A
        finally:
            device.close()


class TestProbe:
    def test_fills_name_model_ios(self, roster, monkeypatch) -> None:
        roster([fake_device(UDID_A)])
        calls: list[Any] = []

        class FakeLockdown:
            def __init__(self, udid=None, mux_address=None):
                calls.append(udid)

            def start_session(self):
                pass

            def all_values(self):
                return {"DeviceName": 'test device', "ProductType": "ExampleProduct,1",
                        "ProductVersion": "27.0", "BuildVersion": "ExampleBuild1"}

            def close(self):
                pass

        monkeypatch.setattr("opendevice.transport.lockdown.LockdownClient", FakeLockdown)
        entry = devices.collect(probe_devices=True)[0]
        assert (entry.name, entry.model, entry.ios, entry.build) == \
            ('test device', "ExampleProduct,1", "27.0", "ExampleBuild1")
        assert entry.title == "ExampleProduct,1 iOS 27.0 (ExampleBuild1)"
        assert calls == [UDID_A]

    def test_failure_keeps_device_listed(self, roster, monkeypatch) -> None:
        'Not paired / device busy -> the list still prints, with empty fields and no exception.'
        roster([fake_device(UDID_A)])

        class BrokenLockdown:
            def __init__(self, *args, **kwargs):
                raise OpenDeviceError('not paired')

        monkeypatch.setattr("opendevice.transport.lockdown.LockdownClient", BrokenLockdown)
        entry = devices.collect(probe_devices=True)[0]
        assert entry.udid == UDID_A and entry.name == ""

    def test_network_only_is_not_probed(self, roster, monkeypatch) -> None:
        roster([fake_device(UDID_A, usb=False)])

        def boom(*args, **kwargs):
            raise AssertionError('Non-USB devices must not be asked over lockdown')

        monkeypatch.setattr("opendevice.transport.lockdown.LockdownClient", boom)
        assert devices.collect(probe_devices=True)[0].usb is False


class TestTable:
    def test_columns_and_link(self) -> None:
        first = devices.DeviceEntry(udid=UDID_A, name='my device', model="ExampleProduct,1",
                                   ios="27.0", build="ExampleBuild1", usb=True)
        second = devices.DeviceEntry(udid=UDID_B, usb=False, network=True)
        table = devices_table([first, second])
        lines = table.splitlines()
        assert lines[0].split() == ["UDID", 'Name', 'Product', 'OS', 'Link']
        assert UDID_A in lines[1] and "USB" in lines[1]
        assert "27.0 (ExampleBuild1)" in lines[1]
        assert UDID_B in lines[2] and "Network" in lines[2]
        assert "ExampleProduct,1" not in lines[2]

    def test_columns_line_up_by_display_width(self) -> None:
        'Wide characters take two columns: padding by ``len()`` is wrong, alignment must use display width.'
        entry = devices.DeviceEntry(udid=UDID_A, name='test-device', model="iPhoneXX,X",
                                    ios="27.0", build="ExampleBuild1", usb=True)
        head, row = devices_table([entry]).splitlines()
        column = lambda line, needle: console.width(line[: line.index(needle)])
        assert column(head, 'Link') == column(row, "USB")

    def test_columns_without_anything_in_them_are_dropped(self) -> None:
        '``--fast`` skips lockdown: the Name / Product / OS columns do not appear at all.'
        lines = devices_table([devices.DeviceEntry(udid=UDID_A, usb=True)]).splitlines()
        assert lines[0].split() == ["UDID", 'Link']
        assert lines[1].split() == [UDID_A, "USB"]

    def test_describe_shape(self) -> None:
        entry = devices.DeviceEntry(udid=UDID_A, usb=True, devid=7)
        assert entry.describe()["udid"] == UDID_A
        assert entry.describe()["devid"] == 7
