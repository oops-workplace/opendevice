from __future__ import annotations

import argparse
import time
from typing import Any, Optional

from ..core import output
from ..core.errors import OpenDeviceError
from .common import open_link


def _screen_points(rsd: Any) -> tuple[float, float]:
    '**Point** size of the main display.'
    from ..remote.coredevice import SERVICE_DEVICE_INFO, CoreDeviceClient

    info = CoreDeviceClient(rsd, SERVICE_DEVICE_INFO).get_display_info()
    displays = info.get("displays") or []
    main = next((d for d in displays if d.get("primary") and not d.get("external")), None) \
        or next((d for d in displays if not d.get("external")), None)
    if main is None:
        raise OpenDeviceError('the display info has no built-in screen')
    size = main.get("nativeSize") or (main.get("currentMode") or {}).get("size") or []
    scale = float(main.get("pointScale")
                  or (main.get("currentMode") or {}).get("preferredUIScale") or 1) or 1
    if len(size) >= 2 and size[0] and size[1]:
        return float(size[0]) / scale, float(size[1]) / scale
    raise OpenDeviceError('cannot read the main screen size')


def cmd_stream(args: argparse.Namespace) -> int:
    'Collect a media stream: H.265 Annex-B written **as is** (no remux, no decode, only stats).'
    import os

    from ..media.stream import MediaStreamSession

    link = open_link(args, need_rsd=True, task='streaming')
    rsd = link.rsd
    output.note(str(rsd.info))

    session = MediaStreamSession(rsd, rsd.stack, udid=args.udid)
    handle = None
    receiver = None
    seen: dict[str, Any] = {"frames": 0, "keyframes": 0, "first_is_key": None}

    def on_access_unit(data: bytes, is_key: bool) -> None:
        seen["frames"] += 1
        seen["keyframes"] += 1 if is_key else 0
        if seen["first_is_key"] is None:
            seen["first_is_key"] = is_key
        if handle is not None:
            handle.write(data)

    try:
        session.prepare_socket()
        session.start()
        output.note(f"video negotiated: {session.describe()}")

        if args.out:
            handle = open(args.out, "wb")
        receiver = session.receiver_for(on_access_unit)
        receiver.start()
        started = time.monotonic()
        deadline = started + args.seconds
        while time.monotonic() < deadline:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if receiver is not None:
            receiver.stop()
        session.stop()
        if handle is not None:
            handle.close()
        link.close()

    if receiver is None:
        return 1
    stats = receiver.stats
    saved = os.path.abspath(args.out) if args.out else None
    report = [
        ('duration', f"{time.monotonic() - started:.1f} s"),
        ('RTP packets', f"{stats['packets']} / {output.size(stats['bytes'])}"),
        ('lost / reordered', f"{stats['lost']} / {stats['reordered']}"),
        ('filtered out', f"{stats['dropped']} (non-video packets: audio / control)"),
        ('access units', f"{seen['frames']} (key frames {seen['keyframes']})"),
        ('first packet is a key frame', 'yes' if seen["first_is_key"] else 'no'),
    ]
    if saved:
        report.append(('written ', f"{saved} ({output.size(os.path.getsize(saved))})"))
    output.line(output.pairs(report))
    if saved:
        output.note(f"open it in VLC: {saved}")
    else:
        output.note('add --out screen.h265 to keep the raw stream (no remux, exactly the bytes received)')
    return 0 if seen["frames"] else 1


def cmd_hid(args: argparse.Namespace) -> int:
    'List the HID surfaces this device registered.'
    from ..input.hid import UniversalHID

    link = open_link(args, need_rsd=True)
    try:
        with UniversalHID(link.rsd) as svc:
            response = svc.list_services()
    finally:
        link.close()

    if args.raw:
        output.json_out(response)
        return 0
    rows = [[item.get("_ServiceID"), item.get("PrimaryUsagePage"),
             item.get("PrimaryUsage"), item.get("Product")]
            for item in response.get("connectedServices", []) or []]
    output.line(output.table(["ID", "Page", "Usage", "Product"], rows,
                             aligns=("r", "r", "r", "l")))
    return 0


def cmd_key(args: argparse.Namespace) -> int:
    'Press a hardware button (Indigo channel, no stream needed).'
    from ..input.hid import IndigoHID

    link = open_link(args, need_rsd=True)
    try:
        with IndigoHID(link.rsd) as indigo:
            page, code = indigo.press(args.name)
    finally:
        link.close()
    output.line(f"pressed {args.name} (usagePage={page} usageCode={code})")
    return 0


