from __future__ import annotations

import hashlib
import json
import os
import plistlib
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Union

from ..core import log as log_module
from ..core import paths, plistio
from ..core.errors import NotSupportedError, OpenDeviceError, ServiceError
from ..transport.service import open_service
from .base import Service

SERVICE = "com.apple.mobile.mobile_image_mounter"
SERVICE_RSD = "com.apple.mobile.mobile_image_mounter.shim.remote"

IMAGE_DEVELOPER = "Developer"
IMAGE_PERSONALIZED = "Personalized"

PERSONALIZED_DDI = "DeveloperDiskImage"

MOUNT_PATH_DEVELOPER = "/Developer"
MOUNT_PATH_PERSONALIZED = "/System/Developer"

_UNLOAD_JOBS = "Failed to unload launchd jobs"
_DEV_MODE_OFF = "Developer mode is not enabled"
_NO_CACHED_MANIFEST = "Failed to find a cached manifest"
_STALE_MANIFEST = "Existing manifest"

UPLOAD_CHUNK = 1 << 20

DDI_FILES = ("Image.dmg", "BuildManifest.plist", "Image.dmg.trustcache")

DDI_URL_ENV = "OPENDEVICE_DDI_URL"
DDI_TOKEN_ENV = "OPENDEVICE_DDI_TOKEN"

DEFAULT_DDI_URL = "https://raw.githubusercontent.com/oops-workplace/opendevice-ddi/main"
DEFAULT_DDI_BUILD = "27A266a"
DDI_INDEX_FILE = "index.json"


def resolve_core_device_version(lockdown: Any = None, rsd: Any = None,
                                default: Optional[str] = None) -> str:
    'CoreDevice version of this device: read from its **mounted DDI entry**, falling back to a constant.'
    if default is None:
        from ..remote.coredevice import DEFAULT_CORE_DEVICE_VERSION

        default = DEFAULT_CORE_DEVICE_VERSION
    version: str = default
    if rsd is not None:
        cached = getattr(rsd, "core_device_version", None)
        if cached:
            return str(cached)
    reachable = lockdown is not None or SERVICE_RSD in (getattr(rsd, "services", None) or {})
    if reachable:
        try:
            with MobileImageMounter.open(lockdown, rsd) as client:
                version = client.core_device_version() or default
        except Exception as exc:                # noqa
            log_module.get(__name__).debug('failed to read the CoreDevice version (using %s): %s', default, exc)
    if rsd is not None:
        rsd.core_device_version = version
    return version


def ddi_state(lockdown: Any = None, rsd: Any = None) -> str:
    'Two lines explaining why a DDI-backed service is missing and how to fix it (feeds :attr:`RSD.ddi_probe`).'
    try:
        with MobileImageMounter.open(lockdown, rsd) as mounter:
            mode = mounter.developer_mode_status()
            mounted = mounter.mounted_path()
    except Exception:                           # noqa
        return ""
    mode_text = {True: 'developer mode on', False: 'developer mode off',
                 None: 'developer mode unknown'}[mode]
    if not mounted:
        if mode is False:
            return ('  image not mounted and developer mode off - this kind of service needs both.\n')
        return ('  image not mounted (' + mode_text + ') - mirroring / touch / keyboard / pasteboard / screenshots all live in the image.\n')
    if mode is False:
        return (f"  image mounted at {mounted}, but developer mode is off - the AX tree / "
                "Instruments are only published with it on.\n")
    return (f"  image mounted at {mounted}, {mode_text} - everything is in place, "
            "so the device / OS may really not support this service.")


def ensure_mounted(lockdown: Any = None, rsd: Any = None,
                   on_status: Optional[Callable[[str], None]] = None) -> Optional[str]:
    'Mount the personalized DDI once the link is up.'
    log = log_module.get(__name__)
    try:
        with MobileImageMounter.open(lockdown, rsd) as mounter:
            mounted = mounter.mounted_path()
            if mounted:
                return mounted
            if mounter.developer_mode_status() is False:
                log.debug('DDI not mounted and developer mode off; skipping the automatic mount')
                return None
            product_type = getattr(rsd, "product_type", None) or None
            identifiers = _quiet(mounter.personalization_identifiers, PERSONALIZED_DDI)
            if not ensure_image(product_type, identifiers, on_status=on_status):
                log.info('DDI not mounted and no usable image for %s; skipping the automatic mount',
                         product_type or '?')
                return None
            _say(on_status, 'image not mounted (the device does not always remount after a reboot), mounting...')
            log.info('DDI not mounted, mounting automatically (product %s)', product_type or "?")
            return mounter.mount(product_type=product_type,
                                 build=getattr(rsd, "build_version", None),
                                 ecid=_ecid_of(lockdown, identifiers), on_status=on_status)
    except Exception as exc:                    # noqa
        log.debug('automatic DDI mount failed: %s', exc)
        return None


