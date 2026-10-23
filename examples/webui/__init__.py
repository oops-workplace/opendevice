import sys
from pathlib import Path


def _bootstrap_opendevice() -> None:
    'Put the repository root first on ``sys.path`` so the local copy wins over an installed one.'
    if "opendevice" in sys.modules:
        return
    repo = Path(__file__).resolve().parents[2]
    if (repo / "opendevice" / "__init__.py").is_file():
        sys.path.insert(0, str(repo))


_bootstrap_opendevice()

from opendevice.ax import AXClient, AXElement, find, find_all, parse_rect, to_tree
from opendevice.input.hid import BUTTONS

from .server import WebUI
from .session import BUTTON_SPECS, Session

__all__ = [
    "Session",
    "WebUI",
    "AXClient",
    "AXElement",
    "find",
    "find_all",
    "parse_rect",
    "to_tree",
    "BUTTONS",
    "BUTTON_SPECS",
]
