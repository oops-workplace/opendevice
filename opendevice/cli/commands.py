from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any, Sequence

from .apps import (
    cmd_afc,
    cmd_applist,
    cmd_install,
    cmd_kill,
    cmd_launch,
    cmd_ps,
    cmd_screenshot,
    cmd_uninstall,
)
from .device import cmd_deviceinfo, cmd_devices
from .media import (
    cmd_click,
    cmd_hid,
    cmd_key,
    cmd_paste,
    cmd_sendtext,
    cmd_stream,
    cmd_swipe,
    cmd_type,
    cmd_uitree,
)
from .system import cmd_devmode, cmd_mounter, cmd_paths, cmd_power, cmd_syslog
from .wireless import cmd_pairing, cmd_unpairing, cmd_wifi


@dataclass(frozen=True)
class Argument:
    'Positional arguments (``install <package>`` / ``afc pull <remote> <local>``).'
    name: str
    help: str = ""
    nargs: str = ""
    metavar: str = ""
    default: Any = None


@dataclass(frozen=True)
class Option:
    'Optional arguments. ``name`` becomes ``--name`` (``dest`` follows argparse rules).'
    name: str
    help: str = ""
    kind: str = "flag"
    args: tuple[str, ...] = ()
    dest: str = ""
    metavar: str = ""
    default: Any = None
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Subcommand:
    'One action under a group command (``afc ls`` / ``power battery``).'
    name: str
    help: str = ""
    arguments: tuple[Argument, ...] = ()
    options: tuple[Option, ...] = ()


@dataclass(frozen=True)
class Command:
    'One top-level command.'
    name: str
    action: Any = None
    help: str = ""
    arguments: tuple[Argument, ...] = ()
    options: tuple[Option, ...] = ()
    subs: tuple[Subcommand, ...] = ()
    dest: str = ""
    sub_help: str = 'action'


def attach(parser: argparse.ArgumentParser, arguments: Sequence[Argument] = (),
           options: Sequence[Option] = ()) -> None:
    'Attach positional arguments and options to one parser.'
    for argument in arguments:
        parser.add_argument(argument.name, **_argument_kwargs(argument))
    for option in options:
        parser.add_argument(*_names(option), **_option_kwargs(option))


def _argument_kwargs(argument: Argument) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if argument.help:
        kwargs["help"] = argument.help
    if argument.nargs:
        kwargs["nargs"] = argument.nargs
    if argument.metavar:
        kwargs["metavar"] = argument.metavar
    if argument.default is not None:
        kwargs["default"] = argument.default
    return kwargs


def _names(option: Option) -> tuple[str, ...]:
    return option.args or (f"--{option.name}",)


def _option_kwargs(option: Option) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"help": option.help}
    if option.kind == "flag":
        kwargs["action"] = "store_true"
    elif option.kind == "append":
        kwargs["action"] = "append"
        kwargs["default"] = option.default if option.default is not None else []
    elif option.kind == "int":
        kwargs["type"] = int
    elif option.kind == "float":
        kwargs["type"] = float
    if option.kind == "choice":
        kwargs["choices"] = option.choices
    if option.dest:
        kwargs["dest"] = option.dest
    if option.metavar:
        kwargs["metavar"] = option.metavar
    if option.default is not None and option.kind not in ("flag", "append"):
        kwargs["default"] = option.default
    return kwargs


def handler_table(commands: Sequence[Command] = ()) -> dict[str, Any]:
    '``{command name: handler}``.'
    return {command.name: command.action
            for command in (commands or COMMANDS) if command.action is not None}


SANDBOX_OPTIONS = (
    Option("app", kind="text", metavar="BUNDLE",
           help="enter this app's sandbox: paths start at its container root (Documents / Library / tmp...). Only available over USB"),
    Option("documents", help='use with --app: only its Documents directory (VendDocuments)'),
)

RM_OPTIONS = SANDBOX_OPTIONS + (
    Option("recursive", args=("-r", "--recursive"),
           help='delete a directory and everything inside (without it only files can be deleted)'),
)

