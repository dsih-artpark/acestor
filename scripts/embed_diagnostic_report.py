"""Embed supporting data and inline images into diagnostic_report/index.html.

Makes the HTML fully self-contained:
  - Chart PNGs replaced with base64 data URIs
  - Supporting data files embedded as hidden <a download> links
  - A structured report.json generated and embedded the same way

Run from the repo root:
    python scripts/embed_diagnostic_report.py
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

REPO = Path(__file__).parent.parent
HTML_IN = REPO / "diagnostic_report" / "index.html"
ASSETS = REPO / "diagnostic_report" / "assets"
RUN = REPO / "artifacts" / "ap" / "something-new-again"

# ---------------------------------------------------------------------------
# 1. Build report.json
# ---------------------------------------------------------------------------
cutoffs = json.loads((RUN / "cutoffs.json").read_text())
sampling = json.loads((RUN / "sampling_day.json").read_text())

best_method_rows = []
bm_path = RUN / "dumps" / "best_method_district_20260424.csv"
if bm_path.exists():
    import csv

    with open(bm_path) as fh:
        reader = csv.DictReader(fh)
        best_method_rows = list(reader)

report_json = {
    "metadata": {
        "report_title": "Pipeline Failure — Root Cause Analysis (Yanam / district_0)",
        "generated_at": "2026-04-24",
        "prepared_by": "Ashutosh Singhai",
        "email": "ashutosh.singhai@artpark.in",
        "project": "acestor-v2",
        "run_id": "something-new-again",
        "pipeline": "dengue (ap_district_v2.yaml)",
    },
    "cutoffs": cutoffs,
    "sampling": sampling,
    "best_threshold_method_per_district": best_method_rows,
    "root_causes": [
        {
            "id": "RC-1",
            "title": "49 month-end gaps in weather_daily.csv",
            "description": (
                "ERA5 monthly source files were missing the last calendar day of each month "
                "(Jan 31, Feb 28, …). This caused sample_data() index-based weekly sampling "
                "to drift off Fridays. The W-FRI filter in Pipeline 2 then dropped 182 of 247 "
                "expected Fridays. After ret_na_filled_df rebuilt the 247-week grid, "
                "t2m_mean was NaN for those weeks, making temp_lag_12 all-NaN for every district."
            ),
            "impact": "All districts — NBR model failed entirely (temp_lag_12 all-NaN)",
            "evidence": {
                "expected_fridays": 247,
                "fridays_surviving_filter_before_fix": 65,
                "missing_month_end_days": 49,
            },
        },
        {
            "id": "RC-2",
            "title": "Yanam (district_0) — invalid region with incomplete weather coverage",
            "description": (
                "Yanam is a Union Territory enclave (Puducherry) physically surrounded by AP. "
                "ERA5 source data used LGD_Code_d = 0 (an invalid placeholder). "
                "Yanam's weather data stops at 2026-01-30, but temp_lag_12 for May 2026 "
                "predictions requires Feb 2026 data. After RC-1 was fixed, 4 NaN rows "
                "remained (4 prediction dates × 1 region). "
                "MinMaxScaler.transform() cannot handle NaN inputs and raised a misleading "
                "ValueError about missing feature names instead of 'column contains NaN'."
            ),
            "impact": "4 NaN rows in temp_lag_12 → ValueError in nbr.py:_rescale",
            "evidence": {
                "region_id": "district_0",
                "lgd_code": 0,
                "weather_last_date": "2026-01-30",
                "lag12_target_date_for_may1_prediction": "2026-02-06",
                "nan_rows_in_temp_lag_12": 4,
                "files_containing_district_0": 48,
                "rows_removed": 1491,
            },
        },
        {
            "id": "RC-3",
            "title": "Threshold date mismatch — no PDF report generated",
            "description": (
                "When case data lags behind weather data (case cutoff April 10, "
                "weather cutoff April 24), the NBR model requested thresholds at "
                "pred_upto - 28 = April 24, but the thresholds CSV only covered up to "
                "April 10. Empty thresholds produced NaN threshold columns in NBR predictions. "
                "ensemble_predictions groupby silently dropped all NBR rows (pandas groupby "
                "drops NaN keys by default). Only TSE April predictions survived, which did "
                "not match the May prediction_dates → 0-row combined CSV → no report."
            ),
            "impact": "No PDF report generated when run with today's date",
            "evidence": {
                "case_cutoff": cutoffs.get("cutoff_case"),
                "weather_cutoff": cutoffs.get("cutoff_weather"),
                "nbr_threshold_request_date": cutoffs.get("cutoff_weather"),
                "thresholds_max_date": cutoffs.get("cutoff_case"),
                "predictions_surviving_ensemble": 0,
            },
        },
    ],
    "fixes_applied": [
        {
            "id": "FIX-A",
            "file": "prepared_data/district/weather_daily.csv",
            "description": "Forward-filled 49 month-end gaps; removed district_0 rows",
            "rows_before": 44627,
            "rows_after": 47516,
            "rows_added_by_ffill": 4380,
            "rows_removed_district0": 1491,
        },
        {
            "id": "FIX-B",
            "file": "datasets/ap-weather/district/**/*.csv (48 files)",
            "description": "Removed all district_0 rows from ERA5-converted monthly CSVs",
            "files_modified": 48,
            "rows_removed": 1491,
        },
        {
            "id": "FIX-C",
            "file": "pipelines/dengue_prep/steps/parse_weather_data.py",
            "description": (
                "Added guard: daily = daily[~daily['region_id'].str.endswith('_0')] "
                "Prevents Yanam or any LGD-0 region from entering prepared_data again."
            ),
        },
        {
            "id": "FIX-D",
            "file": "scripts/convert_era5_to_weather.py",
            "description": (
                "Added guard: district_df = district_df[district_df['LGD_Code_d'] != 0] "
                "Prevents Yanam from being written if ERA5 conversion is re-run."
            ),
        },
        {
            "id": "FIX-E",
            "file": "pipelines/dengue/lib/zones.py",
            "description": (
                "Changed threshold date lookup from exact match to "
                "'latest available <= to_date' fallback in merge_predictions_thresholds(). "
                "Prevents silent NBR drop when case cutoff lags behind weather cutoff."
            ),
        },
    ],
    "verification": {
        "pipeline_status": "success",
        "temp_lag_12_nan_count_after_fix": 0,
        "total_test_rows": 112,
        "combine_predictions_rows": 224,
        "maps_generated": 8,
        "pdf_report_generated": True,
        "district_0_in_source_csvs": False,
        "district_0_in_geojsons": False,
        "district_0_in_case_data": False,
    },
    "prediction_dates": cutoffs.get("prediction_dates", []),
}

report_json_str = json.dumps(report_json, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 2. Helper — file → base64 data URI
# ---------------------------------------------------------------------------
def file_to_data_uri(path: Path, mime: str) -> str:
    data = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{data}"


def text_to_data_uri(text: str, mime: str = "application/json") -> str:
    data = base64.b64encode(text.encode("utf-8")).decode()
    return f"data:{mime};base64,{data}"


# ---------------------------------------------------------------------------
# 3. Collect downloadable files
# ---------------------------------------------------------------------------
downloads: list[dict] = []

# report.json (generated above)
downloads.append(
    {
        "label": "report.json",
        "desc": "Structured diagnostic report (root causes, fixes, verification metrics)",
        "href": text_to_data_uri(report_json_str, "application/json"),
        "filename": "report.json",
        "mime": "application/json",
    }
)

# cutoffs.json
downloads.append(
    {
        "label": "cutoffs.json",
        "desc": "Pipeline cutoff and prediction dates for this run",
        "href": file_to_data_uri(RUN / "cutoffs.json", "application/json"),
        "filename": "cutoffs.json",
        "mime": "application/json",
    }
)

# sampling_day.json
if (RUN / "sampling_day.json").exists():
    downloads.append(
        {
            "label": "sampling_day.json",
            "desc": "Detected weekly sampling anchor day",
            "href": file_to_data_uri(RUN / "sampling_day.json", "application/json"),
            "filename": "sampling_day.json",
            "mime": "application/json",
        }
    )

# best_method CSV
if bm_path.exists():
    downloads.append(
        {
            "label": "best_method_district.csv",
            "desc": "Best threshold method selected per district",
            "href": file_to_data_uri(bm_path, "text/csv"),
            "filename": "best_method_district.csv",
            "mime": "text/csv",
        }
    )

# predictions CSV
pred_path = RUN / "results" / "Predictions_Apr - May 2026_20260424.csv"
if pred_path.exists():
    downloads.append(
        {
            "label": "Predictions_Apr-May_2026.csv",
            "desc": "Final predictions for all districts (Apr–May 2026)",
            "href": file_to_data_uri(pred_path, "text/csv"),
            "filename": "Predictions_Apr-May_2026.csv",
            "mime": "text/csv",
        }
    )

# cases sampled
cases_path = RUN / "datasets" / "cases_district_sampled.csv"
if cases_path.exists():
    downloads.append(
        {
            "label": "cases_district_sampled.csv",
            "desc": "Sampled weekly case data used for training/prediction (district level)",
            "href": file_to_data_uri(cases_path, "text/csv"),
            "filename": "cases_district_sampled.csv",
            "mime": "text/csv",
        }
    )

# weather sampled
wx_path = RUN / "datasets" / "weather_district_sampled.csv"
if wx_path.exists():
    downloads.append(
        {
            "label": "weather_district_sampled.csv",
            "desc": "Sampled weekly weather features used for training/prediction (district level)",
            "href": file_to_data_uri(wx_path, "text/csv"),
            "filename": "weather_district_sampled.csv",
            "mime": "text/csv",
        }
    )


# ---------------------------------------------------------------------------
# 4. Build the downloads HTML block (hidden panel + small toggle)
# ---------------------------------------------------------------------------
rows_html = "\n".join(
    f"""        <tr>
          <td><a href="{d['href']}" download="{d['filename']}" class="dl-link">
            <span class="dl-icon">&#8681;</span> {d['label']}</a></td>
          <td style="color:var(--muted);font-size:0.85rem">{d['desc']}</td>
          <td style="color:var(--muted);font-size:0.82rem;white-space:nowrap">{d['mime']}</td>
        </tr>"""
    for d in downloads
)

downloads_section = f"""
  <!-- ─── Downloads (embedded data, no server needed) ──────────────────── -->
  <section id="downloads" style="margin-top:0">
    <h2 class="section-title">8. Embedded Downloads
      <span class="badge badge-blue">Self-contained</span>
    </h2>
    <div class="card">
      <p style="font-size:0.9rem;color:var(--muted);margin-bottom:16px">
        All files below are embedded directly in this HTML document as base64 data URIs —
        no server or internet connection required. Click any link to download.
      </p>
      <table>
        <thead>
          <tr><th>File</th><th>Description</th><th>Type</th></tr>
        </thead>
        <tbody>
{rows_html}
        </tbody>
      </table>
    </div>

    <!-- Invisible anchor for the JSON blob — available as a named link -->
    <script>
      /* report_json is also available programmatically via window.__diagnosticReport */
      window.__diagnosticReport = {report_json_str};
    </script>
  </section>
