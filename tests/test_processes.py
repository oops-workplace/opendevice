from __future__ import annotations

import pytest

from opendevice.core import log as log_module
from opendevice.core.errors import OpenDeviceError
from opendevice.remote import coredevice
from opendevice.services.installation import executable_path

SAFARI = ("/private/var/containers/Bundle/Application/EXAMPLE/MobileSafari.app"
          "/MobileSafari")


def _token(pid: int, path: str) -> dict:
    return {"processIdentifier": pid, "executableURL": {"relative": f"file://{path}"}}


class _FakeCore(coredevice.CoreDeviceClient):
    'A CoreDeviceClient that never connects: feeds a process list and records killed pids.'

    def __init__(self, tokens: list[dict]) -> None:
        self.tokens = tokens
        self.killed: list[int] = []

    def list_processes(self) -> list[dict]:
        return self.tokens

    def kill_process(self, pid: int, signal: int = coredevice.SIGKILL,
                     timeout: float = 0.0) -> None:
        self.killed.append(pid)


def test_process_fields_come_from_the_real_token_shape() -> None:
    token = _token(2421, SAFARI)
    assert coredevice.process_pid(token) == 2421
    assert coredevice.process_path(token) == SAFARI
    assert coredevice.process_name(token) == "MobileSafari"


def test_missing_fields_read_as_empty() -> None:
    assert coredevice.process_pid({}) is None
    assert coredevice.process_path({}) == ""
    assert coredevice.process_name({}) == ""


def test_kill_executable_matches_full_path() -> None:
    core = _FakeCore([_token(2421, SAFARI), _token(37, "/System/Library/CoreServices"
                                                       "/SpringBoard.app/SpringBoard")])
    assert core.kill_executable(SAFARI) == [2421]
    assert core.killed == [2421]


def test_kill_executable_matches_last_segment_case_insensitively() -> None:
    core = _FakeCore([_token(2421, SAFARI)])
    assert core.kill_executable("mobilesafari") == [2421]


def test_kill_executable_hits_every_process_of_one_app() -> None:
    core = _FakeCore([_token(2421, SAFARI), _token(2422, SAFARI)])
    assert core.kill_executable("MobileSafari") == [2421, 2422]


def test_kill_executable_reports_nothing_when_not_running() -> None:
    core = _FakeCore([_token(37, "/System/Library/CoreServices/SpringBoard.app/SpringBoard")])
    assert core.kill_executable("MobileSafari") == []


class _SignalCore(coredevice.CoreDeviceClient):
    'Only stub the "send signal" step; ``kill_process`` runs the real code.'

    def __init__(self, error: str) -> None:
        self.error = error
        self.log = log_module.get(__name__)

    def send_signal_to_process(self, pid: int, signal: int = coredevice.SIGKILL,
                               timeout: float = 0.0) -> None:
        raise OpenDeviceError(self.error)


def test_kill_process_takes_silence_as_done() -> None:
    'A device that acts but never replies (that is iOS 27) is not a failure.'
    assert _SignalCore('timed out waiting for the XPC response').kill_process(2421) is None


def test_kill_process_still_raises_real_errors() -> None:
    with pytest.raises(OpenDeviceError):
        _SignalCore('no such process').kill_process(2421)


def test_executable_path_joins_path_and_bundle_executable() -> None:
    app = {"Path": "/private/var/containers/Bundle/Application/EXAMPLE/MobileSafari.app",
           "CFBundleExecutable": "MobileSafari"}
    assert executable_path(app) == SAFARI


def test_executable_path_is_empty_when_installation_does_not_say() -> None:
    assert executable_path({}) == ""
    assert executable_path({"Path": "/x.app"}) == ""
