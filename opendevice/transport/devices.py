from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..core.errors import MuxError, OpenDeviceError
from . import usbmux


@dataclass
class DeviceEntry:
    'One device (USB and Wi-Fi records merged).'

    udid: str
    name: str = ""
    model: str = ""
    ios: str = ""
    build: str = ""
    usb: bool = False
    network: bool = False
    devid: int = 0
    properties: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.udid} ({'USB' if self.usb else 'Network'})"

    @property
    def link(self) -> str:
        'The link that is **usable** right now (USB preferred).'
        return "USB" if self.usb else "Network"

    @property
    def title(self) -> str:
        'One-line summary: ``<product-type> iOS <version> (<build>)``.'
        bits = [bit for bit in (self.model, f"iOS {self.ios}" if self.ios else "",
                                f"({self.build})" if self.build else "") if bit]
        return " ".join(bits)

    def describe(self) -> dict[str, Any]:
        'Fields used for JSON / table output.'
        return {
            "udid": self.udid,
            "name": self.name,
            "model": self.model,
            "ios": self.ios,
            "build": self.build,
            "usb": self.usb,
            "network": self.network,
            "devid": self.devid,
        }

    @classmethod
    def from_mux(cls, device: usbmux.MuxDevice, usb: bool) -> "DeviceEntry":
        props = device.properties or {}
        return cls(
            udid=device.udid,
            name=str(props.get("DeviceName", "") or ""),
            model=str(props.get("ProductType", "") or ""),
            ios=str(props.get("ProductVersion", "") or ""),
            build=str(props.get("BuildVersion", "") or ""),
            usb=usb,
            network=not usb,
            devid=device.devid,
            properties=props,
        )


def probe(entry: DeviceEntry, mux_address: Any = None) -> DeviceEntry:
    'Fill in DeviceName / product type / OS version.'
    if not entry.usb or (entry.name and entry.model and entry.ios):
        return entry
    try:
        from .lockdown import LockdownClient

        client = LockdownClient(entry.udid, mux_address)
        try:
            client.start_session()
            values = client.all_values()
        finally:
            client.close()
    except OpenDeviceError:
        return entry
    entry.name = str(values.get("DeviceName") or entry.name)
    entry.model = str(values.get("ProductType") or entry.model)
    entry.ios = str(values.get("ProductVersion") or entry.ios)
    entry.build = str(values.get("BuildVersion") or entry.build)
    return entry


def collect(mux_address: Any = None, probe_devices: bool = False) -> list[DeviceEntry]:
    'List devices, **merging by UDID** the USB and Network records (USB preferred).'
    merged: dict[str, DeviceEntry] = {}
    for device in usbmux.list_devices(mux_address):
        if not device.udid:
            continue
        entry = merged.get(device.udid)
        if entry is None:
            merged[device.udid] = DeviceEntry.from_mux(device, device.is_usb)
            continue
        if device.is_usb and not entry.usb:
            upgraded = DeviceEntry.from_mux(device, usb=True)
            upgraded.network = entry.network or True
            merged[device.udid] = upgraded
        else:
            entry.network = entry.network or not device.is_usb
    entries = list(merged.values())
    if probe_devices:
        for entry in entries:
            probe(entry, mux_address)
    return entries


def match(selector: str, candidates: list[DeviceEntry]) -> DeviceEntry:
    'Find one device by full UDID or **unique prefix** (case-insensitive).'
    wanted = selector.strip().lower()
    exact = [item for item in candidates if item.udid.lower() == wanted]
    if exact:
        return exact[0]
    prefixed = [item for item in candidates if item.udid.lower().startswith(wanted)]
    if len(prefixed) == 1:
        return prefixed[0]
    if not prefixed:
        raise MuxError(f"No device matching {selector!r}. Current: {_udids(candidates)}")
    raise MuxError(f"{selector!r} matches several devices, be more specific: {_udids(prefixed)}")


def resolve(selector: Optional[str] = None, mux_address: Any = None) -> DeviceEntry:
    'Pick one device: with a selector, resolve by full UDID / unique prefix.'
    candidates = collect(mux_address)
    if not candidates:
        raise MuxError('No device found. Check:\n  - cable connected, device unlocked, "Trust This Computer" accepted\n  - on Windows, iTunes / Apple Mobile Device Support installed (provides usbmux)')
    if selector:
        return match(selector, candidates)
    if len(candidates) == 1:
        return candidates[0]
    raise MuxError(f"Found {len(candidates)} devices, pick one with -d: {_udids(candidates)}")


def _udids(candidates: list[DeviceEntry]) -> str:
    return ", ".join(f"{item.udid}({item.link})" for item in candidates) or '(none)'
