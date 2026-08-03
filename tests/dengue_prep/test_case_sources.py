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


def _mock_stream_response(ndjson_lines: list[str], status: int = 200) -> MagicMock:
    """Mock a streaming ``requests.get`` used as a context manager with iter_lines."""
    m = MagicMock()
    m.status_code = status
    m.raise_for_status = MagicMock()
    m.iter_lines = MagicMock(return_value=iter(ndjson_lines))
    m.__enter__ = MagicMock(return_value=m)
    m.__exit__ = MagicMock(return_value=False)
    return m


# Two minimal IHIP-shaped records for streaming tests. Field names match what
# the parser expects; values don't have to be realistic since we only assert
# on the wrapper (stage-and-count), not the content itself.
_FAKE_STREAM_LINES = [
    '{"Region Id": "district_524", "Date Of Onset": "2026-01-15", "Test Suspected For": "Dengue"}',
    '{"Region Id": "district_525", "Date Of Onset": "2026-02-10", "Test Suspected For": "Dengue"}',
]


@patch.dict("os.environ", _DASHBOARD_ENV, clear=False)
def test_dashboard_source_logs_in_and_stages_xlsx(tmp_path: Path) -> None:
    import pandas as pd

    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post.return_value = _mock_json_response({"access_token": "T0K3N"})
        mock_get.return_value = _mock_stream_response(_FAKE_STREAM_LINES)

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
    # File was materialised as a real xlsx from the NDJSON stream; parseable
    # and contains one row per NDJSON line.
    df = pd.read_excel(staged)
    assert len(df) == len(_FAKE_STREAM_LINES)
    assert set(df.columns) >= {"Region Id", "Date Of Onset", "Test Suspected For"}

    # /api/auth/login called with email/password (env vars mapped)
    post_call = mock_post.call_args
    assert post_call.args[0].endswith("/api/auth/login")
    assert post_call.kwargs["json"] == {
        "email": "operator@example.com",
        "password": "hunter2",
    }

    # /api/cases/export/stream called with bearer + from/to + stream=True
    get_call = mock_get.call_args
    assert get_call.args[0].endswith("/api/cases/export/stream")
    assert get_call.kwargs["headers"]["Authorization"] == "Bearer T0K3N"
    assert get_call.kwargs["params"]["from"] == "2026-01-01"
    assert get_call.kwargs["params"]["to"] == "2026-06-30"
    assert get_call.kwargs["stream"] is True


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
def test_dashboard_empty_stream_stages_empty_xlsx(tmp_path: Path) -> None:
    """Empty NDJSON stream = valid 'no cases in window' — stage an empty xlsx.

    The ihip parser recognises header-only files and skips them cleanly, so
    the caller shouldn't treat an empty window as an error.
    """
    import pandas as pd

    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post.return_value = _mock_json_response({"access_token": "T0K3N"})
        mock_get.return_value = _mock_stream_response([])
        source = load_source(
            "dashboard",
            {"source_path": str(tmp_path), "date_start": "2026-01-01"},
        )
        listed = source.list_objects()

    assert len(listed) == 1
    staged = tmp_path / listed[0]
    assert staged.exists()
    # Empty NDJSON → empty DataFrame → header-only xlsx (readable, zero rows).
    df = pd.read_excel(staged)
    assert len(df) == 0


@patch.dict("os.environ", _DASHBOARD_ENV, clear=False)
def test_dashboard_invalid_ndjson_raises(tmp_path: Path) -> None:
    """Non-NDJSON body (e.g. HTML error page bled through) → clear failure."""
    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post.return_value = _mock_json_response({"access_token": "T0K3N"})
        mock_get.return_value = _mock_stream_response(["<html>error</html>"])
        source = load_source(
            "dashboard",
            {"source_path": str(tmp_path), "date_start": "2026-01-01"},
        )
        with pytest.raises(RuntimeError, match="unparseable NDJSON"):
            source.list_objects()


# ---------------------------------------------------------------------------
# Incremental / backfill behaviour
# ---------------------------------------------------------------------------


@patch.dict("os.environ", _DASHBOARD_ENV, clear=False)
def test_dashboard_first_run_fetches_full_range(tmp_path: Path) -> None:
    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post.return_value = _mock_json_response({"access_token": "T0K3N"})
        mock_get.return_value = _mock_bytes_response(_FAKE_XLSX)
        source = load_source(
            "dashboard",
            {
                "source_path": str(tmp_path),
                "date_start": "2026-01-01",
                "date_end": "2026-06-30",
                "backfill_days": 30,
            },
        )
        source.list_objects()

    get_params = mock_get.call_args.kwargs["params"]
    assert get_params["from"] == "2026-01-01"
    assert get_params["to"] == "2026-06-30"


