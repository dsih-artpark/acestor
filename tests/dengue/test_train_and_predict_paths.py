from pipelines.dengue.steps.train_and_predict import _resolve_predictions_paths


def test_resolve_paths_ensemble_mode():
    paths = _resolve_predictions_paths(
        output_mode="ensemble",
        model_names=["ensembleModel"],
        primary="ensembleModel",
    )
    assert paths == {"ensembleModel": "outputs/predictions.csv"}


def test_resolve_paths_both_mode():
    paths = _resolve_predictions_paths(
        output_mode="both",
        model_names=["nbr", "xgb", "rf", "tse", "ensembleModel"],
        primary="ensembleModel",
    )
    assert paths["ensembleModel"] == "outputs/predictions.csv"
    assert paths["nbr"] == "outputs/per_model/predictions_nbr.csv"
    assert paths["xgb"] == "outputs/per_model/predictions_xgb.csv"
    assert paths["rf"] == "outputs/per_model/predictions_rf.csv"
    assert paths["tse"] == "outputs/per_model/predictions_tse.csv"
    assert "_canonical_primary" not in paths


def test_resolve_paths_per_model_mode_promotes_primary():
    paths = _resolve_predictions_paths(
        output_mode="per_model",
        model_names=["nbr", "xgb", "rf", "tse"],
        primary="nbr",
    )
    assert paths["nbr"] == "outputs/per_model/predictions_nbr.csv"
    assert paths["xgb"] == "outputs/per_model/predictions_xgb.csv"
    assert paths["_canonical_primary"] == "outputs/predictions.csv"
    assert "ensembleModel" not in paths


def test_resolve_paths_invalid_mode_raises():
    import pytest

    with pytest.raises(ValueError, match="output_mode must be"):
        _resolve_predictions_paths(output_mode="bogus", model_names=["x"], primary="x")


def test_resolve_paths_per_model_unknown_primary_raises():
    import pytest

    with pytest.raises(ValueError, match="primary"):
        _resolve_predictions_paths(
            output_mode="per_model", model_names=["nbr", "xgb"], primary="missing"
        )