def cmd_type(args: argparse.Namespace) -> int:
    'Type character by character.'
    from ..input.hid import UniversalHID
    from ..media.stream import MediaStreamSession

    link = open_link(args, need_rsd=True)
    rsd = link.rsd
    session = None
    try:
        session = MediaStreamSession(rsd, rsd.stack, udid=args.udid)
        session.prepare_socket()
        session.start()
        output.note(f"streaming started (auth gate): {session.describe()}")
        time.sleep(0.5)
        with UniversalHID(rsd) as svc:
            count = svc.type_text(args.text)
    finally:
        if session is not None:
            session.stop()
        link.close()
    output.line(f"typed {count} characters (from {len(args.text)})")
    return 0


def _approve_paste(rsd: Any, attempts: int = 4) -> bool:
    'Dismiss the iOS "Allow Paste" prompt.'
    from ..ax import AXClient, find
    from ..input.hid import UniversalHID

    for _ in range(attempts):
        try:
            with AXClient(rsd) as ax:
                elements = ax.fetch(timeout=8.0)
        except Exception:
            time.sleep(0.5)
            continue
        target = find(elements, 'Allow Paste') or find(elements, "Allow Paste")
        if target is not None and target.center is not None:
            width, height = _screen_points(rsd)
            center = target.center
            with UniversalHID(rsd) as hid:
                hid.tap(center[0] / width, center[1] / height)
            return True
        time.sleep(0.7)
    return False


def _points(text: str) -> Optional[tuple[float, float]]:
    '``"200,300"`` -> ``(200.0, 300.0)``.'
    parts = [bit.strip() for bit in text.split(",")]
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def _normalize(rsd: Any, x: float, y: float) -> tuple[float, float]:
    '**Point** coordinates from the control tree -> 0..1 for touch.'
    if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
        return x, y
    width, height = _screen_points(rsd)
    return x / width, y / height


def cmd_uitree(args: argparse.Namespace) -> int:
    'Fetch the control tree of the current screen.'
    from ..ax import AXClient, find, format_tree
    from ..input.hid import UniversalHID
    from ..media.stream import MediaStreamSession

    link = open_link(args, need_rsd=True)
    rsd = link.rsd
    output.note(rsd.info)

    session = None
    ax = AXClient(rsd)
    try:
        if args.home:
            from ..input.hid import IndigoHID
            with IndigoHID(rsd) as indigo:
                indigo.press("home")
            time.sleep(0.8)

        if args.tap is not None:
            session = MediaStreamSession(rsd, rsd.stack, udid=args.udid)
            session.prepare_socket()
            session.start()
            output.note(f"streaming started (auth gate): {session.describe()}")

        elements = ax.fetch()
        output.note(f"controls{len(elements)}; payload {ax.last_len} bytes")
        if ax.stale:
            output.warn('the payload is only ~1.7KB - the device-side AX session looks wedged (a device reboot recovers it)')

        if args.json:
            output.json_out([{
                "object_id": str(el.object_id), "pid": el.pid, "label": el.label,
                "description": el.description, "frame": list(el.frame) if el.frame else None,
                "traits": el.traits, "enabled": el.enabled, "children": list(el.children),
            } for el in elements])
            return 0

        tree = format_tree(elements, limit=args.limit, contains=args.filter)
        output.line(tree if tree else ' (no matching control)')

        if args.tap is None:
            return 0

        target = find(elements, args.tap)
        if target is None or target.center is None:
            output.die(f"no element with a frame found: {args.tap!r}")
        width, height = _screen_points(rsd)
        center = target.center
        norm = (center[0] / width, center[1] / height)
        output.note(f"hit{target.text!r} center {center} / screen {width:.0f}x{height:.0f} points, normalized {norm[0]:.4f},{norm[1]:.4f}")

        before = {el.text for el in elements if el.text}
        time.sleep(0.4)
        with UniversalHID(rsd) as hid:
            hid.tap(norm[0], norm[1])
        output.note('tapped ')

        time.sleep(1.2)
        after_elements = ax.fetch()
        after = {el.text for el in after_elements if el.text}
        output.note(f"re-fetched {len(after_elements)} controls; payload {ax.last_len} bytes")
        appeared = sorted(after - before)[:12]
        vanished = sorted(before - after)[:12]
        if appeared:
            output.note(f"the screen changed, new: {appeared}")
        if vanished:
            output.note(f"the screen changed, gone: {vanished}")
        if not appeared and not vanished:
            output.note('the control set did not change (the target may already have been open, or the tap did nothing)')
        return 0
    finally:
        ax.close()
        if session is not None:
            session.stop()
        link.close()


