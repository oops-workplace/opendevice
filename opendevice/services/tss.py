from __future__ import annotations

import plistlib
import socket
import urllib.error
import urllib.request
import uuid
from typing import Any, Mapping, Optional

from ..core import log as log_module
from ..core.errors import OpenDeviceError

TSS_URL = "http://gs.apple.com/TSS/controller?action=2"

CLIENT_VERSION = "libauthinstall-973.0.1"
CRYPTEX_CLIENT_VERSION = "libauthinstall-1155.0.5"

HOST_PLATFORM = "mac"

DEFAULT_TIMEOUT = 30.0

log = log_module.get(__name__)


def new_request(version: str = CLIENT_VERSION) -> dict[str, Any]:
    'TSS request skeleton (the three ``@`` metadata keys are always required by Apple clients).'
    return {
        "@HostPlatformInfo": HOST_PLATFORM,
        "@VersionInfo": version,
        "@UUID": str(uuid.uuid4()).upper(),
    }


def _number(value: Any) -> Any:
    'Manifest constants come as strings (``"0xFF10"`` / ``true``) but must be sent as numbers.'
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return value
    return value


def cryptex_request(identity: Mapping[str, Any], nonce: bytes, udid: str,
                    digests: Mapping[str, bytes]) -> dict[str, Any]:
    'Build a **cryptex DDI** TSS request (the cryptex ticket is called ``Cryptex1,Ticket``).'
    request = new_request(CRYPTEX_CLIENT_VERSION)
    request.update({
        "@BBTicket": True,
        "@Cryptex1,Ticket": True,
        "Cryptex1,Nonce": nonce,
        "Cryptex1,ProductionMode": 1,
        "Cryptex1,UniqueTagList": b"",
        "Cryptex1,UDID": b"\x00\x00\x00\x00" + bytes.fromhex(str(udid).replace("-", "")),
    })
    for key, value in identity.items():
        if str(key).startswith("Cryptex1,"):
            request[str(key)] = _number(value)
    for key, digest in digests.items():
        request[key] = {"Digest": digest}
    return request


def apply_restore_request_rules(entry: dict[str, Any], parameters: Mapping[str, Any],
                                rules: list[Any]) -> dict[str, Any]:
    "Adjust one entry by the manifest's ``RestoreRequestRules`` (Apple's condition -> action table)."
    for rule in rules:
        conditions = rule.get("Conditions") or {}
        matched = True
        for key, expected in conditions.items():
            if key in ("ApRawProductionMode", "ApCurrentProductionMode"):
                actual = parameters.get("ApProductionMode")
            elif key == "ApRawSecurityMode":
                actual = parameters.get("ApSecurityMode")
            elif key == "ApRequiresImage4":
                actual = parameters.get("ApSupportsImg4")
            elif key == "ApDemotionPolicyOverride":
                actual = parameters.get("DemotionPolicy")
            elif key == "ApInRomDFU":
                actual = parameters.get("ApInRomDFU")
            else:
                log.debug('unseen condition %s in RestoreRequestRules (treated as no match)', key)
                actual = None
            matched = bool(actual) and expected == actual
            if not matched:
                break
        if not matched:
            continue
        for key, value in (rule.get("Actions") or {}).items():
            if value == 255:
                continue
            entry.pop(key, None)
            entry[key] = value
    return entry


def pick_build_identity(build_manifest: Mapping[str, Any],
                        identifiers: Mapping[str, Any]) -> dict[str, Any]:
    'Pick the BuildIdentity matching the device BoardId / ChipID from the BuildManifest.'
    board_id = int(identifiers.get("BoardId") or 0)
    chip_id = int(identifiers.get("ChipID") or 0)
    for identity in build_manifest.get("BuildIdentities") or []:
        if int(str(identity.get("ApBoardID") or "0"), 0) == board_id \
                and int(str(identity.get("ApChipID") or "0"), 0) == chip_id:
            return identity
    raise OpenDeviceError(
        f"no matching BuildIdentity in the BuildManifest "
        f"(BoardId {board_id:#x} / ChipID {chip_id:#x}): "
        "this copied manifest does not belong to this device"
    )


