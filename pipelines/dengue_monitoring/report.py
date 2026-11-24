"""Self-contained, filterable HTML/SVG monitoring reports; no external assets."""

from __future__ import annotations

from html import escape

import numpy as np
import pandas as pd

from .metrics import SERIES


def chart(group: pd.DataFrame) -> str:
    group = group.sort_values("target_end")
    values = list(group.signed_error.dropna()) + list(group.p75.dropna()) + [0.0]
    low, high = min(values), max(values)
    pad = max((high - low) * 0.12, 0.1)
    low, high = low - pad, high + pad
    dates = pd.to_datetime(group.target_end)
    first, last = dates.min(), dates.max()
    span = max((last - first).days, 7)

    def x(date):
        return 76 + 790 * (pd.Timestamp(date) - first).days / span

    def y(value):
        return 270 - 220 * (value - low) / (high - low)

    bits = [
        '<svg class="error-chart" viewBox="0 0 920 330" role="img" aria-label="Signed relative error by target week, with historical p75 and zero reference lines">',
        '<text x="76" y="22" class="axis-title">Signed relative error</text>',
    ]
    # Grid is drawn first so it never obscures the observations.
    for value in np.linspace(low, high, 6):
        bits += [
            f'<path class="grid horizontal" d="M76 {y(value):.2f} H866"/>',
            f'<text x="65" y="{y(value)+4:.2f}" text-anchor="end">{value:.2f}</text>',
        ]
    ticks = sorted(
        set(int(i) for i in np.linspace(0, len(dates) - 1, min(6, len(dates))))
    )
    for index in ticks:
        date = dates.iloc[index]
        bits += [
            f'<path class="grid vertical" d="M{x(date):.2f} 50 V270"/>',
            f'<text x="{x(date):.2f}" y="299" text-anchor="middle">{date.strftime("%d %b %Y")}</text>',
        ]
    bits.append(
        '<text x="471" y="324" text-anchor="middle" class="axis-title">Target week ending</text>'
    )
    for value, label, color, dash in (
        (0, "Zero error", "#94a3b8", "3 4"),
        (group.p75.iloc[0], "Historical p75", "#b7791f", "6 4"),
    ):
        if pd.notna(value):
            bits.append(
                f'<path d="M76 {y(value):.2f} H866" stroke="{color}" stroke-width="1" stroke-dasharray="{dash}"><title>{label}: {value:.4g}</title></path>'
            )
    previous = None
    for row in group.itertuples():
        if pd.isna(row.signed_error):
            title = escape(
                f"{pd.Timestamp(row.target_end).date()}: {getattr(row, 'status', 'undefined error')}"
            )
            cx = x(row.target_end)
            bits.append(
                f'<path class="missing-mark" d="M{cx-2:.2f} 276 l4 4 m-4 0 l4 -4"><title>{title}</title></path>'
            )
            previous = None
            continue
        cx, cy = x(row.target_end), y(row.signed_error)
        if previous and pd.Timestamp(row.target_end) - previous[0] == pd.Timedelta(
            days=7
        ):
            bits.append(
                f'<path d="M{previous[1]:.2f} {previous[2]:.2f} L{cx:.2f} {cy:.2f}" stroke="#94a3b8" stroke-width="1.1" fill="none"/>'
            )
        color = (
            "#dc2626"
            if row.alert
            else (
                "#d97706"
                if row.upper_flag
                else "#7c3aed" if row.underprediction_flag else "#0284c7"
            )
        )
        title = escape(
            f"{pd.Timestamp(row.target_end).date()}: error={row.signed_error:.4f}; upper={row.upper_flag}; underprediction={row.underprediction_flag}; alert={row.alert}"
        )
        bits.append(
            f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="2.5" fill="{color}"><title>{title}</title></circle>'
        )
        previous = (pd.Timestamp(row.target_end), cx, cy)
    return "".join(bits) + "</svg>"


