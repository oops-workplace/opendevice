from __future__ import annotations

import argparse
import json
from typing import Any

from ..core import lock, output
from ..transport import devices
from .common import open_link

INFO_KEYS = [
    "DeviceName", "ProductType", "ProductVersion", "BuildVersion",
    "UniqueDeviceID", "UniqueChipID", "DeviceClass", "DeviceColor",
    "InternationalMobileEquipmentIdentity", "WiFiAddress", "CPUArchitecture",
]

COLUMNS = [("UDID", "udid"), ('Name', "name"), ('Product', "model"),
           ('OS', "os"), ('Link', "link"), ('Note', "note")]

UNKNOWN = "-"


def cmd_devices(args: argparse.Namespace) -> int:
    entries = devices.collect(getattr(args, "mux", None),
                              probe_devices=not getattr(args, "fast", False))
    busy = lock.holders()
    if getattr(args, "json", False):
        output.json_out({"busy": busy,
                         "devices": [entry.describe() for entry in entries]})
        return 0 if entries else 1
    if not entries:
        output.warn('No device')
        output.note('  - cable connected, device unlocked, "Trust This Computer" accepted')
        output.note('  - on Windows, iTunes / Apple Mobile Device Support installed (provides usbmux)')
        return 1

    output.line(devices_table(entries, busy))
    if busy:
        output.note('"Busy" in Note means a long task holds it (--force can take over)')
    if len(entries) > 1:
        output.note('Pick a device: opendevice -d <udid> deviceinfo (full UDID or unique prefix)')
    return 0


def devices_table(entries: list[devices.DeviceEntry],
                  busy: dict[str, Any] | None = None) -> str:
    'Device table: the Note column says who is holding it.'
    rows = [_row_of(entry, busy) for entry in entries]
    kept = [pair for pair in COLUMNS
            if pair[1] in ("udid", "link") or any(row[pair[1]] for row in rows)]
    headers = [title for title, _key in kept]
    body = [[_cell(row, key) for _title, key in kept] for row in rows]
    return output.table(headers, body)


def _cell(row: dict[str, str], key: str) -> str:
    'Cell text: columns asked with ``--fast`` show ``-``; others stay empty.'
    value = row[key]
    return value or (UNKNOWN if key in ("name", "model", "os") else "")


def _row_of(entry: devices.DeviceEntry,
            busy: dict[str, Any] | None) -> dict[str, str]:
    'One device -> one table row (an empty build is merged into the OS column; Note only when needed).'
    notes = []
    holder = (busy or {}).get(entry.udid)
    if holder:
        notes.append(f"busy{holder.get('task') or ' long task'}"
                     f" (PID {holder.get('pid')})")
    version = " ".join(bit for bit in (entry.ios,
                                       f"({entry.build})" if entry.build else "") if bit)
    return {
        "udid": entry.udid,
        "name": entry.name,
        "model": entry.model,
        "os": version,
        "link": entry.link,
        "note": ', '.join(notes),
    }


def _screen_info(rsd: Any) -> dict[str, Any]:
    'Main screen resolution, from CoreDevice display info (lockdown has no such keys).'
    from ..core.errors import OpenDeviceError
    from ..remote.coredevice import SERVICE_DEVICE_INFO, CoreDeviceClient

    info = CoreDeviceClient(rsd, SERVICE_DEVICE_INFO).get_display_info()
    displays = info.get("displays") or []
    main = next((d for d in displays if d.get("primary") and not d.get("external")), None) \
        or next((d for d in displays if not d.get("external")), None)
    if main is None:
        raise OpenDeviceError('the display info has no built-in screen')
    size = main.get("nativeSize") or (main.get("currentMode") or {}).get("size") or []
    if len(size) < 2 or not size[0] or not size[1]:
        raise OpenDeviceError('cannot read the main screen size')
    mode = main.get("currentMode") or {}
    scale = float(main.get("pointScale") or mode.get("preferredUIScale") or 1) or 1
    w, h = int(size[0]), int(size[1])
    out: dict[str, Any] = {
        "resolution": f"{w}x{h}",
        "width_px": w,
        "height_px": h,
        "points": f"{w / scale:g}x{h / scale:g} @ {scale:g}x",
    }
    if mode.get("refreshRate"):
        out["refresh_hz"] = mode["refreshRate"]
    orientation = (info.get("orientation") or {}).get("currentDeviceOrientation")
    if orientation:
        out["orientation"] = orientation
    for key, name in (("colorGamut", "color_gamut"), ("bitDepth", "bit_depth")):
        if mode.get(key):
            out[name] = mode[key]
    return out


def cmd_deviceinfo(args: argparse.Namespace) -> int:
    'Device info: ``--screen`` for the main screen, default for the common lockdown keys, ``--all`` for everything.'
    screen = getattr(args, "screen", False)
    link = open_link(args, need_rsd=(screen or getattr(args, "link", "auto") == "wifi"))
    try:
        if screen:
            info = _screen_info(link.rsd)
            if args.json:
                output.json_out(info)
            else:
                output.line(output.pairs(list(info.items())))
            return 0
        if link.lockdown is not None:
            values = link.lockdown.all_values()
        else:
            values = dict(getattr(link.rsd, "properties", {}) or {})
            values.setdefault("ProductVersion", values.get("OSVersion"))
    finally:
        link.close()

    if not values:
        output.die('the device returned no information')
    chosen = (sorted(values) if args.all
              else [key for key in INFO_KEYS if values.get(key) is not None])
    if args.json:
        output.json_out({key: values[key] for key in chosen})
        return 0
    output.line(output.pairs([(key, _flat(values[key])) for key in chosen]))
    return 0


def _flat(value: Any) -> str:
    'Flatten dict / list values (like ``com.apple.disk_usage``) into one JSON line instead of a block.'
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=_jsonable)
    return str(value)


def _jsonable(value: Any) -> str:
    "JSON fallback: ``str()`` on ``bytes`` gives a blob of ``b'...'``; show the byte count instead."
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    return str(value)
