# Troubleshooting

Common errors and how to fix them.

---

## Pipeline fails with `EmptyDataError` in `identify_cutoff_dates`

**Symptom**

```
pandas.errors.EmptyDataError: No columns to parse from file
```

**Cause**

`data.weather_download.enabled` is `false` (or missing). When disabled, the download step returns an empty file list. `parse_weather_data` writes an empty string to the weather artifact, and the next step fails trying to read it.

**Fix**

Set `enabled: true` and point `source_path` at your pre-parsed weather CSVs:

```yaml
data:
  weather_download:
    enabled: true
    source_backend: "filesystem"
    source_path: "ap_datasets/parsednetcdf/district"
    region_type: "district"
```

---

## `AttributeError: Can only use .str accessor with string values, not floating`

**Symptom**

```
AttributeError: Can only use .str accessor with string values, not floating
```

Occurs in `assess_thresholds` or `generate_maps`.

**Cause**

`regionID` values are numeric floats (e.g. `502.0`) rather than prefixed strings (e.g. `"district_502"`). The pipeline expects string IDs of the form `"{region_type}_{id}"`.

**Fix**

Ensure all region IDs are prefixed everywhere they appear:

- **Case CSV** (`location.admin2.ID` column): `"district_502"`
- **Weather CSV** (`region_id` column): `"district_502"`
- **GeoJSON filenames**: `district_502.geojson`

If you are using a data transformation script, add the prefix when writing each file. For example:

```python
df["location.admin2.ID"] = "district_" + df["District Code"].astype("Int64").astype(str)
```

---

## `pdflatex` not found — PDF not generated despite `compile_pdf: true`

**Symptom**

```
FileNotFoundError: [Errno 2] No such file or directory: 'pdflatex'
```

or the report step completes but no `.pdf` is produced.

**Cause**

`uv run` uses a clean subprocess environment. If `pdflatex` is installed outside the standard `PATH` (e.g. `/Library/TeX/texbin` on macOS), it is not visible to the pipeline.

**Fix**

Prepend the TeX binary directory when invoking the pipeline:

```bash
PATH="/Library/TeX/texbin:$PATH" uv run python -m acestor.run \
  --pipeline pipelines.dengue.pipeline:build_pipeline \
  --config configs/ap_district.yaml \
  --run-id ap-v1
```

To make this permanent, add to your `.env` file or shell profile:

```bash
export PATH="/Library/TeX/texbin:$PATH"
```

Verify pdflatex is available before running:

```bash
which pdflatex
```

---

## Maps are missing from the PDF report

**Symptom**

The PDF compiles but contains no choropleth map images, only empty sections.

**Cause**

`generate_report` filters prediction figures to only those whose start date is ≥ `run_date`. If `run_date` is set to today and the data cutoff is in the past (e.g. weather data ends several weeks ago), all prediction weeks fall before `run_date` and are filtered out.

**Fix**

Leave `run_date` blank in your config so the pipeline uses the actual data cutoff as the reference date:

```yaml
run:
  run_date: ""   # leave blank — the pipeline resolves this automatically
```

If you must set a fixed `run_date`, set it to a date close to your data cutoff, not today's date.

---

## ERA5 wide CSV fails with `time data does not match format`

**Symptom**

```
ValueError: time data '2022-01-31-1' does not match format '%Y-%m-%d'
```

Occurs when parsing ERA5 monthly CSV files with a transformation script.

**Cause**

ERA5 CSVs sometimes contain duplicate date columns (e.g. two columns named `mean.X2022.01.31`). Pandas automatically renames the second occurrence to `mean.X2022.01.31.1`, which after replacing dots with dashes becomes `2022-01-31-1` — an unparseable date string.

**Fix**

Filter date columns with a strict regex before processing:

```python
import re
_DATE_COL_RE = re.compile(r"^mean\.X\d{4}\.\d{2}\.\d{2}$")

date_cols = [c for c in df.columns if _DATE_COL_RE.match(c)]
```

This drops any pandas-suffixed duplicates automatically.

---

## No pipeline logs — all output is silent

**Symptom**

The pipeline runs (or fails) but prints nothing to the terminal. No log file is created.

**Cause**

The `logging:` section is missing from the YAML config. Without it, the pipeline attaches a `NullHandler` and all log records are silently discarded.

**Fix**

Add a `logging` section to your config:

```yaml
logging:
  level: INFO
```

Valid levels: `DEBUG`, `INFO`, `WARNING`, `ERROR`.

---

## Pipeline run exits with status `failed` but no error message

If logging is enabled but output is still sparse, run with `DEBUG` level to see every step:

```yaml
logging:
  level: DEBUG
```

Also check that your `storages.artifacts.filesystem.base_path` directory is writable and has sufficient disk space.

---

## `openpyxl` not installed — Excel read fails

**Symptom**

```
ModuleNotFoundError: No module named 'openpyxl'
```

**Cause**

`openpyxl` is required to read `.xlsx` files but is not installed in the active virtual environment.

**Fix**

```bash
uv add openpyxl
```

Or add it to `pyproject.toml` under the appropriate extras and re-sync:

```bash
uv sync --all-extras
```

---

## GeoJSON step skips rows — `invalid literal for int()`

**Symptom**

Warning during GeoJSON transformation:

```
WARNING: Skipping row with invalid LGD code: 'NA'
```

**Cause**

Some rows in the district shapefile contain `'NA'` or other non-numeric values in the LGD code column. These cannot be converted to integer region IDs.

**Behaviour**

The transformation script skips these rows and logs a warning. This is expected for placeholder or non-standard entries (e.g. Yanam region with LGD code `0`).

**Fix**

No action required if the skipped regions are not part of your target geography. If a valid district is being skipped, check the raw shapefile for data quality issues in the LGD column.
