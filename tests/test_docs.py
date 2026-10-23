from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

SKIP_PARTS = {"__pycache__", ".venv", "egg-info", ".ref", ".pytest_cache", "node_modules"}

IMPORT_RE = re.compile(
    r"^\s*(?:>>>\s*)?(from\s+opendevice[\w.]*\s+import\s+[^\n#]+"
    r"|import\s+opendevice[\w.]*(?:\s+as\s+\w+)?)\s*$")
ATTR_RE = re.compile(r"\bopendevice\.([a-z_][a-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)")


def documents() -> list[Path]:
    found = []
    for path in ROOT.rglob("*.md"):
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        found.append(path)
    return sorted(found)


def test_there_are_documents_to_check() -> None:
    'At least the README must be found - otherwise the glob is broken.'
    names = {path.name for path in documents()}
    assert "README.md" in names


@pytest.mark.parametrize("document", documents(), ids=lambda path: path.name)
def test_documented_imports_work(document: Path) -> None:
    'Every import written in the docs must import successfully.'
    failures = []
    for number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), 1):
        match = IMPORT_RE.match(line)
        if not match:
            continue
        statement = match.group(1).strip().rstrip("`").strip()
        try:
            exec(statement, {})                      # noqa
        except Exception as exc:                     # noqa: BLE001
            failures.append(f"{document.name}:{number}  {statement}\n      "
                            f"{type(exc).__name__}: {exc}")
    assert not failures, 'imports in the docs do not work:\n' + "\n  ".join(failures)


@pytest.mark.parametrize("document", documents(), ids=lambda path: path.name)
def test_documented_module_attributes_exist(document: Path) -> None:
    '``opendevice.<package>.<attribute>`` mentioned in the docs must exist.'
    failures = []
    for number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), 1):
        for package, attribute in ATTR_RE.findall(line):
            if package in {"exceptions", "cli"}:
                continue
            try:
                module = importlib.import_module(f"opendevice.{package}")
            except ImportError as exc:
                failures.append(f"{document.name}:{number}  opendevice.{package} does not exist ({exc})")
                continue
            if not hasattr(module, attribute):
                failures.append(
                    f"{document.name}:{number}  opendevice.{package}.{attribute} does not exist (available: {', '.join(sorted(n for n in dir(module) if not n.startswith('_')))[:120]})")
    assert not failures, 'module attributes in the docs do not exist:\n' + "\n  ".join(failures)


def code_lines(document: Path):
    'Yield ``(line number, text)`` for lines inside fenced code blocks.'
    inside = False
    for number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if inside:
            yield number, line


def test_documented_commands_exist() -> None:
    "Command names in the README's ``opendevice <command>`` must be in the table."
    from opendevice.cli.commands import COMMANDS

    known = {command.name for command in COMMANDS}
    missing = []
    for path in documents():
        for number, line in code_lines(path):
            match = re.match(r"\s*(?:python -m )?opendevice\s+([a-z][\w-]*)", line)
            if match and match.group(1) not in known:
                missing.append(f"{path.name}:{number}  opendevice {match.group(1)} is not a command (available: {', '.join(sorted(known))})")
    assert not missing, 'commands in the docs do not exist:\n  ' + "\n  ".join(missing)

