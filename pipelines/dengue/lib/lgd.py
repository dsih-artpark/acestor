"""LGD code lookup, generic by spatial level.

Reads {region_id, lgd_code, name} from reference/lgd/<state>_<level>.csv —
a committed, version-controlled snapshot extracted from the geojsons
(see scripts/extract_lgd_codes.py).

The pipeline calls `add_lgd_column(df, state, spatial_res, region_col)`
before writing any predictions CSV; that's the single integration point.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[3]
REFERENCE_DIR = REPO / "reference" / "lgd"

LGD_COLUMN = "lgdCode"


@lru_cache(maxsize=32)
def lgd_code_lookup(state: str, spatial_res: str) -> dict[str, str]:
    """Return {region_id: lgd_code} for the given state + spatial level.

    Missing CSV → empty dict (caller decides whether to warn/skip).
    """
    path = REFERENCE_DIR / f"{state.lower()}_{spatial_res.lower()}.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path, dtype=str)
    return dict(zip(df["region_id"], df["lgd_code"], strict=True))


def add_lgd_column(
    df: pd.DataFrame,
    *,
    state: str,
    spatial_res: str,
    region_col: str = "regionID",
) -> pd.DataFrame:
    """Add an `lgdCode` column to `df`, looked up by `region_col`.

    No-op if the lookup table is empty or the region column is missing. Idempotent
    when the column already exists (overwrites — the lookup is the source of truth).
    """
    if region_col not in df.columns:
        return df
    lookup = lgd_code_lookup(state, spatial_res)
    if not lookup:
        log.warning(
            "add_lgd_column: no LGD lookup table for state=%r spatial_res=%r "
            "(expected reference/lgd/%s_%s.csv) — lgdCode column not added",
            state,
            spatial_res,
            state.lower(),
            spatial_res.lower(),
        )
        return df
    df = df.copy()
    df[LGD_COLUMN] = df[region_col].map(lookup)
    unmapped = df.loc[df[LGD_COLUMN].isna(), region_col].unique()
    if len(unmapped):
        log.warning(
            "add_lgd_column: %d region(s) have no LGD code in "
            "reference/lgd/%s_%s.csv — lgdCode will be NaN: %s",
            len(unmapped),
            state.lower(),
            spatial_res.lower(),
            sorted(map(str, unmapped))[:20],
        )
    return df


def infer_state_from_geojson_path(geojson_base_path: str) -> str | None:
    """Best-effort: extract state code from a geojson base path.

    `ap_datasets/geojsons/geojsons_AP` → 'ap'
    `od_datasets/geojsons/geojsons_OD` → 'od'
    """
    name = Path(geojson_base_path).name
    if "_" in name:
        return name.rsplit("_", 1)[-1].lower() or None
    return None
