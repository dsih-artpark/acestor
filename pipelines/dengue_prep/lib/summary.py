"""End-of-run summaries for the prep pipeline.

Renders a rich terminal table and a companion markdown file so operators
can eyeball what happened without scraping log lines. Written to be shared
across the case-parse and weather-parse/download steps.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd
from rich.console import Console
from rich.table import Table


# ---------------------------------------------------------------------------
# Data classes — collected as the parsers run, rendered at the end.
# ---------------------------------------------------------------------------


@dataclass
class FileParseStats:
    """One row's worth of the per-file breakdown table."""

    filename: str = ""
    read: int = 0
    after_filter: int = 0
    after_date_parse: int = 0
    after_region_resolve: int = 0
    after_scope_check: int = 0
    kept: int = 0
    date_column_used: str = ""
    date_column_was_fallback: bool = False
    resolution_method: str = ""


@dataclass
class CaseParseStats:
    """Aggregate stats for a `parse_ihip_files` invocation."""

    region_type: str = ""
    files: list[FileParseStats] = field(default_factory=list)
    drop_reasons: Counter = field(default_factory=Counter)
    output_path: str = ""
    output_rows: int = 0
    target_region_ids: frozenset[str] = field(default_factory=frozenset)
    covered_region_ids: set[str] = field(default_factory=set)
    latest_date: Optional[pd.Timestamp] = None
    per_region_latest: dict[str, pd.Timestamp] = field(default_factory=dict)
    per_region_case_count: dict[str, int] = field(default_factory=dict)
    # region_id → display name lookup, sourced from the geojson so both
    # terminal + markdown reports show human-readable names.
    region_names: dict[str, str] = field(default_factory=dict)
    # region_id → ordered ancestor chain (root → leaf), each entry has
    # {"region_type", "id", "name"}. Used to render dynamic hierarchy columns
    # in the coverage table so the parent chain (e.g. district / ULB / ward)
    # is discoverable without any hard-coded region-type list.
    region_hierarchy: dict[str, list[dict[str, str]]] = field(default_factory=dict)


@dataclass
class MonthFetch:
    year_month: str  # "2026-06"
    fetched_start: str
    fetched_end: str
    rows_added: int


@dataclass
class WeatherDownloadStats:
    source_mode: str = ""
    region_type: str = ""
    months_fetched: list[MonthFetch] = field(default_factory=list)
    months_skipped_complete: int = 0
    months_upstream_empty: int = 0


@dataclass
class WeatherParseStats:
    region_type: str = ""
    files_loaded: int = 0
    rows_total: int = 0
    span_start: Optional[pd.Timestamp] = None
    span_end: Optional[pd.Timestamp] = None
    target_region_ids: frozenset[str] = field(default_factory=frozenset)
    covered_region_ids: set[str] = field(default_factory=set)
    per_region_latest: dict[str, pd.Timestamp] = field(default_factory=dict)
    variables: list[str] = field(default_factory=list)
    output_path: str = ""
    region_names: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Renderers — rich for terminal, plain markdown for the artifact.
# ---------------------------------------------------------------------------


