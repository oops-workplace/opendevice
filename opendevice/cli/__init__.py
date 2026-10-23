from __future__ import annotations

import argparse
import sys
from typing import Any, Optional

from .. import __version__
from ..core import log
from ..core.errors import OpenDeviceError
from .commands import COMMANDS, Command, attach, handler_table

HANDLERS = handler_table()

GLOBALS_AFTER_COMMAND: tuple[tuple[tuple[str, ...], dict[str, Any]], ...] = (
    (("-v", "--verbose"), dict(dest="verbose_sub", action="count", default=0,
                               help='log level (same as the global -v)')),
    (("--log",), dict(dest="log_sub", action="store_true",
                      help='write the log to the default file (same as the global --log)')),
    (("--log-file",), dict(dest="log_file_sub", metavar="PATH", default=None,
                           help='write the log to a given file (same as the global --log-file)')),
    (("-d", "--device"), dict(dest="device_sub", metavar="UDID",
                              help='target device (same as the global -d)')),
    (("--link",), dict(dest="link_sub", choices=("auto", "usb", "wifi"), default=None,
                       help='link (same as the global --link)')),
    (("--mux",), dict(dest="mux_sub", metavar="MUX", default=None,
                      help='usbmux address (same as the global --mux)')),
    (("--force",), dict(dest="force_sub", action="store_true",
                        help='go ahead even if the device is held (same as the global --force)')),
)

EPILOG = """Common flows:
  opendevice devices                see what is attached
  opendevice uitree --limit 20      control tree of the current screen (feed the coordinates to click)
  opendevice click 200,300          tap (also 0.5,0.5 or a control's text)
  opendevice swipe 200,600 200,200  swipe

By domain:
  device        devices deviceinfo
  ui            uitree click swipe key type hid sendtext paste
  apps & files  applist install uninstall launch kill ps afc screenshot
  screen        stream
  wireless      pairing unpairing wifi
  system        devmode power syslog mounter paths
"""


def _add_globals(parser: argparse.ArgumentParser) -> None:
    'The global arguments at the top level (written before the command).'
    parser.add_argument("-d", "--device", dest="device", metavar="UDID",
                        help='target device: a full UDID or a unique prefix; with several devices attached it errors out and lists the candidates')
    parser.add_argument("--link", choices=("auto", "usb", "wifi"), default="auto",
                        help='link: auto (default, USB when the device is on USB) / usb / wifi')
    parser.add_argument("--force", action="store_true",
                        help='go ahead and take over even when another process holds the device (streaming / syslog)')
    parser.add_argument("--mux", help='usbmux address (Windows default 127.0.0.1:27015)')
    parser.add_argument("--version", action="version", version=f"opendevice {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help='logging: -v for INFO, -vv for every frame (also OPENDEVICE_LOG=debug)')
    parser.add_argument("--log", action="store_true",
                        help='also write the log to opendevice.log in the package log directory')
    parser.add_argument("--log-file", metavar="PATH", default=None,
                        help='also write the log to a given file')


def _add_globals_after_command(parser: argparse.ArgumentParser) -> None:
    'The same globals also hang after each command (as in ``list -v``).'
    for names, kwargs in GLOBALS_AFTER_COMMAND:
        if names[0] in parser._option_string_actions:
            continue
        parser.add_argument(*names, **kwargs)


def _attach_command(sub: Any, command: Command) -> argparse.ArgumentParser:
    'Attach one entry of the command table as a sub-parser (group commands get another level).'
    child = sub.add_parser(command.name, help=command.help)
    attach(child, command.arguments, command.options)
    if command.subs:
        actions = child.add_subparsers(dest=command.dest or f"{command.name}_action",
                                       required=True, metavar=command.sub_help)
        for item in command.subs:
            grand = actions.add_parser(item.name, help=item.help)
            attach(grand, item.arguments, item.options)
    return child


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opendevice", description="",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_globals(parser)
    sub = parser.add_subparsers(dest="command", required=True, metavar='command')
    for command in COMMANDS:
        _add_globals_after_command(_attach_command(sub, command))
    return parser


def merge_globals(args: argparse.Namespace) -> argparse.Namespace:
    'Merge back the globals given after the sub-command and settle the final values.'
    args.device = getattr(args, "device_sub", None) or getattr(args, "device", None)
    args.link = getattr(args, "link_sub", None) or getattr(args, "link", None) or "auto"
    args.mux = getattr(args, "mux_sub", None) or getattr(args, "mux", None)
    args.force = bool(getattr(args, "force", False) or getattr(args, "force_sub", False))
    args.udid = args.device
    args.log_path = (getattr(args, "log_file", None)
                     or getattr(args, "log_file_sub", None))
    if args.log_path is None and (getattr(args, "log", False)
                                  or getattr(args, "log_sub", False)):
        args.log_path = ""
    args.log_verbose = max(getattr(args, "verbose", 0) or 0,
                           getattr(args, "verbose_sub", 0) or 0,
                           log.level_from_env())
    return args


def _utf8_output() -> None:
    'Use UTF-8 for redirected output; an interactive console keeps its own encoding.'
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        if getattr(stream, "isatty", lambda: False)():
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main(argv: Optional[list[str]] = None) -> int:
    _utf8_output()
    args = merge_globals(build_parser().parse_args(argv))
    log.configure(verbose=args.log_verbose, path=args.log_path)
    try:
        return HANDLERS[args.command](args)
    except OpenDeviceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('interrupted', file=sys.stderr)
        return 130
