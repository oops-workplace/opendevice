from __future__ import annotations

import hashlib
import plistlib
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Union

from ...transport.service import open_service
from ..base import Service
from ...core import plistio
from ...core.errors import NotSupportedError, OpenDeviceError, ServiceError
from .image import DdiImage, cache_dir, cached_manifest, cached_trustcache, check_dmg_matches, \
    no_image_text, usable_images

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
              product_type: Optional[str] = None, version: Optional[str] = None,
              ecid: Optional[int] = None,
              on_status: Optional[Callable[[str], None]] = None) -> str:
        '''Mount a personalized image: **sign -> push (ReceiveBytes) -> mount (MountImage)**; returns the mount path.
        '''
        mounted = self.mounted_path(image_type)
        if mounted:
            raise ServiceError(f"image already mounted ({mounted}), no need to mount again")
        if self.developer_mode_status() is False:
            raise ServiceError(_explain_mount_error(_DEV_MODE_OFF))

        identifiers = _quiet(self.personalization_identifiers, PERSONALIZED_DDI)
        if image is not None:
            body = image if isinstance(image, bytes) else Path(image).read_bytes()
            return self._mount_one(body, None, manifest, image_type, extras,
                                   product_type, ecid, on_status, identifiers)

        chosen = usable_images(product_type, identifiers, version)
        if not chosen:
            raise FileNotFoundError(no_image_text(product_type, identifiers, version))
        failures: list[str] = []
        for item in chosen:
            _say(on_status, f"using the cached image: {item.build}")
            try:
                return self._mount_one(item.image.read_bytes(), item, manifest, image_type,
                                       extras, product_type, ecid, on_status,
                                       identifiers)
            except (ServiceError, OpenDeviceError) as exc:
                if not _retryable(str(exc)):
                    raise
                failures.append(f"{item.build}: {exc}")
                _say(on_status, f"this one ({item.build}) failed to mount, trying the next one...")
        raise ServiceError('none of the images mounted:\n' + "\n  ".join(failures))

    def _mount_one(self, body: bytes, item: Optional[DdiImage], manifest: Optional[bytes],
                   image_type: str, extras: Optional[dict[str, Any]],
                   product_type: Optional[str],
                   ecid: Optional[int], on_status: Optional[Callable[[str], None]],
                   identifiers: Optional[Mapping[str, Any]] = None) -> str:
        'Run one image end to end: sign -> push -> mount.'
        if manifest is None:
            _say(on_status, 'asking the device for the personalization signature (IM4M)...')
            manifest = self.personalization_manifest(
                PERSONALIZED_DDI, hashlib.sha384(body).digest())
        if not manifest:
            check_dmg_matches(body, item.manifest if item else
                              cached_manifest(product_type), identifiers)
            self._replace_conn()
            manifest = self._sign_via_tss(
                item.manifest if item else None, identifiers, product_type, ecid,
                on_status)

        _say(on_status, f"pushing the image ({len(body) // 1024} KB)...")
        self.upload_image(body, manifest, image_type)
        custom: dict[str, Any] = dict(extras or {})
        trustcache = item.trustcache if item else cached_trustcache(product_type)
        if trustcache is not None:
            custom.setdefault("ImageTrustCache", Path(trustcache).read_bytes())
        _say(on_status, 'mounting...')
        self.mount_image(manifest, image_type, custom)
        return self.mounted_path(image_type) or ' (the device reported no mount path; confirm with mounter list)'

    def _sign_via_tss(self, manifest_path: Optional[Path],
                      identifiers: Optional[Mapping[str, Any]],
                      product_type: Optional[str], ecid: Optional[int],
                      on_status: Optional[Callable[[str], None]]) -> bytes:
        'When the device has no cached signature, trade the nonce for an IM4M from Apple TSS.'
        from .. import tss

        manifest_path = manifest_path or cached_manifest(product_type)
        if manifest_path is None:
            raise FileNotFoundError(
                f"the device has no cached signature, and {cache_dir(product_type)} "
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
        'Unmount the image (``UnmountImage`` + ``MountPath``); returns the unmounted path.'
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
                    "    rebooting with developer mode on makes the system mount the "
                    "cryptex DDI again.\n"
                    "  - To turn the mode back on: `opendevice devmode enable`"
                ) from None
            if "no matching entry" in text.lower() or "not mounted" in text.lower():
                raise ServiceError(f"{path} has no matching image entry (it may already be unmounted)") from None
            raise
        return path


def connect(lockdown: Any = None, rsd: Any = None) -> MobileImageMounter:
    'Open a mobile_image_mounter over the available link.'
    return MobileImageMounter.open(lockdown, rsd)


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


def _quiet(call: Callable[..., Any], *args: Any) -> Optional[Any]:
    'Call it and treat a raise as nothing.'
    try:
        return call(*args)
    except Exception:                        # noqa: BLE001
        return None


_RETRY_MARKERS = ("isn't eligible", "not eligible", 'MountImage returned ', "eligible",
                  "do not match")


def _retryable(text: str) -> bool:
    'Whether a failed mount should be retried with the next image.'
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _RETRY_MARKERS)
