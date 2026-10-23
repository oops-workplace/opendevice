"""Fail when the release tag and the package version disagree.

Published versions cannot be replaced, so a mismatch has to stop the release.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "opendevice" / "__init__.py"


def main() -> int:
    match = re.search(r'__version__ = "([^"]+)"', SOURCE.read_text(encoding="utf-8"))
    if not match:
        print("cannot find __version__ in", SOURCE)
        return 1
    version = match.group(1)
    tag = os.environ.get("GITHUB_REF_NAME", "").lstrip("v")
    print(f"tag {tag!r} / __version__ {version!r}")
    if tag and tag != version:
        print("the release tag and the package version must match")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
