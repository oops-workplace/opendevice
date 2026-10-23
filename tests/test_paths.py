from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from opendevice.core import paths
from opendevice.services import mounter
from opendevice.wireless import rrp


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    'Point OPENDEVICE_HOME at a temp dir so tests never touch real user data.'
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
    return tmp_path


class TestLocations:
    def test_env_home_wins(self, tmp_path) -> None:
        assert paths.home() == tmp_path
        assert paths.root() == tmp_path
        assert paths.records_dir() == tmp_path

    def test_default_is_dot_opendevice(self, monkeypatch) -> None:
        'Without the override everything lives under ``~/.opendevice``.'
        monkeypatch.delenv(paths.ENV_HOME, raising=False)
        assert paths.home() is None
        assert paths.root() == Path.home() / ".opendevice"
        assert paths.records_dir() == paths.root()

    def test_layout(self, tmp_path) -> None:
        'Records sit at the top level; the rest get their own sub-directory.'
        assert paths.records_dir() == tmp_path
        assert paths.ddi_dir() == tmp_path / "ddi"
        assert paths.ddi_dir("ExampleProduct,1") == tmp_path / "ddi" / "ExampleProduct,1"
        assert paths.ddi_dir("ExampleProduct,1", "27.0") == \
            tmp_path / "ddi" / "ExampleProduct,1" / "27.0"
        assert paths.logs_dir() == tmp_path / "logs"
        assert paths.tmp_dir() == tmp_path / "tmp"
        assert paths.locks_dir() == tmp_path / "locks"

    def test_ensure_creates(self, tmp_path) -> None:
        target = paths.ddi_dir("ExampleProduct,1")
        assert not target.is_dir()
        assert paths.ensure(target) == target and target.is_dir()

    def test_lock_file_is_created_inside_locks(self, tmp_path) -> None:
        path = paths.lock_file("00008140-0001")
        assert path.parent == tmp_path / "locks"
        assert path.name == "00008140-0001.lock" and path.parent.is_dir()


class TestRecordDirWiring:
    def test_save_goes_to_the_root_then_loads(self) -> None:
        record = rrp.RemotePairRecord(identifier="EXAMPLE-0001",
                                      public_key=b"p" * 32, private_key=b"s" * 32)
        path = record.save()
        assert Path(path).parent == paths.root()
        assert Path(path).name == "remote_EXAMPLE-0001.plist"

        loaded = rrp.RemotePairRecord.load("EXAMPLE-0001")
        assert loaded is not None and loaded.private_key == b"s" * 32

    def test_load_any_sees_the_root(self) -> None:
        paths.root().mkdir(parents=True, exist_ok=True)
        (paths.root() / "remote_XXX.plist").write_bytes(
            plistlib.dumps({"public_key": b"p" * 32, "private_key": b"s" * 32}))
        found = [item.identifier for item in rrp.RemotePairRecord.load_any()]
        assert found == ["XXX"]


class TestUnpair:
    'Delete local pairing records (this host\'s side of Unpair) - files only, no device, no network.'

    @staticmethod
    def _make(directory: Path, name: str, payload: "dict | None" = None) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"remote_{name}.plist"
        path.write_bytes(plistlib.dumps(payload or {"private_key": b"s" * 32}))
        return path

    def test_removes_only_the_named_device(self) -> None:
        keep = self._make(paths.records_dir(), "EXAMPLE-0002")
        gone = self._make(paths.records_dir(), "EXAMPLE-0001")
        assert paths.unpair(identifier="EXAMPLE-0001") == [gone]
        assert not gone.exists()
        assert keep.is_file()

    def test_unpair_all_sweeps_every_record(self) -> None:
        first = self._make(paths.records_dir(), "AAA")
        second = self._make(paths.records_dir(), "BBB")
        assert paths.unpair(all_records=True) == [first, second]
        assert not first.exists() and not second.exists()

    def test_missing_record_is_noop(self) -> None:
        assert paths.unpair(identifier="nope") == []
        assert paths.unpair(all_records=True) == []

    def test_corrupt_record_is_still_removed(self) -> None:
        'Unparsable records must still be deletable - delete by path, not via ``RemotePairRecord.load``.'
        path = self._make(paths.records_dir(), "BROKEN")
        path.write_bytes(b"not a plist at all")
        assert paths.unpair(identifier="BROKEN") == [path]
        assert not path.exists()

    def test_requires_exactly_one_mode(self) -> None:
        with pytest.raises(ValueError):
            paths.unpair()
        with pytest.raises(ValueError):
            paths.unpair(identifier="x", all_records=True)

    def test_record_paths_lists_glob_sorted(self) -> None:
        self._make(paths.records_dir(), "BBB")
        self._make(paths.records_dir(), "AAA")
        assert [item.name for item in paths.record_paths(all_records=True)] == \
            ["remote_AAA.plist", "remote_BBB.plist"]

    def test_record_paths_without_identifier_does_not_touch_disk(self) -> None:
        'With an identifier only paths are computed; the directory need not exist.'
        found = paths.record_paths(identifier="EXAMPLE-0001")
        assert [item.name for item in found] == ["remote_EXAMPLE-0001.plist"]
        assert not any(item.exists() for item in found)


