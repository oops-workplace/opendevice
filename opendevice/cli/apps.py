from __future__ import annotations

import argparse
import os
from typing import Any, Optional

from ..core import output
from .common import open_link


def cmd_afc(args: argparse.Namespace) -> int:
    'Device filesystem: ls / pull / push / rm (``--app`` enters an app sandbox).'
    import posixpath

    from ..services.afc import Afc

    link = open_link(args)
    bundle = getattr(args, "app", None)
    try:
        afc = (Afc.open_app(link.lockdown, link.rsd, bundle,
                            documents=bool(getattr(args, "documents", False)))
               if bundle else Afc.open(link.lockdown, link.rsd))
        with afc:
            if args.afc_action == "ls":
                rows = []
                for name in sorted(afc.listdir(args.path)):
                    try:
                        info = afc.stat(posixpath.join(args.path, name))
                    except Exception:                  # noqa
                        info = {}
                    kind = ("d" if str(info.get("st_ifmt", "")).upper() == "S_IFDIR"
                            else "-")
                    rows.append([kind, int(info.get("st_size") or 0), name])
                where = args.path + (f" ({bundle} sandbox)" if bundle else "")
                output.note(f"path {where}, {len(rows)} entries")
                output.line(output.table(["", 'bytes', 'name'], rows,
                                         aligns=("l", "r", "l")))
            elif args.afc_action == "pull":
                _pull_with_progress(afc, args.remote, args.local)
                output.line(f"\rpulled to {args.local}")
            elif args.afc_action == "push":
                _push_with_progress(afc, args.local, args.remote)
                output.line(f"\rpushed to {args.remote}")
            elif args.afc_action == "rm":
                recursive = bool(getattr(args, "recursive", False))
                if not recursive and afc.is_dir(args.path):
                    output.die(f"{args.path} is a directory: add -r to delete its contents too")
                (afc.remove_tree if recursive else afc.remove)(args.path)
                output.line(f"deleted {args.path}")
    finally:
        link.close()
    return 0


def _percent_line(total: int):
    'A one-line refreshing callback using ``\\n``.'
    last = {"pct": -1}

    def progress(written: int) -> None:
        if not total:
            return
        pct = int(written * 100 / total)
        if pct != last["pct"]:
            last["pct"] = pct
            print(f"\r{pct:3d}%  {written}/{total} bytes", end="", flush=True)

    return progress


def _local_size(path: str) -> int:
    'Total size of a local file/directory (recursive for directories).'
    if os.path.isdir(path):
        return sum(os.path.getsize(os.path.join(folder, name))
                   for folder, _dirs, files in os.walk(path) for name in files)
    return os.path.getsize(path)


def _pull_with_progress(afc: Any, remote: str, local: str) -> None:
    'Pull files/directories: refresh a single line with the percentage when the total size is known.'
    try:
        total = 0 if afc.is_dir(remote) else afc.size(remote)
    except Exception:                              # noqa
        total = 0
    afc.pull(remote, local, on_progress=_percent_line(total))


def _push_with_progress(afc: Any, local: str, remote: str) -> None:
    'Push files/directories: the local total is always known, so refresh a single line.'
    afc.push(local, remote, on_progress=_percent_line(_local_size(local)))


def cmd_install(args: argparse.Namespace) -> int:
    from ..services.installation import InstallationProxy

    link = open_link(args)
    proxy = None
    try:
        proxy = InstallationProxy.open(link.lockdown, link.rsd)
        last = {"percent": -1}

        def progress(percent: int, status: str) -> None:
            if percent != last["percent"]:
                last["percent"] = percent
                print(f"\r{percent:3d}%  {status[:60]:<60}", end="", flush=True)

        proxy.install(args.package, on_progress=progress,
                      developer=args.developer, upgrade=args.upgrade)
        output.line(f"\rinstalled: {args.package}")
    finally:
        if proxy is not None:
            proxy.close()
        link.close()
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    from ..services.installation import InstallationProxy

    link = open_link(args)
    last = {"pct": -1}

    def progress(percent: int, status: str) -> None:
        if percent != last["pct"]:
            last["pct"] = percent
            print(f"\r{percent:3d}%  {status[:60]:<60}", end="", flush=True)

    try:
        with InstallationProxy.open(link.lockdown, link.rsd) as proxy:
            proxy.uninstall(args.bundle_id, on_progress=progress)
    finally:
        link.close()
    output.line(f"\runinstalled: {args.bundle_id}")
    return 0


