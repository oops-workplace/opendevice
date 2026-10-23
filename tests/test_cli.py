from __future__ import annotations

import pytest

from opendevice.cli import build_parser, merge_globals

CASES = [
    ["devices"],
    ["devices", "--json", "--fast"],
    ["devices", "-d", "iphone8"],
    ["deviceinfo"],
    ["deviceinfo", "--all"],
    ["deviceinfo", "--all", "--json"],
    ["stream", "--seconds", "3", "--out", "s.h265"],
    ["hid"],
    ["hid", "--raw"],
    ["key", "home"],
    ["key", "12,64"],
    ["type", "abc"],
    ["uitree", "--limit", "5"],
    ["uitree", "--tap", 'search', "--home"],
    ["uitree", "--json"],
    ["click", "200,300"],
    ["click", "0.5,0.5"],
    ["click", 'search', "--exact", "--no-verify"],
    ["swipe", "200,600", "200,200"],
    ["paste", "--set", 'text'],
    ["paste", "--get"],
    ["sendtext", 'sample', "--no-approve"],
    ["pairing", "--force"],
    ["unpairing"],
    ["unpairing", "--all"],
    ["unpairing", "-d", "EXAMPLE-0001"],
    ["wifi", "--list"],
    ["wifi", "--seconds", "0"],
    ["afc", "ls", "/"],
    ["afc", "pull", "/Documents/a.txt", "./a.txt"],
    ["afc", "push", "./a.txt", "/Documents/a.txt"],
    ["afc", "ls", "/", "--app", "com.example.app"],
    ["afc", "pull", "/Documents/a.txt", "./", "--app", "com.example.app", "--documents"],
    ["afc", "rm", "/PublicStaging/a.txt"],
    ["afc", "rm", "/DCIM/100APPLE", "-r"],
    ["afc", "rm", "/Documents", "--recursive", "--app", "com.example.app"],
    ["install", "MyApp.ipa", "--developer", "--upgrade"],
    ["uninstall", "com.example.app"],
    ["applist", "--json"],
    ["applist", "--system"],
    ["applist", "--all"],
    ["launch", "com.example.app", "--arg=-x", "--keep-existing"],
    ["ps"],
    ["ps", "--all", "--json"],
    ["ps", "--front"],
    ["screenshot", "--out", "s.png"],
    ["devmode", "status"],
    ["devmode", "reveal"],
    ["devmode", "enable"],
    ["devmode", "accept"],
    ["power", "info"],
    ["power", "battery"],
    ["power", "ioreg", "--class", "IOPMPowerSource"],
    ["power", "restart"],
    ["power", "shutdown"],
    ["power", "sleep"],
    ["syslog", "--seconds", "5", "--filter", "SpringBoard"],
    ["paths"],
    ["paths", "--json"],
    ["devices", "-v", "--log"],
    ["deviceinfo", "--log-file=run.log"],
    ["mounter", "list"],
    ["mounter", "lookup", "--type", "Personalized"],
    ["mounter", "status"],
    ["mounter", "nonce", "--type", "Personalized"],
    ["mounter", "mount"],
    ["mounter", "unmount"],
]


@pytest.mark.parametrize("argv", CASES, ids=lambda argv: " ".join(argv))
def test_parses(argv: list[str]) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    assert args.command == argv[0]


def test_all_commands_registered() -> None:
    'The sub-command table is below - a missing entry means the wiring missed one.'
    import argparse

    parser = build_parser()
    subparsers = next(action for action in parser._actions
                      if isinstance(action, argparse._SubParsersAction))
    assert sorted(subparsers.choices) == [
        "afc", "applist", "click", "deviceinfo", "devices", "devmode",
        "hid", "install", "key", "kill", "launch", "mounter", "pairing", "paste",
        "paths", "power", "ps", "screenshot", "sendtext", "stream", "swipe", "syslog",
        "type", "uitree", "uninstall", "unpairing", "wifi",
    ]


def test_common_flags_reach_every_command() -> None:
    parser = build_parser()
    args = parser.parse_args(["-d", "EXAMPLE-0001", "--mux", "127.0.0.1:27015",
                              "deviceinfo"])
    assert args.device == "EXAMPLE-0001"
    assert args.mux == "127.0.0.1:27015"


@pytest.mark.parametrize("argv,expected", [
    (["-d", "EXAMPLE-0001", "deviceinfo"], "EXAMPLE-0001"),
    (["deviceinfo", "-d", "EXAMPLE-0001"], "EXAMPLE-0001"),
    (["deviceinfo", "--device", "EXAMPLE-0001"], "EXAMPLE-0001"),
    (["deviceinfo", "-d", "A", "--device", "B"], "B"),
])
def test_device_selector_before_and_after_the_command(argv: list[str],
                                                     expected: str) -> None:
    assert merge_globals(build_parser().parse_args(argv)).device == expected


@pytest.mark.parametrize("argv,expected", [
    (["deviceinfo"], "auto"),
    (["--link", "wifi", "deviceinfo"], "wifi"),
    (["deviceinfo", "--link", "usb"], "usb"),
    (["--link", "usb", "deviceinfo", "--link", "wifi"], "wifi"),
])
def test_link_choice(argv: list[str], expected: str) -> None:
    assert merge_globals(build_parser().parse_args(argv)).link == expected


def _level(args) -> int:
    '``-v`` counts whether placed before or after; the highest level wins.'
    return max(getattr(args, "verbose", 0) or 0, getattr(args, "verbose_sub", 0) or 0)


@pytest.mark.parametrize("argv,expected", [
    (["-v", "devices"], 1), (["devices", "-v"], 1),
    (["-vv", "devices"], 2), (["devices", "-vv"], 2),
    (["-v", "devices", "-v"], 1),
    (["-v", "devices", "-vv"], 2),
])
def test_verbose_works_before_and_after_the_command(argv: list[str], expected: int) -> None:
    assert _level(build_parser().parse_args(argv)) == expected


@pytest.mark.parametrize("argv,expected", [
    (["--log-file", "run.log", "devices"], "run.log"),
    (["devices", "--log-file", "run.log"], "run.log"),
    (["--log", "devices"], ""),
    (["devices", "--log"], ""),
])
def test_log_destination_flags(argv: list[str], expected: str) -> None:
    args = build_parser().parse_args(argv)
    path = getattr(args, "log_file", None) or getattr(args, "log_file_sub", None)
    if path is None and (getattr(args, "log", False) or getattr(args, "log_sub", False)):
        path = ""
    assert path == expected


def test_grouped_commands_need_an_action() -> None:
    '``afc`` / ``power`` / ``devmode`` / ``mounter`` take sub-actions; a missing one must error.'
    parser = build_parser()
    for argv in (["afc"], ["power"], ["devmode"], ["mounter"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_version_exits_zero() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--version"])
    assert excinfo.value.code == 0
