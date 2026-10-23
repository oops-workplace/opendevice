from __future__ import annotations

from typing import Any, Callable, Optional

from ...core import log as log_module
from .image import digest_conflicts, release_of
from .service import PERSONALIZED_DDI, MobileImageMounter
from .source import ensure_image


def resolve_core_device_version(lockdown: Any = None, rsd: Any = None,
                                default: Optional[str] = None) -> str:
    'CoreDevice version of this device: read from its **mounted DDI entry**, falling back to a constant.'
    if default is None:
        from ...remote.coredevice import DEFAULT_CORE_DEVICE_VERSION

        default = DEFAULT_CORE_DEVICE_VERSION
    version: str = default
    if rsd is not None:
        cached = getattr(rsd, "core_device_version", None)
        if cached:
            return str(cached)
    reachable = lockdown is not None or "com.apple.mobile.mobile_image_mounter.shim.remote" in \
        (getattr(rsd, "services", None) or {})
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
    'Why a DDI-backed service is missing and how to fix it (feeds :attr:`RSD.ddi_probe`).'
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
            version = release_of(version_of(lockdown, rsd))
            identifiers = _quiet(mounter.personalization_identifiers, PERSONALIZED_DDI)
            if not ensure_image(product_type, identifiers, version, on_status=on_status):
                log.info('DDI not mounted and no usable image for %s; skipping the automatic mount',
                         product_type or '?')
                for item, expected in digest_conflicts(product_type, identifiers, version):
                    _say(on_status, f"the cached image {item.build} is not the payload this "
                                    f"device needs (it needs {expected.hex()[:16]}..., the cache "
                                    f"has {item.digest.hex()[:16]}...)")
                return None
            _say(on_status, 'image not mounted (the device does not always remount after a reboot), mounting...')
            log.info('DDI not mounted, mounting automatically (product %s)', product_type or "?")
            return mounter.mount(product_type=product_type, version=version,
                                 ecid=_ecid_of(lockdown, identifiers), on_status=on_status)
    except Exception as exc:                    # noqa
        log.debug('automatic DDI mount failed: %s', exc)
        return None


def version_of(lockdown: Any = None, rsd: Any = None) -> Optional[str]:
    'The iOS version the device runs (e.g. ``27.0``).'
    if rsd is not None:
        value = getattr(rsd, "product_version", None)
        if value:
            return str(value)
    if lockdown is not None:
        try:
            value = lockdown.all_values().get("ProductVersion")
            if value:
                return str(value)
        except Exception:                        # noqa: BLE001
            pass
    return None


def _ecid_of(lockdown: Any, identifiers: Optional[dict[str, Any]]) -> Optional[int]:
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


def _quiet(call: Callable[..., Any], *args: Any) -> Optional[Any]:
    'Call it and treat a raise as nothing.'
    try:
        return call(*args)
    except Exception:                        # noqa: BLE001
        return None


def _say(callback: Optional[Callable[[str], None]], text: str) -> None:
    if callback is not None:
        callback(text)
