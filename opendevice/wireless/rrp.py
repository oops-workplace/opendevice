from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import plistlib
import socket
import struct
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..core import paths
from ..core.errors import OpenDeviceError

RPPAIRING_MAGIC = b"RPPairing"
CDTUNNEL_MAGIC = b"CDTunnel"

WIRE_PROTOCOL_VERSION = 19

REQUESTED_MTU = 16000

RECORD_PREFIX = paths.RECORD_PREFIX
RECORD_SUFFIX = paths.RECORD_SUFFIX


class TLV:
    """``PairingDataComponentType``."""

    METHOD = 0x00
    IDENTIFIER = 0x01
    SALT = 0x02
    PUBLIC_KEY = 0x03
    PROOF = 0x04
    ENCRYPTED_DATA = 0x05
    STATE = 0x06
    ERROR = 0x07
    SIGNATURE = 0x0A
    INFO = 0x11


def build_tlv(items: "list[tuple[int, bytes]]") -> bytes:
    '``type(1B) + len(1B) + data``; data longer than 255 bytes is chunked.'
    out = bytearray()
    for kind, data in items:
        if not data:
            out += bytes([kind, 0])
            continue
        for offset in range(0, len(data), 255):
            chunk = data[offset:offset + 255]
            out += bytes([kind, len(chunk)]) + chunk
    return bytes(out)


def parse_tlv(data: bytes) -> "dict[int, bytes]":
    'Decode TLV.'
    result: dict[int, bytes] = {}
    offset = 0
    while offset + 2 <= len(data):
        kind = data[offset]
        length = data[offset + 1]
        chunk = data[offset + 2:offset + 2 + length]
        if len(chunk) < length:
            break
        result[kind] = result.get(kind, b"") + chunk
        offset += 2 + length
    return result


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    buffer = bytearray()
    while len(buffer) < count:
        chunk = sock.recv(count - len(buffer))
        if not chunk:
            raise OpenDeviceError('peer closed the connection mid-frame')
        buffer += chunk
    return bytes(buffer)


def send_frame(sock: socket.socket, obj: Any, magic: bytes = RPPAIRING_MAGIC) -> None:
    'Send one frame: magic + big-endian length + JSON.'
    body = json.dumps(obj, default=_json_default).encode()
    sock.sendall(magic + struct.pack(">H", len(body)) + body)


def recv_frame(sock: socket.socket, magic: bytes = RPPAIRING_MAGIC) -> Any:
    'Receive one frame; an unexpected magic indicates a wrong port or a different protocol.'
    got = _recv_exact(sock, len(magic))
    if got != magic:
        raise OpenDeviceError(f"expected {magic!r} frame, got {got!r}")
    (length,) = struct.unpack(">H", _recv_exact(sock, 2))
    return json.loads(_recv_exact(sock, length))


def _json_default(value: Any) -> Any:
    'Values JSON cannot represent: ``bytes`` becomes a base64 string (the form Apple uses).'
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode()
    raise TypeError(f"cannot serialize{type(value).__name__}")


def find_record_for(service: "RemoteService") -> Optional[RemotePairRecord]:
    'Find the local pairing record for an advertised service.'
    records = RemotePairRecord.load_any()
    if not records:
        return None
    for record in records:
        if record.identifier == service.identifier:
            return record
    return records[0] if len(records) == 1 else None


@dataclass
class RemotePairRecord:
    'One remote pairing record (``remote_<identifier>.plist``).'

    identifier: str
    public_key: bytes = b""
    private_key: bytes = b""
    remote_unlock_host_key: str = ""
    peer_alt_irk: bytes = b""
    path: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, identifier: str) -> Optional["RemotePairRecord"]:
        path = paths.records_dir() / f"{RECORD_PREFIX}{identifier}{RECORD_SUFFIX}"
        if not path.is_file():
            return None
        with open(path, "rb") as handle:
            data = plistlib.load(handle)
        return cls(
            identifier=identifier,
            public_key=bytes(data.get("public_key") or b""),
            private_key=bytes(data.get("private_key") or b""),
            remote_unlock_host_key=str(data.get("remote_unlock_host_key") or ""),
            peer_alt_irk=bytes(data.get("peer_alt_irk") or b""),
            path=str(path),
            raw=dict(data),
        )

    @classmethod
    def load_any(cls) -> "list[RemotePairRecord]":
        'All known remote pairing records (used to pick one when no identifier is given).'
        directory = paths.records_dir()
        if not directory.is_dir():
            return []
        found: list[RemotePairRecord] = []
        for path in sorted(directory.glob(f"{RECORD_PREFIX}*{RECORD_SUFFIX}")):
            identifier = path.name[len(RECORD_PREFIX):-len(RECORD_SUFFIX)]
            if not identifier:
                continue
            record = cls.load(identifier)
            if record is not None:
                found.append(record)
        return found

    def save(self) -> Path:
        'Write into the package\'s records directory (:func:`opendevice.paths.records_dir`).'
        payload = dict(self.raw)
        payload.update({
            "public_key": self.public_key,
            "private_key": self.private_key,
            "remote_unlock_host_key": self.remote_unlock_host_key,
        })
        if self.peer_alt_irk:
            payload["peer_alt_irk"] = self.peer_alt_irk

        directory = paths.records_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{RECORD_PREFIX}{self.identifier}{RECORD_SUFFIX}"
        with open(path, "wb") as handle:
            plistlib.dump(payload, handle)
        self.path = str(path)
        return path