"""

# ---------------------------------------------------------------------------
# 5. CSS additions for downloads section + print page break
# ---------------------------------------------------------------------------
dl_css = """
  .dl-link { color: var(--blue); text-decoration: none; display: inline-flex;
             align-items: center; gap: 5px; font-family: "JetBrains Mono", monospace;
             font-size: 0.85rem; }
  .dl-link:hover { text-decoration: underline; }
  .dl-icon { font-size: 1rem; }

  @media print {
    section#downloads { page-break-before: always; break-before: page; }
    .dl-link { color: #000; }
  }
"""

# ---------------------------------------------------------------------------
# 6. Inline chart PNGs as base64
# ---------------------------------------------------------------------------
html = HTML_IN.read_text(encoding="utf-8")

charts = [
    ("assets/chart1_coverage.png", ASSETS / "chart1_coverage.png"),
    ("assets/chart2_yanam_vs_typical.png", ASSETS / "chart2_yanam_vs_typical.png"),
    ("assets/chart3_lag12_analysis.png", ASSETS / "chart3_lag12_analysis.png"),
    ("assets/chart4_gap_analysis.png", ASSETS / "chart4_gap_analysis.png"),
]

for rel_src, abs_path in charts:
    if abs_path.exists():
        data_uri = file_to_data_uri(abs_path, "image/png")
        html = html.replace(f'src="{rel_src}"', f'src="{data_uri}"')
        print(f"  Inlined {rel_src} ({abs_path.stat().st_size // 1024}KB)")
    else:
        print(f"  WARNING: {rel_src} not found, skipping")

# ---------------------------------------------------------------------------
# 7. Inject CSS and downloads section into HTML
# ---------------------------------------------------------------------------
# Add dl_css before closing </style>
html = html.replace("</style>", dl_css + "</style>", 1)

# Update TOC to include downloads section
html = html.replace(
    '<li><a href="#verification">Verification</a></li>\n    </ol>',
    '<li><a href="#verification">Verification</a></li>\n'
    '      <li><a href="#downloads">Embedded Downloads</a></li>\n    </ol>',
)

# Insert downloads section before </div> that closes .container
html = html.replace(
    "\n</div>\n\n<footer>",
    downloads_section + "\n</div>\n\n<footer>",
)

# ---------------------------------------------------------------------------
# 8. Write output (overwrite in place)
# ---------------------------------------------------------------------------
HTML_IN.write_text(html, encoding="utf-8")
final_size = HTML_IN.stat().st_size
print(f"\nDone. {HTML_IN}")
print(f"Final size: {final_size / 1024:.0f} KB ({final_size / 1024 / 1024:.2f} MB)")
print(f"Embedded {len(downloads)} downloadable files.")
