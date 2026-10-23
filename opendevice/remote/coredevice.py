from __future__ import annotations

import plistlib
import time
import uuid
from typing import Any, Optional

from ..core import log as log_module
from ..core.errors import OpenDeviceError
from . import xpc
from .remotexpc import RemoteXPCConnection
from .rsd import RSD

ERROR_KEYS = ("NSLocalizedDescription", "NSDebugDescription",
              "NSLocalizedFailureReason", "NSLocalizedRecoverySuggestion")


def _resolve_archive(obj: Any, objects: list[Any], depth: int = 0) -> Any:
    'Resolve references in ``$objects`` (``plistlib.UID``) into real values.'
    if depth > 8:
        return None
    if isinstance(obj, plistlib.UID):
        index = obj.data
        return (_resolve_archive(objects[index], objects, depth + 1)
                if 0 <= index < len(objects) else None)
    if isinstance(obj, dict):
        if "NS.keys" in obj and "NS.objects" in obj:
            keys = [_resolve_archive(k, objects, depth + 1) for k in obj["NS.keys"]]
            values = [_resolve_archive(v, objects, depth + 1) for v in obj["NS.objects"]]
            return dict(zip(keys, values))
        return {key: _resolve_archive(value, objects, depth + 1)
                for key, value in obj.items() if not key.startswith("$")}
    if isinstance(obj, list):
        return [_resolve_archive(item, objects, depth + 1) for item in obj]
    return obj


def archive_root(blob: bytes) -> Any:
    'Decode an NSKeyedArchive into plain Python objects (``None`` when it cannot be decoded).'
    try:
        values = plistlib.loads(blob)
    except Exception:                             # noqa
        return None
    objects = values.get("$objects") if isinstance(values, dict) else None
    if not isinstance(objects, list):
        return None
    root = (values.get("$top") or {}).get("root")
    return _resolve_archive(root, objects) if root is not None else None


def readable_bytes(blob: bytes) -> str:
    'Fallback for undecodable archives: keep readable characters only; never put binary into an error message.'
    text = "".join(ch if ch.isprintable() else " " for ch in blob.decode("utf-8", "replace"))
    return " ".join(text.split())[:300]


def archive_strings(blob: bytes) -> dict[str, str]:
    'Pull the :data:`ERROR_KEYS` keys out of an ``NSKeyedArchive`` (``bplist00``).'
    values = plistlib.loads(blob)
    objects = values.get("$objects") if isinstance(values, dict) else None
    if not isinstance(objects, list):
        return {}

    def resolve(item: Any) -> Any:
        if isinstance(item, plistlib.UID):
            index = item.data
            return objects[index] if 0 <= index < len(objects) else None
        return item

    found: dict[str, str] = {}
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for key in ERROR_KEYS:
            text = resolve(obj.get(key)) if key in obj else None
            if isinstance(text, str) and text and key not in found:
                found[key] = text
    return found


def describe_error(response: Any) -> str:
    'Turn a CoreDevice error into one readable line.'
    error = response.get("CoreDevice.error") if isinstance(response, dict) else None
    if isinstance(error, dict):
        blob = error.get("userInfoWithNSSecureCoding")
        if blob is None:
            blob = error.get("userInfo")
        if isinstance(blob, bytes):
            root = archive_root(blob)
            if isinstance(root, dict):
                for key in ERROR_KEYS:
                    if isinstance(root.get(key), str) and root[key]:
                        return root[key]
            try:
                found = archive_strings(blob)
            except Exception:                     # noqa
                found = {}
            for key in ERROR_KEYS:
                if found.get(key):
                    return found[key]
            if found:
                return next(iter(found.values()))
            return readable_bytes(blob)
        if blob is not None:
            return str(blob)[:200]
    return str(response)[:200]


SERVICE_DEVICE_INFO = "com.apple.coredevice.deviceinfo"
SERVICE_DISPLAY = "com.apple.coredevice.displayservice"
SERVICE_SCREEN_CAPTURE = "com.apple.coredevice.screencaptureservice"
SERVICE_APP = "com.apple.coredevice.appservice"

