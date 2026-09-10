"""GET /datasets/<dir>?download=1 streams a zip of the folder."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from acestor.webui import app as webui_app


@pytest.fixture()
def datasets_root(tmp_path: Path, monkeypatch) -> Path:
    """Point the webui's dataset scoping at a tmp tree with one *_datasets root."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "gba_datasets" / "prepared_data").mkdir(parents=True)
    (root / "gba_datasets" / "prepared_data" / "a.csv").write_text("h1,h2\n1,2\n")
    (root / "gba_datasets" / "prepared_data" / "b.csv").write_text("h1,h2\n3,4\n")
    (root / "gba_datasets" / "prepared_data" / "sub").mkdir()
    (root / "gba_datasets" / "prepared_data" / "sub" / "c.txt").write_text("nested")
    monkeypatch.setattr(webui_app, "_datasets_project_root", lambda: root)
    return root


def test_folder_download_returns_zip_with_expected_files(datasets_root: Path):
    client = TestClient(webui_app.app)
    resp = client.get("/datasets/gba_datasets/prepared_data?download=1")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "prepared_data.zip" in resp.headers["content-disposition"]

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = sorted(zf.namelist())
    assert names == ["a.csv", "b.csv", "sub/c.txt"]
    # Content preserved
    assert zf.read("a.csv") == b"h1,h2\n1,2\n"
    assert zf.read("sub/c.txt") == b"nested"


def test_folder_download_of_root_datasets_dir_uses_dir_name(datasets_root: Path):
    """Downloading a *_datasets root should still yield a named archive."""
    client = TestClient(webui_app.app)
    resp = client.get("/datasets/gba_datasets?download=1")
    assert resp.status_code == 200
    assert "gba_datasets.zip" in resp.headers["content-disposition"]


def test_folder_download_over_size_ceiling_returns_413(datasets_root: Path, monkeypatch):
    """A folder above the uncompressed ceiling must fail loud, not OOM the box."""
    # Shrink the ceiling to 1 KB so a tiny fixture trips it.
    monkeypatch.setattr(
        webui_app, "_DATASETS_ZIP_MAX_UNCOMPRESSED_BYTES", 1024, raising=True
    )
    # Add ~2 KB of content
    big = datasets_root / "gba_datasets" / "prepared_data" / "big.bin"
    big.write_bytes(b"X" * 2048)

    client = TestClient(webui_app.app)
    resp = client.get("/datasets/gba_datasets/prepared_data?download=1")
    assert resp.status_code == 413
    assert "larger than" in resp.json()["detail"]


def test_file_download_unchanged(datasets_root: Path):
    """Regression guard: single-file download still works as before."""
    client = TestClient(webui_app.app)
    resp = client.get("/datasets/gba_datasets/prepared_data/a.csv?download=1")
    assert resp.status_code == 200
    assert resp.content == b"h1,h2\n1,2\n"


def test_folder_browse_without_download_still_renders_html(datasets_root: Path):
    """Regression guard: without ?download=1, the folder view still renders."""
    client = TestClient(webui_app.app)
    resp = client.get("/datasets/gba_datasets/prepared_data")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    # Both the file and the subdir should appear
    assert "a.csv" in resp.text
    assert "sub" in resp.text
    # New affordance: [download .zip] link for the sub-directory row
    assert "[download .zip]" in resp.text
    # Top-of-folder link too
    assert "[download this folder as .zip]" in resp.text
