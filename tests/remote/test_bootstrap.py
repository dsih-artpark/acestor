"""Bootstrap command composition — pin the shell contract."""

from __future__ import annotations

from acestor.remote.bootstrap import (
    REMOTE_WORKSPACE,
    remote_run_command,
    system_bootstrap_script,
    uv_sync_command,
)


def test_system_bootstrap_installs_deps_and_uv():
    s = system_bootstrap_script()
    # OS packages the geospatial stack needs
    assert "libgeos-dev" in s
    assert "libproj-dev" in s
    assert "libgdal-dev" in s
    # uv installer is idempotent
    assert "astral.sh/uv/install.sh" in s
    assert "command -v uv" in s
    # Non-interactive apt
    assert "DEBIAN_FRONTEND=noninteractive" in s
    # apt lock wait loop
    assert "waiting for apt lock" in s


def test_uv_sync_all_extras_by_default():
    cmd = uv_sync_command()
    assert REMOTE_WORKSPACE in cmd
    assert "uv sync --all-extras" in cmd


def test_uv_sync_none_extras():
    cmd = uv_sync_command(extras="none")
    assert "uv sync" in cmd
    assert "--extra" not in cmd
    assert "--all-extras" not in cmd


def test_uv_sync_named_extra():
    cmd = uv_sync_command(extras="dengue")
    assert "uv sync --extra dengue" in cmd


def test_remote_run_command_mirrors_acestor_run_flags():
    cmd = remote_run_command(
        pipeline="pipelines.dengue.pipeline:build_pipeline",
        config="configs/ka_district.yaml",
        run_id="r1",
        overrides=["model_configs.rf.tune=true"],
        clean=True,
    )
    assert "uv run python -m acestor.run" in cmd
    assert "--pipeline 'pipelines.dengue.pipeline:build_pipeline'" in cmd
    assert "--config 'configs/ka_district.yaml'" in cmd
    assert "--run-id 'r1'" in cmd
    assert "--set 'model_configs.rf.tune=true'" in cmd
    assert "--clean" in cmd
    assert REMOTE_WORKSPACE in cmd


def test_remote_run_command_omits_clean_when_false():
    cmd = remote_run_command(
        pipeline="p:build",
        config="cfg.yaml",
        run_id="r2",
    )
    assert "--clean" not in cmd
    assert "--set" not in cmd


def test_remote_run_command_quotes_values_with_single_quotes():
    """A path with a single quote must survive shell parsing."""
    cmd = remote_run_command(
        pipeline="p:build",
        config="configs/kid's file.yaml",
        run_id="r",
    )
    # POSIX single-quote escape: ' → '\''
    assert "'configs/kid'\\''s file.yaml'" in cmd