@dataclass
class RemoteService:
    'A device advertising ``_remotepairing._tcp`` on the local network.'

    identifier: str
    host: str
    port: int
    addresses: "list[str]" = field(default_factory=list)
    auth_tag: str = ""
    version: str = ""
    raw: dict[str, str] = field(default_factory=dict)

    def best_address(self) -> str:
        'Pick a connectable address.'
        for address in self.addresses:
            if ":" not in address:
                return address
        return self.addresses[0] if self.addresses else self.host


def discover(timeout: float = 4.0) -> "list[RemoteService]":
    'Browse ``_remotepairing._tcp`` on the local network.'
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError as exc:                # pragma: no cover
        raise OpenDeviceError(
            'Wi-Fi discovery needs zeroconf: pip install zeroconf'
        ) from exc

    services: list[RemoteService] = []

    class _Listener(ServiceListener):
        def add_service(self, zc: Any, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name, timeout=2000)
            if info is None:
                return
            properties = {}
            for key, value in (info.properties or {}).items():
                k = key.decode() if isinstance(key, bytes) else str(key)
                v = value.decode() if isinstance(value, bytes) else str(value or "")
                properties[k] = v
            addresses = []
            for packed in info.addresses:
                try:
                    addresses.append(socket.inet_ntop(
                        socket.AF_INET6 if len(packed) == 16 else socket.AF_INET, packed))
                except OSError:
                    continue
            identifier = properties.get("identifier", "")
            if not identifier:
                identifier = name.split(".", 1)[0]
            services.append(RemoteService(
                identifier=identifier,
                host=(info.server or "").rstrip("."),
                port=info.port or 0,
                addresses=addresses,
                auth_tag=properties.get("authTag", ""),
                version=properties.get("ver", ""),
                raw=properties,
            ))

        def update_service(self, zc: Any, type_: str, name: str) -> None:
            pass

        def remove_service(self, zc: Any, type_: str, name: str) -> None:
            pass

    zeroconf = Zeroconf()
    try:
        ServiceBrowser(zeroconf, "_remotepairing._tcp.local.", _Listener())
        time.sleep(timeout)
    finally:
        zeroconf.close()
    return services


