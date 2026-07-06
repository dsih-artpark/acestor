"""Pins the improved error message from `validate_model_configs`.

The message must name (a) the offending keys, (b) both the models list and
the model_configs keys for context, and (c) both fixes — including the CLI
`--set` trap that recreates a key after it's been removed from YAML.

Validation lives in ``pipelines.dengue.configs`` and is called from
``build_pipeline`` — misconfig fails at pipeline construction, before any
step runs. Not inside train_and_predict where it would fire after prep /
thresholds / weather have already consumed time.
"""

from __future__ import annotations

import pytest

from pipelines.dengue.configs import validate_model_configs as _validate_model_configs


def test_no_error_when_all_keys_are_valid_models():
    # Every model_configs key is present in models — no error raised.
    _validate_model_configs({"rf": {}, "xgb": {}}, ["rf", "xgb"])
    _validate_model_configs({}, ["rf"])


def test_no_error_when_model_configs_is_subset_of_models():
    # It's fine to have models without any per-model config.
    _validate_model_configs({"rf": {}}, ["rf", "xgb", "tse"])


def test_raises_when_a_key_is_not_in_models():
    with pytest.raises(ValueError) as exc:
        _validate_model_configs({"xgb": {}}, ["rf", "tse", "timesfm"])
    assert "xgb" in str(exc.value)


def test_error_message_names_both_models_list_and_model_configs_keys():
    with pytest.raises(ValueError) as exc:
        _validate_model_configs({"xgb": {"tune": "never"}}, ["rf", "tse"])
    msg = str(exc.value)
    assert "model.models" in msg and "['rf', 'tse']" in msg
    assert "model_configs" in msg and "['xgb']" in msg


def test_error_message_suggests_the_add_to_models_fix():
    with pytest.raises(ValueError) as exc:
        _validate_model_configs({"xgb": {}}, ["rf"])
    msg = str(exc.value)
    # Suggests adding the missing model to model.models list
    assert "Add the model to model.models" in msg


def test_error_message_warns_about_cli_set_trap():
    """The trap: --set model_configs.<name>...=X recreates a removed key.

    The message must call this out explicitly so operators don't keep hitting
    it after editing the YAML.
    """
    with pytest.raises(ValueError) as exc:
        _validate_model_configs({"xgb": {}}, ["rf"])
    msg = str(exc.value)
    assert "--set model_configs.xgb" in msg
    assert "re-creates" in msg  # names the trap explicitly
    # Also suggests the =null runtime remedy
    assert "--set model_configs.xgb=null" in msg


def test_error_message_names_the_specific_offending_key_in_fix_examples():
    """When the mismatch is 'xgb', the message uses 'xgb' in the fix examples,
    not a generic placeholder — so copy-paste actually works."""
    with pytest.raises(ValueError) as exc:
        _validate_model_configs({"xgb": {}}, ["rf", "tse"])
    msg = str(exc.value)
    assert "model_configs.xgb:" in msg  # YAML path
    assert "model_configs.xgb..." in msg  # CLI override path


def test_multiple_unknown_keys_all_reported():
    with pytest.raises(ValueError) as exc:
        _validate_model_configs({"xgb": {}, "nbr": {}, "foo": {}}, ["rf", "tse"])
    msg = str(exc.value)
    # All three should be named
    for k in ["xgb", "nbr", "foo"]:
        assert k in msg


def test_deterministic_key_ordering_in_message():
    """Sorted key list — so the same misconfig gives the same message every run."""
    with pytest.raises(ValueError) as exc1:
        _validate_model_configs({"z": {}, "a": {}, "m": {}}, ["rf"])
    with pytest.raises(ValueError) as exc2:
        _validate_model_configs({"m": {}, "a": {}, "z": {}}, ["rf"])
    assert str(exc1.value) == str(exc2.value)
    assert "['a', 'm', 'z']" in str(exc1.value)