def _ecid_of(lockdown: Any, identifiers: Optional[Mapping[str, Any]]) -> Optional[int]:
    "Device ECID (needed for TSS tickets): lockdown's ``UniqueChipID`` first, then the personalization identity."
    if lockdown is not None:
        try:
            value = lockdown.all_values().get("UniqueChipID")
            if value is not None:
                return int(value)
        except Exception:                        # noqa: BLE001
            pass
    try:
        return int((identifiers or {}).get("UniqueChipID") or 0) or None
    except (TypeError, ValueError):
        return None


def cache_dir(product_type: Optional[str] = None, build: Optional[str] = None) -> Path:
    'Image cache directory: ``<cache root>/ddi``, optionally split by product type / OS (see ``opendevice paths``).'
    return paths.ddi_dir(product_type, build)


def cached_image(product_type: Optional[str] = None,
                 build: Optional[str] = None) -> Optional[Path]:
    'Image payload already in the cache (``.dmg``).'
    for directory in (cache_dir(product_type, build) if product_type and build else None,
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
                    build: Optional[str] = None) -> Optional[Path]:
    '``BuildManifest.plist`` in the cache (needed for TSS tickets).'
    return _cached_file("BuildManifest.plist", product_type, build)


def cached_trustcache(product_type: Optional[str] = None,
                      build: Optional[str] = None) -> Optional[Path]:
    'Trust cache in the cache (``*.trustcache``, passed to MountImage together with the signature).'
    return _cached_file("*.trustcache", product_type, build)