def cmd_click(args: argparse.Namespace) -> int:
    "Tap once: give control coordinates (points) / 0..1, or a control's text (looked up in the tree)."
    from ..ax import AXClient, find
    from ..input.hid import UniversalHID
    from ..media.stream import MediaStreamSession

    link = open_link(args, need_rsd=True)
    rsd = link.rsd
    session = None
    ax = None
    try:
        session = MediaStreamSession(rsd, rsd.stack, udid=args.udid)
        session.prepare_socket()
        session.start()

        spot = _points(args.target)
        if spot is not None:
            norm = _normalize(rsd, *spot)
            with UniversalHID(rsd) as hid:
                hid.tap(norm[0], norm[1])
            width, height = _screen_points(rsd)
            output.line(f"tapped ({spot[0]:.4g},{spot[1]:.4g}) (screen {width:.0f}x{height:.0f} points, normalized {norm[0]:.4f},{norm[1]:.4f})")
            return 0

        ax = AXClient(rsd)
        elements = ax.fetch()
        target = find(elements, args.target, exact=args.exact)
        if target is None or target.center is None:
            output.die(f"no control with a frame found: {args.target!r}")
        norm = _normalize(rsd, *target.center)
        output.note(f"hit{target.text!r} center ({target.center[0]:.0f},{target.center[1]:.0f}), normalized {norm[0]:.4f},{norm[1]:.4f}")
        time.sleep(0.3)
        with UniversalHID(rsd) as hid:
            hid.tap(norm[0], norm[1])
        output.note('tapped ')
        if args.no_verify:
            return 0

        before = {el.text for el in elements if el.text}
        time.sleep(1.2)
        after_elements = ax.fetch()
        after = {el.text for el in after_elements if el.text}
        output.note(f"re-fetched {len(after_elements)} controls")
        appeared = sorted(after - before)[:12]
        vanished = sorted(before - after)[:12]
        if appeared:
            output.note(f"the screen changed, new: {appeared}")
        if vanished:
            output.note(f"the screen changed, gone: {vanished}")
        if not appeared and not vanished:
            output.note('the control set did not change (the target may already have been open, or the tap did nothing)')
        return 0
    finally:
        if ax is not None:
            ax.close()
        if session is not None:
            session.stop()
        link.close()


def cmd_swipe(args: argparse.Namespace) -> int:
    'Swipe: start -> end, accepting control coordinates or 0..1.'
    from ..input.hid import UniversalHID
    from ..media.stream import MediaStreamSession

    start, end = _points(args.start), _points(args.end)
    if start is None or end is None:
        output.die('coordinates must be x,y (e.g. 200,600 200,200)')

    link = open_link(args, need_rsd=True)
    rsd = link.rsd
    session = None
    try:
        session = MediaStreamSession(rsd, rsd.stack, udid=args.udid)
        session.prepare_socket()
        session.start()
        norm_start, norm_end = _normalize(rsd, *start), _normalize(rsd, *end)
        with UniversalHID(rsd) as hid:
            hid.swipe(norm_start, norm_end)
        output.line(f"swiped ({start[0]:.4g},{start[1]:.4g}) -> "
                    f"({end[0]:.4g},{end[1]:.4g})")
        output.note(f"normalized{norm_start[0]:.4f},{norm_start[1]:.4f} -> "
                    f"{norm_end[0]:.4f},{norm_end[1]:.4f}")
        return 0
    finally:
        if session is not None:
            session.stop()
        link.close()


def cmd_paste(args: argparse.Namespace) -> int:
    from ..input.pasteboard import PasteboardClient

    link = open_link(args, need_rsd=True)
    rsd = link.rsd
    output.note(rsd.info)
    try:
        with PasteboardClient(rsd) as board:
            if args.set is not None:
                reply = board.set_text(args.set, args.name)
                output.line(f"written to the pasteboard ({len(args.set)} characters), device ack{reply.get('command')}")
            if args.get or args.set is None:
                output.line(f"pasteboard content: {board.get_text(args.name)!r}")
    finally:
        link.close()
    return 0


def cmd_sendtext(args: argparse.Namespace) -> int:
    from ..input.hid import UniversalHID
    from ..input.pasteboard import PasteboardClient
    from ..media.stream import MediaStreamSession

    link = open_link(args, need_rsd=True)
    rsd = link.rsd
    output.note(rsd.info)

    session = None
    try:
        if not args.no_paste:
            session = MediaStreamSession(rsd, rsd.stack, udid=args.udid)
            session.prepare_socket()
            session.start()
            output.note(f"streaming started (auth gate): {session.describe()}")

        with PasteboardClient(rsd) as board:
            reply = board.set_text(args.text)
            output.line(f"written to the pasteboard ({len(args.text)} characters), device ack{reply.get('command')}")
            back = board.get_text()
            output.note(f"read-back check: {'matches' if back == args.text else 'differs'}"
                        f" ({len(back)} characters)")

        if not args.no_paste:
            time.sleep(0.4)
            with UniversalHID(rsd) as hid:
                hid.paste()
            output.note('sent Cmd+V')
            if args.no_approve:
                return 0
            time.sleep(0.5)
            if _approve_paste(rsd):
                output.note('auto-approved the paste prompt')
            else:
                output.note(' (no "Allow Paste" prompt seen; the first paste may already be authorized)')
    finally:
        if session is not None:
            session.stop()
        link.close()
    return 0
