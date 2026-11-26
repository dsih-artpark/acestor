---
name: onboard-geography
description: Take a new state, city or country from raw files to a first acestor dengue forecast, with data quality checks at every step. Use this whenever someone wants to run or test acestor on a new geography, asks what data is needed or whether their data is good enough, brings case data (a line list or aggregated counts) or boundary files (shapefile or GeoJSON) for a place acestor does not cover yet, or asks for a data readiness or gap assessment, even if they do not say "onboard".
---

# Onboard a new geography

A person arrives with case data and boundaries for a place acestor has never
run on. They want to know whether the data is good enough and, if it is, to
see a first forecast. This skill takes them there in gated stages. Each stage
runs deterministic checks and saves a report.

The checks exist because the pipelines accept bad input quietly, and a
forecast that ran looks trustworthy whether or not its inputs were. The
dangerous faults are the ones where the file is well formed but means
something other than what the pipeline assumes: counts read as cases, cases
placed at the hospital instead of the home, running totals read as new cases,
dates shifted by a day/month swap. Catch these before a forecast exists.

## How the stages work

All commands run from the repository root. Write paths out in full; do not
put them in shell variables.

```bash
uv run python .claude/skills/onboard-geography/scripts/onboard.py <stage> \
  --workdir <work folder> --region-type <level> ...
```

- `--region-type` is the administrative level (`district`, `subdistrict`,
  `mandal`, `corp`, `zone` or `ward`). Pass the same value to every stage. It
  defaults to `district`, and a stage run with the wrong level looks in the
  wrong boundary folder.
- Each stage prints the checks that did not pass (add `--full` for all),
  saves the full report to `<work folder>/onboarding/<stage>.json` with a hash
  of each input file, and exits 1 if any check failed. A rerun keeps the
  earlier report as `<stage>.run<n>.json`.
- **`fail` stops the work.** If the user can resolve it now, resolve it and
  rerun the stage. If it needs the data supplier, or the user cannot be asked,
  stop there and go straight to the readiness summary, with the questions
  that need an answer. "Use your judgment" does not cover a failed check: a
  forecast built on a guess looks as valid as any other. Do not repair data on a guess: no renaming codes,
  no shifting dates, no dropping rows, unless the user confirms what the
  values mean.
- **`warn` does not stop the work** but every warning must reach the user in
  plain words.

## What to ask for

- The case data: one file, or a folder of files such as one per year.
  `inspect` works out whether they hold one row per case or counts per unit
  and date, but ask the user too, as a cross-check.
- Boundaries for the level to forecast at: a shapefile, GeoJSON or
  GeoPackage, or a folder of GeoJSON files.
- A short code and a display name for the geography, and the level.

Make a work folder for the geography and keep everything inside it.

Line lists hold personal data. Read them where they are, never quote rows in
chat or in a report, and report only counts, dates and unit codes. If the
data is on a server or in cloud storage, ask the user for the location and
for confirmation before copying anything.

## Stages

### 1. Boundaries

```bash
... onboard.py columns --file <boundary file>
... onboard.py boundaries --workdir <work folder> --region-type <level> \
  --input <boundary file> --code-col <code column> --name-col <name column>
```

`columns` lists a file's or folder's columns with the number of distinct
values in each, for boundary and case files alike. It shows no values, since
a line list's values are personal data; do not print them yourself either. `--parent-id` and `--parent-name` are optional: give them when
the user names the unit above (for example `state_29`, Karnataka), and leave
them out otherwise.

The stage checks codes, shapes and the coordinate system, then writes one
GeoJSON per unit under `<work folder>/geojsons/<level>s/`, named
`<level>_<code>`. Codes need not be numeric.

### 2. Inspect the case data

```bash
... onboard.py columns --file <case file or folder>
... onboard.py inspect --workdir <work folder> --region-type <level> \
  --cases <case file or folder> --date-col <col> --unit-col <col> [--count-col <col>]
```