def render_case_parse_summary(
    stats: CaseParseStats,
    *,
    run_date: Optional[pd.Timestamp] = None,
    console: Optional[Console] = None,
) -> None:
    console = console or Console()

    console.rule(f"[bold cyan]CASE PREP SUMMARY — region_type={stats.region_type}")

    # 1. Per-file table
    tbl = Table(show_header=True, header_style="bold", title="Per-file breakdown")
    tbl.add_column("File", overflow="fold")
    tbl.add_column("Read", justify="right")
    tbl.add_column("After filter", justify="right")
    tbl.add_column("Dates OK", justify="right")
    tbl.add_column("Region OK", justify="right")
    tbl.add_column("In scope", justify="right")
    tbl.add_column("Kept", justify="right", style="green")
    tbl.add_column("Date col", overflow="fold")
    tbl.add_column("Resolver", overflow="fold")
    for f in stats.files:
        date_col = f.date_column_used + (
            " [yellow](fallback)[/yellow]" if f.date_column_was_fallback else ""
        )
        tbl.add_row(
            f.filename,
            str(f.read),
            str(f.after_filter),
            str(f.after_date_parse),
            str(f.after_region_resolve),
            str(f.after_scope_check),
            str(f.kept),
            date_col,
            f.resolution_method,
        )
    console.print(tbl)

    # 2. Drop-reason totals
    if stats.drop_reasons:
        tbl2 = Table(show_header=True, header_style="bold", title="Drop-reason totals")
        tbl2.add_column("Reason")
        tbl2.add_column("Rows dropped", justify="right", style="red")
        for reason, n in stats.drop_reasons.most_common():
            tbl2.add_row(reason, f"{n:,}")
        tbl2.add_row(
            "[bold]Total dropped[/bold]",
            f"[bold]{sum(stats.drop_reasons.values()):,}[/bold]",
        )
        console.print(tbl2)

    # 3. Coverage against target geojson region set — dynamic hierarchy columns
    if stats.target_region_ids:
        missing = sorted(stats.target_region_ids - stats.covered_region_ids)

        # Discover ancestor region_types across ALL targets, then order them
        # by first-appearance-in-any-chain (root→leaf). Some regions may have
        # shorter chains (e.g. Bhubaneswar's ward geojson may not thread
        # through district); union-of-types-with-consistent-order keeps every
        # ancestor label its own column.
        ancestor_types: list[str] = []
        _seen: set[str] = set()
        for rid in stats.target_region_ids:
            chain = stats.region_hierarchy.get(rid, [])
            for e in chain[:-1]:
                t = e["region_type"]
                if t not in _seen:
                    ancestor_types.append(t)
                    _seen.add(t)

        # Derive the leaf region_type from any target's chain — it becomes
        # the label of the "name" column (e.g. "ward" instead of a generic
        # "Name"). Falls back to "Name" if no chain is available.
        leaf_type = stats.region_type or "Name"
        for rid in stats.target_region_ids:
            chain = stats.region_hierarchy.get(rid, [])
            if chain:
                leaf_type = chain[-1]["region_type"] or leaf_type
                break

        tbl3 = Table(
            show_header=True,
            header_style="bold",
            title=(
                f"Target-region coverage — {len(stats.covered_region_ids)}/"
                f"{len(stats.target_region_ids)} covered"
            ),
        )
        for a in ancestor_types:
            tbl3.add_column(a)
        tbl3.add_column(leaf_type)
        tbl3.add_column("region_id")
        tbl3.add_column("Total cases", justify="right")
        tbl3.add_column("Latest date")
        tbl3.add_column("Status")

        # Sort by ancestor chain so related regions cluster; end_section=True
        # between groups (deepest ancestor changes) draws a subtle divider.
        def sort_key(rid: str) -> tuple[str, ...]:
            chain = stats.region_hierarchy.get(rid, [])
            ancestors = tuple(e["name"] or e["id"] for e in chain[:-1])
            return ancestors + (rid,)

        sorted_targets = sorted(stats.target_region_ids, key=sort_key)
        last_group_key: tuple[str, ...] | None = None
        for i, rid in enumerate(sorted_targets):
            chain = stats.region_hierarchy.get(rid, [])
            # Type-based placement so BHUBANESWAR (a ULB) always lands in the
            # 'ulb' column even if its chain doesn't reach the 'district' type.
            type_to_label = {
                e["region_type"]: (e["name"] or e["id"]) for e in chain[:-1]
            }
            cells: list[str] = [type_to_label.get(t, "—") for t in ancestor_types]
            group_key = tuple(cells)  # divide when the full ancestor path changes

            latest = stats.per_region_latest.get(rid)
            cases = stats.per_region_case_count.get(rid, 0)
            name = stats.region_names.get(rid, "—")
            if latest is None:
                cells.extend([name, rid, "0", "—", "[bold red]MISSING[/bold red]"])
            else:
                cells.extend(
                    [
                        name,
                        rid,
                        f"{cases:,}",
                        str(latest.date()),
                        "[green]✓[/green]",
                    ]
                )

            # Draw a divider whenever the deepest-ancestor group changes,
            # so BBK's wards visually cluster and Cuttack's follow after a line.
            end_section = (
                last_group_key is not None and group_key != last_group_key and i > 0
            )
            if end_section:
                # Re-add the previous row with end_section=True. rich doesn't
                # let us edit a row post-add, so we mark the CURRENT row's
                # section start via a lightweight visual: prefix the first
                # ancestor cell with a dim rule. Cleaner alternative: use
                # add_section() on group boundaries.
                tbl3.add_section()
            tbl3.add_row(*cells)
            last_group_key = group_key

        console.print(tbl3)
        if missing:
            labeled = [f"{rid} ({stats.region_names.get(rid, '?')})" for rid in missing]
            console.print(
                f"[bold red]  {len(missing)} target region(s) have zero rows: "
                f"{labeled}[/bold red]"
            )

    # 4. Aggregation gap — raw rows kept vs (region_id, date) tuples written.
    raw_kept = sum(f.kept for f in stats.files)
    if raw_kept > 0 and stats.output_rows > 0:
        ratio = raw_kept / stats.output_rows
        console.print(
            f"\n[bold]Raw rows kept:[/bold] {raw_kept:,}  →  "
            f"[bold]Aggregated to (region_id, date):[/bold] {stats.output_rows:,}  "
            f"([dim]avg {ratio:.1f} raw rows per output row[/dim])"
        )

    # 5. Output + freshness
    console.print(
        f"[bold]Output:[/bold] [cyan]{stats.output_path}[/cyan] "
        f"({stats.output_rows:,} rows)"
    )
    if stats.latest_date is not None and run_date is not None:
        staleness = (run_date.normalize() - stats.latest_date.normalize()).days
        console.print(
            f"[bold]Freshness:[/bold] latest date "
            f"[cyan]{stats.latest_date.date()}[/cyan] "
            f"({staleness} days stale vs run_date {run_date.date()})"
        )
    console.rule()


