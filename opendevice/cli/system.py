from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional

from ..core import output
from .common import open_link


def _mode_text(state: Optional[bool]) -> str:
    'Developer mode has three states: on / off / the device does not answer.'
    if state is None:
        return 'device does not answer'
    return 'on' if state else 'off'


def cmd_devmode(args: argparse.Namespace) -> int:
    from ..services.amfi import AmfiService
    from ..services.mounter import MobileImageMounter

    link = open_link(args, usb_only=True)
    try:
        if args.dev_action == "status":
            state = MobileImageMounter.open(link.lockdown).developer_mode_status()
            output.line(f"developer mode: {_mode_text(state)}")
        elif args.dev_action == "reveal":
            AmfiService(link.lockdown).reveal()
            output.line('developer mode switch is now visible (Settings -> Privacy & Security)')
        elif args.dev_action == "enable":
            amfi = AmfiService(link.lockdown)
            amfi.enable(wait=True, on_status=output.note)
            link.lockdown = amfi.lockdown
        else:
            AmfiService(link.lockdown).accept_after_restart()
            output.line('confirmed (developer mode on)')
    finally:
        link.close()
    return 0


def cmd_power(args: argparse.Namespace) -> int:
    from ..services.diagnostics import DiagnosticsRelay

    link = open_link(args)
    try:
        with DiagnosticsRelay.open(link.lockdown, link.rsd) as relay:
            action = args.power_action
            if action == "info":
                output.json_out(relay.info())
            elif action == "battery":
                output.json_out(relay.battery())
            elif action == "ioreg":
                output.json_out(relay.ioregistry(plane=args.plane, name=args.name,
                                                 io_class=args.io_class))
            else:
                getattr(relay, action)()
                output.line({"restart": 'rebooting', "shutdown": 'shutting down',
                             "sleep": 'screen off'}[action])
    finally:
        link.close()
    return 0


def cmd_syslog(args: argparse.Namespace) -> int:
    from ..services.syslog import SyslogRelay

    link = open_link(args, task='collecting the system log')
    seconds = None if not args.seconds else args.seconds
    count = 0
    try:
        relay = SyslogRelay.open(link.lockdown, link.rsd)

        def show(line: str) -> None:
            if not args.filter or args.filter in line:
                output.line(line)

        try:
            count = relay.follow(seconds, show)
        finally:
            relay.close()
    finally:
        link.close()
    output.note(f" ({count} lines)")
    return 0


def cmd_paths(args: argparse.Namespace) -> int:
    from ..core import paths

    info: dict[str, Any] = paths.describe()
    if args.json:
        output.json_out(info)
        return 0

    if info["home_override"]:
        output.line(f"OPENDEVICE_HOME = {info['home_override']}")
    output.line(output.pairs([
        ('data directory', f"{info['root']}  ({info['records_count']} pairing records)"),
        ('disk images', info["ddi"]),
        ('logs / temp', info["logs"]),
        ('device locks', f"{info['locks']} ({info['locks_count']} held by long tasks)"),
    ], indent=0, separator="  "))
    for directory in info["apple_usb_records"]:
        output.line(f"Apple\'s USB records  {directory}  (read-only, not ours)")
    return 0


def _product_of(link: Any) -> Optional[str]:
    'Product type (``ProductType``).'
    if link.lockdown is None:
        return None
    try:
        return link.lockdown.all_values().get("ProductType")
    except Exception:                            # noqa: BLE001
        return None


def _ecid_of(link: Any) -> Optional[int]:
    'Device ECID (needed for TSS tickets).'
    if link.lockdown is None:
        return None
    try:
        return int(link.lockdown.all_values().get("UniqueChipID"))
    except (TypeError, ValueError):                 # noqa
        return None


def cmd_mounter(args: argparse.Namespace) -> int:
    from ..services import mounter as mounter_module

    link = open_link(args)
    try:
        with mounter_module.MobileImageMounter.open(link.lockdown, link.rsd) as mounter:
            action = args.mounter_action
            if action == "list":
                for entry in mounter.devices():
                    output.line(json.dumps(entry, ensure_ascii=False, default=str))
            elif action == "lookup":
                signature = mounter.lookup(args.type)
                output.line(f"{args.type}: " + (f"signature{len(signature)} bytes" if signature
                                                else 'not mounted / no signature'))
            elif action == "status":
                state = mounter.developer_mode_status()
                output.line(f"developer mode: {_mode_text(state)}")
            elif action == "nonce":
                output.line(mounter.nonce(args.type).hex() or '(empty)')
            elif action == "images":
                images = mounter_module.list_images()
                if not images:
                    output.line(f"none at all ({mounter_module.cache_dir()})")
                    output.note('a personalized image is three pieces: Image.dmg / BuildManifest.plist / *.trustcache, placed in the image cache directory (one directory per build).')
                    return 0
                try:
                    identifiers = mounter.personalization_identifiers(
                        mounter_module.PERSONALIZED_DDI)
                except Exception:                   # noqa
                    identifiers = None
                product = getattr(link.rsd, "product_type", None) or _product_of(link)
                for item in images:
                    usable = item.supports(product) and (
                        item.identity_for(identifiers) is not None if identifiers else True)
                    output.line(f"{item.build}  "
                                f"{'OK for this device' if usable else 'unusable for this device'}  "
                                f"{len(item.supported)} product types{item.image.parent}")
                return 0
            elif action == "fetch":
                item = mounter_module.fetch_image(
                    args.build, base=getattr(args, "url", None) or None,
                    token=getattr(args, "token", None) or None,
                    on_status=output.note,
                    verify=not getattr(args, "no_verify", False))
                output.line(f"installed {item.build} ({len(item.supported)} product types) ->{item.image.parent}")
                return 0
            elif action == "mount":
                manifest = None
                if getattr(args, "manifest", None):
                    manifest = Path(args.manifest).read_bytes()
                product_type = getattr(link.rsd, "product_type", None)
                ddi_build = getattr(args, "build", None) or None
                if not getattr(args, "image", None) and not getattr(args, "no_fetch", False):
                    mounter_module.ensure_image(
                        product_type,
                        mounter.personalization_identifiers(mounter_module.PERSONALIZED_DDI),
                        ddi_build, base=getattr(args, "url", None) or None,
                        token=getattr(args, "token", None) or None, on_status=output.note)
                path = mounter.mount(
                    image=getattr(args, "image", None), manifest=manifest,
                    product_type=product_type,
                    build=getattr(link.rsd, "build_version", None),
                    ddi_build=ddi_build,
                    ecid=_ecid_of(link), on_status=output.note)
                output.line(f"mounted at {path}")
                output.note(f"CoreDevice version: {mounter.core_device_version() or '(unreadable)'}")
            else:
                output.line(f"unmounted{mounter.unmount(getattr(args, 'path', None))}")
    finally:
        link.close()
    return 0
