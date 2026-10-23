from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ...core import log as log_module
from ...core import paths
from ...core.errors import OpenDeviceError, ServiceError
from .image import (DdiImage, cache_dir, check_dmg_matches, list_images, release_of,
                    usable_images)

DDI_FILES = ("Image.dmg", "BuildManifest.plist", "Image.dmg.trustcache")

DDI_URL_ENV = "OPENDEVICE_DDI_URL"
DDI_TOKEN_ENV = "OPENDEVICE_DDI_TOKEN"

DEFAULT_DDI_URL = "https://raw.githubusercontent.com/oops-workplace/opendevice-ddi/main"
DDI_INDEX_FILE = "index.json"


def source_base(base: Optional[str] = None) -> str:
    'Image source: explicit argument, then ``OPENDEVICE_DDI_URL``, then the project mirror.'
    return base or os.environ.get(DDI_URL_ENV, "").strip() or DEFAULT_DDI_URL


def remote_versions(base: Optional[str] = None, token: Optional[str] = None) -> list[str]:
    'The iOS releases the source offers (``index.json``).'
    info = _read_info(source_base(base), token)
    versions = [str(item) for item in info.get("versions") or ()]
    if not versions and info.get("latest"):
        versions = [str(info["latest"])]
    return versions


def version_for(version: Optional[str], base: Optional[str] = None,
                token: Optional[str] = None) -> Optional[str]:
    'The release the source offers for that device version.'
    offered = remote_versions(base, token)
    if not version:
        return offered[0] if offered else None
    wanted = release_of(version)
    for candidate in offered:
        if release_of(candidate) == wanted:
            return candidate
    return None


def ensure_image(product_type: Optional[str] = None,
                 identifiers: Optional[Mapping[str, Any]] = None,
                 version: Optional[str] = None,
                 base: Optional[str] = None, token: Optional[str] = None,
                 on_status: Optional[Callable[[str], None]] = None) -> list[DdiImage]:
    'Images usable for this device; downloads from the source when the cache has none.'
    items = usable_images(product_type, identifiers, version)
    if items:
        return items
    base = source_base(base)
    name = version_for(version, base, token)
    if name is None:
        offered = remote_versions(base, token)
        hint = ""
        if offered and not all(item.isdigit() for item in offered):
            hint = "; the source still uses the old layout (one directory per build)"
        elif offered:
            hint = "; it offers: " + ", ".join(offered)
        raise FileNotFoundError(f"the source has no image for iOS {version} ({base}){hint}")
    _say(on_status, f"no cached image, fetching {name} from {base}...")
    try:
        fetch_image(name, base=base, token=token, on_status=on_status)
    except Exception as exc:                    # noqa: BLE001
        log_module.get(__name__).debug("fetching %s from %s failed: %s", name, base, exc)
        return []
    return usable_images(product_type, identifiers, version)


def fetch_image(version: str, base: Optional[str] = None, token: Optional[str] = None, *,
                on_status: Optional[Callable[[str], None]] = None) -> DdiImage:
    'Fetch an image into the cache from a source (defaults to the project mirror).'
    base = source_base(base)
    token = token or os.environ.get(DDI_TOKEN_ENV, "").strip() or None

    target = paths.ensure(cache_dir(None, version))
    for name in DDI_FILES:
        _say(on_status, f"fetching {name}…")
        (target / name).write_bytes(_read_source(base, version, name, token))
    check_dmg_matches((target / DDI_FILES[0]).read_bytes(), target / DDI_FILES[1])
    for item in list_images():
        if item.image.parent == target:
            _say(on_status, f"installed {item.build} ({len(item.supported)} product types)")
            return item
    raise ServiceError(f"downloaded it but could not recognise it as an image: {target}")


def _read_info(base: str, token: Optional[str]) -> dict[str, Any]:
    'The source index (``index.json``).'
    try:
        return json.loads(_read_index(base, token) or b"{}")
    except ValueError:
        return {}


def _read_index(base: str, token: Optional[str]) -> bytes:
    if urllib.parse.urlparse(base).scheme in ("http", "https"):
        return _http_get(f"{base.rstrip('/')}/{DDI_INDEX_FILE}", token) or b""
    path = Path(base) / DDI_INDEX_FILE
    return path.read_bytes() if path.is_file() else b""


def _read_source(base: str, version: str, name: str, token: Optional[str],
                 optional: bool = False) -> bytes:
    'Read one file from the source: HTTP(S) or a local directory.'
    if urllib.parse.urlparse(base).scheme in ("http", "https"):
        for relative in (f"{version}/{name}", name):
            data = _http_get(f"{base.rstrip('/')}/{relative}", token)
            if data is not None:
                return data
        if optional:
            return b""
        raise OpenDeviceError(
            f"cannot fetch {name} (source {base}): a private source needs {DDI_TOKEN_ENV}, "
            f"or check whether it is {version}")
    for path in (Path(base) / version / name, Path(base) / name):
        if path.is_file():
            return path.read_bytes()
    if optional:
        return b""
    raise FileNotFoundError(f"the source has no {name}: {Path(base) / version}")


def _http_get(url: str, token: Optional[str]) -> Optional[bytes]:
    'GET one file.'
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except Exception:                            # noqa: BLE001
        return None


def _say(callback: Optional[Callable[[str], None]], text: str) -> None:
    if callback is not None:
        callback(text)