class TestUnpairCommand:
    '``opendevice unpairing`` end to end (temp home only, no device).'

    @staticmethod
    def _run(argv: "list[str]") -> int:
        from opendevice.cli import main
        return main(argv)

    @staticmethod
    def _make(name: str) -> Path:
        paths.records_dir().mkdir(parents=True, exist_ok=True)
        path = paths.records_dir() / f"remote_{name}.plist"
        path.write_bytes(plistlib.dumps({"private_key": b"s" * 32}))
        return path

    def test_deletes_the_named_device(self) -> None:
        path = self._make("EXAMPLE-0001")
        assert self._run(["unpairing", "-d", "EXAMPLE-0001"]) == 0
        assert not path.exists()

    def test_single_record_needs_no_device(self) -> None:
        path = self._make("EXAMPLE-0001")
        assert self._run(["unpairing"]) == 0
        assert not path.exists()

    def test_several_records_need_a_device(self) -> None:
        first = self._make("AAA")
        second = self._make("BBB")
        assert self._run(["unpairing"]) == 1
        assert first.is_file() and second.is_file()

    def test_all_flag_sweeps(self) -> None:
        first = self._make("AAA")
        second = self._make("BBB")
        assert self._run(["unpairing", "--all"]) == 0
        assert not first.exists() and not second.exists()

    def test_nothing_to_do_is_not_an_error(self) -> None:
        assert self._run(["unpairing"]) == 0
        assert self._run(["unpairing", "--all"]) == 0


class TestDescribe:
    def test_shape(self, tmp_path) -> None:
        info = paths.describe()
        assert info["home_override"] == str(tmp_path)
        assert info["root"] == str(tmp_path)
        assert info["records_count"] == 0
        assert info["ddi"] == str(tmp_path / "ddi")
        assert info["logs"] == str(tmp_path / "logs")
        assert any("Apple" in str(item) or "Lockdown" in str(item)
                   for item in info["apple_usb_records"])

    def test_counts_records_and_locks(self) -> None:
        paths.ensure(paths.root())
        (paths.root() / "remote_AAA.plist").write_bytes(plistlib.dumps({"private_key": b"k"}))
        paths.ensure(paths.locks_dir())
        (paths.locks_dir() / "AAA.lock").write_bytes(b"")
        info = paths.describe()
        assert info["records_count"] == 1
        assert info["locks_count"] == 1


class TestMounterCache:
    def test_image_cache_under_ddi(self, tmp_path) -> None:
        assert mounter.cache_dir() == tmp_path / "ddi"
        assert mounter.cache_dir("ExampleProduct,1", "27.0") == \
            tmp_path / "ddi" / "ExampleProduct,1" / "27.0"

    def test_cached_image_prefers_the_most_specific_dir(self, tmp_path) -> None:
        'Prefer the one with a product/OS sub-directory (one image matches one build).'
        paths.ensure(mounter.cache_dir("ExampleProduct,1", "ExampleBuild1"))
        specific = mounter.cache_dir("ExampleProduct,1", "ExampleBuild1") / "iOS_DDI.dmg"
        specific.write_bytes(b"dmg")
        paths.ensure(mounter.cache_dir())
        (mounter.cache_dir() / "other.dmg").write_bytes(b"other")
        assert mounter.cached_image("ExampleProduct,1", "ExampleBuild1") == specific