Name the columns yourself, after looking at `columns` and asking the user
when it is not obvious. Which column is the date, the unit or the count is a
question about what the data means, and column names mislead: real files
carry several dates (onset, sample, report), several locations (patient,
facility, lab) and several counts (suspected, tested, positive, running
totals). Leave `--count-col` out for a line list.

This stage is the gap assessment the user came for. It reports the kind of
file, date range, frequency, total cases, and these judgements:

| Question | Checks | What to do when it objects |
|---|---|---|
| Do all files share a layout? | `files_have_columns` | Leave out the files it names, or ask for them in the same layout |
| Are rows cases or counts? | `kind_clear` | Ask the user. Counts split by age, sex, facility or area type are added together only once they confirm the groups do not overlap |
| Are counts filled in? | `blank_counts` | Ask whether blank means zero or not reported. The pipeline reads a day left out as zero too, so add `--blank-as-zero` here and in stage 3 if blank means zero or the user accepts the undercount; report the number of blanks either way |
| Can every row be placed? | `rows_have_dates`, `rows_have_units` | Tell the user how many rows are left out |
| Is every row a dengue case? | `one_kind_of_case` | Rerun with `--filter "Column=Value1,Value2"` to keep the positive dengue rows, and use the same filter in stage 4 |
| Are cases placed where patients live? | `unit_is_patient_location`, `one_location_column`, `coordinates_are_patient_locations`, `cases_spread_across_units` | Ask which column is the patient's residence. A facility column puts cases where people are treated |
| Are counts new cases? | `counts_are_new_cases` | Running totals must be converted by the supplier |
| Are dates read correctly? | `dates_readable`, `frequency` | Ask the supplier; do not shift dates |
| Is one year unlike the others? | `years_are_comparable` | Ask whether it was an outbreak or a change in the data (a repeated file, a wider case definition, more testing). Do not correct it yourself |
| Is the series complete and current? | `history_length`, `data_is_recent`, `no_missing_periods`, `units_report_to_the_end`, `no_long_silences` | Ask whether gaps are zero cases or no reporting. A stretch with no record for any unit is missing data, and the pipeline would read it as zero cases |
| Do unit codes match the boundaries? | `case_units_have_boundaries` | Ask whether the units are miscoded or missing from the boundary file. `near_matches` lists codes that differ only by zeros, spaces or letter case |

If the user confirms that units without a boundary should be left out, go on:
stage 3 sets them aside and reports how many cases went with them.

### 3. Prepare the cases

The two kinds of file take different routes. Follow the `kind` from stage 2.

**Aggregated counts** are standardised here and written directly:

```bash
... onboard.py cases --workdir <work folder> --region-type <level> --input <file> \
  --date-col <col> --unit-col <col> --count-col <col>
```

Add `--sum-breakdowns` only after the user confirms that rows splitting one
unit and date are separate groups, and `--blank-as-zero` only after they
confirm that a blank count means zero. The report reconciles the totals:
source cases equal written cases, plus cases set aside for units without a
boundary, plus cases on rows without a date. Keep `written_cases` for stage 5.

**A line list** is parsed by the prep pipeline, which counts one case per
row. Skip this command and pass the line-list options in stage 4. The parser
needs the unit as a full ID (`district_344`) or a numeric code (`344`). If
the file has neither, stop and tell the user what is missing.

The parser also needs dates in one layout and without a time zone. When
`parser_reads_dates` fails, ask the user before changing anything; with their
agreement, rewrite only the date column as YYYY-MM-DD in a copy of the files,
and run stage 2 again on the copy.

### 4. Configs

```bash
... onboard.py configs --workdir <work folder> --region-type <level> \
  --name <code> --title "<name>" --date-start <first data date> \
  --run-date <see below> --history-years <calendar years of data>
# line list only:
#   --linelist-dir <folder> --linelist-date-col <col>
#   --linelist-unit-col <col of full IDs>  or  --linelist-code-col <col of numeric codes>
#   --linelist-filter "Column=Value1,Value2"   (the filters used in stage 2)
```

- **Run date.** Use the last date in the case data (`date_end` from stage
  2). A later date fails `run_date_has_data`: the pipeline forecasts from the
  gap and comes out too low, without an error. If the user asked for a later
  date, run on the last data date and say so. For weekly data it must fall on the weekday the weeks are dated on
  (`week_date_weekday` from stage 2); on any other weekday the pipeline leaves
  the latest week out.