def cmd_applist(args: argparse.Namespace) -> int:
    from ..services.installation import InstallationProxy

    wanted = "Any" if args.all else ("System" if args.system else "User")
    link = open_link(args)
    try:
        with InstallationProxy.open(link.lockdown, link.rsd) as proxy:
            found = proxy.apps(app_type=wanted)
    finally:
        link.close()

    if args.json:
        output.json_out(found)
        return 0
    details = [app for app in found if app.get("CFBundleIdentifier")]
    details.sort(key=lambda item: str(item["CFBundleIdentifier"]))
    output.note(f"apps{len(found)} ({wanted})")
    output.line(output.table(
        ["Bundle ID", 'name', 'version'],
        [[app.get("CFBundleIdentifier"), app.get("CFBundleDisplayName")
          or app.get("CFBundleName") or "", app.get("CFBundleShortVersionString") or ""]
         for app in details]))
    return 0


def cmd_launch(args: argparse.Namespace) -> int:
    from ..remote.coredevice import SERVICE_APP, CoreDeviceClient

    link = open_link(args, need_rsd=True)
    try:
        with CoreDeviceClient(link.rsd, SERVICE_APP) as core:
            result = core.launch_application(
                args.bundle_id, arguments=args.arg,
                kill_existing=not args.keep_existing)
    finally:
        link.close()

    if args.json:
        output.json_out(result)
        return 0
    pid = _pid_of(result)
    output.line(f"launched {args.bundle_id}" + (f" (pid {pid})" if pid else ""))
    return 0


def _pid_of(payload: Any) -> Optional[int]:
    'Dig the pid out of the CoreDevice reply.'
    if isinstance(payload, dict):
        for key in ("processIdentifier", "pid"):
            if isinstance(payload.get(key), int):
                return payload[key]
        values: Any = payload.values()
    elif isinstance(payload, (list, tuple)):
        values = payload
    else:
        return None
    for value in values:
        found = _pid_of(value)
        if found is not None:
            return found
    return None


def _front_pid(rsd: Any) -> Optional[int]:
    'Pid of the foreground app.'
    from ..ax import AXClient, to_tree

    with AXClient(rsd) as ax:
        elements = ax.fetch(timeout=8.0)
    for _depth, element in to_tree(elements):
        return element.pid
    return None


def _app_name(path: str) -> str:
    '``/var/.../Example.app/Example`` -> ``Example``.'
    for part in path.split("/"):
        if part.endswith(".app"):
            return part[:-4]
    return ""


def cmd_ps(args: argparse.Namespace) -> int:
    'Processes: by default only **app** processes (use --all to include system daemons).'
    from ..remote.coredevice import (
        SERVICE_APP,
        CoreDeviceClient,
        process_name,
        process_path,
        process_pid,
    )

    link = open_link(args, need_rsd=True)
    front: Optional[int] = None
    try:
        with CoreDeviceClient(link.rsd, SERVICE_APP) as core:
            found = core.list_processes()
        if args.front:
            front = _front_pid(link.rsd)
    finally:
        link.close()

    if args.front:
        hit = next((t for t in found if process_pid(t) == front), None)
        if hit is None:
            output.die(f"no foreground process found (the control tree reports pid={front})")
        path = process_path(hit)
        output.line(output.pairs([("PID", front),
                                  ('process', _app_name(path) or process_name(hit)),
                                  ('path', path)]))
        return 0

    if args.json:
        output.json_out(found)
        return 0

    if not args.all:
        found = [t for t in found
                 if "/var/containers/Bundle/Application/" in process_path(t)]
        output.note(f"processes{len(found)} (app processes; --all for everything)")
    else:
        output.note(f"processes{len(found)} (all)")
    rows = [[process_pid(token),
             _app_name(process_path(token)) or process_name(token),
             process_path(token)]
            for token in sorted(found, key=lambda item: process_pid(item) or 0)]
    output.line(output.table(["PID", 'process', 'path'], rows, aligns=("r", "l", "l")))
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    from ..remote.coredevice import SERVICE_APP, CoreDeviceClient
    from ..services.installation import InstallationProxy, executable_path

    link = open_link(args, need_rsd=True)
    try:
        with CoreDeviceClient(link.rsd, SERVICE_APP) as core:
            target = str(args.target)
            if target.isdigit():
                core.kill_process(int(target), args.signal)
                pids = [int(target)]
            else:
                with InstallationProxy.open(link.lockdown, link.rsd) as proxy:
                    found = proxy.lookup([target]).get(target) or {}
                pids = core.kill_executable(executable_path(found) or target, args.signal)
    finally:
        link.close()

    if not pids:
        output.die(f"no running process found: {args.target}")
    killed = ", ".join(str(pid) for pid in pids)
    output.line(f"stopped {args.target} (pid {killed}, signal {args.signal})")
    return 0


def cmd_screenshot(args: argparse.Namespace) -> int:
    from ..remote.coredevice import SERVICE_SCREEN_CAPTURE, CoreDeviceClient

    link = open_link(args, need_rsd=True)
    try:
        with CoreDeviceClient(link.rsd, SERVICE_SCREEN_CAPTURE) as core:
            image = core.capture_screenshot()
        with open(args.out, "wb") as handle:
            handle.write(image)
        output.line(f"saved {len(image)} bytes ->{args.out}")
    finally:
        link.close()
    return 0
