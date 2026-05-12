"""Tests for ThresholdsConfig.methods and resolve_threshold_config."""

from pipelines.dengue.configs import ThresholdsConfig, resolve_threshold_config


def _base_raw():
    return {
        "region_type": "district",
        "n_weeks": 4,
        "historical_n_years": 4,
        "excluded_years": [],
        "included_years": [],
    }


def test_default_methods_are_both():
    cfg = ThresholdsConfig.from_raw(_base_raw())
    assert set(cfg.methods) == {"historical", "prev_nweeks"}


def test_methods_can_be_overridden_via_yaml():
    raw = {**_base_raw(), "methods": ["historical"]}
    cfg = ThresholdsConfig.from_raw(raw)
    assert cfg.methods == ["historical"]


def test_resolve_threshold_config_inherits_base():
    cfg = ThresholdsConfig.from_raw(_base_raw())
    resolved = resolve_threshold_config(cfg, {})
    assert resolved.n_weeks == 4
    assert resolved.historical_n_years == 4


def test_resolve_threshold_config_overrides_n_weeks():
    cfg = ThresholdsConfig.from_raw(_base_raw())
    resolved = resolve_threshold_config(cfg, {"n_weeks": 8})
    assert resolved.n_weeks == 8
    assert resolved.historical_n_years == 4  # unchanged


def test_resolve_threshold_config_overrides_n_years():
    cfg = ThresholdsConfig.from_raw(_base_raw())
    resolved = resolve_threshold_config(cfg, {"historical_n_years": 2})
    assert resolved.historical_n_years == 2
    assert resolved.n_weeks == 4  # unchanged


def test_resolve_threshold_config_overrides_excluded_years():
    cfg = ThresholdsConfig.from_raw(_base_raw())
    resolved = resolve_threshold_config(cfg, {"excluded_years": [2020]})
    assert resolved.excluded_years == [2020]


def test_unknown_override_key_is_ignored():
    cfg = ThresholdsConfig.from_raw(_base_raw())
    resolved = resolve_threshold_config(cfg, {"nonexistent_key": 99})
    assert resolved.n_weeks == cfg.n_weeks