F_GET_DEVICE_INFO = "com.apple.coredevice.feature.getdeviceinfo"
F_GET_DISPLAY_INFO = "com.apple.coredevice.feature.getdisplayinfo"
F_LAUNCH_APPLICATION = "com.apple.coredevice.feature.launchapplication"
F_LIST_PROCESSES = "com.apple.coredevice.feature.listprocesses"
F_SEND_SIGNAL_TO_PROCESS = "com.apple.coredevice.feature.sendsignaltoprocess"

SIGTERM = 15
SIGKILL = 9

DEFAULT_SIGNAL_TIMEOUT = 4.0

A_MEDIASTREAM_START = "com.apple.coredevice.action.mediastreamstart"
A_MEDIASTREAM_STOP = "com.apple.coredevice.action.mediastreamstop"
A_CAPTURE_SCREENSHOT = "com.apple.coredevice.action.capturescreenshot"

DEFAULT_CORE_DEVICE_VERSION = "642.16"


def _version_dict(version: str) -> dict[str, Any]:
    parts = version.split(".")
    return {
        "components": [xpc.XpcUInt64Type(int(p)) for p in parts],
        "originalComponentsCount": xpc.XpcInt64Type(len(parts)),
        "stringValue": version,
    }


class CoreDeviceClient:
    'Caller for one CoreDevice service (one RemoteXPC connection per service).'

    def __init__(self, rsd: RSD, service_name: str,
                 core_device_version: Optional[str] = None) -> None:
        self.rsd = rsd
        self.service_name = service_name
        self.core_device_version = (core_device_version
                                    or getattr(rsd, "core_device_version", None)
                                    or DEFAULT_CORE_DEVICE_VERSION)
        self.xpc: Optional[RemoteXPCConnection] = None
        self.log = log_module.get(__name__)

    def connect(self) -> "CoreDeviceClient":
        port = self.rsd.service_port(self.service_name)
        if not self.rsd.uses_remote_xpc(self.service_name):
            raise OpenDeviceError(f"{self.service_name} is not a RemoteXPC service")
        conn = self.rsd.stack.connect(port)
        self.xpc = RemoteXPCConnection(conn)
        self.xpc.handshake()
        return self

    def close(self) -> None:
        if self.xpc is not None:
            self.xpc.close()
            self.xpc = None

    def __enter__(self) -> "CoreDeviceClient":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    def invoke(self, identifier: str, input_: Optional[dict[str, Any]] = None,
               service_name: Optional[str] = None, timeout: float = 15.0) -> Any:
        is_action = ".action." in identifier
        if service_name and service_name != self.service_name:
            self.close()
            self.service_name = service_name
            self.connect()
        if self.xpc is None:
            self.connect()

        request = {
            "CoreDevice.CoreDeviceDDIProtocolVersion": xpc.XpcInt64Type(2 if is_action else 0),
            "CoreDevice.action": {},
            "CoreDevice.coreDeviceVersion": _version_dict(self.core_device_version),
            "CoreDevice.deviceIdentifier": str(uuid.uuid4()),
            "CoreDevice.input": dict(input_ or {}),
            "CoreDevice.invocationIdentifier": str(uuid.uuid4()),
        }
        key = ("CoreDevice.actionIdentifier" if is_action
               else "CoreDevice.featureIdentifier")
        request[key] = identifier

        if self.xpc is None:
            raise OpenDeviceError('CoreDevice connection was not established (connect() failed)')
        response = self.xpc.send_receive(request, timeout)
        if "CoreDevice.output" not in response:
            raise OpenDeviceError(f"{identifier} call failed: {describe_error(response)}")
        return response["CoreDevice.output"]

    def get_device_info(self) -> dict[str, Any]:
        return self.invoke(F_GET_DEVICE_INFO, service_name=SERVICE_DEVICE_INFO)

    def get_display_info(self) -> dict[str, Any]:
        return self.invoke(F_GET_DISPLAY_INFO, service_name=SERVICE_DEVICE_INFO)

    def capture_screenshot(self, display_unique_id: Optional[str] = None,
                           image_format: str = "png", attempts: int = 3) -> bytes:
        'Take a single PNG screenshot; much cheaper than starting a video stream.'
        for attempt in range(1, max(1, attempts) + 1):
            try:
                return self._capture_screenshot(display_unique_id, image_format)
            except OpenDeviceError as exc:
                retryable = "already in progress" in str(exc)
                if not retryable or attempt >= attempts:
                    raise
                self.log.debug('screenshot is held by the previous request, retrying in %.1fs (attempt %d)',
                               0.3 * attempt, attempt)
                time.sleep(0.3 * attempt)
        raise OpenDeviceError('capturescreenshot: out of retries')

    def _capture_screenshot(self, display_unique_id: Optional[str],
                            image_format: str) -> bytes:
        output = self.invoke(A_CAPTURE_SCREENSHOT, {
            "displayUniqueID": display_unique_id,
            "requestedFormat": image_format,
        }, service_name=SERVICE_SCREEN_CAPTURE)
        if not isinstance(output, dict) or "image" not in output:
            raise OpenDeviceError(f"unexpected capturescreenshot response: {str(output)[:200]}")
        return bytes(output["image"])

    def launch_application(self, bundle_id: str, arguments: Optional[list[str]] = None,
                           environment: Optional[dict[str, str]] = None,
                           kill_existing: bool = True) -> Any:
        'Launch an app by bundle id.'
        return self.invoke(F_LAUNCH_APPLICATION, {
            "applicationSpecifier": {"bundleIdentifier": {"_0": bundle_id}},
            "options": {
                "arguments": list(arguments or []),
                "environmentVariables": dict(environment or {}),
                "standardIOUsesPseudoterminals": True,
                "startStopped": False,
                "terminateExisting": kill_existing,
                "user": {"shortName": "mobile"},
                "platformSpecificOptions": plistlib.dumps({}),
            },
            "standardIOIdentifiers": {},
        }, service_name=SERVICE_APP)

    def list_processes(self) -> list[dict[str, Any]]:
        'Current device processes (raw ``processTokens`` entries).'
        output = self.invoke(F_LIST_PROCESSES, service_name=SERVICE_APP)
        tokens = output.get("processTokens") if isinstance(output, dict) else None
        return [token for token in tokens or [] if isinstance(token, dict)]

    def send_signal_to_process(self, pid: int, signal: int = SIGKILL,
                               timeout: float = DEFAULT_SIGNAL_TIMEOUT) -> Any:
        'Send a signal to a pid (SIGKILL by default).'
        return self.invoke(F_SEND_SIGNAL_TO_PROCESS, {
            "process": {"processIdentifier": xpc.XpcInt64Type(int(pid))},
            "signal": xpc.XpcInt64Type(int(signal)),
        }, service_name=SERVICE_APP, timeout=timeout)

    def kill_process(self, pid: int, signal: int = SIGKILL,
                     timeout: float = DEFAULT_SIGNAL_TIMEOUT) -> Any:
        'Kill a process by pid.'
        try:
            return self.send_signal_to_process(pid, signal, timeout)
        except OpenDeviceError as exc:
            if 'timed out' not in str(exc):
                raise
            self.log.debug('no ack for the signal (the device most likely acted on it): pid %s', pid)
            return None

    def kill_executable(self, target: str, signal: int = SIGKILL) -> list[int]:
        'Kill processes by executable path and return the pids killed.'
        wanted = target.rstrip("/").lower()
        pids: list[int] = []
        for token in self.list_processes():
            pid = process_pid(token)
            if pid is not None and _matches(process_path(token), wanted):
                pids.append(pid)
        for pid in pids:
            self.kill_process(pid, signal)
        return pids


def _matches(path: str, wanted: str) -> bool:
    'Path equals ``wanted``, or its last component does (both case-insensitive).'
    if not wanted:
        return False
    path = path.rstrip("/").lower()
    return path == wanted or path.rsplit("/", 1)[-1] == wanted


def process_pid(token: dict[str, Any]) -> Optional[int]:
    'Extract the pid from a processToken.'
    value = token.get("processIdentifier")
    if isinstance(value, dict):
        value = value.get("processIdentifier")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def process_path(token: dict[str, Any]) -> str:
    'Extract the executable path from a processToken (strips the ``file://`` prefix).'
    url = token.get("executableURL")
    if isinstance(url, dict):
        url = url.get("relative") or url.get("_0")
    if not isinstance(url, str):
        return ""
    return url[len("file://"):] if url.startswith("file://") else url


def process_name(token: dict[str, Any]) -> str:
    'Executable name (last component of the path).'
    return process_path(token).rsplit("/", 1)[-1]
