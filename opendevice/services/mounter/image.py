from __future__ import annotations

import hashlib
import plistlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from ...core import paths
from ...core.errors import OpenDeviceError, ServiceError

_MANIFESTS: dict[tuple[str, int, int], dict[str, Any]] = {}
_DIGESTS: dict[tuple[str, int, int], bytes] = {}


def cache_dir(product_type: Optional[str] = None, version: Optional[str] = None) -> Path:
    'Image cache directory: ``<cache root>/ddi``, optionally split by product type / iOS version.'
    return paths.ddi_dir(product_type, version)


def cached_image(product_type: Optional[str] = None,
                 version: Optional[str] = None) -> Optional[Path]:
    'Image payload already in the cache (``.dmg``).'
    for directory in (cache_dir(product_type, version) if product_type and version else None,
                      cache_dir(product_type) if product_type else None,
                      cache_dir()):
        if directory is None or not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.dmg")):
            if path.is_file():
                return path
    root = cache_dir()
    if root.is_dir():
        for path in sorted(root.rglob("*.dmg")):
            if path.is_file():
                return path
    return None


def cached_manifest(product_type: Optional[str] = None,
                    version: Optional[str] = None) -> Optional[Path]:
    '``BuildManifest.plist`` in the cache (needed for TSS tickets).'
    return _cached_file("BuildManifest.plist", product_type, version)


def cached_trustcache(product_type: Optional[str] = None,
                      version: Optional[str] = None) -> Optional[Path]:
    'Trust cache in the cache (``*.trustcache``, passed to MountImage together with the signature).'
    return _cached_file("*.trustcache", product_type, version)