@dataclass(frozen=True)
class DdiImage:
    '**One** personalized DDI (payload + manifest + trust cache).'

    build: str
    image: Path
    manifest: Path
    trustcache: Optional[Path] = None
    supported: tuple[str, ...] = ()

    def supports(self, product_type: Optional[str]) -> bool:
        'Whether this image supports the given product type (no support list means no filtering).'
        if not product_type or not self.supported:
            return True
        return product_type in self.supported

    def identity_for(self, identifiers: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        'Pick a BuildIdentity from this manifest by device identity.'
        from . import tss

        try:
            manifest = plistlib.loads(self.manifest.read_bytes())
        except Exception:                        # noqa
            return None
        try:
            return tss.pick_build_identity(manifest, identifiers)
        except OpenDeviceError:
            return None


def list_images() -> list[DdiImage]:
    'All DDIs in the image cache directory.'
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
        supported: tuple[str, ...] = ()
        build = directory.name if directory != root else ""
        try:
            info = plistlib.loads(manifest.read_bytes())
            build = str(info.get("ProductBuildVersion") or build) or directory.name
            supported = tuple(str(item) for item in info.get("SupportedProductTypes") or ())
        except Exception:                        # noqa
            pass
        trustcache = next(iter(sorted(directory.glob("*.trustcache"))), None)
        found.append(DdiImage(build=build, image=image, manifest=manifest,
                              trustcache=trustcache, supported=supported))
    found.sort(key=lambda item: (len(item.supported), _freshness(item)), reverse=True)
    return found


def _freshness(item: DdiImage) -> float:
    'When this image entered the cache (mtime of the manifest).'
    try:
        return item.manifest.stat().st_mtime
    except OSError:                              # noqa: BLE001
        return 0.0


def pick_image(product_type: Optional[str] = None,
               identifiers: Optional[Mapping[str, Any]] = None) -> Optional[DdiImage]:
    'Pick one for the device: **product type in the support list** + **a matching board/chip identity**.'
    for item in list_images():
        if not item.supports(product_type):
            continue
        if identifiers and item.identity_for(identifiers) is None:
            continue
        return item
    return None


def usable_images(product_type: Optional[str] = None,
                  identifiers: Optional[Mapping[str, Any]] = None,
                  build: Optional[str] = None) -> list[DdiImage]:
    '**All** images usable for this device, ordered by preference (only that build when ``build`` is given).'
    items = list_images()
    if build:
        items = [item for item in items if item.build == build]
    return [item for item in items
            if item.supports(product_type)
            and (item.identity_for(identifiers) is not None if identifiers else True)]


def source_base(base: Optional[str] = None) -> str:
    'Image source: explicit argument, then ``OPENDEVICE_DDI_URL``, then the project mirror.'
    return base or os.environ.get(DDI_URL_ENV, "").strip() or DEFAULT_DDI_URL


def remote_builds(base: Optional[str] = None, token: Optional[str] = None) -> list[str]:
    'Build names the source advertises in ``index.json``, newest first.'
    try:
        info = json.loads(_read_index(source_base(base), token) or b"{}")
    except ValueError:
        info = {}
    builds = [str(item) for item in info.get("builds") or ()]
    if not builds and info.get("latest"):
        builds = [str(info["latest"])]
    return builds or [DEFAULT_DDI_BUILD]


def _read_index(base: str, token: Optional[str]) -> bytes:
    if urllib.parse.urlparse(base).scheme in ("http", "https"):
        return _http_get(f"{base.rstrip('/')}/{DDI_INDEX_FILE}", token) or b""
    path = Path(base) / DDI_INDEX_FILE
    return path.read_bytes() if path.is_file() else b""


def ensure_image(product_type: Optional[str] = None,
                 identifiers: Optional[Mapping[str, Any]] = None,
                 build: Optional[str] = None, base: Optional[str] = None,
                 token: Optional[str] = None,
                 on_status: Optional[Callable[[str], None]] = None) -> list[DdiImage]:
    'Images usable for this device; downloads from the source when the cache has none.'
    items = usable_images(product_type, identifiers, build)
    if items:
        return items
    base = source_base(base)
    for name in ([build] if build else remote_builds(base, token)):
        _say(on_status, f"no cached image, fetching {name} from {base}...")
        try:
            fetch_image(name, base=base, token=token, on_status=on_status)
        except Exception as exc:                # noqa: BLE001 - a bad build must not stop the next one
            log_module.get(__name__).debug("fetching %s from %s failed: %s", name, base, exc)
            continue
        items = usable_images(product_type, identifiers, build)
        if items:
            return items
    return []


def _cached_file(pattern: str, product_type: Optional[str],
                 build: Optional[str]) -> Optional[Path]:
    for directory in (cache_dir(product_type, build) if product_type and build else None,
                      cache_dir(product_type) if product_type else None,
                      cache_dir()):
        if directory is None or not directory.is_dir():
            continue
        for path in sorted(directory.rglob(pattern)):
            if path.is_file():
                return path
    return None


class MobileImageMounter(Service):
    'Disk image mount service (synchronous).'

    NAME = SERVICE
    RSD_NAME = SERVICE_RSD

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request(payload)

    def _replace_conn(self) -> None:
        'Swap the persistent connection for a **fresh** one.'
        if self.lockdown is None and self.rsd is None:
            return
        old, self.conn = self.conn, None
        if old is not None:
            old.close()
        self.conn = open_service(self.lockdown, self.rsd, self.NAME, self.RSD_NAME)

    def devices(self) -> list[dict[str, Any]]:
        'Image entries the device knows about (``CopyDevices``).'
        return list(self._request({"Command": "CopyDevices"}).get("EntryList") or [])

    def lookup(self, image_type: str = IMAGE_DEVELOPER) -> Optional[bytes]:
        'Fetch the signature for one image type.'
        response = self._request({"Command": "LookupImage", "ImageType": image_type})
        if not response.get("ImagePresent", True):
            return None
        signature = response.get("ImageSignature") or None
        if isinstance(signature, list):
            return bytes(signature[0]) if signature else None
        return bytes(signature) if signature else None

    def is_mounted(self, image_type: str = IMAGE_DEVELOPER) -> bool:
        try:
            return bool(self.lookup(image_type))
        except OpenDeviceError:
            return False

    def developer_mode_status(self) -> Optional[bool]:
        'Whether developer mode is enabled.'
        response = self._request({"Command": "QueryDeveloperModeStatus"})
        if not isinstance(response, dict):
            return None
        status = response.get("DeveloperModeStatus")
        return None if status is None else bool(status)

    def nonce(self, personalized_type: Optional[str] = None) -> bytes:
        'Device nonce used by personalized images.'
        payload: dict[str, Any] = {"Command": "QueryNonce"}
        if personalized_type:
            payload["PersonalizedImageType"] = personalized_type
        response = self._request(payload)
        if "PersonalizationNonce" not in response:
            raise NotSupportedError('the device does not support QueryNonce')
        return bytes(response["PersonalizationNonce"])

    def mounted_path(self, image_type: str = IMAGE_PERSONALIZED) -> Optional[str]:
        'Where the currently mounted image lives.'
        for entry in self.devices():
            if str(entry.get("DiskImageType") or "") != image_type:
                continue
            if entry.get("IsMounted") and entry.get("MountPath"):
                return str(entry["MountPath"])
        return None

    def core_device_version(self, image_type: str = IMAGE_PERSONALIZED) -> Optional[str]:
        'Read the CoreDevice version from the mounted image entry (``Project=CoreDevice`` in ``ProjectMetadata``).'
        for entry in self.devices():
            if image_type and str(entry.get("DiskImageType") or "") != image_type:
                continue
            info = entry.get("PersonalizedImageVersionInfo") or {}
            for item in info.get("ProjectMetadata") or []:
                if str(item.get("Project") or "") == "CoreDevice":
                    return str(item.get("Version") or "") or None
            if entry.get("PersonalizedImageVersion"):
                return str(entry["PersonalizedImageVersion"])
        return None

    def personalization_identifiers(self, personalized_type: Optional[str] = None) -> dict[str, Any]:
        'Ask the device for its personalization identity (``QueryPersonalizationIdentifiers``: ``Ap,*`` / BoardId / ChipID).'
        payload: dict[str, Any] = {"Command": "QueryPersonalizationIdentifiers"}
        if personalized_type:
            payload["PersonalizedImageType"] = personalized_type
        response = self._request(payload)
        identifiers = response.get("PersonalizationIdentifiers") or response
        return dict(identifiers) if isinstance(identifiers, dict) else {}

    def personalization_manifest(self, personalized_type: str = PERSONALIZED_DDI,
                                 signature: bytes = b"") -> Optional[bytes]:
        'Ask the device for the IM4M it has **already cached** (``QueryPersonalizationManifest``).'
        try:
            response = self._request({
                "Command": "QueryPersonalizationManifest",
                "PersonalizedImageType": personalized_type,
                "ImageType": personalized_type,
                "ImageSignature": signature,
            })
        except ServiceError as exc:
            if _NO_CACHED_MANIFEST in str(exc) or _STALE_MANIFEST in str(exc):
                return None
            raise
        manifest = response.get("ImageSignature")
        if not manifest:
            return None
        if isinstance(manifest, list):
            return bytes(manifest[0]) if manifest else None
        return bytes(manifest)

    def upload_image(self, image: bytes, signature: bytes,
                     image_type: str = IMAGE_PERSONALIZED) -> None:
        'Push the image payload into the device (``ReceiveBytes``); ``MountImage`` needs it first.'
        conn = self.conn
        if conn is None:
            raise ServiceError('this service has no persistent connection')
        conn.send({"Command": "ReceiveBytes", "ImageType": image_type,
                   "ImageSize": len(image), "ImageSignature": signature})
        ack = conn.recv()
        if not isinstance(ack, dict) or ack.get("Status") != "ReceiveBytesAck":
            raise ServiceError(f"ReceiveBytes refused: {_short(ack)}")
        for start in range(0, len(image), UPLOAD_CHUNK):
            plistio.sendall(conn.sock, image[start:start + UPLOAD_CHUNK])
        done = conn.recv()
        if not isinstance(done, dict) or done.get("Status") != "Complete":
            raise ServiceError(f"not all image bytes were sent: {_short(done)}")

    def mount_image(self, signature: bytes, image_type: str = IMAGE_PERSONALIZED,
                    extras: Optional[dict[str, Any]] = None) -> None:
        'Mount an image that was already pushed (``MountImage``).'
        payload: dict[str, Any] = {"Command": "MountImage",
                                   "ImageType": image_type,
                                   "ImageSignature": signature}
        if extras:
            payload.update(extras)
        try:
            response = self._request(payload)
        except ServiceError as exc:
            raise ServiceError(_explain_mount_error(str(exc))) from None
        if _DEV_MODE_OFF in str(response.get("DetailedError") or ""):
            raise ServiceError(_explain_mount_error(_DEV_MODE_OFF))
        if response.get("Status") != "Complete":
            raise ServiceError(f"MountImage returned {response.get('Status')}: {_short(response)}")

    def mount(self, image: Optional[Union[str, Path, bytes]] = None,
              manifest: Optional[bytes] = None,
              image_type: str = IMAGE_PERSONALIZED,
              extras: Optional[dict[str, Any]] = None,
              product_type: Optional[str] = None, build: Optional[str] = None,
              ddi_build: Optional[str] = None, ecid: Optional[int] = None,
              on_status: Optional[Callable[[str], None]] = None) -> str:
        'Mount a personalized image: **sign -> push (ReceiveBytes) -> mount (MountImage)**; returns the mount path.'
        mounted = self.mounted_path(image_type)
        if mounted:
            raise ServiceError(f"image already mounted ({mounted}), no need to mount again")
        if self.developer_mode_status() is False:
            raise ServiceError(_explain_mount_error(_DEV_MODE_OFF))

        identifiers = _quiet(self.personalization_identifiers, PERSONALIZED_DDI)
        if image is not None:
            body = image if isinstance(image, bytes) else Path(image).read_bytes()
            return self._mount_one(body, None, manifest, image_type, extras,
                                   product_type, build, ecid, on_status)

        chosen = usable_images(product_type, identifiers, ddi_build)
        if not chosen:
            raise FileNotFoundError(_no_image_text(product_type, identifiers))
        failures: list[str] = []
        for item in chosen:
            _say(on_status, f"using the cached image: {item.build}")
            try:
                return self._mount_one(item.image.read_bytes(), item, manifest, image_type,
                                       extras, product_type, build, ecid, on_status,
                                       identifiers)
            except (ServiceError, OpenDeviceError) as exc:
                if not _retryable(str(exc)):
                    raise
                failures.append(f"{item.build}: {exc}")
                _say(on_status, f"this one ({item.build}) failed to mount, trying the next one...")
        raise ServiceError('none of the images mounted:\n' + "\n  ".join(failures))

    def _mount_one(self, body: bytes, item: Optional[DdiImage], manifest: Optional[bytes],
                   image_type: str, extras: Optional[dict[str, Any]],
                   product_type: Optional[str], build: Optional[str],
                   ecid: Optional[int], on_status: Optional[Callable[[str], None]],
                   identifiers: Optional[Mapping[str, Any]] = None) -> str:
        'Run one image end to end: sign -> push -> mount.'
        if manifest is None:
            _say(on_status, 'asking the device for the personalization signature (IM4M)...')
            manifest = self.personalization_manifest(
                PERSONALIZED_DDI, hashlib.sha384(body).digest())
        if not manifest:
            _check_dmg_matches(body, item.manifest if item else
                               cached_manifest(product_type, build))
            self._replace_conn()
            manifest = self._sign_via_tss(
                item.manifest if item else None, identifiers, product_type, build, ecid,
                on_status)

        _say(on_status, f"pushing the image ({len(body) // 1024} KB)...")
        self.upload_image(body, manifest, image_type)
        custom: dict[str, Any] = dict(extras or {})
        trustcache = item.trustcache if item else cached_trustcache(product_type, build)
        if trustcache is not None:
            custom.setdefault("ImageTrustCache", Path(trustcache).read_bytes())
        _say(on_status, 'mounting...')
        self.mount_image(manifest, image_type, custom)
        return self.mounted_path(image_type) or ' (the device reported no mount path; confirm with mounter list)'

    def _sign_via_tss(self, manifest_path: Optional[Path],
                      identifiers: Optional[Mapping[str, Any]],
                      product_type: Optional[str], build: Optional[str],
                      ecid: Optional[int],
                      on_status: Optional[Callable[[str], None]]) -> bytes:
        'When the device has no cached signature, trade the nonce for an IM4M from Apple TSS.'
        from . import tss

        manifest_path = manifest_path or cached_manifest(product_type, build)
        if manifest_path is None:
            raise FileNotFoundError(
                f"the device has no cached signature, and {cache_dir(product_type, build)} "
                "has no BuildManifest.plist.\n"
                "  A personalized image needs three pieces (all in that directory):\n"
                "    - Image.dmg              image payload\n"
                "    - BuildManifest.plist    manifest used for signing\n"
                "    - *.trustcache           trust cache for the image\n"
                "  They are fetched from Apple's TSS service on a Mac with Xcode and signed\n"
                "  for this device; copy the three files into the directory above and retry."
            )

        identifiers = identifiers or self.personalization_identifiers(PERSONALIZED_DDI)
        if not identifiers:
            raise ServiceError('the device did not answer QueryPersonalizationIdentifiers, cannot sign a ticket')
        if ecid is None:
            raise ServiceError('TSS signing needs the device ECID (lockdown UniqueChipID); pass ecid=...')
        _say(on_status, 'asking the device for a nonce...')
        nonce = self.nonce(PERSONALIZED_DDI)

        build_manifest = plistlib.loads(manifest_path.read_bytes())
        identity = tss.pick_build_identity(build_manifest, identifiers)
        _say(on_status, 'requesting a signature from Apple TSS...')
        request = tss.personalized_request(identifiers, identity, nonce, ecid=ecid)
        ticket = tss.img4_ticket(request)
        _say(on_status, f"signature received ({len(ticket)} bytes)")
        return ticket

    def unmount(self, mount_path: Optional[str] = None,
                image_type: str = IMAGE_PERSONALIZED) -> str:
        'Unmount the image (``UnmountImage`` + ``MountPath``, available since iOS 14); returns the unmounted path.'
        path = mount_path or self.mounted_path(image_type)
        if not path:
            raise ServiceError('no image is mounted, nothing to unmount')
        if image_type == IMAGE_PERSONALIZED and path == MOUNT_PATH_DEVELOPER:
            path = MOUNT_PATH_PERSONALIZED
        try:
            self._request({"Command": "UnmountImage", "MountPath": path})
        except ServiceError as exc:
            text = str(exc)
            if _UNLOAD_JOBS in text:
                raise ServiceError(
                    f"cannot unmount {path}: the device says \"{_UNLOAD_JOBS}\".\n"
                    "  - Daemons inside the DDI (dtappserviced / dtremotedisplayd and friends) "
                    "are still running,\n"
                    "    and launchd will not unload them; in practice this fails with "
                    "developer mode on or off.\n"
                    "  - To get rid of the image: **turn developer mode off and reboot** "
                    "(nothing is mounted on boot);\n"
                    "    rebooting with developer mode on makes iOS 27 mount the cryptex DDI "
                    "again.\n"
                    "  - To turn the mode back on: `opendevice devmode enable`"
                ) from None
            if "no matching entry" in text.lower() or "not mounted" in text.lower():
                raise ServiceError(f"{path} has no matching image entry (it may already be unmounted)") from None
            raise
        return path


def _short(value: Any, limit: int = 200) -> str:
    'Shorten a device reply dict into one line for an error message.'
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _explain_mount_error(text: str) -> str:
    "Translate the device's own words on a failed mount into something readable (mode off is the common case)."
    if _DEV_MODE_OFF in text:
        return ('cannot mount: the device says "Developer mode is not enabled".\n  - Enable it in Settings -> Privacy & Security -> Developer Mode (the device reboots),\n    or use `opendevice devmode enable` (request -> wait for reboot -> confirm);\n  - once the image is mounted the RSD service table grows from 62 to 83 entries\n    (the com.apple.coredevice.* ones), which enables mirroring / touch / keyboard /\n    buttons / pasteboard / screenshots.')
    return text


def _say(callback: Optional[Callable[[str], None]], text: str) -> None:
    if callback is not None:
        callback(text)


def fetch_image(build: str, base: Optional[str] = None, token: Optional[str] = None, *,
                on_status: Optional[Callable[[str], None]] = None,
                verify: bool = True) -> DdiImage:
    'Fetch an image into the cache from a source (defaults to the project mirror; never touches the network unless called explicitly).'
    base = source_base(base)
    token = token or os.environ.get(DDI_TOKEN_ENV, "").strip() or None

    target = paths.ensure(cache_dir() / build)
    for name in DDI_FILES:
        _say(on_status, f"fetching {name}…")
        (target / name).write_bytes(_read_source(base, build, name, token))
    if verify:
        _verify_checksums(target, _read_source(base, build, "SHA256SUMS", token,
                                               optional=True), on_status)
    _check_dmg_matches((target / DDI_FILES[0]).read_bytes(), target / DDI_FILES[1])
    for item in list_images():
        if item.image.parent == target:
            _say(on_status, f"installed {item.build} ({len(item.supported)} product types)")
            return item
    raise ServiceError(f"downloaded it but could not recognise it as an image: {target}")


def _read_source(base: str, build: str, name: str, token: Optional[str],
                 optional: bool = False) -> bytes:
    'Read one file from the source: HTTP(S) or a local directory.'
    if urllib.parse.urlparse(base).scheme in ("http", "https"):
        for relative in (f"{build}/{name}", name):
            data = _http_get(f"{base.rstrip('/')}/{relative}", token)
            if data is not None:
                return data
        if optional:
            return b""
        raise OpenDeviceError(
            f"cannot fetch {name} (source {base}): a private source needs {DDI_TOKEN_ENV}, or check whether it is {build}")
    for path in (Path(base) / build / name, Path(base) / name):
        if path.is_file():
            return path.read_bytes()
    if optional:
        return b""
    raise FileNotFoundError(f"the source has no {name}: {Path(base) / build}")


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


def _verify_checksums(target: Path, sums: bytes,
                      on_status: Optional[Callable[[str], None]]) -> None:
    'Verify against ``SHA256SUMS`` from the source (``<hash>  <filename>``); skipped when absent.'
    if not sums:
        _say(on_status, 'no SHA256SUMS in the source, skipping verification')
        return
    checked = 0
    for line in sums.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, name = parts[0], parts[1].lstrip("*")
        path = target / name
        if not path.is_file():
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual.lower() != digest.lower():
            raise ServiceError(f"{name} failed verification (expected {digest}, got {actual})")
        checked += 1
    _say(on_status, f"verified ({checked} files)")


def _quiet(call: Callable[..., Any], *args: Any) -> Optional[Any]:
    'Call it and treat a raise as nothing.'
    try:
        return call(*args)
    except Exception:                        # noqa: BLE001
        return None


def _check_dmg_matches(body: bytes, manifest_path: Optional[Path]) -> None:
    'Whether the local dmg and the BuildManifest belong together.'
    if manifest_path is None:
        return
    try:
        info = plistlib.loads(Path(manifest_path).read_bytes())
    except Exception:                        # noqa
        return
    for identity in info.get("BuildIdentities") or []:
        digest = ((identity.get("Manifest") or {}).get("PersonalizedDMG") or {}).get("Digest")
        if digest is None:
            continue
        actual = hashlib.sha384(body).digest()
        if bytes(digest) != actual:
            raise ServiceError(
                f"the image payload and BuildManifest do not match ({Path(manifest_path)}):\n"
                f"  manifest digest for PersonalizedDMG  {bytes(digest).hex()[:24]}...\n"
                f"  local dmg digest                     {actual.hex()[:24]}...\n"
                "  All three pieces must come from the same build; fetch a matching set "
                "instead of mixing."
            )
        return


_RETRY_MARKERS = ("isn't eligible", "not eligible", 'MountImage returned ', "eligible")


def _retryable(text: str) -> bool:
    'Whether a failed mount should be retried with the next image.'
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _RETRY_MARKERS)


