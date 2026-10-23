from __future__ import annotations

import logging

import pytest

from opendevice.core import log, paths
from opendevice.core.errors import OpenDeviceError
from opendevice.services import (
    Afc,
    AmfiService,
    DiagnosticsRelay,
    InstallationProxy,
    MobileImageMounter,
    Service,
    SyslogRelay,
)


@pytest.fixture(autouse=True)
def _clean_log():
    log.reset()
    yield
    log.reset()


class TestRedact:
    def test_secret_keys_hidden(self) -> None:
        payload = {"HostID": "abc", "HostPrivateKey": b"\x01\x02",
                   "private_key": b"x", "DeviceCertificate": b"y", "EscrowBag": b"z"}
        safe = log.redact(payload)
        assert safe["HostID"] == "abc"
        assert safe["HostPrivateKey"] == "<hidden>"
        assert safe["private_key"] == "<hidden>"
        assert safe["DeviceCertificate"] == "<hidden>"
        assert safe["EscrowBag"] == "<hidden>"

    def test_bytes_become_length_only(self) -> None:
        assert log.redact({"mac": b"\x11\x22\x33"})["mac"] == "<3B>"

    def test_long_string_truncated(self) -> None:
        text = "x" * 500
        redacted = log.redact(text)
        assert redacted.startswith("x" * 120) and "…(500)" in redacted

    def test_nested_structures(self) -> None:
        payload = {"Display": [{"EscrowBag": b"sig"}], "keep": 1}
        safe = log.redact(payload)
        assert safe["Display"][0]["EscrowBag"] == "<hidden>"
        assert safe["keep"] == 1

    def test_does_not_touch_the_original(self) -> None:
        payload = {"private_key": b"secret"}
        log.redact(payload)
        assert payload == {"private_key": b"secret"}


class TestConfigure:
    def test_silent_by_default(self) -> None:
        'Without configuration there must be no output (NullHandler and no level set).'
        logger = log.get()
        assert any(isinstance(handler, logging.NullHandler) for handler in logger.handlers)
        assert logger.level == logging.NOTSET

    @pytest.mark.parametrize("verbose,expected", [
        (0, logging.WARNING), (1, logging.INFO), (2, logging.DEBUG), (5, logging.DEBUG)])
    def test_verbose_levels(self, verbose: int, expected: int) -> None:
        log.configure(verbose=verbose, stream=False, force=True)
        assert log.get().level == expected

    def test_writes_to_file(self, tmp_path) -> None:
        target = tmp_path / "run.log"
        log.configure(verbose=2, path=str(target), stream=False, force=True)
        log.get("services.afc").info('install starting %s', {"private_key": b"x"})
        logging.getLogger("opendevice").handlers[0].flush()
        text = target.read_text(encoding="utf-8")
        assert 'install starting' in text
        assert "<hidden>" in text and "private_key" in text

    def test_path_empty_means_our_logs_dir(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
        log.configure(verbose=1, path="", stream=False, force=True)
        assert (tmp_path / "logs" / "opendevice.log").exists()

    def test_second_configure_is_ignored_unless_forced(self) -> None:
        log.configure(verbose=1, stream=False, force=True)
        log.configure(verbose=2, stream=False)
        assert log.get().level == logging.INFO

    @pytest.mark.parametrize("value,expected", [
        ("debug", 2), ("DEBUG", 2), ("vv", 2), ("info", 1), ("v", 1), ("", 0), ("nope", 0)])
    def test_env_level(self, monkeypatch, value: str, expected: int) -> None:
        monkeypatch.setenv(log.ENV, value)
        assert log.level_from_env() == expected

    def test_get_prefixes_namespace(self) -> None:
        assert log.get("services.afc").name == "opendevice.services.afc"
        assert log.get("opendevice.core.log").name == "opendevice.core.log"
        assert log.get().name == "opendevice"


class TestLinkCoverage:
    'Which services can use Wi-Fi, pinned as a table so a change turns the test red.'

    MATRIX = {
        Afc: True, DiagnosticsRelay: True, SyslogRelay: True,
        InstallationProxy: True, MobileImageMounter: True,
        AmfiService: False,
    }

    @pytest.mark.parametrize("service,over_wifi", MATRIX.items(),
                             ids=lambda item: getattr(item, "__name__", str(item)))
    def test_matrix(self, service: type, over_wifi: bool) -> None:
        assert service.available_over_wifi() is over_wifi

    def test_every_service_shares_the_base(self) -> None:
        for service in self.MATRIX:
            assert issubclass(service, Service)
            assert service.NAME.startswith("com.apple.")

    def test_amfi_refuses_wifi_with_a_clear_error(self) -> None:
        with pytest.raises(OpenDeviceError, match='USB-only'):
            AmfiService.open(rsd=object())

    def test_open_service_without_rsd_name_says_usb_only(self) -> None:
        from opendevice.transport.service import open_service

        with pytest.raises(Exception, match='USB-only'):
            open_service(None, object(), "com.apple.amfi.lockdown")
