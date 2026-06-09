"""LGD code lookup, generic by state + spatial level.

Reads {region_id, lgd_code, name} from reference/lgd/<state>/<level>s.csv —
a committed snapshot extracted from <state>_datasets/lgd_normalized/
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


def _lookup_path(state: str, spatial_res: str) -> Path:
    return REFERENCE_DIR / state.lower() / f"{spatial_res.lower()}s.csv"


@lru_cache(maxsize=32)
def lgd_code_lookup(state: str, spatial_res: str) -> dict[str, str]:
    """Return {region_id: lgd_code} for the given state + spatial level.

    Missing CSV → empty dict (caller decides whether to warn/skip).
    """
    path = _lookup_path(state, spatial_res)
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
            "(expected %s) — lgdCode column not added. Regenerate with: "
            "uv run python scripts/extract_lgd_codes.py --state %s",
            state,
            spatial_res,
            _lookup_path(state, spatial_res).relative_to(REPO),
            state.upper(),
        )
        return df
    df = df.copy()
    df[LGD_COLUMN] = df[region_col].map(lookup)
    # Place lgdCode immediately after the region column for readability.
    cols = [c for c in df.columns if c != LGD_COLUMN]
    insert_at = cols.index(region_col) + 1
    cols.insert(insert_at, LGD_COLUMN)
    df = df[cols]
    unmapped = df.loc[df[LGD_COLUMN].isna(), region_col].unique()
    if len(unmapped):
        log.warning(
            "add_lgd_column: %d region(s) have no LGD code in %s "
            "— lgdCode will be NaN: %s",
            len(unmapped),
            _lookup_path(state, spatial_res).relative_to(REPO),
            sorted(map(str, unmapped))[:20],
        )
    return df


def require_state(config: dict) -> str:
    """Return `state` from config, raising a clear error if absent.

    The state must be declared at the top level of the run config (e.g. `state: "AP"`)
    so the predictions writers know which `reference/lgd/<state>/` subdir to use.
    """
    state = (config.get("state") or "").strip()
    if not state:
        raise ValueError(
            'Required config field `state` is missing. Add e.g. `state: "AP"` '
            "at the top level of the run config so the LGD lookup can resolve "
            "reference/lgd/<state>/<level>s.csv. Supported values: any state "
            "with a reference/lgd/<state>/ subdirectory."
        )
    return state.lower()
