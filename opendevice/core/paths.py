from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

APP_NAME = "opendevice"
ENV_HOME = "OPENDEVICE_HOME"

LOCKDOWN_DIRS = {
    "win32": [os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Apple", "Lockdown")],
    "darwin": ["/var/db/lockdown"],
    "linux": ["/var/lib/lockdown"],
}
LOCKDOWN_DIRS["cygwin"] = LOCKDOWN_DIRS["win32"]

RECORD_PREFIX = "remote_"
RECORD_SUFFIX = ".plist"


def home() -> Optional[Path]:
    'Where ``OPENDEVICE_HOME`` points (``None`` when unset, meaning the default).'
    value = os.environ.get(ENV_HOME)
    return Path(value).expanduser() if value else None


def root() -> Path:
    'Single data directory: pairing records at the top level, everything else in sub-directories.'
    override = home()
    return override if override is not None else Path.home() / f".{APP_NAME}"


def records_dir() -> Path:
    'Remote pairing records (``remote_<UDID>.plist``) sit at the top level of :func:`root`.'
    return root()


def ddi_dir(product_type: Optional[str] = None, build: Optional[str] = None) -> Path:
    'Disk image (DDI) cache: ``ddi[/<ProductType>[/<iOS build>]]``.'
    path = root() / "ddi"
    if product_type:
        path = path / product_type
    if build:
        path = path / build
    return path


def logs_dir() -> Path:
    return root() / "logs"


def locks_dir() -> Path:
    'Device locks: ``locks/<UDID>.lock`` (see :mod:`opendevice.core.lock`).'
    return root() / "locks"


def lock_file(udid: str) -> Path:
    'Lock file path for one device.'
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in str(udid))
    return ensure(locks_dir()) / f"{safe}.lock"


def tmp_dir() -> Path:
    return root() / "tmp"


def ensure(path: Path) -> Path:
    'Create the directory and return it (``mkdir -p``).'
    path.mkdir(parents=True, exist_ok=True)
    return path


def record_paths(identifier: Optional[str] = None, *,
                 all_records: bool = False) -> list[Path]:
    'Candidate paths for record files.'
    if identifier and all_records:
        raise ValueError('pass either identifier or all_records, not both')
    if not identifier and not all_records:
        raise ValueError('pass identifier or all_records=True')

    directory = records_dir()
    if identifier:
        return [directory / f"{RECORD_PREFIX}{identifier}{RECORD_SUFFIX}"]
    try:
        return sorted(directory.glob(f"{RECORD_PREFIX}*{RECORD_SUFFIX}"))
    except OSError:
        return []


def unpair(identifier: Optional[str] = None, *,
           all_records: bool = False) -> list[Path]:
    'Delete **local** remote pairing records and return the paths that were removed.'
    removed: list[Path] = []
    for path in record_paths(identifier, all_records=all_records):
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        try:
            os.unlink(path)
        except OSError:
            continue
        removed.append(path)
    return removed


def describe() -> dict[str, object]:
    'Overview for the CLI and debugging: where each directory is and how much it holds.'
    base = root()
    apple_dirs = [Path(item) for item in LOCKDOWN_DIRS.get(sys.platform, [])]
    return {
        "home_override": str(home()) if home() else None,
        "root": str(base),
        "root_exists": base.is_dir(),
        "records_count": (len(list(base.glob(f"{RECORD_PREFIX}*{RECORD_SUFFIX}")))
                          if base.is_dir() else 0),
        "ddi": str(ddi_dir()),
        "logs": str(logs_dir()),
        "locks": str(locks_dir()),
        "locks_count": len(list(locks_dir().glob("*.lock"))) if locks_dir().is_dir() else 0,
        "apple_usb_records": sorted({str(path) for path in apple_dirs}),
    }
