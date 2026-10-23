from __future__ import annotations

from typing import Any, Callable, Optional

from ..core.errors import OpenDeviceError
from ..remote.coredevice import SERVICE_APP, SERVICE_SCREEN_CAPTURE, CoreDeviceClient
from ..services import afc as afc_module
from ..services import amfi as amfi_module
from ..services import diagnostics as diag_module
from ..services import installation as installation_module
from ..services import syslog as syslog_module


class ServicesMixin:
    'Device services (part of ``DeviceSession``; do not instantiate alone).'

    def _installed(self) -> installation_module.InstallationProxy:
        'One installation_proxy (the link is picked by lockdown / RSD).'
        return installation_module.InstallationProxy.open(self.lockdown, self.rsd)

    def afc(self) -> afc_module.Afc:
        'Device filesystem (list / pull / push).'
        return afc_module.Afc.open(self.lockdown, self.rsd)

    def apps(self) -> list[dict[str, Any]]:
        'Installed app list (``installation_proxy`` Browse).'
        with self._installed() as proxy:
            return proxy.apps()

    def install(self, path: str, on_progress: Optional[Callable[[int, str], None]] = None,
                developer: bool = False, upgrade: bool = False) -> None:
        'Install an ipa (or an unpacked ``.app`` directory) on the device.'
        proxy = self._installed()
        try:
            proxy.install(path, on_progress=on_progress, developer=developer, upgrade=upgrade)
        finally:
            proxy.close()

    def uninstall(self, bundle_id: str,
                  on_progress: Optional[Callable[[int, str], None]] = None) -> None:
        'Uninstall an app.'
        with self._installed() as proxy:
            proxy.uninstall(bundle_id, on_progress=on_progress)

    def launch(self, bundle_id: str, arguments: Optional[list[str]] = None,
               kill_existing: bool = True) -> Any:
        'Launch an app (CoreDevice appservice).'
        with CoreDeviceClient(self._require_rsd(), SERVICE_APP) as core:
            return core.launch_application(bundle_id, arguments=arguments,
                                           kill_existing=kill_existing)

    def processes(self) -> list[dict[str, Any]]:
        'Current device processes (CoreDevice appservice).'
        with CoreDeviceClient(self._require_rsd(), SERVICE_APP) as core:
            return core.list_processes()

    def kill(self, target: str, signal: int = 9) -> list[int]:
        'Stop an app: ``target`` is a bundle id / executable name / pid; returns the killed pids.'
        with CoreDeviceClient(self._require_rsd(), SERVICE_APP) as core:
            if target.isdigit():
                core.kill_process(int(target), signal)
                return [int(target)]
            found = self._installed().lookup([target]).get(target) or {}
            executable = installation_module.executable_path(found)
            return core.kill_executable(executable or target, signal)

    def screenshot(self, path: Optional[str] = None) -> bytes:
        'Take one static screenshot (PNG).'
        with CoreDeviceClient(self._require_rsd(), SERVICE_SCREEN_CAPTURE) as core:
            image = core.capture_screenshot()
        if path:
            with open(path, "wb") as handle:
                handle.write(image)
        return image

    def diagnostics(self) -> Any:
        'Diagnostics service (info / IORegistry / reboot, shutdown, sleep).'
        return diag_module.DiagnosticsRelay.open(self.lockdown, self.rsd)

    def battery(self) -> dict[str, Any]:
        'Battery info.'
        relay = self.diagnostics()
        try:
            return relay.battery()
        finally:
            relay.close()

    def restart(self) -> None:
        'Reboot the device.'
        self._power("restart")

    def shutdown(self) -> None:
        'Shut down.'
        self._power("shutdown")

    def sleep(self) -> None:
        'Sleep (screen off).'
        self._power("sleep")

    def _power(self, action: str) -> None:
        relay = self.diagnostics()
        try:
            getattr(relay, action)()
        finally:
            relay.close()

    def syslog(self, seconds: Optional[float] = None,
               on_line: Optional[Callable[[str], None]] = None) -> int:
        'Collect the system log (``seconds=None`` means until the link drops); returns the line count.'
        relay = syslog_module.SyslogRelay.open(self.lockdown, self.rsd)
        try:
            return relay.follow(seconds, on_line)
        finally:
            relay.close()

    def developer_mode(self, enable: bool = False, wait: bool = True,
                       on_status: Optional[Callable[[str], None]] = None) -> Optional[bool]:
        'Read / enable developer mode (there is no protocol to disable it; use Settings).'
        if not enable:
            return amfi_module.developer_mode_status(self.lockdown, self.rsd)
        if self.lockdown is None:
            raise OpenDeviceError('enabling developer mode needs USB (lockdown); Wi-Fi links cannot do it')
        amfi = amfi_module.AmfiService(self.lockdown)
        amfi.enable(wait=wait, on_status=on_status)
        self.lockdown = amfi.lockdown
        return True