def personalized_request(identifiers: Mapping[str, Any],
                         build_identity: Mapping[str, Any], nonce: bytes,
                         ecid: Optional[int] = None,
                         extra: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    'Build a TSS request that signs the personalized DDI.'
    board_id = int(identifiers.get("BoardId") or 0)
    chip_id = int(identifiers.get("ChipID") or 0)
    if ecid is None:
        ecid = int(identifiers.get("UniqueChipID") or 0)

    request = new_request()
    for key, value in identifiers.items():
        if str(key).startswith("Ap,"):
            request[str(key)] = value

    parameters: dict[str, Any] = {"ApProductionMode": True, "ApSecurityDomain": 1,
                                  "ApSecurityMode": True, "ApSupportsImg4": True}
    request.update({
        "@ApImg4Ticket": True,
        "@BBTicket": True,
        "ApBoardID": board_id,
        "ApChipID": chip_id,
        "ApECID": ecid,
        "ApNonce": nonce,
        "ApProductionMode": True,
        "ApSecurityDomain": 1,
        "ApSecurityMode": True,
        "SepNonce": b"\x00" * 20,
        "UID_MODE": False,
    })
    if extra:
        request.update(extra)

    entries = build_identity.get("Manifest") or {}
    shared_rules = (((entries.get("LoadableTrustCache") or {}).get("Info") or {})
                    .get("RestoreRequestRules")) or []
    for name, entry in entries.items():
        info = entry.get("Info")
        if info is None:
            continue
        personalize = info.get("Personalize")
        if personalize is None:
            personalize = bool(entry.get("Trusted", False))
        if not personalize:
            continue
        tss_entry = {key: value for key, value in entry.items() if key != "Info"}
        rules = info.get("RestoreRequestRules") or shared_rules
        if rules:
            tss_entry = apply_restore_request_rules(tss_entry, parameters, rules)
        if tss_entry.get("Digest") is None:
            tss_entry["Digest"] = b""
        request[name] = tss_entry
    return request


def send(request: Mapping[str, Any], timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    'Send the request and return the TSS plist response (the ticket is in ``ApImg4Ticket``).'
    body = plistlib.dumps(dict(request))
    http = urllib.request.Request(TSS_URL, data=body, headers={
        "Cache-Control": "no-cache",
        "Content-type": 'text/xml; charset="utf-8"',
        "User-Agent": "InetURL/1.0",
        "Expect": "",
    })
    log.debug('TSS -> %d bytes', len(body))
    try:
        with urllib.request.urlopen(http, timeout=timeout) as response:
            content = response.read()
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        raise OpenDeviceError(f"cannot reach Apple TSS ({TSS_URL}): {exc}") from None

    head, _, tail = content.partition(b"REQUEST_STRING=")
    if b"MESSAGE=" in head:
        message = head.split(b"MESSAGE=", 1)[1].split(b"&", 1)[0].decode("utf-8", "replace")
    else:
        message = head.decode("utf-8", "replace")
    if message != "SUCCESS":
        raise OpenDeviceError(
            f"Apple TSS rejected this request: {message.strip() or message[:200]}\n"
            '  - "isn\'t eligible for the requested build" means the BuildManifest identity does not\n'
            "    match the device (wrong BuildIdentity, or the wrong manifest)."
        )
    payload = plistlib.loads(tail) if tail else {}
    log.debug("TSS ← %s", sorted(payload.keys()))
    return dict(payload)


def img4_ticket(request: Mapping[str, Any], timeout: float = DEFAULT_TIMEOUT) -> bytes:
    'Convenience wrapper around :func:`send`: fetch that IM4M directly.'
    response = send(request, timeout)
    ticket = response.get("ApImg4Ticket")
    if not ticket:
        raise OpenDeviceError(
            f"TSS replied SUCCESS without an ApImg4Ticket: {sorted(response.keys())}")
    return bytes(ticket)
