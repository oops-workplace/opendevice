from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webui.__main__ import main  # noqa: E402 - path must be set up first

if __name__ == "__main__":
    sys.exit(main())
