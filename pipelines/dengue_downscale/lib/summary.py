"""End-of-run summary for the dengue_downscale pipeline.

Mirrors the dengue_prep summary in shape: a rich terminal table + a markdown
file appended to the shared prep_summary.md alongside the run's artifacts.
Sections:

1. Source — which parent run this downscale consumed
2. Per-parent share computation — which parents used data-driven vs uniform
3. Conservation sanity — cheap numerical health check
4. Risk direction — how child zones compare to their parent's zone
5. Zone re-derivation health — how many children got predictionZone=0 (the
   "white-map" red flag)
6. Orphan children — child regions in mapping with no case history
7. Target coverage — child geojson count vs downscale output count
8. Output — final CSV path + row counts
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd
from rich.console import Console
from rich.table import Table


@dataclass
class ParentShareStats:
    parent_id: str = ""
    parent_name: str = ""
    n_children: int = 0
    n_children_with_cases: int = 0
    n_children_missing_from_cases: int = 0
    orphan_children: list[str] = field(default_factory=list)
    total_cases_in_window: int = 0
    uniform_fallback: bool = False


@dataclass
class DownscaleStats:
    parent_level: str = ""
    child_level: str = ""
    source_run_id: str = ""
    source_cutoff_date: Optional[pd.Timestamp] = None
    source_freshness_days: Optional[int] = None
    as_of_date: Optional[pd.Timestamp] = None
    window_weeks: int = 0
    classification_method: str = ""

    n_parent_rows: int = 0
    n_child_rows: int = 0
    n_children_in_mapping: int = 0
    n_children_in_geojson: int = 0

    per_parent: list[ParentShareStats] = field(default_factory=list)

    # Diagnostics from downscale_diagnostics
    conservation_max_abs_err: float = 0.0
    n_parent_weeks: int = 0
    n_parents_uniform: int = 0
    n_children_below_parent: int = 0
    n_children_above_parent: int = 0
    n_children_match_parent: int = 0

    # Zone-zero breakdown (predictionZone=0 == "prediction outside every band")
    n_zone_zero_by_key: dict[tuple[str, str, str], int] = field(default_factory=dict)
    n_total_by_key: dict[tuple[str, str, str], int] = field(default_factory=dict)
    zone_zero_top_regions: list[tuple[str, int]] = field(default_factory=list)

    # Orphan children — appeared in mapping but never in cases_daily.csv
    orphan_children_names: dict[str, str] = field(default_factory=dict)

    output_path: str = ""


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def render_downscale_summary(
    stats: DownscaleStats,
    *,
    run_date: Optional[pd.Timestamp] = None,
    console: Optional[Console] = None,
) -> None:
    console = console or Console()
    console.rule(
        f"[bold cyan]DOWNSCALE SUMMARY — {stats.parent_level} → {stats.child_level}"
    )

    # 1. Source
    src = Table(show_header=True, header_style="bold", title="Source")
    src.add_column("Field")
    src.add_column("Value")
    src.add_row("Parent level", stats.parent_level)
    src.add_row("Child level", stats.child_level)
    src.add_row("source_run_id", stats.source_run_id or "?")
    if stats.source_cutoff_date is not None:
        src.add_row("Source cutoff date", str(stats.source_cutoff_date.date()))
    if stats.source_freshness_days is not None:
        src.add_row(
            "Source freshness",
            f"{stats.source_freshness_days} days vs today",
        )
    if stats.as_of_date is not None:
        src.add_row("as_of_date (cases CSV latest)", str(stats.as_of_date.date()))
    src.add_row("window_weeks", str(stats.window_weeks))
    src.add_row("classification_method", stats.classification_method)
    src.add_row("Parent rows loaded", f"{stats.n_parent_rows:,}")
    src.add_row("Child rows produced", f"{stats.n_child_rows:,}")
    console.print(src)

    # 2. Per-parent share computation
    if stats.per_parent:
        tbl = Table(
            show_header=True,
            header_style="bold",
            title="Per-parent share computation",
        )
        tbl.add_column("Parent")
        tbl.add_column("Name")
        tbl.add_column("Children", justify="right")
        tbl.add_column("With recent cases", justify="right")
        tbl.add_column("No history", justify="right")
        tbl.add_column("Cases in window", justify="right")
        tbl.add_column("Share source")
        for p in stats.per_parent:
            share_source = (
                "[yellow]uniform fallback[/yellow]"
                if p.uniform_fallback
                else "[green]data-driven[/green]"
            )
            tbl.add_row(
                p.parent_id,
                p.parent_name or "—",
                str(p.n_children),
                str(p.n_children_with_cases),
                str(p.n_children_missing_from_cases),
                f"{p.total_cases_in_window:,}",
                share_source,
            )
        console.print(tbl)

    # 3. Conservation sanity + 4. Risk direction combined
    sanity = Table(
        show_header=True, header_style="bold", title="Sanity & risk direction"
    )
    sanity.add_column("Metric")
    sanity.add_column("Value", justify="right")
    ok = "[green]✓[/green]" if stats.conservation_max_abs_err < 1e-6 else "[red]⚠[/red]"
    sanity.add_row(
        f"conservation_max_abs_err {ok}",
        f"{stats.conservation_max_abs_err:.3g}",
    )
    sanity.add_row("Parent-weeks evaluated", f"{stats.n_parent_weeks:,}")
    sanity.add_row("Parents with uniform-split fallback", str(stats.n_parents_uniform))
    sanity.add_row("Children below parent zone", f"{stats.n_children_below_parent:,}")
    sanity.add_row("Children above parent zone", f"{stats.n_children_above_parent:,}")
    sanity.add_row(
        "Children matching parent zone", f"{stats.n_children_match_parent:,}"
    )
    console.print(sanity)

    # 5. Zone re-derivation health
    if stats.n_total_by_key:
        z = Table(
            show_header=True,
            header_style="bold",
            title='Zone re-derivation — predictionZone=0 ("unclassified") rate',
        )
        z.add_column("Model")
        z.add_column("Threshold method")
        z.add_column("Target week")
        z.add_column("Zone=0", justify="right")
        z.add_column("Total", justify="right")
        z.add_column("% zero", justify="right")
        for key in sorted(stats.n_total_by_key):
            model, thresh, week = key
            total = stats.n_total_by_key[key]
            zero = stats.n_zone_zero_by_key.get(key, 0)
            pct = 100.0 * zero / total if total else 0.0
            style = "[red]" if pct > 50 else "[yellow]" if pct > 20 else ""
            end = "[/red]" if pct > 50 else "[/yellow]" if pct > 20 else ""
            z.add_row(
                model,
                thresh,
                week,
                f"{zero:,}",
                f"{total:,}",
                f"{style}{pct:.0f}%{end}",
            )
        console.print(z)

    # 6. Orphans (child regions in geojson mapping but never seen in cases)
    if stats.orphan_children_names:
        orphans_list = [
            f"{rid} ({stats.orphan_children_names.get(rid, '?')})"
            for rid in sorted(stats.orphan_children_names)
        ]
        console.print(
            f"[bold red]  {len(orphans_list)} orphan child region(s) with no case history: "
            f"{orphans_list}[/bold red]"
        )

    # 7. Coverage
    if stats.n_children_in_geojson:
        console.print(
            f"[bold]Coverage:[/bold] "
            f"geojson children [cyan]{stats.n_children_in_geojson}[/cyan]  "
            f"→ mapping [cyan]{stats.n_children_in_mapping}[/cyan]  "
            f"→ downscaled rows [cyan]{stats.n_child_rows:,}[/cyan]"
        )

    # 8. Output
    if stats.output_path:
        console.print(f"[bold]Output:[/bold] [cyan]{stats.output_path}[/cyan]")
    if run_date is not None and stats.source_cutoff_date is not None:
        staleness = (run_date.normalize() - stats.source_cutoff_date.normalize()).days
        console.print(
            f"[bold]Source freshness:[/bold] parent-run cutoff "
            f"[cyan]{stats.source_cutoff_date.date()}[/cyan] "
            f"({staleness} days vs run_date {run_date.date()})"
        )
    console.rule()


# ---------------------------------------------------------------------------
# Markdown emitter
# ---------------------------------------------------------------------------


def _md_downscale(stats: DownscaleStats, run_date: Optional[pd.Timestamp]) -> str:
    lines: list[str] = [
        f"## Downscale — {stats.parent_level} → {stats.child_level}",
        "",
        "### Source",
        "",
        f"- source_run_id: `{stats.source_run_id}`",
    ]
    if stats.source_cutoff_date is not None:
        lines.append(f"- Source cutoff date: `{stats.source_cutoff_date.date()}`")
    if stats.source_freshness_days is not None:
        lines.append(
            f"- Source freshness: **{stats.source_freshness_days} days vs today**"
        )
    if stats.as_of_date is not None:
        lines.append(f"- as_of_date: `{stats.as_of_date.date()}`")
    lines += [
        f"- window_weeks: {stats.window_weeks}",
        f"- classification_method: `{stats.classification_method}`",
        f"- Parent rows: **{stats.n_parent_rows:,}** → Child rows: **{stats.n_child_rows:,}**",
    ]

    if stats.per_parent:
        lines += [
            "",
            "### Per-parent share computation",
            "",
            "| Parent | Name | Children | With recent cases | No history | Cases in window | Share source |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
        for p in stats.per_parent:
            src = "uniform fallback" if p.uniform_fallback else "data-driven"
            lines.append(
                f"| {p.parent_id} | {p.parent_name or '—'} | {p.n_children} | "
                f"{p.n_children_with_cases} | {p.n_children_missing_from_cases} | "
                f"{p.total_cases_in_window:,} | {src} |"
            )

    ok = "OK" if stats.conservation_max_abs_err < 1e-6 else "WARN"
    lines += [
        "",
        "### Sanity & risk direction",
        "",
        f"- conservation_max_abs_err: **{stats.conservation_max_abs_err:.3g}** ({ok})",
        f"- Parent-weeks evaluated: **{stats.n_parent_weeks:,}**",
        f"- Parents with uniform-split fallback: **{stats.n_parents_uniform}**",
        f"- Children below parent zone: **{stats.n_children_below_parent:,}**",
        f"- Children above parent zone: **{stats.n_children_above_parent:,}**",
        f"- Children matching parent zone: **{stats.n_children_match_parent:,}**",
    ]

    if stats.n_total_by_key:
        lines += [
            "",
            "### Zone re-derivation — predictionZone=0 rate",
            "",
            "| Model | Threshold method | Target week | Zone=0 | Total | % zero |",
            "|---|---|---|---:|---:|---:|",
        ]
        for key in sorted(stats.n_total_by_key):
            model, thresh, week = key
            total = stats.n_total_by_key[key]
            zero = stats.n_zone_zero_by_key.get(key, 0)
            pct = 100.0 * zero / total if total else 0.0
            lines.append(
                f"| {model} | {thresh} | {week} | {zero:,} | {total:,} | {pct:.0f}% |"
            )

    if stats.orphan_children_names:
        lines += [
            "",
            "### Orphan child regions (no case history)",
            "",
        ]
        for rid in sorted(stats.orphan_children_names):
            lines.append(f"- `{rid}` — {stats.orphan_children_names.get(rid, '?')}")

    if stats.n_children_in_geojson:
        lines += [
            "",
            f"**Coverage:** geojson children **{stats.n_children_in_geojson}** → "
            f"mapping **{stats.n_children_in_mapping}** → "
            f"downscaled rows **{stats.n_child_rows:,}**",
        ]

    if stats.output_path:
        lines.append(f"\n**Output:** `{stats.output_path}`")
    if run_date is not None and stats.source_cutoff_date is not None:
        staleness = (run_date.normalize() - stats.source_cutoff_date.normalize()).days
        lines.append(
            f"**Source freshness:** parent cutoff `{stats.source_cutoff_date.date()}` "
            f"({staleness} days vs run_date `{run_date.date()}`)"
        )
    return "\n".join(lines)


def write_markdown_report(
    dest: Path,
    *,
    downscale: DownscaleStats,
    run_date: Optional[pd.Timestamp] = None,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = _md_downscale(downscale, run_date)
    if dest.exists():
        existing = dest.read_text()
        if body not in existing:
            dest.write_text(existing.rstrip() + "\n\n---\n\n" + body + "\n")
    else:
        dest.write_text(body + "\n")