STYLE = """
*{box-sizing:border-box}
body{font:14px system-ui,sans-serif;margin:0;background:#f6f8fb;color:#172033}
main{max-width:1240px;margin:auto;padding:32px 24px 64px}
h1{font-size:28px;margin:0 0 12px}h2{font-size:18px;margin:0 0 8px}
p{line-height:1.6}.muted{color:#64748b}.intro{max-width:950px}
.filters{display:flex;flex-wrap:wrap;gap:16px;padding:20px;background:white;border:1px solid #dce3ec;border-radius:10px;margin:24px 0 12px}
.filters label{display:flex;flex:1;min-width:145px;flex-direction:column;gap:7px;font-weight:600}
select{font:inherit;color:inherit;background:white;border:1px solid #cbd5e1;border-radius:6px;padding:9px;min-width:0;width:100%}
select:focus-visible,summary:focus-visible{outline:2px solid #2563eb;outline-offset:3px}
.legend{display:flex;flex-wrap:wrap;gap:12px 22px;margin:16px 0 8px;color:#475569;font-size:12px}
.legend span{display:inline-flex;align-items:center;gap:7px}
.dot{width:6px;height:6px;border-radius:50%;display:inline-block}
.swatch{width:23px;height:0;display:inline-block;border-top:1px dashed #b7791f}
.swatch.zero{border-color:#94a3b8}.cross{color:#94a3b8;font-style:normal}
.chart-card{background:white;border:1px solid #dce3ec;border-radius:10px;padding:22px;margin:16px 0}
[hidden]{display:none!important}
.error-chart{width:100%;display:block}
.error-chart text{font:11px system-ui,sans-serif;fill:#64748b}
.error-chart .axis-title{font-size:12px;fill:#475569}
.grid{stroke:#e9edf2;stroke-width:.7;fill:none}
.missing-mark{stroke:#94a3b8;stroke-width:1;fill:none}
details{margin-top:16px}summary{cursor:pointer;font-weight:600;padding:8px 0}
.table-scroll{overflow:auto;max-height:480px}
table{border-collapse:collapse;font-size:12px;white-space:nowrap}
th,td{padding:8px 10px;border:1px solid #e2e8f0;text-align:left}
th{background:#f1f5f9;position:sticky;top:0}
code{overflow-wrap:anywhere;font-size:11px}
.empty{padding:24px;background:white;border:1px dashed #cbd5e1;border-radius:8px}
@media(max-width:600px){main{padding:20px 12px}.chart-card{padding:12px}.filters{gap:12px;padding:14px}h1{font-size:23px}}
"""

SCRIPT = """
(() => {
  const cards = Array.from(document.querySelectorAll('.chart-card'));
  const fields = ['scope', 'region', 'model', 'lead'];
  const controls = Object.fromEntries(fields.map(key => [key, document.getElementById('filter-' + key)]));
  function options(key, source, initial) {
    const control = controls[key], previous = control.value;
    const values = Array.from(new Set(source.map(card => card.dataset[key])));
    values.sort((a,b) => a.localeCompare(b, undefined, {numeric:true}));
    control.replaceChildren();
    if (key !== 'scope') control.add(new Option('All ' + ({region:'regions',model:'models',lead:'leads'})[key], ''));
    values.forEach(value => control.add(new Option(key === 'lead' ? 'Lead ' + value : value, value)));
    control.value = initial ? (values[0] || '') : (values.includes(previous) ? previous : '');
  }
  function update() {
    let visible = 0;
    for (const card of cards) {
      const show = fields.every(key => !controls[key].value || card.dataset[key] === controls[key].value);
      card.hidden = !show;
      if (show) visible++;
    }
    document.getElementById('chart-count').textContent = 'Showing ' + visible + ' of ' + cards.length + ' charts';
    document.getElementById('empty-state').hidden = visible !== 0;
  }
  options('scope', cards, true);
  const scoped = () => cards.filter(card => card.dataset.scope === controls.scope.value);
  ['region','model','lead'].forEach(key => options(key, scoped(), true));
  const first = scoped()[0];
  if (first) ['region','model','lead'].forEach(key => { controls[key].value = first.dataset[key]; });
  controls.scope.addEventListener('change', () => {
    ['region','model','lead'].forEach(key => options(key, scoped(), false));
    update();
  });
  ['region','model','lead'].forEach(key => controls[key].addEventListener('change', update));
  update();
})();
"""


