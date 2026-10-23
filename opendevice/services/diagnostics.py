from __future__ import annotations

from typing import Any, Optional

from .base import Service

SERVICE = "com.apple.mobile.diagnostics_relay"
SERVICE_RSD = "com.apple.mobile.diagnostics_relay.shim.remote"

INFO_ALL = "All"


class DiagnosticsRelay(Service):
    'Device diagnostics (synchronous).'

    NAME = SERVICE
    RSD_NAME = SERVICE_RSD

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        'Every diagnostics response must carry ``Status: Success``.'
        return self.request(payload, expect_status=True)

    def info(self, kind: str = INFO_ALL) -> dict[str, Any]:
        'Diagnostics info (``kind`` may be ``All`` / ``Battery`` / ``WiFi`` / ``GasGauge`` ...).'
        return self._request({"Request": kind}).get("Diagnostics") or {}

    def mobile_gestalt(self, keys: list[str]) -> dict[str, Any]:
        'Query MobileGestalt keys (device identity / capabilities).'
        response = self._request({"Request": "MobileGestalt", "MobileGestaltKeys": list(keys)})
        return response.get("Diagnostics", {}).get("MobileGestalt") or {}

    def ioregistry(self, plane: Optional[str] = None, name: Optional[str] = None,
                   io_class: Optional[str] = None) -> dict[str, Any]:
        'Query IORegistry entries (narrow by plane / entry name / class).'
        payload: dict[str, Any] = {"Request": "IORegistry"}
        if plane:
            payload["CurrentPlane"] = plane
        if name:
            payload["EntryName"] = name
        if io_class:
            payload["EntryClass"] = io_class
        return self._request(payload).get("Diagnostics", {}).get("IORegistry") or {}

    def battery(self) -> dict[str, Any]:
        'Battery / power info (``IOPMPowerSource`` in IORegistry).'
        return self.ioregistry(io_class="IOPMPowerSource")

    def wifi(self) -> dict[str, Any]:
        'Wi-Fi interface info.'
        return (self.ioregistry(name="AppleBCMWLANSkywalkInterface")
                or self.ioregistry(io_class="IO80211Interface"))

    def restart(self) -> None:
        'Reboot.'
        self._request({"Request": "Restart"})

    def shutdown(self) -> None:
        'Shut down.'
        self._request({"Request": "Shutdown"})

    def sleep(self) -> None:
        'Sleep (screen off, roughly the power button).'
        self._request({"Request": "Sleep"})


def connect(lockdown: Any = None, rsd: Any = None) -> DiagnosticsRelay:
    'Open a diagnostics_relay over the available link.'
    return DiagnosticsRelay.open(lockdown, rsd)