def render_weather_download_summary(
    stats: WeatherDownloadStats, *, console: Optional[Console] = None
) -> None:
    console = console or Console()
    console.rule(
        f"[bold cyan]WEATHER DOWNLOAD SUMMARY "
        f"— source={stats.source_mode}, region_type={stats.region_type}"
    )
    if stats.months_fetched:
        tbl = Table(show_header=True, header_style="bold", title="Months touched")
        tbl.add_column("Year-Month")
        tbl.add_column("Fetched start")
        tbl.add_column("Fetched end")
        tbl.add_column("Rows added", justify="right", style="green")
        for m in stats.months_fetched:
            tbl.add_row(m.year_month, m.fetched_start, m.fetched_end, str(m.rows_added))
        console.print(tbl)
    console.print(
        f"Months skipped (already complete): [cyan]{stats.months_skipped_complete}[/cyan]  "
        f"Months upstream returned empty: [yellow]{stats.months_upstream_empty}[/yellow]"
    )
    console.rule()


def render_weather_parse_summary(
    stats: WeatherParseStats,
    *,
    run_date: Optional[pd.Timestamp] = None,
    console: Optional[Console] = None,
) -> None:
    console = console or Console()
    console.rule(f"[bold cyan]WEATHER PARSE SUMMARY — region_type={stats.region_type}")
    console.print(
        f"Files loaded: [cyan]{stats.files_loaded}[/cyan]   "
        f"Rows aggregated: [cyan]{stats.rows_total:,}[/cyan]   "
        f"Variables: [cyan]{stats.variables}[/cyan]"
    )
    if stats.span_start is not None and stats.span_end is not None:
        console.print(
            f"Span: [cyan]{stats.span_start.date()}[/cyan] "
            f"→ [cyan]{stats.span_end.date()}[/cyan]"
        )
    if stats.target_region_ids:
        missing = sorted(stats.target_region_ids - stats.covered_region_ids)
        labeled = [f"{rid} ({stats.region_names.get(rid, '?')})" for rid in missing]
        console.print(
            f"Target-region coverage: [cyan]"
            f"{len(stats.covered_region_ids & stats.target_region_ids)}/"
            f"{len(stats.target_region_ids)}[/cyan]"
            + (f"    [bold red]missing: {labeled}[/bold red]" if missing else "")
        )
    if stats.output_path:
        console.print(f"[bold]Output:[/bold] [cyan]{stats.output_path}[/cyan]")
    if stats.span_end is not None and run_date is not None:
        staleness = (run_date.normalize() - stats.span_end.normalize()).days
        console.print(
            f"[bold]Freshness:[/bold] latest date "
            f"[cyan]{stats.span_end.date()}[/cyan] "
            f"({staleness} days stale vs run_date {run_date.date()})"
        )
    console.rule()


