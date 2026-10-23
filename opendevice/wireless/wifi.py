from __future__ import annotations

import socket
import ssl
import sys
from typing import Any, Optional

from ..core.errors import OpenDeviceError
from ..remote.tunnel import TunnelInfo, WifiTunnel, handshake_cdtunnel
from .rrp import (
    RemotePairingClient,
    RemotePairRecord,
    RemoteService,
    discover,
    find_record_for,
)

PSK_CIPHERS = "PSK:@SECLEVEL=0"


def _psk_context(psk: bytes) -> ssl.SSLContext:
    'TLS 1.2-PSK client context (client certificates and verification are off; identity comes from the PSK).'
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        context.set_ciphers(PSK_CIPHERS)
    except ssl.SSLError as exc:
        raise OpenDeviceError(
            f"this Python's SSL backend ({ssl.OPENSSL_VERSION}) cannot select a PSK "
            "cipher suite; the Wi-Fi tunnel needs an OpenSSL build (LibreSSL does not work)."
        ) from exc
    context.set_psk_client_callback(lambda hint=None: (None, psk))
    return context


def list_devices(timeout: float = 4.0) -> "list[RemoteService]":
    'List devices advertising ``_remotepairing`` on the local network.'
    return discover(timeout)


def available(udid: Optional[str] = None, *,
              discover_timeout: float = 4.0,
              verify_timeout: float = 6.0) -> bool:
    'Whether Wi-Fi can reach this device now: a pairing record exists and the device is recognised on the network.'
    if not udid:
        return False
    record = _load_record(udid)
    if record is None:
        return False
    services = discover(discover_timeout)
    if not services:
        return False
    if len(services) == 1:
        return True
    try:
        _identify(services, record, verify_timeout)
    except OpenDeviceError:
        return False
    return True


def _load_record(udid: str) -> Optional[RemotePairRecord]:
    'Find the remote pairing record by UDID (or unique prefix).'
    record = RemotePairRecord.load(udid)
    if record is not None:
        return record
    wanted = udid.strip().lower()
    matches = [item for item in RemotePairRecord.load_any()
               if item.identifier.lower().startswith(wanted)]
    return matches[0] if len(matches) == 1 else None


def _identify(services: "list[RemoteService]", record: RemotePairRecord,
              timeout: float) -> RemoteService:
    'With several devices, try pair-verify one by one and take the one that succeeds (Bonjour names do not match UDIDs, so trying is the only way).'
    if len(services) == 1:
        return services[0]
    for service in services:
        client = RemotePairingClient(service.best_address(), service.port, record,
                                     timeout=timeout)
        try:
            client.connect()
            return service
        except Exception:                    # noqa
            continue
        finally:
            client.close()
    raise OpenDeviceError(
        'no device on the network accepts this pairing record: ' + ", ".join(s.identifier for s in services))


def open_wifi_tunnel(udid: Optional[str] = None,
                     host: Optional[str] = None,
                     port: Optional[int] = None,
                     identifier: Optional[str] = None,
                     record: Optional[RemotePairRecord] = None,
                     timeout: float = 15.0,
                     discover_timeout: float = 4.0) -> tuple[WifiTunnel, TunnelInfo]:
    'Open a Wi-Fi tunnel, returning ``(tunnel, tunnel info)``.'
    if host is None or port is None:
        services = discover(discover_timeout)
        if not services:
            raise OpenDeviceError(
                'no _remotepairing device found on the network. Check:\n  • phone and computer are on the **same subnet**, and the phone is on Wi-Fi (not just a hotspot)\n  • the phone is unlocked\n  • this device has been **remotely paired** once over USB'
            )
        if udid:
            record = record or _load_record(udid)
            if record is None:
                raise OpenDeviceError(
                    f"No remote pairing record for {udid!r}. "
                    "**Remote pairing can only be done over USB**: pair it once with the "
                    "cable attached before Wi-Fi will recognise this device."
                )
            service = _identify(services, record, timeout)
        elif identifier:
            services = [s for s in services if s.identifier == identifier]
            if not services:
                raise OpenDeviceError(f"no device advertising identifier={identifier!r} was found")
            service = services[0]
        else:
            service = services[0]
        host, port = service.best_address(), service.port
    else:
        service = RemoteService(identifier=identifier or "", host=host, port=port)

    if record is None:
        record = find_record_for(service) if service.identifier else None
        if record is None:
            records = RemotePairRecord.load_any()
            record = records[0] if len(records) == 1 else None
        if record is None:
            raise OpenDeviceError(
                'No usable remote pairing record on this host. **Remote pairing can only be done over USB**: pair the device once with the cable attached before Wi-Fi will recognise it.'
            )

    if sys.version_info < (3, 13):
        raise OpenDeviceError(
            f"the Wi-Fi tunnel needs Python 3.13+ (current: {sys.version.split()[0]}), "
            "because the data plane uses stdlib TLS-PSK (SSLContext.set_psk_client_callback)."
        )

    client = RemotePairingClient(host, port, record, timeout=timeout)
    try:
        client.connect()
        listener_port = client.create_tcp_listener()
        psk = client.encryption_key
        if not psk:
            raise OpenDeviceError('pair-verify produced no shared secret')
    finally:
        client.close()

    raw = socket.create_connection((host, listener_port), timeout=timeout)
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    try:
        tls = _psk_context(psk).wrap_socket(raw, server_hostname=host)
    except Exception:
        raw.close()
        raise
    try:
        info = handshake_cdtunnel(tls)
    except Exception:
        tls.close()
        raise
    return WifiTunnel(tls, info), info


def open_wifi_rsd(udid: Optional[str] = None,
                  host: Optional[str] = None,
                  port: Optional[int] = None,
                  identifier: Optional[str] = None,
                  record: Optional[RemotePairRecord] = None,
                  timeout: float = 15.0,
                  discover_timeout: float = 4.0) -> Any:
    'Straight to RSD (convenience entry point for the CLI and higher layers).'
    from ..remote.rsd import RSD

    tunnel, info = open_wifi_tunnel(udid=udid, host=host, port=port,
                                   identifier=identifier, record=record,
                                   timeout=timeout, discover_timeout=discover_timeout)
    rsd = RSD(tunnel, info)
    rsd.connect()
    return rsd


__all__ = ["PSK_CIPHERS", "list_devices", "open_wifi_rsd", "open_wifi_tunnel"]
