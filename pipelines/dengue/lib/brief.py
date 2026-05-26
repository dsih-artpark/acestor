"""HTML brief context builder + Jinja renderer.

Builds the dict the brief.html.j2 template consumes and writes the rendered
HTML to disk. Replaces the LaTeX module pipelines/dengue/lib/report.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "html.j2"]),
    )


def load_region_names(geojson_dir: Path) -> dict[str, str]:
    """Return {region_id: name} from all geojsons under geojson_dir (or its subdirs)."""
    names: dict[str, str] = {}
    for p in Path(geojson_dir).rglob("*.geojson"):
        try:
            d = json.loads(p.read_text())
            feat = d.get("features", [{}])[0]
            props = feat.get("properties", {})
            rid = props.get("region_id")
            nm = props.get("name")
            if rid and nm:
                names[rid] = str(nm).title()  # KURNOOL → Kurnool
        except Exception:
            continue
    return names


def build_brief_context(
    *,
    predictions: pd.DataFrame,
    run_date: str,
    charts_relpath: str,
    is_downscale: bool,
    document_title: str,
    region_type: str = "district",
    parent_region_type: str = "district",
    downscale_diagnostics: dict[str, Any] | None = None,
    region_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    region_label = region_type.replace("_", " ")
    return {
        "document_title": document_title,
        "is_downscale": is_downscale,
        "hero_chart_relpath": f"{charts_relpath}/hero_forecast.png",
        "weekly_blocks": _weekly_blocks(predictions, charts_relpath, region_names),
        "action_matrix": _action_matrix(),
        "run_date": run_date,
        "footer_meta": {"generated": run_date},
        "downscale_diagnostics": downscale_diagnostics or {},
        "region_label": region_label,  # "district" / "mandal" / "block"
        "region_label_plural": f"{region_label}s",  # "districts" / "mandals" / "blocks"
        "parent_region_label": parent_region_type.replace("_", " "),
    }


_ZONE_BAND = {
    1: ("Low", "low"),
    2: ("Medium", "med"),
    3: ("High", "high"),
    4: ("Very High", "vhigh"),
}


def _pretty_week_label(start: str) -> str:
    """Format a week-starting date as '18 May – 24 May 2026'."""
    s = pd.Timestamp(start)
    e = s + pd.Timedelta(days=6)
    return f"{s.strftime('%d %b')} – {e.strftime('%d %b %Y')}"


def _weekly_blocks(
    df: pd.DataFrame,
    charts_relpath: str,
    region_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    rn = region_names or {}
    weeks = sorted(df["startDatePredictedWeek"].unique())
    for i, wk in enumerate(weeks, start=1):
        sub = df[df["startDatePredictedWeek"] == wk]
        medium_plus = sub[sub["predictionZone"] >= 2].copy()
        rows = []
        for _, r in medium_plus.iterrows():
            z = int(r["predictionZone"])
            label, css = _ZONE_BAND.get(z, ("", "low"))
            rid = r["regionID"]
            rows.append(
                {
                    "regionID": rid,
                    "regionName": rn.get(rid, rid),
                    "band_text": label,
                    "band_class": css,
                    "prediction_int": int(round(float(r["prediction"]))),
                    "range_low": max(
                        0, int(round(float(r["prediction"]) - float(r["StdDev"])))
                    ),
                    "range_high": int(
                        round(float(r["prediction"]) + float(r["StdDev"]))
                    ),
                }
            )
        blocks.append(
            {
                "week_label": str(wk),
                "week_label_pretty": _pretty_week_label(wk),
                "map_relpath": f"{charts_relpath}/risk_map_w{i}.png",
                "rows": rows,
            }
        )
    return blocks


def _action_matrix() -> list[dict[str, str]]:
    return [
        {"label": "Low", "band_class": "low", "action": "Routine surveillance."},
        {
            "label": "Medium",
            "band_class": "med",
            "action": "Increased monitoring · larval surveys · community awareness.",
        },
        {
            "label": "High",
            "band_class": "high",
            "action": "Source reduction · intensified vector control · daily review.",
        },
        {
            "label": "Very High",
            "band_class": "vhigh",
            "action": "Fogging · hospital alert · resource pre-positioning.",
        },
    ]


def render_brief(context: dict[str, Any]) -> str:
    return _env().get_template("brief.html.j2").render(**context)