COMMANDS: tuple[Command, ...] = (
    Command(
        "devices", cmd_devices,
        help='list devices',
        options=(Option("json", help='output JSON (including who holds it)'),
                 Option("fast", help='only ask usbmux, without the product type / OS version (does not touch the device)')),
    ),
    Command("deviceinfo", cmd_deviceinfo, help='device info (lockdown keys)',
            options=(Option("all", help='list every lockdown key (the default shows the common ones)'),
                     Option("screen", help='main screen resolution (CoreDevice display info)'),
                     Option("json", help='output JSON'))),

    Command(
        "uitree", cmd_uitree, help='control tree of the current screen (with coordinates)',
        options=(Option("limit", kind="int", default=40, help='how many rows to print at most'),
                 Option("filter", kind="text", metavar="TEXT", help='only show elements whose text contains this'),
                 Option("tap", kind="text", metavar="TEXT",
                        help='find an element by text, tap its center, then re-fetch and compare (end-to-end check)'),
                 Option("home", help='press Home first, then fetch'),
                 Option("json", help='print the raw fields of every control (JSON)')),
    ),
    Command(
        "click", cmd_click, help="tap: coordinates or a control's text",
        arguments=(Argument("target", help='200,300 (point coordinates from the tree) / 0.5,0.5 (normalized) / a search term (find by text and tap its center)'),),
        options=(Option("exact", help='match the text exactly (substring by default)'),
                 Option("no-verify", help='do not re-fetch and compare when tapping by text')),
    ),
    Command(
        "swipe", cmd_swipe, help='swipe: start end',
        arguments=(Argument("start", metavar="X1,Y1", help='start, e.g. 200,600'),
                   Argument("end", metavar="X2,Y2", help='end, e.g. 200,200')),
    ),
    Command(
        "hid", cmd_hid, help='list the HID surfaces this device registered (low-level query)',
        options=(Option("raw", help='print the raw surface response as JSON (including the authenticated / builtIn flags)'),),
    ),
    Command(
        "key", cmd_key, help='press a hardware button: home / power / volumeUp / volumeDown / mute / siri',
        arguments=(Argument("name", metavar="NAME",
                            help='home / power / volumeUp / volumeDown / mute / siri (siri = long power press)'),),
    ),
    Command(
        "type", cmd_type, help='type character by character (ASCII only; use sendtext for other scripts)',
        arguments=(Argument("text", metavar="TEXT", help='the text to type (ASCII characters only)'),),
    ),
    Command(
        "sendtext", cmd_sendtext,
        help='send a string into the input field (pasteboard + Cmd+V)',
        arguments=(Argument("text", help='the text to send'),),
        options=(Option("no-paste", help='only write the pasteboard, no Cmd+V (and no stream)'),
                 Option("no-approve", help='do not auto-dismiss the "Allow Paste" prompt')),
    ),
    Command(
        "paste", cmd_paste, help='device pasteboard (write / read)',
        options=(Option("set", kind="text", metavar="TEXT", help='write TEXT into the device pasteboard'),
                 Option("get", help='read the pasteboard text back'),
                 Option("name", kind="text", default="general", help='pasteboard name (default general)')),
    ),

    Command(
        "stream", cmd_stream, help='collect the media stream (H.265 Annex-B, written as is)',
        options=(Option("seconds", kind="float", default=15.0, help='how many seconds to collect'),
                 Option("out", kind="text", metavar="PATH",
                        help='write the Annex-B bytes to a file as is (mpv / VLC can play it)')),
    ),

    Command(
        "applist", cmd_applist, help='installed apps (by default only user-installed ones)',
        options=(Option("system", help='only system apps'),
                 Option("all", help='system + user-installed (list everything)'),
                 Option("json", help='output the full JSON')),
    ),
    Command(
        "install", cmd_install, help='install a package (ipa / .app)',
        arguments=(Argument("package"),),
        options=(Option("developer", help='install as a developer app'),
                 Option("upgrade", help='upgrade when already installed')),
    ),
    Command("uninstall", cmd_uninstall, help='uninstall an app',
            arguments=(Argument("bundle_id"),)),
    Command(
        "launch", cmd_launch, help='launch an app',
        arguments=(Argument("bundle_id"),),
        options=(Option("arg", kind="append",
                        help='launch arguments, repeatable; ones starting with - must be written as --arg=-x'),
                 Option("keep-existing", help='do not kill the running instance first'),
                 Option("json", help='print the raw device response')),
    ),
    Command(
        "kill", cmd_kill,
        help='stop an app or a process',
        arguments=(Argument("target", help='bundle id (e.g. com.example.app) or a pid'),),
        options=(Option("signal", kind="int", default=9,
                        help='signal number, 9 (SIGKILL) by default; 15 = SIGTERM (let it exit)'),),
    ),
    Command(
        "ps", cmd_ps, help='processes (by default only apps)',
        options=(Option("all", help='include system daemons'),
                 Option("front", help='only the app on the current screen'),
                 Option("json", help='print the raw processToken')),
    ),
    Command(
        "afc", cmd_afc, help='device filesystem (ls / pull / push / rm; --app enters an app sandbox)',
        dest="afc_action",
        subs=(Subcommand("ls", help='list a directory',
                         arguments=(Argument("path", help='directory on the device'),),
                         options=SANDBOX_OPTIONS),
              Subcommand("pull", help='device -> host (recursive for directories)',
                         arguments=(Argument("remote", metavar="SRC", help='path on the device'),
                                    Argument("local", metavar="DST", help='path on this host')),
                         options=SANDBOX_OPTIONS),
              Subcommand("push", help='host -> device (recursive for directories)',
                         arguments=(Argument("local", metavar="SRC", help='path on this host'),
                                    Argument("remote", metavar="DST", help='path on the device')),
                         options=SANDBOX_OPTIONS),
              Subcommand("rm", help='delete a file or directory on the device',
                         arguments=(Argument("path", help='path on the device; add -r for directories'),),
                         options=RM_OPTIONS)),
    ),
    Command(
        "screenshot", cmd_screenshot, help='take one static screenshot (PNG)',
        options=(Option("out", kind="text", metavar="PATH", default="screenshot.png"),),
    ),

    Command(
        "pairing", cmd_pairing, help='establish remote pairing over USB (a prerequisite for the Wi-Fi link)',
        options=(Option("force", help='rebuild even if a record exists (the old one is deleted)'),),
    ),
    Command(
        "unpairing", cmd_unpairing, help='delete the local remote pairing records (this host side of Unpair)',
        options=(Option("all", help='delete the records of every device'),),
    ),
    Command(
        "wifi", cmd_wifi, help='Wi-Fi link: bonjour + RemotePairing',
        options=(Option("list", help='only list devices advertising on the network'),
                 Option("host", kind="text", help='device address; without it, discover via bonjour'),
                 Option("identifier", kind="text", help='pick the device to connect to'),
                 Option("seconds", kind="float", default=8.0,
                        help='how long to stream after the tunnel is up (0 = tunnel only)')),
    ),

    Command(
        "devmode", cmd_devmode, help='developer mode (status / enable / confirm)',
        dest="dev_action",
        subs=(Subcommand("status", help='check whether it is enabled'),
              Subcommand("reveal", help='reveal the developer mode switch in Settings'),
              Subcommand("enable", help='enable: request -> wait for the reboot -> confirm'),
              Subcommand("accept", help='only confirm once after a reboot')),
    ),
    Command(
        "power", cmd_power, help='power and diagnostics', dest="power_action",
        subs=(Subcommand("info", help='all diagnostics info'),
              Subcommand("battery", help='battery / power'),
              Subcommand("ioreg", help='IORegistry (narrow with --class / --name / --plane)',
                         options=(Option("class", kind="text", dest="io_class"),
                                  Option("name", kind="text"),
                                  Option("plane", kind="text"))),
              Subcommand("restart", help='reboot'),
              Subcommand("shutdown", help='shut down'),
              Subcommand("sleep", help='sleep')),
    ),
    Command(
        "syslog", cmd_syslog, help='collect the system log',
        options=(Option("seconds", kind="float", default=10.0, help='how long to collect (0 = forever)'),
                 Option("filter", kind="text", metavar="TEXT", help='only show lines containing this')),
    ),
    Command(
        "mounter", cmd_mounter, help='disk image (DDI)', dest="mounter_action",
        subs=(Subcommand("list", help='image entries known right now'),
              Subcommand("lookup", help='signature of one image type',
                         options=(Option("type", kind="text", default="Developer"),)),
              Subcommand("status", help='whether developer mode is enabled'),
              Subcommand("nonce", help='nonce used by personalized images',
                         options=(Option("type", kind="text"),)),
              Subcommand("images", help='which images are cached and which one works for this device'),
              Subcommand("fetch", help='fetch an image into the cache (default source: the opendevice-ddi mirror)',
                        arguments=(Argument("version", metavar="VERSION",
                                            help='the iOS version to fetch (27.0); the source decides the build'),),
                        options=(Option("url", kind="text", metavar="URL",
                                        help='source base URL (defaults to OPENDEVICE_DDI_URL, then the built-in mirror; may also be a local directory / share)'),
                                 Option("token", kind="text", metavar="TOKEN",
                                        help='token for a private source (defaults to OPENDEVICE_DDI_TOKEN)'))),
              Subcommand("mount", help='mount a personalized image (ReceiveBytes + MountImage)',
                        options=(Option("image", kind="text", metavar="PATH",
                                        help='DDI payload (.dmg); look in the image cache when omitted'),
                                 Option("manifest", kind="text", metavar="PATH",
                                        help='IM4M signature file; ask the device for its cache when omitted'),

                                  Option("url", kind="text", metavar="URL",
                                         help='source to fetch from when the cache has no image (defaults to OPENDEVICE_DDI_URL, then the built-in mirror)'),
                                  Option("token", kind="text", metavar="TOKEN",
                                         help='token for a private source (defaults to OPENDEVICE_DDI_TOKEN)'),
                                  Option("no-fetch", help='never download: fail when the cache has no image'))),
              Subcommand("unmount", help='unmount the image',
                         options=(Option("path", kind="text", metavar="MOUNTPATH",
                                         help='mount path; unmount the currently mounted one when omitted'),))),
    ),
    Command(
        "paths", cmd_paths, help='where the opendevice directories live',
        options=(Option("json"),),
    ),
)
