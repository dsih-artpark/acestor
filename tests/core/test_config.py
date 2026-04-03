"""Tests for acestor.core.config."""

import os
import textwrap
from pathlib import Path

import pytest

from acestor.core.config import _resolve_env, _resolve_env_recursive, PipelineConfig


# ---------------------------------------------------------------------------
# _resolve_env
# ---------------------------------------------------------------------------


def test_resolve_env_set_var(monkeypatch):
    monkeypatch.setenv("MY_VAR", "hello")
    assert _resolve_env("${MY_VAR}") == "hello"


def test_resolve_env_unset_var_no_default():
    os.environ.pop("NONEXISTENT_VAR_XYZ", None)
    assert _resolve_env("${NONEXISTENT_VAR_XYZ}") == ""


def test_resolve_env_unset_var_with_default():
    os.environ.pop("NONEXISTENT_VAR_XYZ", None)
    assert _resolve_env("${NONEXISTENT_VAR_XYZ:-fallback}") == "fallback"


def test_resolve_env_set_var_overrides_default(monkeypatch):
    monkeypatch.setenv("MY_VAR", "real")
    assert _resolve_env("${MY_VAR:-fallback}") == "real"


def test_resolve_env_no_placeholder():
    assert _resolve_env("plain string") == "plain string"


def test_resolve_env_multiple_placeholders(monkeypatch):
    monkeypatch.setenv("A", "foo")
    monkeypatch.setenv("B", "bar")
    assert _resolve_env("${A}/${B}") == "foo/bar"


# ---------------------------------------------------------------------------
# _resolve_env_recursive
# ---------------------------------------------------------------------------


def test_resolve_env_recursive_dict(monkeypatch):
    monkeypatch.setenv("HOST", "localhost")
    result = _resolve_env_recursive({"host": "${HOST}", "port": 5432})
    assert result == {"host": "localhost", "port": 5432}


def test_resolve_env_recursive_list(monkeypatch):
    monkeypatch.setenv("ITEM", "x")
    result = _resolve_env_recursive(["${ITEM}", "literal"])
    assert result == ["x", "literal"]


def test_resolve_env_recursive_nested(monkeypatch):
    monkeypatch.setenv("VAL", "deep")
    result = _resolve_env_recursive({"a": {"b": ["${VAL}"]}})
    assert result == {"a": {"b": ["deep"]}}


def test_resolve_env_recursive_non_string_passthrough():
    assert _resolve_env_recursive(42) == 42
    assert _resolve_env_recursive(3.14) == 3.14
    assert _resolve_env_recursive(None) is None


# ---------------------------------------------------------------------------
# PipelineConfig.from_yaml
# ---------------------------------------------------------------------------


def test_from_yaml_loads_config(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        textwrap.dedent(
            """\
        pipeline:
          name: test
        run:
          run_date: "2026-01-01"
    """
        )
    )
    config = PipelineConfig.from_yaml(cfg_file)
    assert config.raw["pipeline"]["name"] == "test"
    assert config.path == cfg_file


def test_from_yaml_resolves_env_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "secret")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("api_key: ${TEST_KEY}\n")
    config = PipelineConfig.from_yaml(cfg_file)
    assert config.raw["api_key"] == "secret"


def test_from_yaml_empty_file(tmp_path):
    cfg_file = tmp_path / "empty.yaml"
    cfg_file.write_text("")
    config = PipelineConfig.from_yaml(cfg_file)
    assert config.raw == {}


def test_from_yaml_file_not_found():
    with pytest.raises(FileNotFoundError):
        PipelineConfig.from_yaml(Path("/nonexistent/path.yaml"))


def test_from_yaml_invalid_yaml(tmp_path):
    import yaml

    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("key: [unclosed")
    with pytest.raises(yaml.YAMLError):
        PipelineConfig.from_yaml(cfg_file)
