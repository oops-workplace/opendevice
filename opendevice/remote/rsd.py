from __future__ import annotations

import uuid
from typing import Any, Callable, Optional

from ..core import log as log_module
from ..core.errors import OpenDeviceError
from . import remotexpc
from .ipstack import IpStack
from .tunnel import TunnelInfo

log = log_module.get(__name__)

DDI_SERVICE_PREFIXES = (
    "com.apple.coredevice.",
    "com.apple.instruments.",
    "com.apple.accessibility.axAuditDaemon.",
)

DDI_HINT = (
    '  These services live in the personalized disk image (DDI) and are only published when developer mode is on and the image is mounted.\n  Check: opendevice devmode status / opendevice mounter list')


class RSD:
    'RemoteServiceDiscovery for one device.'

    def __init__(self, tunnel: "Any", info: TunnelInfo,
                 peer_uuid: Optional[uuid.UUID] = None,
                 threaded: bool = False) -> None:
        self.tunnel = tunnel
        self.info = info
        self.stack = IpStack(tunnel, info.host_address, info.device_address, info.mtu,
                             threaded=threaded)
        self.peer_uuid = peer_uuid or uuid.uuid4()
        self.peer_info: dict[str, Any] = {}
        self.xpc: Optional[remotexpc.RemoteXPCConnection] = None
        self.core_device_version: Optional[str] = None
        self.ddi_probe: Optional[Callable[[], str]] = None

    def connect(self, timeout: float = 15.0) -> dict[str, Any]:
        conn = self.stack.connect(self.info.rsd_port, timeout=timeout)
        self.xpc = remotexpc.RemoteXPCConnection(conn)
        self.xpc.handshake()
        self.xpc.send_request(remotexpc.handshake_message(self.peer_uuid))
        self.peer_info = self.xpc.receive_response(timeout)
        if not self.peer_info:
            raise OpenDeviceError('RSD handshake returned nothing')
        log.info('RSD ready: %d services  %s iOS %s',
                 len(self.services), self.product_type or "?", self.product_version or "?")
        return self.peer_info

    @property
    def properties(self) -> dict[str, Any]:
        return self.peer_info.get("Properties", {}) or {}

    @property
    def services(self) -> dict[str, Any]:
        return self.peer_info.get("Services", {}) or {}

    def service_entry(self, name: str) -> dict[str, Any]:
        entry = self.services.get(name)
        if entry is None:
            text = f"device does not provide this service: {name}"
            hint = self._ddi_hint(name)
            raise OpenDeviceError(f"{text}\n{hint}" if hint else text)
        return entry

    def _ddi_hint(self, name: str) -> str:
        'Add a why/how-to-fix line when a DDI-backed service is missing.'
        if not name.startswith(DDI_SERVICE_PREFIXES):
            return ""
        if self.ddi_probe is not None:
            try:
                live = self.ddi_probe() or ""
            except Exception:                   # noqa
                live = ""
            if live:
                return live
        return DDI_HINT

    def service_port(self, name: str) -> int:
        return int(self.service_entry(name)["Port"])

    def service_features(self, name: str) -> list[str]:
        'Feature list this service advertised in the handshake (CoreDevice exposes the ``feature.*`` keys).'
        props = self.service_entry(name).get("Properties", {}) or {}
        return [str(item) for item in props.get("Features") or ()]

    def uses_remote_xpc(self, name: str) -> bool:
        props = self.service_entry(name).get("Properties", {}) or {}
        return bool(props.get("UsesRemoteXPC"))

    def udid(self) -> str:
        return str(self.properties.get("UniqueDeviceID", ""))

    @property
    def product_type(self) -> Optional[str]:
        'Product type, e.g. ``<product-type>``.'
        value = self.properties.get("ProductType")
        return str(value) if value else None

    @property
    def product_version(self) -> Optional[str]:
        'OS version, e.g. ``27.0``.'
        for key in ("OSVersion", "ProductVersion"):
            value = self.properties.get(key)
            if value:
                return str(value)
        return None

    @property
    def build_version(self) -> Optional[str]:
        'OS build (e.g. ``<os-build>``).'
        value = self.properties.get("BuildVersion")
        return str(value) if value else None

    def close(self) -> None:
        if self.xpc is not None:
            self.xpc.close()
            self.xpc = None
