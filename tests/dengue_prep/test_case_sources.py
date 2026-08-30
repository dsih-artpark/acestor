"""Case-source plugin architecture — resolver + filesystem source."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
from unittest.mock import MagicMock, patch

import pandas as pd
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
# the parser expects. Sample Collected Date is the same column the source
# uses to shard fresh rows by year, so the values here matter for the year
# each row lands in.
_FAKE_STREAM_LINES = [
    (
        '{"Region Id": "district_524", "Date Of Onset": "2026-01-15", '
        '"Sample Collected Date": "2026-01-15", "Test Suspected For": "Dengue"}'
    ),
    (
        '{"Region Id": "district_525", "Date Of Onset": "2026-02-10", '
        '"Sample Collected Date": "2026-02-10", "Test Suspected For": "Dengue"}'
    ),
]


def _write_year_shard(path: Path, rows: list[dict[str, Any]]) -> None:
    """Helper for tests that need to pre-seed a real year-shard file."""
    import pandas as pd  # noqa: PLC0415

    pd.DataFrame(rows).to_csv(path, index=False)


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

    # Year-sharded staging: both stream records fall in 2026 (Sample
    # Collected Date), so they land in a single dashboard_2026.csv shard.
    assert listed == ["dashboard_2026.csv"]
    staged = tmp_path / "staging" / listed[0]
    df = pd.read_csv(staged)
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
def test_dashboard_empty_stream_writes_no_shard(tmp_path: Path) -> None:
    """Empty NDJSON stream = no rows to shard by year → no shard file
    written. list_objects() returns []; the parser sees nothing and skips
    the source cleanly (existing empty-file semantics).
    """
    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post.return_value = _mock_json_response({"access_token": "T0K3N"})
        mock_get.return_value = _mock_stream_response([])
        source = load_source(
            "dashboard",
            {"source_path": str(tmp_path), "date_start": "2026-01-01"},
        )
        listed = source.list_objects()

    assert listed == []
    # No shard files on disk either.
    assert list(tmp_path.glob("dashboard_*.csv")) == []


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
    """Second-run behaviour: with a year shard already on disk, the daily
    refresh should fetch only ``[date_end - backfill_days + 1, date_end]``
    — not the full history."""
    # Pre-seed a year shard so the source treats this as an incremental run.
    _write_year_shard(
        tmp_path / "dashboard_2026.csv",
        [{"Region Id": "district_524", "Sample Collected Date": "2026-01-15"}],
    )

    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post.return_value = _mock_json_response({"access_token": "T0K3N"})
        # Stream that keeps the shard non-empty post-fetch (so downstream
        # asserts have something to look at).
        mock_get.return_value = _mock_stream_response(
            [
                (
                    '{"Region Id": "district_525", '
                    '"Sample Collected Date": "2026-06-15", '
                    '"Test Suspected For": "Dengue"}'
                )
            ]
        )
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

    # Fetch window = date_end (Jun 30) - 29 days = Jun 1 → Jun 30.
    get_params = mock_get.call_args.kwargs["params"]
    assert get_params["from"] == "2026-06-01"
    assert get_params["to"] == "2026-06-30"

    # Still exactly one shard on disk (2026), with pre-cutoff row kept and
    # the fresh Jun 15 row appended.
    assert listed == ["dashboard_2026.csv"]
    df = pd.read_csv(tmp_path / "dashboard_2026.csv")
    assert set(df["Sample Collected Date"]) == {"2026-01-15", "2026-06-15"}


@patch.dict("os.environ", _DASHBOARD_ENV, clear=False)
def test_dashboard_historical_year_shards_are_not_re_read(tmp_path: Path) -> None:
    """Year-sharded design invariant: a daily backfill run only touches the
    year(s) intersected by the ``[cutoff, date_end]`` window. Older year
    shards (e.g. 2024) are cold storage and MUST NOT be rewritten. Their
    mtime should be unchanged after the run, proving the source didn't
    even read them (let alone write)."""
    import os
    import time

    # Pre-seed a historical shard (2024) and a current-year shard (2026).
    old_shard = tmp_path / "dashboard_2024.csv"
    cur_shard = tmp_path / "dashboard_2026.csv"
    _write_year_shard(
        old_shard, [{"Sample Collected Date": "2024-07-15", "Region Id": "district_1"}]
    )
    _write_year_shard(
        cur_shard, [{"Sample Collected Date": "2026-01-10", "Region Id": "district_2"}]
    )
    # Back-date the mtimes so we can detect a rewrite as an mtime change.
    long_ago = time.time() - 3600
    os.utime(old_shard, (long_ago, long_ago))
    os.utime(cur_shard, (long_ago, long_ago))
    old_mtime = old_shard.stat().st_mtime

    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post.return_value = _mock_json_response({"access_token": "T0K3N"})
        mock_get.return_value = _mock_stream_response(
            [
                (
                    '{"Region Id": "district_3", '
                    '"Sample Collected Date": "2026-06-15", '
                    '"Test Suspected For": "Dengue"}'
                )
            ]
        )
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

    # Fetch window scoped to backfill: 30 days ending Jun 30.
    get_params = mock_get.call_args.kwargs["params"]
    assert get_params["from"] == "2026-06-01"
    assert get_params["to"] == "2026-06-30"

    # Both shards still listed.
    assert set(listed) == {"dashboard_2024.csv", "dashboard_2026.csv"}

    # 2024 shard MUST be byte-identical / mtime-unchanged (cold storage
    # invariant — this is the whole point of year-sharding).
    assert (
        old_shard.stat().st_mtime == old_mtime
    ), "historical 2024 shard was rewritten — year-sharding invariant broken"
    # 2026 shard SHOULD have been rewritten (the daily refresh touched it).
    assert cur_shard.stat().st_mtime > long_ago
    df = pd.read_csv(cur_shard)
    assert set(df["Sample Collected Date"]) == {"2026-01-10", "2026-06-15"}


@patch.dict("os.environ", _DASHBOARD_ENV, clear=False)
def test_dashboard_backfill_zero_only_fetches_next_year(tmp_path: Path) -> None:
    """backfill_days=0 means 'never re-fetch a covered year'. With a 2026
    shard on disk the next fetch is 2027-01-01 → date_end. If date_end is
    still within 2026 there's nothing new to fetch (network stays quiet).

    This is a deliberate semantic change from the old behaviour (which
    computed gaps from individual file end dates): year-sharding decides
    coverage at year granularity, which is coarser but drops the whole
    coverage-tracking-drift class of bugs."""
    _write_year_shard(
        tmp_path / "dashboard_2026.csv",
        [{"Sample Collected Date": "2026-06-01", "Region Id": "district_1"}],
    )

    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post.return_value = _mock_json_response({"access_token": "T0K3N"})
        mock_get.return_value = _mock_stream_response([])
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

    # backfill_days=0 and 2026 already covered → no fetch called.
    assert mock_get.call_count == 0


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


# ---------------------------------------------------------------------------
# Year-sharded staging — regression tests for the Aug 2026 refactor.
#
# Previous designs stored one xlsx per fetch chunk (which accumulated 280+
# overlapping snapshots and inflated case counts 20-100×) or one consolidated
# xlsx (which OOM'd t3.micro at ~80 MB when pandas read the whole history).
# Year-sharded caps peak RAM at one year's worth of rows, keeps historical
# data in cold storage, and uses drop-and-refetch by date window for daily
# refresh — no dedup logic, no rename dance, no trim state.
# ---------------------------------------------------------------------------


def _write_ihip_xlsx(path: Path, rows: list[dict[str, Any]]) -> None:
    """Materialise a real (parseable) minimal IHIP-shaped xlsx for tests."""
    import pandas as pd  # noqa: PLC0415

    pd.DataFrame(rows).to_excel(path, index=False)


@patch.dict("os.environ", _DASHBOARD_ENV, clear=False)
def test_dashboard_daily_run_drops_and_refetches_backfill_window(
    tmp_path: Path,
) -> None:
    """Core year-sharded invariant: a daily run
      1. drops rows dated >= (date_end - backfill_days + 1) from the year shard
      2. concats fresh fetch rows for that year
      3. atomic-writes the shard back

    So rows OUTSIDE the backfill window survive untouched, and rows INSIDE
    the window are replaced wholesale by the fresh fetch — no dedup logic,
    no accumulation."""
    # Pre-seed a 2026 shard with rows both inside AND outside the coming
    # 30-day backfill window (which will be Jun 1 → Jun 30 for date_end=Jun 30).
    _write_year_shard(
        tmp_path / "dashboard_2026.csv",
        [
            # Outside the backfill window — MUST survive.
            {"Sample Collected Date": "2026-03-15", "Region Id": "district_1"},
            {"Sample Collected Date": "2026-05-30", "Region Id": "district_2"},
            # Inside the backfill window — MUST be dropped and replaced.
            {"Sample Collected Date": "2026-06-05", "Region Id": "district_stale"},
            {"Sample Collected Date": "2026-06-20", "Region Id": "district_stale"},
        ],
    )

    # Fresh fetch delivers different rows for the backfill window.
    fresh_stream = [
        (
            '{"Region Id": "district_fresh_1", '
            '"Sample Collected Date": "2026-06-10", '
            '"Test Suspected For": "Dengue"}'
        ),
        (
            '{"Region Id": "district_fresh_2", '
            '"Sample Collected Date": "2026-06-25", '
            '"Test Suspected For": "Dengue"}'
        ),
    ]

    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post.return_value = _mock_json_response({"access_token": "T0K3N"})
        mock_get.return_value = _mock_stream_response(fresh_stream)
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

    assert listed == ["dashboard_2026.csv"]
    df = pd.read_csv(tmp_path / "dashboard_2026.csv")

    # Pre-cutoff rows preserved.
    outside_dates = {"2026-03-15", "2026-05-30"}
    # Stale-in-window rows dropped.
    stale_dates = {"2026-06-05", "2026-06-20"}
    # Fresh-in-window rows appended.
    fresh_dates = {"2026-06-10", "2026-06-25"}

    got = set(df["Sample Collected Date"].dropna().tolist())
    assert outside_dates.issubset(got), "pre-cutoff rows were lost"
    assert got.isdisjoint(
        stale_dates
    ), "stale in-window rows survived — drop-and-refetch failed"
    assert fresh_dates.issubset(got), "fresh fetch rows didn't land in the shard"


@patch.dict("os.environ", _DASHBOARD_ENV, clear=False)
def test_dashboard_legacy_files_migrate_to_year_shards(tmp_path: Path) -> None:
    """First-run migration: legacy dashboard_<from>_to_<to>.{csv,xlsx} files
    left over from earlier designs should be read once, sharded by year, and
    the originals deleted. Preserves all rows; keeps rows in their own year."""
    _write_ihip_xlsx(
        tmp_path / "dashboard_2024-01-01_to_2024-12-31.xlsx",
        [
            {"Sample Collected Date": "2024-07-15", "Region Id": "district_A"},
            {"Sample Collected Date": "2024-11-20", "Region Id": "district_B"},
        ],
    )
    _write_ihip_xlsx(
        tmp_path / "dashboard_2025-01-01_to_2025-12-31.xlsx",
        [
            {"Sample Collected Date": "2025-06-10", "Region Id": "district_C"},
        ],
    )

    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post.return_value = _mock_json_response({"access_token": "T0K3N"})
        # Empty stream — we're testing migration only, not fresh fetch merge.
        mock_get.return_value = _mock_stream_response([])
        source = load_source(
            "dashboard",
            {
                "source_path": str(tmp_path),
                "date_start": "2024-01-01",
                "date_end": "2025-12-31",
                "backfill_days": 30,
            },
        )
        listed = source.list_objects()

    # Legacy files replaced with year shards.
    assert "dashboard_2024.csv" in listed
    assert "dashboard_2025.csv" in listed
    assert not (tmp_path / "dashboard_2024-01-01_to_2024-12-31.xlsx").exists()
    assert not (tmp_path / "dashboard_2025-01-01_to_2025-12-31.xlsx").exists()

    df24 = pd.read_csv(tmp_path / "dashboard_2024.csv")
    df25 = pd.read_csv(tmp_path / "dashboard_2025.csv")
    assert set(df24["Sample Collected Date"]) == {"2024-07-15", "2024-11-20"}
    assert set(df25["Sample Collected Date"]) == {"2025-06-10"}


@patch.dict("os.environ", _DASHBOARD_ENV, clear=False)
def test_dashboard_legacy_migration_left_alone_when_unreadable(
    tmp_path: Path,
) -> None:
    """If a legacy file can't be read (fake bytes, corrupt archive), migration
    MUST leave it in place rather than silently drop data. On the next run
    it'll be retried; if still unreadable, an operator can inspect it manually.
    """
    # A garbage 'xlsx' — pandas.read_excel will raise on this.
    (tmp_path / "dashboard_2024-01-01_to_2024-12-31.xlsx").write_bytes(_FAKE_XLSX)

    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post.return_value = _mock_json_response({"access_token": "T0K3N"})
        mock_get.return_value = _mock_stream_response([])
        source = load_source(
            "dashboard",
            {
                "source_path": str(tmp_path),
                "date_start": "2024-01-01",
                "date_end": "2025-12-31",
                "backfill_days": 30,
            },
        )
        source.list_objects()

    # Legacy file NOT deleted.
    assert (tmp_path / "dashboard_2024-01-01_to_2024-12-31.xlsx").exists()
    # No spurious shard files got created either.
    assert list(tmp_path.glob("dashboard_2024.csv")) == []