def render(
    current: pd.DataFrame,
    reference: pd.DataFrame,
    baseline_id: str,
    *,
    run_id: str = "",
    execution_id: str = "",
) -> str:
    pieces = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Acestor forecast monitoring</title>",
        f"<style>{STYLE}</style></head><body><main>",
        "<h1>Forecast performance monitoring</h1>",
        (
            f'<p class="muted">Run: {escape(run_id)} · Execution: {escape(execution_id)}</p>'
            if run_id
            else ""
        ),
        '<p class="intro muted">Compare signed forecast errors with historical p75. Select a region, model and lead below; choose “All” to compare several series. Each scope and spatial scale keeps its own reference.</p>',
        '<div class="filters" role="group" aria-label="Chart filters">',
    ]
    for key, label in (
        ("scope", "Scope / scale / derivation"),
        ("region", "Region"),
        ("model", "Model"),
        ("lead", "Lead"),
    ):
        pieces.append(
            f'<label for="filter-{key}">{label}<select id="filter-{key}" aria-label="{label}"></select></label>'
        )
    pieces += [
        '</div><p id="chart-count" class="muted" role="status" aria-live="polite"></p>',
        '<div class="legend" aria-label="Plot legend">',
        '<span><i class="dot" style="background:#0284c7"></i>No flag</span>',
        '<span><i class="dot" style="background:#d97706"></i>Above p75</span>',
        '<span><i class="dot" style="background:#dc2626"></i>Alert: 2+ consecutive weeks above p75</span>',
        '<span><i class="dot" style="background:#7c3aed"></i>Underprediction</span>',
        '<span><i class="swatch"></i>Historical p75</span>',
        '<span><i class="swatch zero"></i>Zero error</span>',
        '<span><i class="cross">×</i>Unavailable error</span></div>',
        '<p class="muted">Hover over a point for its value and both flag reasons. Point colour prioritises alert, then above-p75, then underprediction. Missing weeks break the line and alert sequence.</p>',
        "<noscript><p>Enable JavaScript to use the filters. All charts are shown below.</p></noscript>",
        '<p id="empty-state" class="empty" hidden>No charts match this selection.</p>',
    ]
    for keys, group in current.groupby(SERIES, sort=True):
        scope, scale, derivation, model, lead, region = map(str, keys)
        scope_label = f"{scope} / {scale} / {derivation}"
        attrs = " ".join(
            f'data-{key}="{escape(value, quote=True)}"'
            for key, value in (
                ("scope", scope_label),
                ("region", region),
                ("model", model),
                ("lead", lead),
            )
        )
        threshold = group.p75.iloc[0]
        threshold_text = f"{threshold:.4g}" if pd.notna(threshold) else "unavailable"
        pieces += [
            f'<section class="chart-card" {attrs}><h2>{escape(region)} — {escape(model)}, lead {escape(lead)}</h2>',
            f'<div class="muted">{escape(scope_label)} · Historical p75: {threshold_text}</div>',
            chart(group),
            '<details><summary>Predictions, actuals and flags</summary><div class="table-scroll">',
            group.to_html(index=False, escape=True, na_rep="NA"),
            "</div></details></section>",
        ]
    pieces += [
        '<details><summary>Historical reference statistics — all series</summary><div class="table-scroll">',
        reference.drop(columns="baseline_id", errors="ignore").to_html(
            index=False, escape=True, na_rep="NA"
        ),
        '</div></details><details><summary>Current coverage — all series</summary><div class="table-scroll">',
        current.groupby(["scope", "spatial_scale", "status"])
        .size()
        .rename("observations")
        .reset_index()
        .to_html(index=False, escape=True),
        "</div></details><details><summary>Method and reference identity</summary>",
        "<p>Signed error = (predicted − actual) / actual. References pool regions within each scope, scale, model and lead. Actuals completeness is unverified. Zero-actual observations are excluded. Alerts are recorded only; no notifications are sent.</p>",
        f"<p>Historical reference: <code>{escape(baseline_id)}</code></p></details>",
        f"</main><script>{SCRIPT}</script></body></html>",
    ]
    return "\n".join(pieces)
