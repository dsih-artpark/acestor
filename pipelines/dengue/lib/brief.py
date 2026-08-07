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


def load_child_geojson_combined(
    geojson_dir: Path,
    *,
    simplify_tolerance: float = 0.0015,
) -> dict:
    """Combine per-region geojsons into a single FeatureCollection, simplified.

    Keeps only essential properties: region_id, name, parent, parent_name.
    Simplifies polygon geometry to keep inlined JSON manageable.
    """
    from shapely.geometry import MultiPolygon, Polygon, mapping, shape
    from shapely.geometry.polygon import orient

    def _d3_wind(g):
        """Force exterior rings clockwise (sign=-1.0).

        d3-geo uses a spherical convention where CCW exterior rings are
        interpreted as 'everything OUTSIDE this ring' (the antimeridian-spanning
        complement). With CCW exterior rings, d3.geoBounds returns global
        bounds, projection.fitSize falls back to the default world view, and
        every path renders a viewBox-spanning rectangle (solid-coloured-block
        bug). CW exterior rings are what d3 expects — opposite to RFC 7946's
        cartesian convention."""
        if isinstance(g, Polygon):
            return orient(g, sign=-1.0)
        if isinstance(g, MultiPolygon):
            return MultiPolygon([orient(p, sign=-1.0) for p in g.geoms])
        return g

    features = []
    for p in sorted(Path(geojson_dir).glob("*.geojson")):
        try:
            d = json.loads(p.read_text())
            f = d.get("features", [{}])[0]
            props = f.get("properties", {})
            keep = {
                k: props.get(k) for k in ("region_id", "name", "parent", "parent_name")
            }
            geom = shape(f["geometry"])
            if simplify_tolerance > 0:
                geom = geom.simplify(simplify_tolerance, preserve_topology=True)
            geom = _d3_wind(geom)
            features.append(
                {
                    "type": "Feature",
                    "properties": keep,
                    "geometry": mapping(geom),
                }
            )
        except Exception:
            continue
    return {"type": "FeatureCollection", "features": features}


def compute_parent_lookup(
    feature_collection: dict,
    region_names: dict[str, str] | None = None,
) -> dict:
    """Return ``{parent_id: {"name": str, "bbox": [...], "child_ids": [str]}}``.

    Parent name fallback order:
      1. The child feature's ``parent_name`` property (baked into the geojson)
      2. ``region_names[parent_id]`` (if the caller supplied a name lookup —
         typically loaded from the parent-level geojsons where the real name
         lives)
      3. Raw ``parent_id`` (last resort, keeps the UI navigable)
    """
    from shapely.geometry import shape

    region_names = region_names or {}
    by_parent: dict = {}
    for f in feature_collection.get("features", []):
        props = f["properties"]
        pid = props.get("parent")
        if not pid:
            continue
        display_name = props.get("parent_name") or region_names.get(pid) or pid
        entry = by_parent.setdefault(
            pid,
            {"name": display_name, "bboxes": [], "child_ids": []},
        )
        entry["child_ids"].append(props.get("region_id"))
        b = shape(f["geometry"]).bounds  # (minx, miny, maxx, maxy)
        entry["bboxes"].append(b)
    # Reduce bboxes to combined bbox per parent
    out: dict = {}
    for pid, e in by_parent.items():
        if not e["bboxes"]:
            continue
        xs0 = min(b[0] for b in e["bboxes"])
        ys0 = min(b[1] for b in e["bboxes"])
        xs1 = max(b[2] for b in e["bboxes"])
        ys1 = max(b[3] for b in e["bboxes"])
        out[pid] = {
            "name": str(e["name"]).title(),
            "bbox": [xs0, ys0, xs1, ys1],
            "child_ids": e["child_ids"],
        }
    return out


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
    interactive_map_data: dict | None = None,
) -> dict[str, Any]:
    region_label = region_type.replace("_", " ")

    # Build child→parent map from interactive_map_data if present.
    child_to_parent: dict[str, str] | None = None
    if interactive_map_data:
        parent_lookup = interactive_map_data.get("parent_lookup", {})
        child_to_parent = {}
        for pid, pdata in parent_lookup.items():
            for cid in pdata.get("child_ids", []):
                child_to_parent[cid] = pid

    return {
        "document_title": document_title,
        "is_downscale": is_downscale,
        # Empty string signals "no static hero chart" — the partial skips
        # the <img>. Callers pass charts_relpath="" to opt out.
        "hero_chart_relpath": (
            f"{charts_relpath}/hero_forecast.png" if charts_relpath else ""
        ),
        "weekly_blocks": _weekly_blocks(
            predictions, charts_relpath, region_names, child_to_parent=child_to_parent
        ),
        "action_matrix": _action_matrix(),
        "run_date": run_date,
        "footer_meta": {"generated": run_date},
        "downscale_diagnostics": downscale_diagnostics or {},
        "region_label": region_label,  # "district" / "mandal" / "block"
        "region_label_plural": f"{region_label}s",  # "districts" / "mandals" / "blocks"
        "parent_region_label": parent_region_type.replace("_", " "),
        "interactive_map_data": interactive_map_data or None,
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
    *,
    child_to_parent: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """For each predicted week, group Medium+ regions by zone band (highest first).

    Each block has ``zone_groups``: a list of {label, band_class, regions[], region_entries[]}
    sorted Very High → High → Medium. Regions within a group are sorted by name.

    ``region_entries`` is a list of {id, name, parent} dicts suitable for JS filtering.
    ``regions`` is kept for backward compatibility (list of display names).
    """
    blocks: list[dict[str, Any]] = []
    rn = region_names or {}
    c2p = child_to_parent or {}
    weeks = sorted(df["startDatePredictedWeek"].unique())
    for i, wk in enumerate(weeks, start=1):
        sub = df[df["startDatePredictedWeek"] == wk]
        medium_plus = sub[sub["predictionZone"] >= 2].copy()
        zone_groups: list[dict[str, Any]] = []
        # Iterate zones high → low so Very High shows first.
        for z in sorted(_ZONE_BAND, reverse=True):
            in_zone = medium_plus[medium_plus["predictionZone"] == z]
            if in_zone.empty:
                continue
            label, css = _ZONE_BAND[z]
            entries = sorted(
                (
                    {
                        "id": r["regionID"],
                        "name": rn.get(r["regionID"], r["regionID"]),
                        "parent": c2p.get(r["regionID"], ""),
                    }
                    for _, r in in_zone.iterrows()
                ),
                key=lambda e: e["name"],
            )
            regions = [e["name"] for e in entries]
            zone_groups.append(
                {
                    "label": label,
                    "band_class": css,
                    "regions": regions,
                    "region_entries": entries,
                }
            )
        blocks.append(
            {
                "week_idx": i,
                "week_label": str(wk),
                "week_label_pretty": _pretty_week_label(wk),
                "map_relpath": (
                    f"{charts_relpath}/risk_map_w{i}.png" if charts_relpath else ""
                ),
                "zone_groups": zone_groups,
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