class RemotePairingClient:
    'An RRP control connection.'

    def __init__(self, host: str = "", port: int = 0,
                 record: Optional[RemotePairRecord] = None,
                 host_identifier: Optional[str] = None, timeout: float = 15.0,
                 sock: Optional[socket.socket] = None) -> None:
        self.host = host
        self.port = port
        self.record = record
        self.timeout = timeout
        self.sock = sock
        self._srp: Any = None

        self.x25519_private = X25519PrivateKey.generate()
        self.ed25519_private = Ed25519PrivateKey.generate()
        self.identifier = host_identifier or _host_identifier()
        if record is not None and record.raw.get("host_identifier"):
            self.identifier = record.raw["host_identifier"]

        self.encryption_key: Optional[bytes] = None
        self.client_cip: Optional[ChaCha20Poly1305] = None
        self.server_cip: Optional[ChaCha20Poly1305] = None
        self.handshake_info: dict[str, Any] = {}
        self.peer_identifier: str = ""

        self._sequence_number = 0
        self._encrypted_sequence_number = 0

    def _send_plain(self, inner: dict[str, Any]) -> None:
        if self.sock is None:
            raise OpenDeviceError('not connected to the device yet (call connect)')
        send_frame(self.sock, {
            "message": {"plain": {"_0": inner}},
            "originatedBy": "host",
            "sequenceNumber": self._sequence_number,
        })
        self._sequence_number += 1

    def _receive_plain(self) -> dict[str, Any]:
        if self.sock is None:
            raise OpenDeviceError('not connected to the device yet (call connect)')
        response = recv_frame(self.sock)
        plain = response.get("message", {}).get("plain", {}).get("_0")
        if not isinstance(plain, dict):
            raise OpenDeviceError(f"not a plaintext response: {str(response)[:200]}")
        return plain

    def _send_receive_encrypted(self, request: dict[str, Any]) -> dict[str, Any]:
        'Send an encrypted request and decode the response.'
        if self.sock is None or self.client_cip is None or self.server_cip is None:
            raise OpenDeviceError('encrypted channel is not established yet (run pair-verify first)')
        nonce = struct.pack("<Q", self._encrypted_sequence_number) + b"\x00" * 4
        try:
            ciphertext = self.client_cip.encrypt(nonce, json.dumps(request).encode(), b"")

            send_frame(self.sock, {
                "message": {"streamEncrypted": {"_0": base64.b64encode(ciphertext).decode()}},
                "originatedBy": "host",
                "sequenceNumber": self._sequence_number,
            })
            response = recv_frame(self.sock)
        finally:
            self._encrypted_sequence_number += 1

        raw = response.get("message", {}).get("streamEncrypted", {}).get("_0")
        if raw is None:
            raise OpenDeviceError(f"not an encrypted response: {str(response)[:200]}")
        if isinstance(raw, str):
            raw = base64.b64decode(raw)
        plaintext = self.server_cip.decrypt(nonce, raw, None)
        inner = json.loads(plaintext).get("response", {}).get("_1")
        if not isinstance(inner, dict):
            raise OpenDeviceError(f"unrecognized response shape: {plaintext[:200]!r}")
        if "errorExtended" in inner:
            detail = (inner["errorExtended"].get("_0", {})
                      .get("userInfo", {}).get("NSLocalizedDescription"))
            raise OpenDeviceError(f"device rejected the request: {detail}")
        return inner

    def connect(self) -> "RemotePairingClient":
        'Connect and complete pair-verify. Failures carry actionable hints.'
        if self.sock is None:
            self.sock = socket.create_connection((self.host, self.port),
                                                 timeout=self.timeout)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            self._handshake()
            if not self._pair_verify():
                raise OpenDeviceError('pair-verify rejected by the device (the pairing record may be stale)')
            self._init_encryption_keys()
        except Exception:
            self.close()
            raise
        return self

    def _handshake(self) -> None:
        self._send_plain({"request": {"_0": {"handshake": {"_0": {
            "hostOptions": {"attemptPairVerify": True},
            "wireProtocolVersion": WIRE_PROTOCOL_VERSION,
        }}}}})
        response = self._receive_plain()
        try:
            self.handshake_info = response["response"]["_1"]["handshake"]["_0"]
        except (KeyError, TypeError) as exc:
            raise OpenDeviceError(f"unrecognized handshake response: {str(response)[:300]}") from exc
        peer = self.handshake_info.get("peerDeviceInfo") or {}
        self.peer_identifier = str(peer.get("identifier") or "")

    def _pair_verify(self) -> bool:
        """pair-verify: M1 → M2 → M3 → M4."""
        if self.sock is None:
            raise OpenDeviceError('not connected to the device yet (call connect)')
        our_public = self.x25519_private.public_key().public_bytes_raw()

        self._send_plain({"event": {"_0": {"pairingData": {"_0": {
            "data": base64.b64encode(build_tlv([
                (TLV.STATE, b"\x01"),
                (TLV.PUBLIC_KEY, our_public),
            ])).decode(),
            "kind": "verifyManualPairing",
            "startNewSession": True,
        }}}}})
        data = parse_tlv(self._receive_pairing_data())

        if TLV.ERROR in data:
            self._send_pair_verify_failed()
            return False

        peer_public = X25519PublicKey.from_public_bytes(data[TLV.PUBLIC_KEY])
        self.encryption_key = self.x25519_private.exchange(peer_public)

        derived = _hkdf(self.encryption_key, b"Pair-Verify-Encrypt-Salt",
                        b"Pair-Verify-Encrypt-Info")
        cip = ChaCha20Poly1305(derived)
        private = self._signing_key()
        signature = private.sign(our_public + self.identifier.encode()
                                 + peer_public.public_bytes_raw())
        encrypted = cip.encrypt(b"\x00\x00\x00\x00PV-Msg03", build_tlv([
            (TLV.IDENTIFIER, self.identifier.encode()),
            (TLV.SIGNATURE, signature),
        ]), b"")

        self._send_plain({"event": {"_0": {"pairingData": {"_0": {
            "data": base64.b64encode(build_tlv([
                (TLV.STATE, b"\x03"),
                (TLV.ENCRYPTED_DATA, encrypted),
            ])).decode(),
            "kind": "verifyManualPairing",
            "startNewSession": False,
        }}}}})
        data = parse_tlv(self._receive_pairing_data())

        if TLV.ERROR in data:
            self._send_pair_verify_failed()
            return False
        return True

    def _signing_key(self) -> Ed25519PrivateKey:
        'Ed25519 private key used for signing (an all-zero placeholder when there is no pairing record, matching Apple).'
        if self.record is not None and self.record.private_key:
            return Ed25519PrivateKey.from_private_bytes(self.record.private_key)
        return Ed25519PrivateKey.from_private_bytes(b"\x00" * 32)

    def _receive_pairing_data(self) -> bytes:
        response = self._receive_plain()
        event = response.get("event", {}).get("_0") or {}
        if "pairingData" in event:
            payload = event["pairingData"]["_0"]
            data = payload["data"] if isinstance(payload, dict) else payload
            return base64.b64decode(data) if isinstance(data, str) else bytes(data)
        if "pairingRejectedWithError" in event:
            raise OpenDeviceError("device rejected the pairing (did the user tap Don't Allow?)")
        raise OpenDeviceError(f"unexpected event: {str(event)[:200]}")

    def _send_pair_verify_failed(self) -> None:
        self._send_plain({"event": {"_0": {"pairVerifyFailed": {}}}})

    def _init_encryption_keys(self) -> None:
        'Derive the two control-channel keys (separate for send and receive).'
        if self.encryption_key is None:
            raise OpenDeviceError('pair-verify produced no shared secret')
        self.client_cip = ChaCha20Poly1305(_hkdf(self.encryption_key, None,
                                                 b"ClientEncrypt-main"))
        self.server_cip = ChaCha20Poly1305(_hkdf(self.encryption_key, None,
                                                 b"ServerEncrypt-main"))

    def pair_setup(self, pin: Optional[str] = None,
                   device_identifier: Optional[str] = None) -> RemotePairRecord:
        'Establish a remote pairing over a **trusted channel** and return the record that was written.'
        if not self.handshake_info:
            self._handshake()
        self._pair_consent(pin)
        self._pair_srp_proof()

        self.ed25519_private = Ed25519PrivateKey.generate()
        alt_irk = self._save_pair_record_on_peer()
        self._init_encryption_keys()
        host_key = self._create_remote_unlock()

        record = RemotePairRecord(
            identifier=device_identifier or self.peer_identifier or self.identifier,
            public_key=self.ed25519_private.public_key().public_bytes_raw(),
            private_key=self.ed25519_private.private_bytes_raw(),
            remote_unlock_host_key=host_key,
            peer_alt_irk=alt_irk,
        )
        record.save()
        return record

    def _pair_consent(self, pin: Optional[str]) -> None:
        'M1/M2: fetch the SRP salt and the server public key. No Trust prompt appears over USB.'
        self._send_pairing_event({
            "data": base64.b64encode(build_tlv([
                (TLV.METHOD, b"\x00"),
                (TLV.STATE, b"\x01"),
            ])).decode(),
            "kind": "setupManualPairing",
            "sendingHost": _host_name(),
            "startNewSession": True,
        })
        event = self._receive_plain().get("event", {}).get("_0") or {}
        if "pairingRejectedWithError" in event:
            raise OpenDeviceError('device rejected the pairing')
        if "awaitingUserConsent" in event:
            data = self._receive_pairing_data()
        elif "pairingData" in event:
            payload = event["pairingData"]["_0"]
            raw = payload["data"] if isinstance(payload, dict) else payload
            data = base64.b64decode(raw) if isinstance(raw, str) else bytes(raw)
        else:
            raise OpenDeviceError(f"unexpected event: {str(event)[:200]}")

        tlv = parse_tlv(data)
        if TLV.ERROR in tlv:
            raise OpenDeviceError('device asked for backoff (BACKOFF), retry later')
        if TLV.PUBLIC_KEY not in tlv or TLV.SALT not in tlv:
            raise OpenDeviceError(f"pair-setup response is missing fields: {sorted(tlv)}")
        self._init_srp(tlv[TLV.PUBLIC_KEY], tlv[TLV.SALT], pin)

    def _init_srp(self, server_public: bytes, salt: bytes,
                  pin: Optional[str]) -> None:
        'Start an SRP-6a session (3072-bit prime + SHA512, username fixed to ``Pair-Setup``).'
        try:
            from srptools import SRPClientSession, SRPContext
            from srptools.constants import PRIME_3072, PRIME_3072_GEN
        except ImportError as exc:                # pragma: no cover
            raise OpenDeviceError(
                'pair-setup needs srptools: pip install srptools'
            ) from exc

        import binascii

        session = SRPClientSession(SRPContext(
            "Pair-Setup", password=pin or "000000", prime=PRIME_3072,
            generator=PRIME_3072_GEN, hash_func=hashlib.sha512))
        session.process(server_public.hex(), salt.hex())
        self._srp = session
        self.encryption_key = binascii.unhexlify(session.key)

    def _pair_srp_proof(self) -> None:
        'M3/M4: submit the SRP proof and verify the server proof.'
        import binascii

        session = self._srp
        client_public = binascii.unhexlify(session.public)
        proof = binascii.unhexlify(session.key_proof)
        data = self._send_receive_pairing_data({
            "data": base64.b64encode(build_tlv([
                (TLV.STATE, b"\x03"),
                (TLV.PUBLIC_KEY, client_public[:255]),
                (TLV.PUBLIC_KEY, client_public[255:]),
                (TLV.PROOF, proof),
            ])).decode(),
            "kind": "setupManualPairing",
            "sendingHost": _host_name(),
            "startNewSession": False,
        })
        tlv = parse_tlv(data)
        if TLV.ERROR in tlv:
            raise OpenDeviceError('SRP proof rejected (wrong pairing code?)')
        if not session.verify_proof(tlv[TLV.PROOF].hex().encode()):
            raise OpenDeviceError('server SRP proof verification failed')

    def _save_pair_record_on_peer(self) -> bytes:
        'M5/M6: register this host\'s Ed25519 public key on the device and fetch its altIRK.'
        if self.encryption_key is None:
            raise OpenDeviceError('pair-verify produced no shared secret')

        from ..core.errors import OpenDeviceError as _Err

        try:
            from opack2 import dumps
        except ImportError as exc:                # pragma: no cover
            raise _Err('pair-setup needs opack2: pip install opack2') from exc

        setup_key = _hkdf(self.encryption_key, b"Pair-Setup-Encrypt-Salt",
                          b"Pair-Setup-Encrypt-Info")
        sign_base = _hkdf(self.encryption_key, b"Pair-Setup-Controller-Sign-Salt",
                          b"Pair-Setup-Controller-Sign-Info")
        public = self.ed25519_private.public_key().public_bytes_raw()
        signature = self.ed25519_private.sign(sign_base + self.identifier.encode()
                                              + public)

        bt_address, bt_mac = _host_bt_address()
        info = dumps({
            "altIRK": _host_alt_irk(),
            "btAddr": bt_address,
            "mac": bt_mac,
            "remotepairing_serial_number": _host_serial(),
            "accountID": self.identifier,
            "model": HOST_MODEL,
            "name": _host_name(),
        })

        cip = ChaCha20Poly1305(setup_key)
        encrypted = cip.encrypt(b"\x00\x00\x00\x00PS-Msg05", build_tlv([
            (TLV.IDENTIFIER, self.identifier.encode()),
            (TLV.PUBLIC_KEY, public),
            (TLV.SIGNATURE, signature),
            (TLV.INFO, info),
        ]), b"")

        data = self._send_receive_pairing_data({
            "data": base64.b64encode(build_tlv([
                (TLV.ENCRYPTED_DATA, encrypted[:255]),
                (TLV.ENCRYPTED_DATA, encrypted[255:]),
                (TLV.STATE, b"\x05"),
            ])).decode(),
            "kind": "setupManualPairing",
            "sendingHost": _host_name(),
            "startNewSession": False,
        })
        tlv = parse_tlv(data)
        if TLV.ERROR in tlv:
            raise OpenDeviceError('device failed to store the host public key')
        decrypted = cip.decrypt(b"\x00\x00\x00\x00PS-Msg06",
                                tlv[TLV.ENCRYPTED_DATA], b"")
        return self._peer_alt_irk(parse_tlv(decrypted))

    @staticmethod
    def _peer_alt_irk(device_tlv: "dict[int, bytes]") -> bytes:
        'Extract the altIRK from the device INFO reply (OPACK).'
        raw = device_tlv.get(TLV.INFO)
        if not raw:
            return b""
        try:
            from opack2 import loads

            info = loads(raw)
            value = info.get("altIRK") if isinstance(info, dict) else None
            return bytes(value) if value else b""
        except Exception:                         # noqa
            return b""

    def _create_remote_unlock(self) -> str:
        'Fetch the remote unlock key (echoed back as the RSDCheckin EscrowBag).'
        try:
            response = self._send_receive_encrypted(
                {"request": {"_0": {"createRemoteUnlockKey": {}}}})
            return str(response["createRemoteUnlockKey"]["hostKey"])
        except Exception:                         # noqa
            return ""

    def _send_pairing_event(self, pairing_data: dict[str, Any]) -> None:
        self._send_plain({"event": {"_0": {"pairingData": {"_0": pairing_data}}}})

    def _send_receive_pairing_data(self, pairing_data: dict[str, Any]) -> bytes:
        self._send_pairing_event(pairing_data)
        return self._receive_pairing_data()

    def create_tcp_listener(self) -> int:
        'Ask the device to open a TCP listener and return its port.'
        if self.encryption_key is None:
            raise OpenDeviceError('pair-verify produced no shared secret')
        response = self._send_receive_encrypted({"request": {"_0": {"createListener": {
            "key": base64.b64encode(self.encryption_key).decode(),
            "peerConnectionsInfo": [{
                "owningPID": os.getpid(),
                "owningProcessName": "CoreDeviceService",
            }],
            "transportProtocolType": "tcp",
        }}}})
        listener = response.get("createListener") or {}
        port = listener.get("port")
        if not port:
            raise OpenDeviceError(f"createListener returned no port: {listener}")
        return int(port)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