# ---------------------------------------------------------------------------
# Markdown export — same content, permanent record next to the run's artifacts.
# ---------------------------------------------------------------------------


def _md_case_parse(stats: CaseParseStats, run_date: Optional[pd.Timestamp]) -> str:
    lines = [
        f"# Case prep summary — region_type=`{stats.region_type}`",
        "",
        "## Per-file breakdown",
        "",
        "| File | Read | After filter | Dates OK | Region OK | In scope | Kept | Date col | Resolver |",
        "|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for f in stats.files:
        date_col = f.date_column_used + (
            " (fallback)" if f.date_column_was_fallback else ""
        )
        lines.append(
            f"| {f.filename} | {f.read} | {f.after_filter} | {f.after_date_parse} "
            f"| {f.after_region_resolve} | {f.after_scope_check} | {f.kept} "
            f"| {date_col} | {f.resolution_method} |"
        )
    if stats.drop_reasons:
        lines += [
            "",
            "## Drop-reason totals",
            "",
            "| Reason | Rows dropped |",
            "|---|---:|",
        ]
        for reason, n in stats.drop_reasons.most_common():
            lines.append(f"| {reason} | {n:,} |")
        lines.append(
            f"| **Total dropped** | **{sum(stats.drop_reasons.values()):,}** |"
        )
    if stats.target_region_ids:
        lines += [
            "",
            f"## Target-region coverage — "
            f"{len(stats.covered_region_ids)}/{len(stats.target_region_ids)}",
            "",
        ]
        # Discover ancestor types (union of first-appearances across chains).
        ancestor_types: list[str] = []
        _seen: set[str] = set()
        for rid in stats.target_region_ids:
            chain = stats.region_hierarchy.get(rid, [])
            for e in chain[:-1]:
                t = e["region_type"]
                if t not in _seen:
                    ancestor_types.append(t)
                    _seen.add(t)

        leaf_type = stats.region_type or "Name"
        for rid in stats.target_region_ids:
            chain = stats.region_hierarchy.get(rid, [])
            if chain:
                leaf_type = chain[-1]["region_type"] or leaf_type
                break
        header_cols = ancestor_types + [
            leaf_type,
            "region_id",
            "Total cases",
            "Latest date",
            "Status",
        ]
        aligns = ["---"] * len(ancestor_types) + ["---", "---", "---:", "---", "---"]
        lines.append("| " + " | ".join(header_cols) + " |")
        lines.append("|" + "|".join(aligns) + "|")

        def sort_key(rid: str) -> tuple[str, ...]:
            chain = stats.region_hierarchy.get(rid, [])
            return tuple(e["name"] or e["id"] for e in chain[:-1]) + (rid,)

        for rid in sorted(stats.target_region_ids, key=sort_key):
            chain = stats.region_hierarchy.get(rid, [])
            type_to_label = {
                e["region_type"]: (e["name"] or e["id"]) for e in chain[:-1]
            }
            ancestors = [type_to_label.get(t, "—") for t in ancestor_types]
            latest = stats.per_region_latest.get(rid)
            cases = stats.per_region_case_count.get(rid, 0)
            name = stats.region_names.get(rid, "—")
            row_cells = list(ancestors) + [
                name,
                rid,
                "0" if latest is None else f"{cases:,}",
                "—" if latest is None else str(latest.date()),
                "**MISSING**" if latest is None else "✓",
            ]
            lines.append("| " + " | ".join(row_cells) + " |")
    raw_kept = sum(f.kept for f in stats.files)
    lines.append("")
    if raw_kept > 0 and stats.output_rows > 0:
        ratio = raw_kept / stats.output_rows
        lines.append(
            f"**Raw rows kept:** {raw_kept:,}  →  "
            f"**Aggregated to (region_id, date):** {stats.output_rows:,}  "
            f"(avg {ratio:.1f} raw rows per output row)"
        )
    lines.append(f"**Output:** `{stats.output_path}` ({stats.output_rows:,} rows)")
    if stats.latest_date is not None and run_date is not None:
        staleness = (run_date.normalize() - stats.latest_date.normalize()).days
        lines.append(
            f"**Freshness:** latest `{stats.latest_date.date()}` "
            f"({staleness} days stale vs run_date `{run_date.date()}`)"
        )
    return "\n".join(lines)


def _md_weather_download(stats: WeatherDownloadStats) -> str:
    lines = [
        f"## Weather download — source `{stats.source_mode}`, region_type `{stats.region_type}`",
        "",
    ]
    if stats.months_fetched:
        lines += [
            "| Year-Month | Fetched start | Fetched end | Rows added |",
            "|---|---|---|---:|",
        ]
        for m in stats.months_fetched:
            lines.append(
                f"| {m.year_month} | {m.fetched_start} | {m.fetched_end} | {m.rows_added} |"
            )
    lines.append("")
    lines.append(f"Months skipped (complete): **{stats.months_skipped_complete}**")
    lines.append(f"Months upstream returned empty: **{stats.months_upstream_empty}**")
    return "\n".join(lines)


def _md_weather_parse(
    stats: WeatherParseStats, run_date: Optional[pd.Timestamp]
) -> str:
    lines = [f"## Weather parse — region_type `{stats.region_type}`", ""]
    lines.append(f"- Files loaded: **{stats.files_loaded}**")
    lines.append(f"- Rows aggregated: **{stats.rows_total:,}**")
    lines.append(f"- Variables: `{stats.variables}`")
    if stats.span_start is not None and stats.span_end is not None:
        lines.append(f"- Span: `{stats.span_start.date()}` → `{stats.span_end.date()}`")
    if stats.target_region_ids:
        missing = sorted(stats.target_region_ids - stats.covered_region_ids)
        covered = len(stats.covered_region_ids & stats.target_region_ids)
        lines.append(
            f"- Target-region coverage: **{covered}/{len(stats.target_region_ids)}**"
            + (f" — missing: {missing}" if missing else "")
        )
    if stats.output_path:
        lines.append(f"- Output: `{stats.output_path}`")
    if stats.span_end is not None and run_date is not None:
        staleness = (run_date.normalize() - stats.span_end.normalize()).days
        lines.append(
            f"- Freshness: {staleness} days stale vs run_date `{run_date.date()}`"
        )
    return "\n".join(lines)


def write_markdown_report(
    dest: Path,
    *,
    case: Optional[CaseParseStats] = None,
    weather_download: Optional[WeatherDownloadStats] = None,
    weather_parse: Optional[WeatherParseStats] = None,
    run_date: Optional[pd.Timestamp] = None,
) -> None:
    """Append a summary section for whichever step ran to a shared report file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    sections: list[str] = []
    if case is not None:
        sections.append(_md_case_parse(case, run_date))
    if weather_download is not None:
        sections.append(_md_weather_download(weather_download))
    if weather_parse is not None:
        sections.append(_md_weather_parse(weather_parse, run_date))
    body = "\n\n---\n\n".join(sections)
    # Append rather than clobber — a single run may write case + weather sections.
    if dest.exists():
        existing = dest.read_text()
        if body not in existing:
            dest.write_text(existing.rstrip() + "\n\n---\n\n" + body + "\n")
    else:
        dest.write_text(body + "\n")