- **History years** is the number of calendar years the data covers, given
  as `calendar_years` in the `history_length` check.

Weather comes from Open-Meteo's ERA5 archive for each unit, up to the run
date, so the user supplies none.

### 5. Prep pipeline, then verify

```bash
uv run python -m acestor.run --pipeline pipelines.dengue_prep.pipeline:build_pipeline \
  --config <work folder>/<code>_<level>_prep.yaml --run-id prep-<run date>
... onboard.py verify-prep --workdir <work folder> --region-type <level> \
  --date-start <first data date> --weather-end <run date> --expect-cases <total>
```

`--expect-cases` is `written_cases` from stage 3, or `total_cases` from stage
2 for a line list. `verify-prep` checks the prepared files against their
schema contracts, compares the prepared cases with what `inspect` read for
every unit and date (which catches a day/month swap that a total would miss),
and checks that every unit has weather for every day.

### 6. Forecast pipeline, then verify

```bash
uv run python -m acestor.run --pipeline pipelines.dengue.pipeline:build_pipeline \
  --config <work folder>/<code>_<level>_forecast.yaml --run-id fc-<run date>
... onboard.py verify-forecast --workdir <work folder> --region-type <level> \
  --run-dir <work folder>/artifacts/forecast/fc-<run date>
```

The first run searches for model settings (100 trials per model, the
pipeline's default) and takes about 10 minutes for one model, so run it in the
background and wait. `tune: false` in the config means "reuse saved settings
when they match", and they never match a new run date, so every change of run
date searches again. Confirm it printed `status=success`, then read
`forecast_near_recent_counts`: a successful run can still forecast near zero.
`verify-forecast` records the settings the search chose. That search is not
seeded, so two identical runs give slightly different forecasts.

In `predictions.csv`, `dateOfComputingPrediction` is the day the pipeline
was executed, not the run date.

## What to give the user

1. **What was supplied:** kind of case data, date range, frequency, units,
   total cases.
2. **What needs their decision:** every failed check, in plain words, with
   who can resolve it.
3. **What they should know:** every warning, plus anything you noticed that
   no check covers, such as boundaries that look like placeholders, very
   small counts, or data limited to positive tests.
4. **What was produced,** if a forecast ran: forecast weeks, units, models,
   and the paths to `outputs/report.html` and `outputs/predictions.csv`. If
   the work stopped at a failed check, say which stage and why.
5. **What this does not show.** A first forecast shows the pipeline runs on
   the data. It says nothing about accuracy, which needs a backtest over past
   seasons against a naive baseline.

## Schema contracts

`... onboard.py contracts` prints the contracts the checks enforce:
aggregated cases as supplied, prepared cases, prepared weather and
predictions. Use it when a user asks what format their data must be in.

## Limits

- **Evaluation is not covered.** `scripts/compute_backtest_metrics.py` is tied
  to one geography's paths. For any backtest, tune once and then set
  `tune: "never"`, or every past date retunes.
- **Levels.** The forecast pipeline supports only the levels listed above;
  `configs` fails for any other.
- **Line lists must carry a unit code.** Placing cases by coordinates or by
  address is supported by the parser but not set up by this skill.
- **Filters cannot say "either".** A filter keeps rows matching every
  condition. A line list with two test columns, where a case is positive on
  either, cannot be filtered to its positives by the skill or by the parser.
  Say so, and ask for a single result column.
- **Only whole-number counts are accepted.** Rates or fractional counts fail
  the aggregated contract.
- **A unit's series ends at its last row,** not at the run date. Counts keep
  their reported zeros, so this mostly affects line lists, where a unit with
  no recent case has no recent row. `series_reach_run_date` in `verify-prep`
  lists the units; their forecasts can be stale or zero without any error, so
  compare them with recent counts and say so in the summary.

## Tests

```bash
uv run pytest .claude/skills/onboard-geography/tests -q --no-cov
```