def _hkdf(ikm: bytes, salt: Optional[bytes], info: bytes, length: int = 32) -> bytes:
    'HKDF-SHA512 as used by RRP.'
    if salt is None:
        return HKDF(algorithm=hashes.SHA512(), length=length, salt=None,
                    info=info).derive(ikm)
    return HKDF(algorithm=hashes.SHA512(), length=length, salt=salt,
                info=info).derive(ikm)


def _host_identifier() -> str:
    "This host's identifier."
    return str(uuid.uuid3(uuid.NAMESPACE_DNS, socket.gethostname())).upper()


def _host_name() -> str:
    'Host name (the ``sendingHost`` / ``name`` fields of pair-setup).'
    return platform.node()


HOST_MODEL = "opendevice"


def _host_serial() -> str:
    'Host serial number.'
    return "opendevice"


def _host_alt_irk() -> bytes:
    '16-byte IRK.'
    return hashlib.sha256(b"opendevice-irk:" + _host_name().encode()).digest()[:16]


def _host_bt_address() -> "tuple[str, bytes]":
    'Fake Bluetooth address (readable string + 6 bytes).'
    raw = hashlib.sha256(b"opendevice-bt:" + _host_name().encode()).digest()[:6]
    return ":".join(f"{byte:02x}" for byte in raw), raw


LOCKDOWN_SERVICE = "com.apple.dt.remotepairingdeviced.lockdown"


def pair_over_lockdown(lockdown: Any, udid: Optional[str] = None,
                       timeout: float = 60.0) -> RemotePairRecord:
    'Create remote pairing credentials over USB and return the record that was written.'
    service = lockdown.start_service(LOCKDOWN_SERVICE)
    try:
        client = RemotePairingClient(sock=service.sock, timeout=timeout)
        client._handshake()
        if client._pair_verify():
            raise OpenDeviceError(
                "this device already has our remote pairing record; no need to pair again.\n"
                f"To rebuild it, delete {paths.records_dir()}/remote_*.plist first."
            )
        return client.pair_setup(device_identifier=udid)
    finally:
        service.close()


__all__ = [
    "CDTUNNEL_MAGIC",
    "LOCKDOWN_SERVICE",
    "RPPAIRING_MAGIC",
    "RemotePairRecord",
    "RemotePairingClient",
    "RemoteService",
    "TLV",
    "build_tlv",
    "discover",
    "pair_over_lockdown",
    "parse_tlv",
    "recv_frame",
    "send_frame",
]
