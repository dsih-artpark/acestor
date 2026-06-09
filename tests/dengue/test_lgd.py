from __future__ import annotations

import pandas as pd
import pytest

from pipelines.dengue.lib.lgd import (
    LGD_COLUMN,
    add_lgd_column,
    lgd_code_lookup,
    require_state,
)


def test_lookup_ap_district_has_known_code():
    lookup = lgd_code_lookup("ap", "district")
    assert lookup["district_515"] == "515"


def test_lookup_ap_mandal_has_known_code():
    lookup = lgd_code_lookup("ap", "mandal")
    assert lookup["mandal_05206"] == "5206"


def test_lookup_missing_state_returns_empty():
    assert lgd_code_lookup("xx", "district") == {}


def test_add_lgd_column_adds_for_known_regions():
    df = pd.DataFrame(
        {"regionID": ["district_515", "district_502"], "prediction": [1, 2]}
    )
    out = add_lgd_column(df, state="ap", spatial_res="district")
    assert LGD_COLUMN in out.columns
    assert list(out[LGD_COLUMN]) == ["515", "502"]


def test_add_lgd_column_unknown_region_yields_nan():
    df = pd.DataFrame({"regionID": ["district_999999"]})
    out = add_lgd_column(df, state="ap", spatial_res="district")
    assert out[LGD_COLUMN].isna().all()


def test_add_lgd_column_noop_when_lookup_empty():
    df = pd.DataFrame({"regionID": ["district_515"]})
    out = add_lgd_column(df, state="xx", spatial_res="district")
    assert LGD_COLUMN not in out.columns


def test_add_lgd_column_noop_when_region_col_missing():
    df = pd.DataFrame({"foo": [1]})
    out = add_lgd_column(df, state="ap", spatial_res="district")
    assert LGD_COLUMN not in out.columns


def test_require_state_returns_lowercased():
    # Configs declare state as UPPERCASE (e.g. "AP"); paths use lowercase.
    # require_state does the conversion so callers don't have to.
    assert require_state({"state": "AP"}) == "ap"
    assert require_state({"state": "OD"}) == "od"
    # Tolerant of casing typos in config:
    assert require_state({"state": "ap"}) == "ap"


def test_require_state_raises_when_missing():
    with pytest.raises(ValueError, match="state"):
        require_state({})


def test_require_state_raises_when_blank():
    with pytest.raises(ValueError, match="state"):
        require_state({"state": "   "})
