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
    "DASHBOARD_CLIENT_ID": "test-client",
    "DASHBOARD_CLIENT_SECRET": "test-secret",
    "DASHBOARD_URL": "https://dashboard.example.com/api/cases",
    "DASHBOARD_TOKEN_URL": "https://dashboard.example.com/oauth/token",
}


def _mock_response(json_body: dict, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.raise_for_status = MagicMock()
    m.json = MagicMock(return_value=json_body)
    return m


@patch.dict("os.environ", _DASHBOARD_ENV, clear=False)
def test_dashboard_source_fetches_and_stages_csv(tmp_path: Path) -> None:
    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post.return_value = _mock_response({"access_token": "T0K3N"})
        mock_get.side_effect = [
            _mock_response(
                {
                    "results": [
                        {
                            "sample_collection_date": "2026-06-01",
                            "date_of_onset": "2026-05-30",
                            "region_id": "ward_gba-63",
                            "test_result": "Positive",
                            "patient_id": "p1",
                        },
                        {
                            "sample_collection_date": "2026-06-02",
                            "date_of_onset": "2026-05-31",
                            "region_id": "ward_gba-64",
                            "test_result": "Positive",
                            "patient_id": "p2",
                        },
                    ]
                }
            ),
            _mock_response({"results": []}),  # end of pagination
        ]

        source = load_source(
            "dashboard",
            {
                "source_path": str(tmp_path / "staging"),
                "date_end": "2026-06-30",
            },
        )
        listed = source.list_objects()

    assert listed == ["dashboard_2026-06-30.csv"]
    staged = tmp_path / "staging" / listed[0]
    df = pd.read_csv(staged)
    # renamed columns are present, source keys are gone
    assert "Date Of Onset" in df.columns
    assert "Region Id" in df.columns
    assert "Sample Collected Date" in df.columns
    assert list(df["Region Id"]) == ["ward_gba-63", "ward_gba-64"]

    # OAuth token was fetched with client-credentials grant
    post_kwargs = mock_post.call_args.kwargs
    assert post_kwargs["data"]["grant_type"] == "client_credentials"
    assert post_kwargs["data"]["client_id"] == "test-client"
    assert post_kwargs["data"]["client_secret"] == "test-secret"

    # API was called with the bearer token
    for call in mock_get.call_args_list:
        assert call.kwargs["headers"]["Authorization"] == "Bearer T0K3N"


@patch.dict("os.environ", {}, clear=True)
def test_dashboard_missing_credentials_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="DASHBOARD_"):
        load_source(
            "dashboard",
            {
                "api_url": "https://x/api",
                "token_url": "https://x/oauth",
                "source_path": str(tmp_path),
            },
        )


@patch.dict("os.environ", _DASHBOARD_ENV, clear=False)
def test_dashboard_zero_rows_raises(tmp_path: Path) -> None:
    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_post.return_value = _mock_response({"access_token": "T0K3N"})
        mock_get.return_value = _mock_response({"results": []})
        source = load_source("dashboard", {"source_path": str(tmp_path)})
        with pytest.raises(RuntimeError, match="zero rows"):
            source.list_objects()
