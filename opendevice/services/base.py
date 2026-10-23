from __future__ import annotations

from typing import Any, Optional, TypeVar

from ..core import log as log_module
from ..core.errors import ServiceError
from ..transport.service import ServiceConnection, open_service

_ServiceT = TypeVar("_ServiceT", bound="Service")


def error_text(name: str, response: dict[str, Any], limit: int = 400) -> str:
    'Turn a device error payload into one sentence; ``DetailedError`` must be included.'
    text = f"{name}: {response.get('Error')}"
    detail = response.get("DetailedError")
    if detail:
        text += f" ({str(detail)[:limit]})"
    return text


class Service:
    'Wrapper for one lockdown / RSD service.'

    NAME = ""
    RSD_NAME: Optional[str] = None

    def __init__(self, conn: Optional[ServiceConnection] = None) -> None:
        self.conn = conn
        self.lockdown: Any = None
        self.rsd: Any = None
        self.log = log_module.get(type(self).__module__)

    @classmethod
    def open(cls: "type[_ServiceT]", lockdown: Any = None, rsd: Any = None) -> "_ServiceT":
        'Open a service over the available link (USB preferred).'
        conn = open_service(lockdown, rsd, cls.NAME, cls.RSD_NAME)
        instance = cls(conn)
        instance.lockdown = lockdown
        instance.rsd = rsd
        instance.log.debug('opening %s (%s)', cls.__name__,
                           "USB" if lockdown is not None else "Wi-Fi")
        return instance

    @classmethod
    def available_over_wifi(cls) -> bool:
        'Whether this service can use Wi-Fi.'
        return bool(cls.RSD_NAME)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def request(self, payload: dict[str, Any], expect_status: bool = False,
                timeout: Optional[float] = None) -> dict[str, Any]:
        'Send one plist request and return the response dict (an ``Error`` is raised).'
        self.log.debug("%s ← %s", type(self).__name__, log_module.redact(payload))
        response = self.conn.request(payload, timeout)
        if not isinstance(response, dict):
            raise ServiceError(f"{type(self).__name__}: response is not a dict: {response!r}")
        if response.get("Error"):
            raise ServiceError(error_text(type(self).__name__, response))
        if expect_status:
            status = response.get("Status")
            if status and status != "Success":
                raise ServiceError(f"{type(self).__name__}: returned {status}: {response!r}")
        return response
