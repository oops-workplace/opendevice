from __future__ import annotations

import os
import posixpath
from typing import Any, Callable, Optional

from ..core.errors import InstallError, ServiceError
from ..transport.service import ServiceConnection, open_service
from . import afc as afc_module
from .base import Service

SERVICE = "com.apple.mobile.installation_proxy"
SERVICE_RSD = "com.apple.mobile.installation_proxy.shim.remote"

STAGING_DIR = "/PublicStaging"

APP_ATTRIBUTES = [
    "CFBundleIdentifier", "CFBundleDisplayName", "CFBundleName",
    "CFBundleShortVersionString", "CFBundleVersion", "ApplicationType",
    "CFBundleExecutable", "Path", "Container",
]


class InstallationProxy(Service):
    'Install / uninstall / list apps (synchronous).'

    NAME = SERVICE
    RSD_NAME = SERVICE_RSD

    def __init__(self, conn: ServiceConnection, afc: Optional[afc_module.Afc] = None,
                 lockdown: Any = None, rsd: Any = None) -> None:
        super().__init__(conn)
        self._afc = afc
        self._lockdown = lockdown
        self._rsd = rsd

    @classmethod
    def open(cls, lockdown: Any = None, rsd: Any = None, afc: Optional[afc_module.Afc] = None,
             ) -> "InstallationProxy":
        return cls(open_service(lockdown, rsd, cls.NAME, cls.RSD_NAME), afc, lockdown, rsd)

    def with_afc(self, afc: afc_module.Afc) -> "InstallationProxy":
        self._afc = afc
        return self

    def _staging(self) -> afc_module.Afc:
        'AFC used as the upload staging channel (opens one over the same link when none is given).'
        if self._afc is None:
            if self._lockdown is None and self._rsd is None:
                raise ServiceError('installing needs AFC as a staging channel; pass one via with_afc()')
            self._afc = afc_module.Afc.open(self._lockdown, self._rsd)
        return self._afc

    def close(self) -> None:
        super().close()
        if self._afc is not None:
            self._afc.close()

    def install(self, path: str, options: Optional[dict[str, Any]] = None,
                on_progress: Optional[Callable[[int, str], None]] = None,
                developer: bool = False, upgrade: bool = False) -> None:
        'Install a local ipa (or .app directory) on the device.'
        target = self._upload(path, on_progress)
        merged = dict(options or {})
        if developer:
            merged.setdefault("PackageType", "Developer")
        try:
            self._run("Upgrade" if upgrade else "Install", {
                "PackagePath": target,
                "ClientOptions": merged,
            }, on_progress)
        finally:
            try:
                self._staging().remove(target)
            except Exception:                    # noqa
                pass

    def uninstall(self, bundle_id: str,
                  on_progress: Optional[Callable[[int, str], None]] = None) -> None:
        self._run("Uninstall", {"ApplicationIdentifier": bundle_id}, on_progress)

    def _upload(self, path: str, on_progress: Optional[Callable[[int, str], None]],
                ) -> str:
        'Upload a package into ``/PublicStaging`` and return its device path.'
        staging = self._staging()
        name = os.path.basename(path.rstrip("/\\")) or "package.ipa"
        remote = posixpath.join(STAGING_DIR, name)
        if not staging.exists(STAGING_DIR):
            staging.mkdir(STAGING_DIR)
        total = _local_size(path)
        if on_progress is not None:
            on_progress(0, f"uploading {name}")

        if os.path.isdir(path):
            staging.mkdir_p(remote)
            for current, _dirs, files in os.walk(path):
                rel = os.path.relpath(current, path)
                base = remote if rel == "." else posixpath.join(remote, *_parts(rel))
                staging.mkdir_p(base)
                for filename in files:
                    staging.push(os.path.join(current, filename),
                                 posixpath.join(base, filename))
            return remote

        with open(path, "rb") as handle:
            def report(written: int) -> None:
                if on_progress is not None and total:
                    on_progress(int(written * 100 / total), f"uploading {name}")

            staging.stream_from(remote, handle, on_progress=report)
        return remote

    def _run(self, command: str, payload: dict[str, Any],
             on_progress: Optional[Callable[[int, str], None]] = None) -> None:
        'Send one command and read until ``Complete``.'
        payload = dict(payload)
        payload["Command"] = command
        self.conn.send(payload)
        while True:
            response = self.conn.recv()
            if not isinstance(response, dict):
                raise InstallError(f"{command} got an unexpected response: {response!r}")
            if response.get("Error"):
                raise InstallError(f"{command} failed: {response['Error']}")
            status = str(response.get("Status") or "")
            if on_progress is not None:
                on_progress(int(response.get("PercentComplete") or 0), status)
            if status == "Complete":
                return
            if not status:
                raise InstallError(f"{command} returned no status: {response!r}")

    def apps(self, attributes: Optional[list[str]] = None,
             app_type: str = "Any") -> list[dict[str, Any]]:
        'List installed apps (``Browse``, accumulating ``CurrentList``).'
        self.conn.send({
            "Command": "Browse",
            "ClientOptions": {
                "ApplicationType": app_type,
                "ReturnAttributes": list(attributes or APP_ATTRIBUTES),
            },
        })
        found: list[dict[str, Any]] = []
        while True:
            response = self.conn.recv()
            if not isinstance(response, dict):
                raise InstallError(f"Browse got an unexpected response: {response!r}")
            if response.get("Error"):
                raise InstallError(f"Browse failed: {response['Error']}")
            found.extend(response.get("CurrentList") or [])
            if str(response.get("Status") or "") == "Complete":
                return found

    def lookup(self, bundle_ids: Optional[list[str]] = None,
               attributes: Optional[list[str]] = None) -> dict[str, Any]:
        'Look up specific bundles (all of them when no bundle_ids are given).'
        options: dict[str, Any] = {
            "ApplicationType": "Any",
            "ReturnAttributes": list(attributes or APP_ATTRIBUTES),
        }
        if bundle_ids:
            options["BundleIDs"] = list(bundle_ids)
        response = self.conn.request({"Command": "Lookup", "ClientOptions": options})
        if not isinstance(response, dict) or response.get("Error"):
            raise InstallError(f"Lookup failed: {response!r}")
        return response.get("LookupResult") or {}


def executable_path(app: dict) -> str:
    'Installed app entry -> its executable path (``Path`` + ``CFBundleExecutable``).'
    path = str(app.get("Path") or "")
    name = str(app.get("CFBundleExecutable") or "")
    return f"{path.rstrip('/')}/{name}" if path and name else ""


def _local_size(path: str) -> int:
    if not os.path.isdir(path):
        return os.path.getsize(path)
    return sum(os.path.getsize(os.path.join(current, name))
               for current, _dirs, files in os.walk(path) for name in files)


def _parts(rel: str) -> list[str]:
    return [part for part in rel.replace("\\", "/").split("/") if part]


def connect(lockdown: Any = None, rsd: Any = None, afc: Optional[afc_module.Afc] = None,
            ) -> InstallationProxy:
    'Open an installation_proxy over the available link.'
    return InstallationProxy.open(lockdown, rsd, afc)
