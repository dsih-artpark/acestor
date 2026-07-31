"""Config walker: which local paths does a pipeline config point to?"""

from __future__ import annotations

from pathlib import Path

from acestor.remote.config_walker import find_input_paths


def test_finds_existing_input_dirs(tmp_path: Path):
    (tmp_path / "ka_datasets" / "geojsons").mkdir(parents=True)
    (tmp_path / "ka_datasets" / "raw_case").mkdir(parents=True)

    cfg = {
        "data": {
            "geojson": {"base_path": "ka_datasets/geojsons"},
            "case_download": {"source_path": "ka_datasets/raw_case"},
        }
    }
    paths = find_input_paths(cfg, tmp_path)
    keys = {p.key_path for p in paths}
    assert keys == {
        "data.geojson.base_path",
        "data.case_download.source_path",
    }
    for p in paths:
        assert p.local_path.exists()
        assert p.local_path.is_dir()


def test_skips_paths_that_do_not_exist_locally(tmp_path: Path):
    """Dashboard-source configs point at dirs that don't exist yet — the
    pipeline creates them on the remote. Walker must silently skip."""
    cfg = {
        "data": {
            "geojson": {"base_path": "ka_datasets/geojsons"},  # not created
            "case_download": {"source_path": "ka_datasets/raw_case"},
        }
    }
    assert find_input_paths(cfg, tmp_path) == []


def test_ignores_output_keys(tmp_path: Path):
    (tmp_path / "outputs").mkdir()
    cfg = {
        "data": {
            "weather_download": {
                "parsed_output_path": "outputs",  # output key — must not sync
            }
        }
    }
    assert find_input_paths(cfg, tmp_path) == []


def test_refuses_paths_outside_project_root(tmp_path: Path):
    outside = tmp_path.parent / "some_other_project"
    outside.mkdir(exist_ok=True)
    cfg = {"data": {"geojson": {"base_path": str(outside)}}}
    project_root = tmp_path
    (project_root / "pyproject.toml").write_text("[project]\n")
    assert find_input_paths(cfg, project_root) == []


def test_deduplicates_paths_that_appear_twice(tmp_path: Path):
    shared = tmp_path / "ka_datasets" / "geojsons"
    shared.mkdir(parents=True)
    cfg = {
        "data": {
            "geojson": {"base_path": "ka_datasets/geojsons"},
            "case_download": {"source_path": "ka_datasets/geojsons"},
        }
    }
    paths = find_input_paths(cfg, tmp_path)
    assert len(paths) == 1


def test_handles_missing_config_sections_gracefully(tmp_path: Path):
    assert find_input_paths({}, tmp_path) == []
    assert find_input_paths({"data": None}, tmp_path) == []
    assert find_input_paths({"data": {"geojson": None}}, tmp_path) == []
