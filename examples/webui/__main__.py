from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional


def resolve_udid(selector: Optional[str]) -> Optional[str]:
    """Resolve ``-d`` into a UDID."""
    from opendevice.transport import devices

    return devices.resolve(selector).udid


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webui",
        description="iOS 27 device: live screen + touch + control tree + virtual keyboard (web UI)",
    )
    parser.add_argument("-d", "--device", dest="device", metavar="UDID",
                        help="target device: full UDID or a unique prefix; "
                             "with several devices, point one per process and keep ports apart")
    parser.add_argument("--bind", default="127.0.0.1", help="listen address; 0.0.0.0 = reachable on the LAN")
    parser.add_argument("--port", type=int, default=8800, help="HTTP port")

    parser.add_argument("--tunnel", choices=("auto", "userspace", "network"),
                        default="auto",
                        help="tunnel backend: auto / userspace (USB, default, no admin needed) / "
                             "network (Wi-Fi, bonjour + RemotePairing, unplug-safe)")
    parser.add_argument("--dump-h265", metavar="PATH", default=None,
                        help="also save the device's raw H.265 stream to an Annex-B file as-is")
    parser.add_argument("--no-video", action="store_true", help="do not open the video stream")
    parser.add_argument(
        "-t", "--connection-type", choices=["usb", "wifi"], default="wifi",
        help="channel policy: wifi = accept wired and wireless (default, least likely to stall); "
             "usb = wired only, stops when USB is gone. "
             "note: touch/keyboard and the media stream share one auth gate; no stream = no controls",
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser automatically")
    parser.add_argument("--force", action="store_true",
                        help="take over even when another process holds this device (streaming / syslog) -- "
                             "two streams will fight; use only if you know the consequences")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def _install_exit_signals() -> None:
    """Route SIGTERM through the normal shutdown path too."""
    import signal

    def _raise_interrupt(_signum, _frame) -> None:
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, _raise_interrupt)
        except (ValueError, OSError):
            continue


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    from opendevice.core import lock as lock_module
    from opendevice.core.errors import DeviceBusyError

    from .server import WebUI
    from .session import Session

    try:
        udid = resolve_udid(args.device)
    except Exception as exc:                    # noqa: BLE001 - say something readable when device pick fails
        print(f"could not pick a device: {exc}", file=sys.stderr)
        return 1

    holder = lock_module.read_holder(udid)
    if holder is not None and args.force:
        print(f"note: --force: taking over (previous holder: {lock_module.describe(holder)})"
              " -- two streams will fight")

    session = Session(
        udid=udid,
        force=args.force,
        enable_video=not args.no_video,
        h265_path=args.dump_h265,
        tunnel_backend=args.tunnel,
        connection_type=args.connection_type,
    )
    ui = WebUI(session, bind=args.bind, port=args.port)
    _install_exit_signals()
    try:
        try:
            session.connect()
        except DeviceBusyError as exc:
            print(f"device is busy: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:                # noqa: BLE001
            print(f"connection failed: {exc}", file=sys.stderr)
            print("hint: the device must be connected, must trust this computer, and have developer mode on.", file=sys.stderr)
            print("      Windows: install iTunes (it provides the Apple Mobile Device Support driver); "
                  "otherwise usbmux cannot reach the device.", file=sys.stderr)
            if sys.platform == "darwin":
                print("      macOS: check that 'Trust This Computer' on the device has been accepted.", file=sys.stderr)
            elif sys.platform == "linux":
                print("      Linux: needs usbmuxd + libimobiledevice.", file=sys.stderr)
            return 1

        ui.serve_forever(open_browser=not args.no_browser)
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