def _no_image_text(product_type: Optional[str],
                   identifiers: Optional[Mapping[str, Any]]) -> str:
    'What to say when nothing fits: what the cache has and what is missing (one image covers a family of models).'
    known = list_images()
    lines = [f"no cached image works for this device (product {product_type or 'unknown'}"
             + (f", board/chip {identifiers.get('BoardId')}/{identifiers.get('ChipID')}"
                if identifiers else "")
             + '): ']
    if known:
        for item in known:
            lines.append(f"  · {item.build} - supports {len(item.supported)} product types (this device: {'yes' if item.supports(product_type) else 'no'})")
    else:
        lines.append(f"  {cache_dir()} has neither;")
    lines.append('  The **three pieces** it needs (same directory, any name):')
    lines.append('    Image.dmg            image payload (one image covers a family of models)')
    lines.append('    BuildManifest.plist  manifest for signing (picks the BuildIdentity and digests)')
    lines.append('    *.trustcache         trust cache for the image')
    lines.append('  Copy them from a Mac with Xcode (the build must match the device):')
    lines.append("    /Library/Developer/CoreDevice/CandidateDDIs/iOS/")
    lines.append("    /Library/Developer/DeveloperDiskImages/iOS_DDI/")
    return "\n".join(lines)


def connect(lockdown: Any = None, rsd: Any = None) -> MobileImageMounter:
    'Open a mobile_image_mounter over the available link.'
    return MobileImageMounter.open(lockdown, rsd)
