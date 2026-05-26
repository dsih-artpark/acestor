"""HTML brief context builder + Jinja renderer.

Builds the dict the brief.html.j2 template consumes and writes the rendered
HTML to disk. Replaces the LaTeX module pipelines/dengue/lib/report.py.
"""

from __future__ import annotations

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


def build_brief_context(
    *,
    predictions: pd.DataFrame,
    run_date: str,
    charts_relpath: str,
    is_downscale: bool,
    document_title: str,
    downscale_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "document_title": document_title,
        "is_downscale": is_downscale,
        "hero_chart_relpath": f"{charts_relpath}/hero_forecast.png",
        "weekly_blocks": _weekly_blocks(predictions, charts_relpath),
        "action_matrix": _action_matrix(),
        "risk_progression": _risk_progression(predictions),
        "run_date": run_date,
        "footer_meta": {"generated": run_date},
        "downscale_diagnostics": downscale_diagnostics or {},
    }


def _weekly_blocks(df: pd.DataFrame, charts_relpath: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    weeks = sorted(df["startDatePredictedWeek"].unique())
    for i, wk in enumerate(weeks, start=1):
        sub = df[df["startDatePredictedWeek"] == wk]
        medium_plus = sub[sub["predictionZone"] >= 2]
        blocks.append(
            {
                "week_label": str(wk),
                "map_relpath": f"{charts_relpath}/risk_map_w{i}.png",
                "rows": medium_plus[
                    ["regionID", "prediction", "predictionZone"]
                ].to_dict("records"),
            }
        )
    return blocks


def _action_matrix() -> list[dict[str, str]]:
    return [
        {"zone": "1", "label": "Low", "action": "Routine surveillance."},
        {
            "zone": "2",
            "label": "Moderate",
            "action": "Targeted vector control; community alerts.",
        },
        {
            "zone": "3",
            "label": "High",
            "action": "Activate response teams; expand testing.",
        },
        {"zone": "4", "label": "Very High", "action": "Full outbreak response."},
    ]


def _risk_progression(df: pd.DataFrame) -> list[dict[str, Any]]:
    weeks = sorted(df["startDatePredictedWeek"].unique())
    rows = []
    for region, sub in df.groupby("regionID"):
        zones_by_week = dict(zip(sub["startDatePredictedWeek"], sub["predictionZone"]))
        rows.append(
            {
                "regionID": region,
                "zones": [int(zones_by_week.get(w, 0)) for w in weeks],
            }
        )
    return rows


def render_brief(context: dict[str, Any]) -> str:
    return _env().get_template("brief.html.j2").render(**context)