@patch.dict("os.environ", _DASHBOARD_ENV, clear=False)
def test_dashboard_incremental_fetch_uses_backfill_window(tmp_path: Path) -> None:
    # Pre-seed a "previous run" staged file covering Jan 1 → Jun 1.
    (tmp_path / "dashboard_2026-01-01_to_2026-06-01.xlsx").write_bytes(_FAKE_XLSX)

    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post.return_value = _mock_json_response({"access_token": "T0K3N"})
        mock_get.return_value = _mock_bytes_response(_FAKE_XLSX)
        source = load_source(
            "dashboard",
            {
                "source_path": str(tmp_path),
                "date_start": "2026-01-01",
                "date_end": "2026-06-30",
                "backfill_days": 30,
            },
        )
        listed = source.list_objects()

    # New fetch should cover latest_end (Jun 1) - 29 days = May 3 → Jun 30.
    get_params = mock_get.call_args.kwargs["params"]
    assert get_params["from"] == "2026-05-03"
    assert get_params["to"] == "2026-06-30"

    # The old Jan 1 → Jun 1 file PARTIALLY overlaps the new May 3 → Jun 30
    # fetch but is NOT fully contained in it (existing_start=Jan 1 <
    # fetch_from=May 3). Per the safer contained-only delete policy, it is
    # kept — data loss on the Jan–May portion would otherwise be permanent
    # if the new fetch fails.
    assert set(listed) == {
        "dashboard_2026-01-01_to_2026-06-01.xlsx",
        "dashboard_2026-05-03_to_2026-06-30.xlsx",
    }
    assert (tmp_path / "dashboard_2026-01-01_to_2026-06-01.xlsx").exists()


@patch.dict("os.environ", _DASHBOARD_ENV, clear=False)
def test_dashboard_non_overlapping_older_files_preserved(tmp_path: Path) -> None:
    # An older file that does NOT overlap the backfill window.
    (tmp_path / "dashboard_2024-01-01_to_2024-12-31.xlsx").write_bytes(_FAKE_XLSX)
    # A recent file that DOES overlap.
    (tmp_path / "dashboard_2026-05-01_to_2026-06-15.xlsx").write_bytes(_FAKE_XLSX)

    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post.return_value = _mock_json_response({"access_token": "T0K3N"})
        mock_get.return_value = _mock_bytes_response(_FAKE_XLSX)
        source = load_source(
            "dashboard",
            {
                "source_path": str(tmp_path),
                "date_start": "2024-01-01",
                "date_end": "2026-06-30",
                "backfill_days": 30,
            },
        )
        listed = source.list_objects()

    # Fetch window: latest_end (Jun 15) - 29 days = May 17 → Jun 30.
    get_params = mock_get.call_args.kwargs["params"]
    assert get_params["from"] == "2026-05-17"

    # Old 2024 file is preserved (no overlap at all). The May 1 → Jun 15 file
    # partially overlaps the new May 17 → Jun 30 fetch but its start extends
    # BEFORE the fetch window — so under the safer contained-only delete
    # policy it is kept (its May 1–16 rows would otherwise be permanently
    # lost if the new fetch failed).
    assert (tmp_path / "dashboard_2024-01-01_to_2024-12-31.xlsx").exists()
    assert (tmp_path / "dashboard_2026-05-01_to_2026-06-15.xlsx").exists()
    # New gap-fill behavior: the coverage has a hole between 2024-12-31 and
    # 2026-05-01 (~16 months). The chunked download splits it into
    # ~365-day chunks: 2025-01-01→2025-12-31 and 2026-01-01→2026-04-30.
    assert set(listed) == {
        "dashboard_2024-01-01_to_2024-12-31.xlsx",
        "dashboard_2025-01-01_to_2025-12-31.xlsx",
        "dashboard_2026-01-01_to_2026-04-30.xlsx",
        "dashboard_2026-05-01_to_2026-06-15.xlsx",
        "dashboard_2026-05-17_to_2026-06-30.xlsx",
    }


@patch.dict("os.environ", _DASHBOARD_ENV, clear=False)
def test_dashboard_backfill_zero_only_fetches_new(tmp_path: Path) -> None:
    """backfill_days=0 = fetch only the day after latest_end → date_end."""
    (tmp_path / "dashboard_2026-01-01_to_2026-06-01.xlsx").write_bytes(_FAKE_XLSX)

    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post.return_value = _mock_json_response({"access_token": "T0K3N"})
        mock_get.return_value = _mock_bytes_response(_FAKE_XLSX)
        source = load_source(
            "dashboard",
            {
                "source_path": str(tmp_path),
                "date_start": "2026-01-01",
                "date_end": "2026-06-30",
                "backfill_days": 0,
            },
        )
        source.list_objects()

    # backfill_days=0 means "no tail re-fetch". The gap after latest_end
    # (Jun 1) is [Jun 2, Jun 30]. So fetch_from = 2026-06-02.
    get_params = mock_get.call_args.kwargs["params"]
    assert get_params["from"] == "2026-06-02"


@patch.dict("os.environ", _DASHBOARD_ENV, clear=False)
def test_dashboard_invalid_backfill_days_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="backfill_days must be an integer"):
        load_source(
            "dashboard",
            {
                "source_path": str(tmp_path),
                "date_start": "2026-01-01",
                "backfill_days": "not-an-int",
            },
        )
