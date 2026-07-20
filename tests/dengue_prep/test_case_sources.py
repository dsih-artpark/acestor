"""Case-source plugin architecture — resolver + filesystem source."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from pipelines.dengue_prep.lib.case_sources import CaseSource, load_source
from pipelines.dengue_prep.lib.case_sources.filesystem import Source as FilesystemSource


class _FakeSource(CaseSource):
    @classmethod
    def build(cls, config: Mapping[str, Any]) -> "_FakeSource":
        return cls()

    def list_objects(self, prefix: str = "") -> list[str]:
        return ["a.csv", "b.csv"]

    def read(self, path: str) -> bytes:
        return b"row1\nrow2\n"


def test_filesystem_source_lists_and_reads(tmp_path: Path) -> None:
    (tmp_path / "cases_1.csv").write_bytes(b"header\nrow1\n")
    (tmp_path / "cases_2.csv").write_bytes(b"header\nrow2\n")

    source = load_source("filesystem", {"source_path": str(tmp_path)})

    assert isinstance(source, CaseSource)
    listed = source.list_objects("")
    assert sorted(str(Path(p).name) for p in listed) == ["cases_1.csv", "cases_2.csv"]
    # read() returns bytes
    payload = source.read(listed[0])
    assert isinstance(payload, bytes) and len(payload) > 0


def test_filesystem_source_missing_config_raises() -> None:
    with pytest.raises(ValueError, match="no directory configured"):
        FilesystemSource.build({})


def test_load_source_by_file_path(tmp_path: Path) -> None:
    plugin_path = tmp_path / "my_source.py"
    plugin_path.write_text(
        "from pipelines.dengue_prep.lib.case_sources import CaseSource\n"
        "class Source(CaseSource):\n"
        "    @classmethod\n"
        "    def build(cls, config):\n"
        "        return cls()\n"
        "    def list_objects(self, prefix=''):\n"
        "        return ['ext.csv']\n"
        "    def read(self, path):\n"
        "        return b'ext-payload'\n"
    )
    source = load_source(str(plugin_path), {})
    assert source.list_objects() == ["ext.csv"]
    assert source.read("ext.csv") == b"ext-payload"


def test_load_source_unknown_builtin_raises() -> None:
    with pytest.raises((ImportError, ModuleNotFoundError)):
        load_source("nonexistent_source", {})


def test_load_source_module_without_source_class_raises(tmp_path: Path) -> None:
    plugin_path = tmp_path / "no_source.py"
    plugin_path.write_text("# empty — no Source class\n")
    with pytest.raises(ImportError, match="must expose a class named 'Source'"):
        load_source(str(plugin_path), {})


def test_load_source_source_not_subclass_raises(tmp_path: Path) -> None:
    plugin_path = tmp_path / "bad_source.py"
    plugin_path.write_text("class Source:\n    pass\n")
    with pytest.raises(TypeError, match="must be a subclass of CaseSource"):
        load_source(str(plugin_path), {})
