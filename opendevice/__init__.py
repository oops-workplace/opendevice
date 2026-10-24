from __future__ import annotations

from .core.errors import (
    AfcError,
    DeveloperModeError,
    InstallError,
    LockdownError,
    MuxError,
    NotPairedError,
    NotSupportedError,
    OpenDeviceError,
    ServiceError,
    TunnelError,
)
from .remote.tunnel import Tunnel, TunnelInfo
from .transport.lockdown import LockdownClient
from .transport.pairing import PairingRecord
from .transport.service import ServiceConnection
from .transport.usbmux import Mux, MuxDevice, connect_port, list_devices

__version__ = "0.1.2"
__all__ = [
    "LockdownClient", "PairingRecord", "ServiceConnection",
    "Tunnel", "TunnelInfo",
    "Mux", "MuxDevice", "connect_port", "list_devices",
    "OpenDeviceError", "MuxError", "NotPairedError", "ServiceError",
    "LockdownError", "TunnelError", "AfcError", "InstallError",
    "DeveloperModeError", "NotSupportedError",
    "remote", "services", "media", "ax",
    "core", "transport", "wireless", "input",
]

_SUBPACKAGES = ("ax", "core", "input", "media", "remote", "services",
                "transport", "wireless")


def __getattr__(name: str) -> object:
    'PEP 562: only import ``opendevice.services`` when the attribute is touched.'
    if name in _SUBPACKAGES:
        import importlib

        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
