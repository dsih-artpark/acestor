"""Tests for ThresholdsConfig.methods and resolve_threshold_config."""

import pathlib

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


def test_generate_thresholds_has_no_hardcoded_calls():
    src = pathlib.Path("pipelines/dengue/steps/generate_thresholds.py").read_text()
    assert "prev_nweeks_threshold_params" not in src
    assert "historical_threshold_params" not in src


def test_generate_thresholds_reads_method_configs_from_thresholds_block():
    src = pathlib.Path("pipelines/dengue/steps/generate_thresholds.py").read_text()
    # Must not read from the old top-level threshold_configs key
    assert 'config.get("threshold_configs")' not in src
    # Must read from method_configs nested inside thresholds:
    assert '"method_configs"' in src


def test_default_classification_method_is_who():
    cfg = ThresholdsConfig.from_raw(_base_raw())
    assert cfg.classification_method == "who"


def test_classification_method_icmr():
    raw = {**_base_raw(), "classification_method": "icmr"}
    cfg = ThresholdsConfig.from_raw(raw)
    assert cfg.classification_method == "icmr"


def test_weighted_baseline_defaults():
    cfg = ThresholdsConfig.from_raw(_base_raw())
    assert cfg.recent_weeks == 4
    assert cfg.sd_window_weeks == 8
    assert cfg.weight_recent == 0.7
    assert cfg.weight_seasonal == 0.3


def test_weighted_baseline_knobs_overridable():
    raw = {
        **_base_raw(),
        "recent_weeks": 6,
        "sd_window_weeks": 12,
        "weight_recent": 0.6,
        "weight_seasonal": 0.4,
    }
    cfg = ThresholdsConfig.from_raw(raw)
    assert cfg.recent_weeks == 6
    assert cfg.sd_window_weeks == 12
    assert cfg.weight_recent == 0.6
    assert cfg.weight_seasonal == 0.4


def test_resolve_threshold_config_overrides_weighted_baseline_knobs():
    cfg = ThresholdsConfig.from_raw(_base_raw())
    resolved = resolve_threshold_config(cfg, {"recent_weeks": 6, "sd_window_weeks": 12})
    assert resolved.recent_weeks == 6
    assert resolved.sd_window_weeks == 12
    assert resolved.weight_recent == 0.7  # unchanged


def test_historical_n_years_defaults_to_4():
    """When historical_n_years is not specified, default must be 4 (PRISM-H §4.2.1)."""
    cfg = ThresholdsConfig.from_raw(
        {
            "region_type": "district",
            "methods": ["historical"],
        }
    )
    assert (
        cfg.historical_n_years == 4
    ), f"Expected historical_n_years=4 by default, got {cfg.historical_n_years}"


def test_resolve_threshold_config_preserves_classification_method():
    raw = {**_base_raw(), "classification_method": "icmr"}
    cfg = ThresholdsConfig.from_raw(raw)
    resolved = resolve_threshold_config(cfg, {"n_weeks": 6})
    assert resolved.classification_method == "icmr"
