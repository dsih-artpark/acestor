"""Case-source plugin architecture — resolver + filesystem source."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
from unittest.mock import MagicMock, patch

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


# ---------------------------------------------------------------------------
# Dashboard source (OAuth client-credentials, mocked HTTP)
# ---------------------------------------------------------------------------

_DASHBOARD_ENV = {
    "DASHBOARD_CLIENT_ID": "operator@example.com",
    "DASHBOARD_CLIENT_SECRET": "hunter2",
    "DASHBOARD_URL": "https://dashboard.example.com",
}

# XLSX magic bytes ("PK" at start of a ZIP archive) — enough to pass the source's
# XLSX sanity check without building a real workbook.
_FAKE_XLSX = b"PK\x03\x04" + b"\x00" * 128


def _mock_json_response(json_body: dict, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.raise_for_status = MagicMock()
    m.json = MagicMock(return_value=json_body)
    return m


def _mock_bytes_response(content: bytes, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.raise_for_status = MagicMock()
    m.content = content
    return m


@patch.dict("os.environ", _DASHBOARD_ENV, clear=False)
def test_dashboard_source_logs_in_and_stages_xlsx(tmp_path: Path) -> None:
    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post.return_value = _mock_json_response({"access_token": "T0K3N"})
        mock_get.return_value = _mock_bytes_response(_FAKE_XLSX)

        source = load_source(
            "dashboard",
            {
                "source_path": str(tmp_path / "staging"),
                "date_start": "2026-01-01",
                "date_end": "2026-06-30",
            },
        )
        listed = source.list_objects()

    assert listed == ["dashboard_2026-01-01_to_2026-06-30.xlsx"]
    staged = tmp_path / "staging" / listed[0]
    assert staged.read_bytes() == _FAKE_XLSX

    # /api/auth/login called with email/password (env vars mapped)
    post_call = mock_post.call_args
    assert post_call.args[0].endswith("/api/auth/login")
    assert post_call.kwargs["json"] == {
        "email": "operator@example.com",
        "password": "hunter2",
    }

    # /api/cases/export.xlsx called with bearer + from/to
    get_call = mock_get.call_args
    assert get_call.args[0].endswith("/api/cases/export.xlsx")
    assert get_call.kwargs["headers"]["Authorization"] == "Bearer T0K3N"
    assert get_call.kwargs["params"]["from"] == "2026-01-01"
    assert get_call.kwargs["params"]["to"] == "2026-06-30"


@patch.dict("os.environ", {}, clear=True)
def test_dashboard_missing_credentials_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="DASHBOARD_"):
        load_source(
            "dashboard",
            {
                "base_url": "https://x",
                "source_path": str(tmp_path),
                "date_start": "2026-01-01",
            },
        )


@patch.dict("os.environ", _DASHBOARD_ENV, clear=False)
def test_dashboard_missing_date_start_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="date_start is required"):
        load_source("dashboard", {"source_path": str(tmp_path)})


@patch.dict("os.environ", _DASHBOARD_ENV, clear=False)
def test_dashboard_empty_response_raises(tmp_path: Path) -> None:
    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post.return_value = _mock_json_response({"access_token": "T0K3N"})
        mock_get.return_value = _mock_bytes_response(b"")
        source = load_source(
            "dashboard",
            {"source_path": str(tmp_path), "date_start": "2026-01-01"},
        )
        with pytest.raises(RuntimeError, match="zero bytes"):
            source.list_objects()


@patch.dict("os.environ", _DASHBOARD_ENV, clear=False)
def test_dashboard_non_xlsx_response_raises(tmp_path: Path) -> None:
    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post.return_value = _mock_json_response({"access_token": "T0K3N"})
        mock_get.return_value = _mock_bytes_response(b"<html>error</html>")
        source = load_source(
            "dashboard",
            {"source_path": str(tmp_path), "date_start": "2026-01-01"},
        )
        with pytest.raises(RuntimeError, match="non-XLSX bytes"):
            source.list_objects()
