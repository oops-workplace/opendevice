from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent / "opendevice"
PACKAGE = "opendevice"

LAYER: dict[str, int] = {
    "core": 0, "transport": 0,
    "remote": 1, "wireless": 1,
    "input": 2, "media": 2, "ax": 2,
    "services": 3,
    "session": 4, "cli": 4,
}

OWNER: dict[str, str] = {
    "errors": "core", "paths": "core", "plistio": "core", "lock": "core", "console": "core",
    "output": "core",
    "usbmux": "transport", "pairing": "transport", "lockdown": "transport",
    "service": "transport", "devices": "transport",
    "tunnel": "remote", "ipstack": "remote", "http2": "remote", "xpc": "remote",
    "remotexpc": "remote", "rsd": "remote", "coredevice": "remote",
    "rrp": "wireless", "wifi": "wireless",
    "hid": "input", "pasteboard": "input",
    "media": "media", "rtp": "media", "stream": "media",
    "dtx": "ax", "ax": "ax",
    "afc": "services", "installation": "services", "diagnostics": "services",
    "syslog": "services", "amfi": "services", "tss": "services",
}

SUBPACKAGES = ("core", "transport", "remote", "wireless", "input", "media", "ax", "services")


def _modules() -> list[Path]:
    return sorted(path for path in PKG.rglob("*.py")
                  if path.name not in {"__init__.py", "__main__.py"}
                  and "__pycache__" not in path.parts
                  and "tests" not in path.parts
                  and "examples" not in path.parts)


def _owner_of(path: Path) -> str:
    'Which package this file belongs to (files at the package root belong to it).'
    return path.relative_to(PKG).with_suffix("").parts[0]


def _layer(name: str) -> int:
    assert name in LAYER, f"{name!r} is not registered in LAYER; do not add packages silently"
    return LAYER[name]


def _imported_owners(path: Path) -> set[str]:
    'Which packages this file depends on (leaves normalized, then mapped to packages).'
    tree = ast.parse(path.read_text(encoding="utf-8"))
    depth = len(path.relative_to(PKG).with_suffix("").parts) - 1
    leaves: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.level or 0) > depth + 1:
                continue
            if node.module:
                leaves.add(node.module.split(".")[-1])
            else:
                leaves.update(alias.name.split(".")[-1] for alias in node.names)
        elif isinstance(node, ast.Import):
            leaves.update(alias.name.split(".")[-1] for alias in node.names
                          if alias.name.startswith(f"{PACKAGE}."))
    return {OWNER[leaf] for leaf in leaves if leaf in OWNER}


def test_owner_table_is_complete() -> None:
    'Every module in the architecture table must exist, and no uncategorized file may sit at the package root.'
    for leaf in OWNER:
        assert list(PKG.rglob(f"{leaf}.py")), f"OWNER registers a module that does not exist{leaf!r}"
    declared = {owner for owner in LAYER}
    for path in _modules():
        assert _owner_of(path) in declared, f"{path.relative_to(PKG)} is not categorized (missing from LAYER)"


@pytest.mark.parametrize("path", _modules(), ids=lambda p: str(p.relative_to(PKG)))
def test_no_upward_dependency(path: Path) -> None:
    'No dependency on a higher layer (core/transport <- remote/wireless <- capabilities <- services <- facade).'
    mine = _layer(_owner_of(path))
    for owner in _imported_owners(path):
        theirs = _layer(owner)
        assert theirs <= mine, (
            f"{path.relative_to(PKG)} ({_owner_of(path)} layer {mine}) depends on the higher {owner} (layer {theirs})")


def test_nothing_imports_the_facade() -> None:
    '``session`` / ``cli`` are the facade; other modules must not import them.'
    for path in _modules():
        if _owner_of(path) in {"session", "cli"}:
            continue
        assert not (_imported_owners(path) & {"session", "cli"}), \
            f"{path.relative_to(PKG)} depends back on the facade"


def test_subpackages_export_their_public_names() -> None:
    'Every subpackage needs an ``__init__.py`` declaring ``__all__`` (the public surface must be explicit).'
    for name in SUBPACKAGES:
        init = PKG / name / "__init__.py"
        assert init.is_file(), f"{name}/ is missing __init__.py"
        tree = ast.parse(init.read_text(encoding="utf-8"))
        assigned = {target.id for node in ast.walk(tree)
                    if isinstance(node, ast.Assign)
                    for target in node.targets
                    if isinstance(target, ast.Name)}
        assert "__all__" in assigned, f"{name}/__init__.py does not define __all__"


def test_no_cross_package_private_imports() -> None:
    "Subpackages must not import each other's private names (leading underscore)."
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                private = [alias.name for alias in node.names
                           if alias.name.startswith("_") and not alias.name.startswith("__")]
                assert not private, \
                    f"{path.relative_to(PKG)} imports private names from a sibling module: {private}"
