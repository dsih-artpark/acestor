"""Config walker: which local paths does a pipeline config point to?"""

from __future__ import annotations

from pathlib import Path

from acestor.remote.config_walker import find_input_paths, find_output_paths


def test_finds_existing_input_dirs(tmp_path: Path):
    (tmp_path / "ka_datasets" / "weather").mkdir(parents=True)

    cfg = {
        "data": {
            "weather_download": {"source_path": "ka_datasets/weather"},
        }
    }
    paths = find_input_paths(cfg, tmp_path)
    keys = {p.key_path for p in paths}
    assert keys == {"data.weather_download.source_path"}
    for p in paths:
        assert p.local_path.exists()
        assert p.local_path.is_dir()


def test_skips_paths_that_do_not_exist_locally(tmp_path: Path):
    """Dashboard-source configs point at dirs that don't exist yet — the
    pipeline creates them on the remote. Walker must silently skip."""
    cfg = {
        "data": {
            "weather_download": {"source_path": "ka_datasets/weather"},
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
    cfg = {"data": {"weather_download": {"source_path": str(outside)}}}
    project_root = tmp_path
    (project_root / "pyproject.toml").write_text("[project]\n")
    assert find_input_paths(cfg, project_root) == []


def test_deduplicates_paths_that_appear_twice(tmp_path: Path):
    """If two config keys point at the same directory (misconfig or shared
    cache), the walker returns one InputPath — rsyncing it twice would be
    wasteful."""
    shared = tmp_path / "shared_cache"
    shared.mkdir()
    cfg = {
        "data": {
            "weather_download": {
                "source_path": "shared_cache",
                "filesystem_base_path": "shared_cache",
            }
        }
    }
    paths = find_input_paths(cfg, tmp_path)
    assert len(paths) == 1


def test_handles_missing_config_sections_gracefully(tmp_path: Path):
    assert find_input_paths({}, tmp_path) == []
    assert find_input_paths({"data": None}, tmp_path) == []
    assert find_input_paths({"data": {"weather_download": None}}, tmp_path) == []


def test_output_walker_picks_up_prepared_data(tmp_path: Path):
    """prepared_data is an OUTPUT (also pushed up, must be pulled back)."""
    (tmp_path / "ka_datasets" / "prepared_data").mkdir(parents=True)
    cfg = {"data": {"prepared_data": {"base_dir": "ka_datasets/prepared_data"}}}

    ins = find_input_paths(cfg, tmp_path)
    outs = find_output_paths(cfg, tmp_path)

    assert [p.key_path for p in ins] == []  # not treated as an input
    assert [p.key_path for p in outs] == ["data.prepared_data.base_dir"]


def test_output_walker_does_not_require_local_dir_to_exist(tmp_path: Path):
    """First remote run: local prepared_data dir may not exist yet — the
    pull-back will create it, so the walker must not skip absent dirs."""
    cfg = {
        "data": {
            "prepared_data": {"base_dir": "ka_datasets/prepared_data"},
            "weather_download": {"parsed_output_path": "ka_datasets/weather"},
        }
    }
    keys = {p.key_path for p in find_output_paths(cfg, tmp_path)}
    assert keys == {
        "data.prepared_data.base_dir",
        "data.weather_download.parsed_output_path",
    }


def test_case_download_source_path_is_treated_as_output(tmp_path: Path):
    """case_download.source_path is an OUTPUT so the dashboard source's
    freshly-fetched xlsx files get rsync'd back to the caller. Enables
    incremental caching: next run's push-up seeds prior xlsx files, and
    the dashboard source's containment/trim logic only fetches the delta."""
    cfg = {"data": {"case_download": {"source_path": "ka_datasets/raw_case"}}}

    ins = find_input_paths(cfg, tmp_path)
    outs = find_output_paths(cfg, tmp_path)

    assert [p.key_path for p in ins] == []  # NOT an input
    assert [p.key_path for p in outs] == ["data.case_download.source_path"]


def test_geojson_base_path_is_treated_as_output(tmp_path: Path):
    """geojson.base_path is an OUTPUT so dashboard-fetched geojsons rsync
    back to the caller after each prep run. Without this, downstream
    non-prep pipelines (e.g. forecast's generate_maps step reading
    geojsons/districts/) crash with FileNotFoundError because the caller
    has nothing to push up before the compute box starts."""
    cfg = {"data": {"geojson": {"base_path": "ka_datasets/geojsons"}}}

    ins = find_input_paths(cfg, tmp_path)
    outs = find_output_paths(cfg, tmp_path)

    assert [p.key_path for p in ins] == []  # NOT an input
    assert [p.key_path for p in outs] == ["data.geojson.base_path"]
