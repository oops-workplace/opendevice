from __future__ import annotations

import pytest

from opendevice.core.errors import OpenDeviceError
from opendevice.remote.tunnel import TunnelHandle, open_rsd


class _Recorder:
    'Record names in call order; a name inside ``fail`` raises (tests cleanup resilience).'

    def __init__(self, name: str, log: list, fail: set = frozenset(),
                 stack: "object | None" = None) -> None:
        self._name = name
        self._log = log
        self._fail = fail
        self.stack = stack

    def close(self) -> None:
        self._log.append(self._name)
        if self._name in self._fail:
            raise RuntimeError(f"{self._name} deliberate failure")


def _handle(fail: set = frozenset()) -> tuple[TunnelHandle, list]:
    log: list = []
    stack = _Recorder("rsd.stack", log, fail)
    return (TunnelHandle(rsd=_Recorder("rsd", log, fail, stack=stack),
                         tunnel=_Recorder("tunnel", log, fail),
                         lockdown=_Recorder("lockdown", log, fail)),
            log)


def test_close_order_is_rsd_then_tunnel_then_stack_then_lockdown() -> None:
    'The wrong order leaves the reader thread stuck on a dead socket; pinned.'
    handle, log = _handle()
    handle.close()
    assert log == ["rsd", "tunnel", "rsd.stack", "lockdown"]


def test_close_keeps_going_when_one_step_fails() -> None:
    'One failing step during cleanup must not stop the rest (otherwise lockdown leaks and the device session stays).'
    handle, log = _handle(fail={"tunnel"})
    handle.close()
    assert "tunnel" in log and "lockdown" in log


def test_close_is_idempotent() -> None:
    handle, log = _handle()
    handle.close()
    handle.close()
    assert log == ["rsd", "tunnel", "rsd.stack", "lockdown"]
    assert handle.rsd is None and handle.tunnel is None and handle.lockdown is None


def test_unknown_backend_names_the_two_valid_ones() -> None:
    with pytest.raises(OpenDeviceError, match="userspace.*network"):
        open_rsd(backend='bluetooth')


def test_open_rsd_is_exported_from_the_package() -> None:
    'Downstream should only ``from opendevice.remote import open_rsd``, never assemble lockdown + tunnel by hand.'
    import opendevice

    assert callable(opendevice.remote.tunnel.open_rsd)
    assert "open_rsd" in opendevice.remote.__all__
    assert "TunnelHandle" in opendevice.remote.__all__