@dataclass(frozen=True)
class DdiImage:
    '**One** personalized DDI (payload + manifest + trust cache).'

    build: str
    image: Path
    manifest: Path
    trustcache: Optional[Path] = None
    supported: tuple[str, ...] = ()
    digest: bytes = b''
    version: str = ''

    def supports(self, product_type: Optional[str]) -> bool:
        'Whether this image supports the given product type (no support list means no filtering).'
        if not product_type or not self.supported:
            return True
        return product_type in self.supported

    def identity_for(self, identifiers: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        'Pick a BuildIdentity from this manifest by device identity.'
        from .. import tss

        manifest = load_manifest(self.manifest)
        if not manifest:
            return None
        try:
            return tss.pick_build_identity(manifest, identifiers)
        except OpenDeviceError:
            return None

    def declared_digest(self, identifiers: Mapping[str, Any]) -> Optional[bytes]:
        'The payload digest this manifest declares **for that device**.'
        identity = self.identity_for(identifiers)
        if identity is None:
            return None
        entry = (identity.get("Manifest") or {}).get("PersonalizedDMG") or {}
        digest = entry.get("Digest")
        return bytes(digest) if digest else None

    def fits(self, identifiers: Mapping[str, Any]) -> bool:
        'Whether this payload is the one the manifest declares for that device.'
        expected = self.declared_digest(identifiers)
        return expected is None or expected == self.digest


def list_images() -> list[DdiImage]:
    'All DDIs in the image cache directory (one entry per release).'
    root = cache_dir()
    if not root.is_dir():
        return []
    candidates = [path for path in sorted(root.iterdir()) if path.is_dir()] + [root]
    found: list[DdiImage] = []
    seen: set[tuple[str, str]] = set()
    for directory in candidates:
        images = sorted(directory.glob("*.dmg"))
        manifests = sorted(directory.glob("BuildManifest.plist"))
        if not images or not manifests:
            continue
        image, manifest = images[0], manifests[0]
        key = (str(image), str(manifest))
        if key in seen:
            continue
        seen.add(key)
        info = load_manifest(manifest)
        build = str(info.get("ProductBuildVersion") or "") \
            or (directory.name if directory != root else "")
        supported = tuple(str(item) for item in info.get("SupportedProductTypes") or ())
        found.append(DdiImage(build=build or directory.name, image=image, manifest=manifest,
                              trustcache=_trustcache_of(directory),
                              supported=supported, digest=_file_digest(image),
                              version=directory.name if directory != root else ""))
    found.sort(key=lambda item: (len(item.supported), _freshness(item)), reverse=True)
    return found


def usable_images(product_type: Optional[str] = None,
                  identifiers: Optional[Mapping[str, Any]] = None,
                  version: Optional[str] = None) -> list[DdiImage]:
    '**All** images usable for this device, ordered by preference.'
    items = [item for item in list_images()
             if same_release(item.version, version)
             and item.supports(product_type)
             and (item.identity_for(identifiers) is not None if identifiers else True)
             and (item.fits(identifiers) if identifiers else True)]
    items.sort(key=lambda item: (item.version == version, len(item.supported),
                                  _freshness(item)), reverse=True)
    return items


def pick_image(product_type: Optional[str] = None,
               identifiers: Optional[Mapping[str, Any]] = None,
               version: Optional[str] = None) -> Optional[DdiImage]:
    'Pick the image that fits the device.'
    items = usable_images(product_type, identifiers, version)
    return items[0] if items else None


def digest_conflicts(product_type: Optional[str] = None,
                     identifiers: Optional[Mapping[str, Any]] = None,
                     version: Optional[str] = None
                     ) -> list[tuple[DdiImage, bytes]]:
    'Cached images that cover this device but carry a payload the manifest does not declare for it.'
    conflicts: list[tuple[DdiImage, bytes]] = []
    for item in list_images():
        if not same_release(item.version, version):
            continue
        if not item.supports(product_type):
            continue
        expected = item.declared_digest(identifiers or {})
        if expected is not None and expected != item.digest:
            conflicts.append((item, expected))
    return conflicts


def major(version: Optional[str]) -> Optional[int]:
    'The iOS major version of a version string ("27.0" -> 27).'
    if not version:
        return None
    head = str(version).split(".")[0].strip()
    return int(head) if head.isdigit() else None


def release_of(version: Optional[str]) -> Optional[str]:
    'The iOS release of a version string ("27.1" -> "27").'
    number = major(version)
    return str(number) if number is not None else None


def same_release(image_version: Optional[str], device_version: Optional[str]) -> bool:
    'Whether an image of that iOS release fits a device running that version.'
    if not image_version or not device_version:
        return True
    if image_version == device_version:
        return True
    left, right = major(image_version), major(device_version)
    if left is None or right is None:
        return True
    return left == right


def check_dmg_matches(body: bytes, manifest_path: Optional[Path],
                       identifiers: Optional[Mapping[str, Any]] = None) -> None:
    'Whether the payload is the one the manifest declares **for this device**.'
    if manifest_path is None:
        return
    info = load_manifest(Path(manifest_path))
    declared: list[bytes] = []
    for identity in info.get("BuildIdentities") or []:
        entry = (identity.get("Manifest") or {}).get("PersonalizedDMG") or {}
        digest = entry.get("Digest")
        if digest:
            declared.append(bytes(digest))
    if not declared:
        return
    actual = hashlib.sha384(body).digest()
    expected = declared_digest(info, identifiers)
    if expected is None:
        if any(actual == digest for digest in declared):
            return
        raise ServiceError(
            f"the image payload and BuildManifest do not match ({Path(manifest_path)}):\n"
            f"  manifest digests      {', '.join(sorted({d.hex()[:24] for d in declared}))}\n"
            f"  local dmg digest      {actual.hex()[:24]}...\n"
            "  the payload, the manifest and the trust cache must come from the same build.")
    if expected != actual:
        raise ServiceError(
            "the image payload and BuildManifest do not match for this device:\n"
            f"  digest this device needs  {expected.hex()[:24]}...\n"
            f"  digest of the payload    {actual.hex()[:24]}...\n"
            "  this device needs another payload of the same build.")


def no_image_text(product_type: Optional[str],
                  identifiers: Optional[Mapping[str, Any]],
                  version: Optional[str] = None) -> str:
    'What to say when nothing fits: what the cache has and what is missing.'
    known = list_images()
    lines = [f"no cached image works for this device (product {product_type or 'unknown'}"
             + (f", board/chip {identifiers.get('BoardId')}/{identifiers.get('ChipID')}"
                if identifiers else "")
             + (f", iOS {version}" if version else "")
             + '): ']
    if known:
        for item in known:
            lines.append(f"  · {item.build}  iOS {item.version or '?'}  "
                         f"{_why_not(item, product_type, identifiers, version)}")
    else:
        lines.append(f"  {cache_dir()} has neither;")
    for item, expected in digest_conflicts(product_type, identifiers, version):
        lines.append(f"  · {item.build} - covers this device, but the cached payload is not "
                     f"the one it declares (this device needs {expected.hex()[:16]}..., "
                     f"the cache has {item.digest.hex()[:16]}...)")
    lines.append('  The **three pieces** it needs (same directory, any name):')
    lines.append('    Image.dmg            image payload (one image covers a family of models)')
    lines.append('    BuildManifest.plist  manifest for signing (picks the BuildIdentity and digests)')
    lines.append('    *.trustcache         trust cache for the image')
    lines.append('  Copy them from a Mac with Xcode (the build must match the device):')
    lines.append("    /Library/Developer/CoreDevice/CandidateDDIs/iOS/")
    lines.append("    /Library/Developer/DeveloperDiskImages/iOS_DDI/")
    return "\n".join(lines)


def _why_not(item: DdiImage, product_type: Optional[str],
             identifiers: Optional[Mapping[str, Any]], version: Optional[str]) -> str:
    'Why that image does not fit the device.'
    if not same_release(item.version, version):
        return f"the device runs iOS {version}, this image is for iOS {item.version or '?'}"
    if not item.supports(product_type):
        return 'does not support this model'
    if identifiers and item.identity_for(identifiers) is None:
        return 'no identity for this device in its manifest'
    if identifiers and not item.fits(identifiers):
        return 'the cached payload is not the one it declares for this device'
    return 'usable'


def declared_digest(info: Mapping[str, Any],
                     identifiers: Optional[Mapping[str, Any]]) -> Optional[bytes]:
    'The payload digest the manifest declares for the device with that identity.'
    if not identifiers:
        return None
    from .. import tss

    try:
        identity = tss.pick_build_identity(info, identifiers)
    except OpenDeviceError:
        return None
    entry = (identity.get("Manifest") or {}).get("PersonalizedDMG") or {}
    digest = entry.get("Digest")
    return bytes(digest) if digest else None


def load_manifest(path: Path) -> dict[str, Any]:
    'Parsed BuildManifest, cached by path + mtime + size.'
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _MANIFESTS.get(key)
    if cached is None:
        try:
            cached = plistlib.loads(path.read_bytes())
        except Exception:                        # noqa
            cached = {}
        _MANIFESTS[key] = cached
    return cached


def _file_digest(path: Path) -> bytes:
    'sha384 of a file, cached by path + mtime + size.'
    try:
        stat = path.stat()
    except OSError:
        return b""
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _DIGESTS.get(key)
    if cached is None:
        try:
            cached = hashlib.sha384(path.read_bytes()).digest()
        except OSError:
            cached = b""
        _DIGESTS[key] = cached
    return cached


def _trustcache_of(directory: Path) -> Optional[Path]:
    'The trust cache in that directory.'
    others = sorted(path for path in directory.glob("*.trustcache") if path.is_file())
    return others[0] if others else None


def _cached_file(pattern: str, product_type: Optional[str],
                 version: Optional[str]) -> Optional[Path]:
    for directory in (cache_dir(product_type, version) if product_type and version else None,
                      cache_dir(product_type) if product_type else None,
                      cache_dir()):
        if directory is None or not directory.is_dir():
            continue
        for path in sorted(directory.rglob(pattern)):
            if path.is_file():
                return path
    return None


def _freshness(item: DdiImage) -> float:
    'When this image entered the cache (mtime of the manifest).'
    try:
        return item.manifest.stat().st_mtime
    except OSError:                              # noqa: BLE001
        return 0.0
