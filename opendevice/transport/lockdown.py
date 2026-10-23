from __future__ import annotations

from typing import Any, Optional

from ..core.errors import LockdownError, NotPairedError
from ..core.plistio import request as plist_request
from . import devices, usbmux
from .pairing import PairingRecord
from .service import ServiceConnection

LABEL = "opendevice"
LOCKDOWN_SERVICE = "com.apple.mobile.lockdown"


class LockdownClient:
    'A lockdownd connection to one device.'

    def __init__(self, udid: Optional[str] = None, mux_address: Any = None,
                 label: str = LABEL) -> None:
        self.label = label
        self.device = devices.resolve(udid, mux_address)
        self.record = PairingRecord.load(self.device.udid)
        self.sock = usbmux.connect_port(self.device.devid, usbmux.LOCKDOWN_PORT, mux_address)
        self.session_id: Optional[str] = None
        self.ssl_enabled = False
        self._ssl_context = None

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
        if self.record is not None:
            self.record.cleanup()

    def __enter__(self) -> "LockdownClient":
        self.start_session()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def query_type(self) -> str:
        response = plist_request(self.sock, {"Label": self.label, "Request": "QueryType"})
        if not isinstance(response, dict) or response.get("Type") != LOCKDOWN_SERVICE:
            raise LockdownError(f"QueryType returned an error: {response!r}")
        return str(response["Type"])

    def start_session(self) -> None:
        'Start the session; swap the socket for a TLS one when SSL is required.'
        self.query_type()
        response = plist_request(self.sock, {
            "Label": self.label,
            "Request": "StartSession",
            "HostID": self.record.host_id,
            "SystemBUID": self.record.system_buid,
        })
        if not isinstance(response, dict):
            raise LockdownError(f"StartSession returned an error: {response!r}")
        if response.get("Error"):
            raise NotPairedError(f"StartSession failed: {response['Error']} (device not trusted?)")
        self.session_id = response.get("SessionID")
        if response.get("EnableSessionSSL"):
            self._ssl_context = self.record.ssl_context()
            self.sock.settimeout(10.0)
            try:
                self.sock = self._ssl_context.wrap_socket(self.sock)
            except BaseException:
                self.record.cleanup()
                raise
            self.ssl_enabled = True

    def get_value(self, domain: Optional[str] = None, key: Optional[str] = None) -> Any:
        payload: dict[str, Any] = {"Label": self.label, "Request": "GetValue"}
        if domain:
            payload["Domain"] = domain
        if key:
            payload["Key"] = key
        response = plist_request(self.sock, payload)
        if not isinstance(response, dict):
            raise LockdownError(f"GetValue returned an error: {response!r}")
        if response.get("Error"):
            raise LockdownError(f"GetValue failed: {response['Error']}")
        return response.get("Value")

    def all_values(self) -> dict[str, Any]:
        value = self.get_value()
        return value if isinstance(value, dict) else {}

    def start_service(self, name: str, use_ssl: Optional[bool] = None) -> ServiceConnection:
        'Start a lockdown service and return a connected ServiceConnection.'
        response = plist_request(self.sock, {
            "Label": self.label, "Request": "StartService", "Service": name,
        })
        if not isinstance(response, dict):
            raise LockdownError(f"StartService({name}) returned an error: {response!r}")
        if response.get("Error"):
            raise LockdownError(f"StartService({name}) failed: {response['Error']}")
        port = response.get("Port")
        if port is None:
            raise LockdownError(f"StartService({name}) response has no Port: {response!r}")
        sock = usbmux.connect_port(self.device.devid, int(port))
        need_ssl = bool(response.get("EnableServiceSSL")) if use_ssl is None else use_ssl
        try:
            context = self.record.ssl_context() if need_ssl else None
            return ServiceConnection.connect(sock, context, name=name)
        except BaseException:
            try:
                sock.close()
            except OSError:
                pass
            raise
