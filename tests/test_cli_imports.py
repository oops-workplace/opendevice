from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "opendevice"
PACKAGE = "opendevice"
CLI = PKG / "cli"


def _package_of(path: Path) -> str:
    '``opendevice/ax/ax.py`` -> ``opendevice.ax``; a root-level ``x.py`` -> ``opendevice``.'
    relative = path.relative_to(PKG).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join([PACKAGE] + parts[:-1])


def _relative_modules(path: Path, tree: ast.AST) -> list[str]:
    'Resolve the full module name a ``from ..x import ...`` points at.'
    here = _package_of(path).split(".")
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and getattr(node, "level", 0) and node.module:
            base = here[:len(here) - (node.level - 1)] or here[:1]
            found.append(".".join(base + node.module.split(".")))
    return found


def test_every_relative_import_resolves() -> None:
    'Whole-package scan: every relative import target must exist.'
    bad: list[str] = []
    checked = 0
    for path in sorted(PKG.rglob("*.py")):
        if path.name == "__init__.py" or any(p.name == "__pycache__" for p in path.parents):
            continue
        if "tests" in path.parts:
            continue
        if "examples" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _relative_modules(path, tree):
            checked += 1
            if importlib.util.find_spec(module) is None:
                bad.append(f"{path.relative_to(PKG)}: {module}")
    assert checked > 0, 'no relative import was scanned; this test is no longer doing its job'
    assert not bad, 'modules that cannot be imported (they only blow up on a cold start):\n' + "\n".join(bad)


def test_wireless_cli_reaches_the_real_modules() -> None:
    'The two wireless commands must point at the real modules in ``wireless`` (no rrp / wifi at the top level).'
    source = (CLI / "wireless.py").read_text(encoding="utf-8")
    assert "from ..wireless.rrp import" in source
    assert "from ..wireless.wifi import" in source
    assert "from ..rrp import" not in source
    assert "from ..wifi import" not in source
