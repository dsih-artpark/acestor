"""Dengue-dashboard backend case source.

Fetches IHIP-shaped case data from a running dengue-dashboard instance and
writes it as an XLSX file into a local staging directory. The rest of the
prep pipeline reads the XLSX unchanged — the dashboard's ``/api/cases/export.xlsx``
endpoint already emits the 30 canonical IHIP L-form headers plus a ``Region Id``
column, matching what ``parse_case_data`` expects.

Auth flow
---------
1. ``POST {base_url}/api/auth/login`` with ``{"email": <client_id>, "password": <client_secret>}``
   → response body ``{"access_token": "..."}``.
2. ``GET {base_url}/api/cases/export/stream?from=<date>&to=<date>`` with
   ``Authorization: Bearer <token>`` → ``application/x-ndjson`` response body,
   one IHIP record per line. Records are collected and materialised locally
   into an XLSX file so the rest of the pipeline (filename pattern, parser)
   stays unchanged.

The older ``/api/cases/export.xlsx`` endpoint is deprecated on the dashboard
side and used to 504 at the ingress on multi-year windows because the server
rendered the entire xlsx before the first byte. The streaming endpoint has
no such render latency.

Env-var names use the generic ``DASHBOARD_CLIENT_ID`` / ``DASHBOARD_CLIENT_SECRET``
convention (email/password under the hood) so the same source can front any
dashboard-compatible backend without renaming secrets.

Configuration
-------------
YAML (data.case_download):
    source_mode:   dashboard
    source_path:   "./cache/dashboard_cases"   # staging dir for downloaded XLSX
    base_url:      "https://dashboard.example.com"  # falls back to DASHBOARD_URL env
    date_start:    "2024-01-01"                 # sent as `?from=`
    date_end:      ""                            # empty → today; sent as `?to=`
    backfill_days: 30                            # on repeat runs, re-fetch last N days
    disease:       "Dengue"                      # optional filter passed through
    chunk_days:    365                           # split fetch window into N-day chunks to avoid 504s

Environment variables:
    DASHBOARD_URL           — base URL (fallback if base_url not in YAML)
    DASHBOARD_CLIENT_ID     — sent as `email` on /api/auth/login
    DASHBOARD_CLIENT_SECRET — sent as `password` on /api/auth/login

Incremental / backfill behaviour
--------------------------------
The source stages one XLSX per fetch, named
``dashboard_<from>_to_<to>.xlsx``, in ``source_path``. On each run:

- First run (empty staging dir): fetch the full ``[date_start, date_end]``.
- Repeat run: fetch ``[max(date_start, latest_staged_end - backfill_days + 1),
  date_end]`` — only the last ``backfill_days`` days plus anything past the
  last staged file's end. Older staged files are left in place.
- Files whose date range overlaps the new fetch window are deleted before the
  new file is written, so ``parse_case_data`` doesn't double-count cases
  present in both.

The reason backfill exists: case reporting lag. A patient with onset date two
weeks ago can be entered into IHIP today, so we cannot assume "dates older
than today are set in stone." ``backfill_days`` is the operator's estimate of
how far back late-arriving cases can appear. To force a full re-download,
delete the staging directory (or set ``backfill_days`` >= the full range).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import requests

from pipelines.dengue_prep.lib.case_sources import CaseSource

# Reparent under the acestor.dengue_prep tree so INFO logs actually reach
# stdout + run.log (acestor's create_logger only attaches handlers to
# ``acestor.*``; module-level loggers under ``pipelines.*`` propagate to root
# where the default level is WARNING and records are silently dropped).
log = logging.getLogger("acestor.dengue_prep.case_sources.dashboard")


_LOGIN_PATH = "/api/auth/login"
# NDJSON streaming endpoint — one IHIP record per line, no server-side render,
# no ingress timeout on multi-year windows (the older ``/api/cases/export.xlsx``
# repeatedly 504'd because it rendered the entire xlsx before the first byte).
_EXPORT_PATH = "/api/cases/export/stream"

_STAGED_FILENAME_RE = re.compile(
    r"^dashboard_(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})\.(?:csv|xlsx)$"
)
# New writes are ``.csv``. openpyxl's cost of materialising a big xlsx in RAM
# (~1 KB/cell, so a 200k-row × 30-col linelist peaks at 6-8 GB) OOM'd t3.micro
# and even bigger caller instances during the 2026-08-27 backfill of a
# 217k-record chunk. csv streams row-by-row so peak memory is O(1 row)
# regardless of file size. The ihip parser already reads both formats
# (pipelines/dengue_prep/lib/ihip.py::_read_file), so this is purely a
# staging-format change.
_STAGED_FILE_SUFFIX = ".csv"

# Default candidate date columns for reading max-date out of pre-existing
# xlsx files. Ordered so the most-populated column in dashboard exports comes
# first. Overridden per-run when the step forwards ``case_parse.date_column``.
_DEFAULT_DATE_CANDIDATE_COLS: tuple[str, ...] = (
    "Test Performed Date",
    "Date Of Onset",
    "Sample Collected Date",
)

# Canonical IHIP header set — used only for empty-window fetches to write a
# parseable header-only csv (empty DataFrame → pd.to_csv writes zero bytes,
# and pd.read_csv on that raises EmptyDataError which the parser can't
# distinguish from a real failure). Not authoritative for schema — the actual
# fetch always yields whatever columns the dashboard's export emits.
_IHIP_HEADERS: tuple[str, ...] = (
    "Patient Name",
    "Contact Number",
    "Gender",
    "Age",
    "Village Or Ward",
    "Sub District",
    "Ulb",
    "District",
    "State",
    "Patient Address",
    "Patient Health Id",
    "Patient Transaction Id",
    "Opd Ipd",
    "Provisional Diagnosis",
    "Date Of Onset",
    "Sample Type",
    "Patient Specimen Id",
    "Provisional Diagnosis Name",
    "Test Suspected For",
    "Test Performed",
    "Sample Collected Date",
    "Test Result",
    "Confirmed Diagnosis",
    "Pathogen Name",
    "Pathogen Subtype",
    "Test Performed Date",
    "Facility Name Pform",
    "Facility Name Lform",
    "Address Geocoded Longitude",
    "Address Geocoded Latitude",
    "Region Id",
)


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _subtract_coverage(
    window_start: date,
    window_end: date,
    coverage: list[tuple[date, date]],
) -> list[tuple[date, date]]:
    """Return sub-ranges of [window_start, window_end] not covered by any span."""
    if not coverage:
        return [(window_start, window_end)]
    # Clip + sort by start.
    clipped = sorted(
        (max(s, window_start), min(e, window_end))
        for s, e in coverage
        if e >= window_start and s <= window_end
    )
    gaps: list[tuple[date, date]] = []
    cursor = window_start
    for s, e in clipped:
        if s > cursor:
            gaps.append((cursor, s - timedelta(days=1)))
        if e >= cursor:
            cursor = e + timedelta(days=1)
    if cursor <= window_end:
        gaps.append((cursor, window_end))
    return gaps


def _merge_into(ranges: list[tuple[date, date]], new: tuple[date, date]) -> None:
    """Insert ``new`` into a sorted list of (start, end), merging overlaps."""
    ranges.append(new)
    ranges.sort()
    merged: list[tuple[date, date]] = []
    for s, e in ranges:
        if merged and s <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    ranges[:] = merged


def _scan_staged_files(staging_dir: Path) -> list[tuple[date, date, Path]]:
    """Return (start_date, end_date, path) for every dashboard_*.xlsx already staged."""
    if not staging_dir.exists():
        return []
    out: list[tuple[date, date, Path]] = []
    for p in staging_dir.iterdir():
        m = _STAGED_FILENAME_RE.match(p.name)
        if m is None:
            continue
        try:
            out.append((_parse_date(m.group(1)), _parse_date(m.group(2)), p))
        except ValueError:
            continue
    return out


def _read_staged_file(path: Path):
    """Read a staged case file into a DataFrame. Format-agnostic — picks
    ``pd.read_csv`` or ``pd.read_excel`` based on file suffix so the trim /
    consolidate paths work uniformly across the transition from xlsx to
    csv staging.

    Uses ``path.suffixes`` (not ``.suffix``) so atomic staging paths like
    ``<name>.csv.tmp`` are treated as csv rather than falling through to
    the xlsx reader on the ``.tmp`` suffix.
    """
    import pandas as pd  # noqa: PLC0415

    suffixes = [s.lower() for s in path.suffixes]
    if ".csv" in suffixes:
        return pd.read_csv(path)
    return pd.read_excel(path)


def _write_staged_file(df, path: Path) -> None:
    """Write a DataFrame to a staged case file. csv writes stream row-by-row
    (bounded memory) — always preferred. xlsx is retained only so callers
    that explicitly ask for ``.xlsx`` (existing on-disk files being trimmed
    in place before their eventual conversion) still work.

    Format is picked from the first meaningful suffix in ``path.suffixes``
    so writes to atomic staging paths like ``<name>.csv.tmp`` still land as
    csv rather than falling through to xlsx.
    """
    suffixes = [s.lower() for s in path.suffixes]
    if ".csv" in suffixes:
        df.to_csv(path, index=False)
    else:
        df.to_excel(path, index=False)


def _read_max_date_from_file(
    path: Path, date_cols: tuple[str, ...] = _DEFAULT_DATE_CANDIDATE_COLS
) -> date | None:
    """Return the max date across candidate date columns, or None. Works for
    both csv- and xlsx-staged files (see ``_read_staged_file``)."""
    import pandas as pd  # noqa: PLC0415

    try:
        df = _read_staged_file(path)
    except Exception as exc:
        log.debug(
            "case_sources.dashboard: could not read %s to detect max date: %s",
            path,
            exc,
        )
        return None
    best: date | None = None
    for col in date_cols:
        if col not in df.columns:
            continue
        parsed = pd.to_datetime(df[col], errors="coerce")
        m = parsed.max()
        if pd.isna(m):
            continue
        d = m.date() if hasattr(m, "date") else _parse_date(str(m)[:10])
        if best is None or d > best:
            best = d
    return best


# Preserve the old name for callers that haven't migrated yet.
_read_max_date_from_xlsx = _read_max_date_from_file


def _trim_file_in_place(
    path: Path, cutoff: date, date_cols: tuple[str, ...] = _DEFAULT_DATE_CANDIDATE_COLS
) -> tuple[int, int]:
    """Drop rows whose max(row date) >= cutoff, save via atomic replace.

    Returns (rows_before, rows_after). Rows are dropped when *any* configured
    date column on that row falls at or after ``cutoff`` — conservative, so
    the incremental refetch of the [cutoff, today] window can't produce a
    duplicate. If none of the candidate columns are present, the file is
    left alone.

    Format-agnostic: reads and rewrites in whatever format the file already
    uses (csv or xlsx). Trimming an xlsx-format file preserves xlsx; a
    csv-format file stays csv.
    """
    import pandas as pd  # noqa: PLC0415

    try:
        df = _read_staged_file(path)
    except Exception as exc:
        # Not a readable staged file (corrupt, wrong format, test fixture...).
        # Leave the file alone rather than crashing the whole prep run.
        log.warning(
            "case_sources.dashboard: could not read %s for trimming (%s) — "
            "left in place, no trim applied",
            path.name,
            exc,
        )
        return 0, 0
    present = [c for c in date_cols if c in df.columns]
    if not present:
        return len(df), len(df)
    cutoff_ts = pd.Timestamp(cutoff)
    at_or_after = pd.Series(False, index=df.index)
    for c in present:
        parsed = pd.to_datetime(df[c], errors="coerce")
        at_or_after |= parsed >= cutoff_ts
    kept = df[~at_or_after]
    if len(kept) == len(df):
        return len(df), len(df)
    tmp = path.with_suffix(path.suffix + ".trimming")
    _write_staged_file(kept, tmp)
    # Sanity-check the write before swapping.
    try:
        _read_staged_file(tmp)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"case_sources.dashboard: trimmed xlsx failed re-read at {tmp}: {exc}"
        ) from exc
    os.replace(tmp, path)
    return len(df), len(kept)


# Legacy alias — callers migrated to _trim_file_in_place.
_trim_xlsx_in_place = _trim_file_in_place


def _scan_external_xlsx_max_dates(
    staging_dir: Path, date_cols: tuple[str, ...] = _DEFAULT_DATE_CANDIDATE_COLS
) -> list[tuple[date, Path]]:
    """Return (max_date, path) for every non-dashboard-pattern xlsx in staging.

    These are typically files an operator dropped into the dir manually
    (e.g. an existing raw_case export). We use their content max-date to
    anchor the next fetch window, but we NEVER delete them.
    """
    if not staging_dir.exists():
        return []
    out: list[tuple[date, Path]] = []
    for p in staging_dir.iterdir():
        if not p.is_file() or p.suffix.lower() != ".xlsx":
            continue
        if _STAGED_FILENAME_RE.match(p.name):
            continue  # owned-by-us files use the fast filename path
        max_d = _read_max_date_from_xlsx(p, date_cols=date_cols)
        if max_d is not None:
            out.append((max_d, p))
    return out


class Source(CaseSource):
    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        staging_dir: Path,
        date_start: str,
        date_end: str,
        backfill_days: int = 30,
        disease: str | None = None,
        selected_region_id: str | None = None,
        date_cols: tuple[str, ...] = _DEFAULT_DATE_CANDIDATE_COLS,
        chunk_days: int = 365,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.staging_dir = staging_dir
        self.date_start = date_start
        self.date_end = date_end
        self.backfill_days = max(0, int(backfill_days))
        self.disease = disease
        self.selected_region_id = selected_region_id
        self.date_cols = tuple(date_cols) if date_cols else _DEFAULT_DATE_CANDIDATE_COLS
        self.chunk_days = max(1, int(chunk_days))
        self._staged_paths: list[str] | None = None
        self._access_token: str | None = None

    @property
    def login_url(self) -> str:
        return f"{self.base_url}{_LOGIN_PATH}"

    @property
    def export_url(self) -> str:
        return f"{self.base_url}{_EXPORT_PATH}"

    @classmethod
    def build(cls, config: Mapping[str, Any]) -> "Source":
        base_url = (
            str(config.get("base_url", "")).strip()
            or os.getenv("DASHBOARD_URL", "").strip()
        )
        if not base_url:
            raise ValueError(
                "case_sources.dashboard: no base_url configured. Set "
                "data.case_download.base_url or DASHBOARD_URL env var."
            )
        client_id = os.getenv("DASHBOARD_CLIENT_ID", "").strip()
        client_secret = os.getenv("DASHBOARD_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise ValueError(
                "case_sources.dashboard: DASHBOARD_CLIENT_ID and "
                "DASHBOARD_CLIENT_SECRET env vars are required."
            )
        staging = Path(
            str(config.get("source_path", "")).strip() or "./cache/dashboard_cases"
        )
        # Env fallbacks for fields the current step doesn't forward from YAML —
        # temporary local patch pending upstream fix in _dict_from_cfg (PR #100).
        date_start = (
            str(config.get("date_start", "")).strip()
            or os.getenv("DASHBOARD_DATE_START", "").strip()
        )
        if not date_start:
            raise ValueError(
                "case_sources.dashboard: data.case_download.date_start is required "
                "(the /api/cases/export.xlsx endpoint requires `from`)."
            )
        date_end = (
            str(config.get("date_end", "")).strip()
            or os.getenv("DASHBOARD_DATE_END", "").strip()
            or date.today().isoformat()
        )
        # `or` chain would silently drop backfill_days=0 (falsy). Explicit
        # None/empty check preserves 0 as a legitimate value.
        backfill_days_raw = config.get("backfill_days")
        if backfill_days_raw is None:
            backfill_days_raw = os.getenv("DASHBOARD_BACKFILL_DAYS") or None
        if backfill_days_raw is None or backfill_days_raw == "":
            backfill_days_raw = 30
        try:
            backfill_days = int(backfill_days_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"case_sources.dashboard: backfill_days must be an integer, got "
                f"{backfill_days_raw!r}"
            ) from exc
        return cls(
            base_url=base_url,
            client_id=client_id,
            client_secret=client_secret,
            staging_dir=staging,
            date_start=date_start,
            date_end=date_end,
            backfill_days=backfill_days,
            disease=str(config.get("disease", "")).strip() or None,
            selected_region_id=(
                str(config.get("selected_region_id", "")).strip()
                or os.getenv("DASHBOARD_SELECTED_REGION_ID", "").strip()
                or None
            ),
            date_cols=tuple(config.get("date_column") or _DEFAULT_DATE_CANDIDATE_COLS),
            chunk_days=int(
                config.get("chunk_days") or os.getenv("DASHBOARD_CHUNK_DAYS") or 365
            ),
        )

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _get_access_token(self) -> str:
        """POST email/password to /api/auth/login, cache the returned token."""
        if self._access_token is not None:
            return self._access_token
        resp = requests.post(
            self.login_url,
            json={"email": self.client_id, "password": self.client_secret},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        token = body.get("access_token") or body.get("token")
        if not token:
            raise RuntimeError(
                f"case_sources.dashboard: login endpoint {self.login_url!r} did not "
                f"return an access token in response body ({body!r})"
            )
        self._access_token = str(token)
        return self._access_token

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def _download_xlsx(self, from_date: str, to_date: str) -> bytes:
        """Stream NDJSON from the dashboard, materialise as XLSX bytes.

        Historical/naming context: this method is still called
        ``_download_xlsx`` and still returns XLSX bytes because the rest of
        the source (filename pattern ``dashboard_..._to_....xlsx``, external
        file trim, parser) all expect XLSX on disk. Only the wire format
        changed: we now hit ``/api/cases/export/stream`` (NDJSON), which
        avoids the 60-second Cloudflare ingress timeout that the old
        ``/api/cases/export.xlsx`` used to trip on multi-year windows.
        """
        import io  # noqa: PLC0415
        import json  # noqa: PLC0415

        import pandas as pd  # noqa: PLC0415

        headers = {"Authorization": f"Bearer {self._get_access_token()}"}
        params: dict[str, str] = {"from": from_date, "to": to_date}
        if self.disease:
            params["disease"] = self.disease
        if self.selected_region_id:
            params["selected_region_id"] = self.selected_region_id

        import sys  # noqa: PLC0415
        import time  # noqa: PLC0415

        from tqdm import tqdm  # noqa: PLC0415

        # TTY-adaptive progress. Interactive terminal → live tqdm bar with
        # rate + elapsed. Captured logs (run.log / CI / systemd journal) →
        # tqdm is disabled and we fall back to a periodic log heartbeat so
        # the operator sees forward motion in the file too.
        is_tty = sys.stderr.isatty()
        _LOG_EVERY_N = 10_000
        _LOG_EVERY_S = 15.0
        started = time.monotonic()
        last_log = started

        records: list[dict[str, Any]] = []
        bar = tqdm(
            desc=f"cases {from_date}→{to_date}",
            unit=" rec",
            unit_scale=False,
            disable=not is_tty,
            file=sys.stderr,
            leave=False,
        )
        try:
            with requests.get(
                self.export_url,
                headers=headers,
                params=params,
                timeout=600,
                stream=True,
            ) as resp:
                resp.raise_for_status()
                for raw_line in resp.iter_lines(decode_unicode=True):
                    if not raw_line:
                        continue
                    try:
                        records.append(json.loads(raw_line))
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            f"case_sources.dashboard: streaming endpoint "
                            f"{self.export_url!r} returned unparseable NDJSON "
                            f"line for from={from_date} to={to_date}: {exc}. "
                            f"First 200 bytes: {raw_line[:200]!r}"
                        ) from exc
                    bar.update(1)
                    if not is_tty:
                        n = len(records)
                        now = time.monotonic()
                        if n % _LOG_EVERY_N == 0 or (now - last_log) >= _LOG_EVERY_S:
                            elapsed = now - started
                            rate = n / elapsed if elapsed > 0 else 0.0
                            log.info(
                                "case_sources.dashboard: streaming %s → %s — "
                                "%d records so far (%.0f rec/s, %.1fs elapsed)",
                                from_date,
                                to_date,
                                n,
                                rate,
                                elapsed,
                            )
                            last_log = now
        finally:
            bar.close()

        elapsed = time.monotonic() - started
        log.info(
            "case_sources.dashboard: stream complete %s → %s — " "%d records in %.1fs",
            from_date,
            to_date,
            len(records),
            elapsed,
        )

        # Empty windows are valid (nothing reported in the range). Return a
        # header-only csv so downstream can distinguish "no data" from
        # "fetch failed"; the ihip parser already skips empty files cleanly.
        # csv (not xlsx) because openpyxl serialisation is O(cells) memory —
        # a 217k-record chunk peaks at ~6-8 GB and OOM'd production callers
        # on 2026-08-27. csv streams row-by-row, peak RAM is O(1 row).
        #
        # For empty windows we still need SOMETHING serialisable: a totally
        # empty csv has no columns, and pd.read_csv on it raises
        # EmptyDataError which the parser doesn't distinguish from real
        # failure. Emit a canonical placeholder header row for empty windows
        # so the file is always parseable-as-header-only.
        df = pd.DataFrame(records)
        if df.empty:
            df = pd.DataFrame(columns=list(_IHIP_HEADERS))
        buf = io.BytesIO()
        df.to_csv(buf, index=False)
        return buf.getvalue()

    _CHUNK_FLOOR_DAYS = 30  # don't halve below this — deeper indicates a real problem

    def _download_chunked(self, from_date: str, to_date: str) -> None:
        """Walk [from_date, to_date] in chunks of ``self.chunk_days`` and stage each.

        On timeout / 502 / 503 / 504, halve the current chunk and retry the same
        start date with the shorter end. Below ``_CHUNK_FLOOR_DAYS`` the failure
        is re-raised — that indicates a real backend problem, not a size issue.
        """
        cur = _parse_date(from_date)
        end = _parse_date(to_date)
        total_days = (end - cur).days + 1
        log.info(
            "case_sources.dashboard: starting chunked download %s → %s "
            "(%d days total, chunk_days=%d)",
            from_date,
            to_date,
            total_days,
            self.chunk_days,
        )
        n_chunks = 0
        while cur <= end:
            chunk_days = self.chunk_days
            n_chunks += 1
            while True:
                chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
                c_from = cur.isoformat()
                c_to = chunk_end.isoformat()
                log.info(
                    "case_sources.dashboard: [chunk %d] fetching %s → %s " "(%d days)…",
                    n_chunks,
                    c_from,
                    c_to,
                    chunk_days,
                )
                try:
                    content = self._download_xlsx(c_from, c_to)
                except (requests.Timeout, requests.HTTPError) as exc:
                    if isinstance(exc, requests.HTTPError):
                        code = getattr(exc.response, "status_code", 0)
                        transient = code in (502, 503, 504)
                    else:
                        transient = True
                    if not transient:
                        raise
                    if chunk_days <= self._CHUNK_FLOOR_DAYS:
                        log.error(
                            "case_sources.dashboard: chunk %s → %s failed even at "
                            "floor size %d days — giving up.",
                            c_from,
                            c_to,
                            self._CHUNK_FLOOR_DAYS,
                        )
                        raise
                    chunk_days = max(self._CHUNK_FLOOR_DAYS, chunk_days // 2)
                    log.warning(
                        "case_sources.dashboard: chunk %s → %s failed (%s) — "
                        "halving to %d days and retrying.",
                        c_from,
                        c_to,
                        exc,
                        chunk_days,
                    )
                    continue
                filename = f"dashboard_{c_from}_to_{c_to}{_STAGED_FILE_SUFFIX}"
                out_path = self.staging_dir / filename
                out_path.write_bytes(content)
                log.info(
                    "case_sources.dashboard: [chunk %d] wrote %d bytes → %s",
                    n_chunks,
                    len(content),
                    out_path,
                )
                cur = chunk_end + timedelta(days=1)
                break

    # ------------------------------------------------------------------
    # CaseSource interface
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Incremental logic
    # ------------------------------------------------------------------

    def _compute_fetch_window(self) -> tuple[str, str]:
        """Legacy single-window helper — kept for backward-compat with tests
        that assert the tail-backfill behaviour. Production path uses
        :meth:`_compute_fetch_ranges` which also detects gaps in coverage.
        """
        end_d = _parse_date(self.date_end)
        start_d = _parse_date(self.date_start)
        staged_ends = [rng[1] for rng in _scan_staged_files(self.staging_dir)]
        external_ends = [
            d
            for d, _ in _scan_external_xlsx_max_dates(
                self.staging_dir, date_cols=self.date_cols
            )
        ]
        all_ends = staged_ends + external_ends
        if not all_ends:
            return self.date_start, self.date_end
        latest_end = max(all_ends)
        backfill_from = latest_end - timedelta(days=max(0, self.backfill_days - 1))
        fetch_from = max(start_d, backfill_from)
        if fetch_from > end_d:
            fetch_from = end_d
        return fetch_from.isoformat(), self.date_end

    def _compute_fetch_ranges(self) -> list[tuple[str, str]]:
        """Return every [from, to] sub-range we still need to fetch.

        Combines two behaviours:
        1. **Gap detection** — walk the config's ``[date_start, date_end]``
           window and skip any sub-range already covered by a staged file
           (both dashboard-pattern and external xlsx). Returns the missing
           sub-ranges. Fixes the silent-hole bug where a mid-timeline file
           (e.g. 2023) is deleted or never fetched and the incremental
           logic never notices.
        2. **Tail backfill** — the last ``backfill_days`` days of the
           configured window are always included, so late-arriving cases
           get re-pulled. Overlaps with existing coverage are OK; the
           :meth:`_delete_fully_contained_staged` step handles them.
        """
        start_d = _parse_date(self.date_start)
        end_d = _parse_date(self.date_end)
        if start_d > end_d:
            return []

        # Coverage from BOTH sources (dashboard-pattern + external xlsx).
        coverage: list[tuple[date, date]] = [
            (s, e) for s, e, _ in _scan_staged_files(self.staging_dir)
        ]
        for max_d, path in _scan_external_xlsx_max_dates(
            self.staging_dir, date_cols=self.date_cols
        ):
            # External file — assume it covers [date_start, max_d]. Not
            # perfect (there could be internal gaps we can't see without
            # reading every row), but a conservative upper bound.
            coverage.append((start_d, max_d))

        # Compute gaps in the config's window that are NOT covered.
        gaps = _subtract_coverage(start_d, end_d, coverage)

        # Tail backfill: re-fetch the last ``backfill_days`` ending at the
        # latest staged end (or date_end if nothing is staged), to catch
        # late-arriving cases. This may overlap existing coverage — that's
        # the point; the fresh fetch replaces stale rows in that tail.
        if self.backfill_days > 0 and coverage:
            latest_end = max(e for _, e in coverage)
            backfill_start = max(
                start_d, latest_end - timedelta(days=self.backfill_days - 1)
            )
            if backfill_start <= end_d:
                # Backfill runs from ``backfill_start`` through date_end so any
                # new data past ``latest_end`` gets picked up in the same range.
                _merge_into(gaps, (backfill_start, end_d))

        return [(a.isoformat(), b.isoformat()) for a, b in gaps]

    def _delete_fully_contained_staged(self, from_date: str, to_date: str) -> int:
        """Reconcile every existing staged file with the new fetch window.

        Three cases:

        1. **Fully contained** (``fetch_from <= existing_start AND existing_end
           <= fetch_to``) — new fetch strictly supersedes existing → delete.
        2. **Partial overlap** — trim existing xlsx in place so its rows end
           at ``fetch_from - 1`` and rename its filename to match. The new
           fetch supplies everything from ``fetch_from`` onward, so parse
           sees no duplicate rows.
        3. **No overlap** — leave alone.

        The trim path handles the common backfill case: an older long-range
        file (e.g. 12 months of history) that partially overlaps a short
        30-day refetch at its tail. Before this change we kept both files
        and warned; now we trim the tail off the old file so parse_case_data
        never sees the overlap window twice.
        """
        fetch_from_d = _parse_date(from_date)
        fetch_to_d = _parse_date(to_date)
        removed = 0
        for existing_start, existing_end, path in _scan_staged_files(self.staging_dir):
            fully_contained = (
                fetch_from_d <= existing_start and existing_end <= fetch_to_d
            )
            if fully_contained:
                try:
                    path.unlink()
                    removed += 1
                    log.info(
                        "case_sources.dashboard: removed superseded staged file %s "
                        "(covers %s → %s, fully within new fetch)",
                        path.name,
                        existing_start.isoformat(),
                        existing_end.isoformat(),
                    )
                except OSError as exc:
                    log.warning(
                        "case_sources.dashboard: could not delete %s: %s", path, exc
                    )
            elif existing_end >= fetch_from_d and existing_start <= fetch_to_d:
                # Partially overlapping — trim the existing file so its rows
                # stop just before the new fetch begins. The new fetch then
                # supplies everything from fetch_from onwards, and no rows
                # are double-counted at parse time.
                new_end = fetch_from_d - timedelta(days=1)
                if new_end < existing_start:
                    # Entire existing range is superseded by the new fetch;
                    # delete outright (same as fully-contained case).
                    try:
                        path.unlink()
                        removed += 1
                        log.info(
                            "case_sources.dashboard: removed staged file %s "
                            "(covers %s → %s, entirely superseded by new fetch %s → %s)",
                            path.name,
                            existing_start.isoformat(),
                            existing_end.isoformat(),
                            from_date,
                            to_date,
                        )
                    except OSError as exc:
                        log.warning(
                            "case_sources.dashboard: could not delete %s: %s",
                            path,
                            exc,
                        )
                    continue
                before, after = _trim_xlsx_in_place(
                    path, cutoff=fetch_from_d, date_cols=self.date_cols
                )
                if before == 0 and after == 0:
                    # trim couldn't run (unreadable / empty / no date columns).
                    # Leave the file as-is with its original filename — a warning
                    # was already logged from _trim_xlsx_in_place.
                    log.warning(
                        "case_sources.dashboard: partial overlap on %s but trim "
                        "was a no-op — filename left as-is; parse may see "
                        "duplicated rows in the overlap window",
                        path.name,
                    )
                    continue
                # Rename the file to reflect its new (shortened) date range so
                # future _scan_staged_files reflects reality and future
                # overlap checks work off correct filename ranges.
                # Preserve the source file's suffix — trimming an xlsx file
                # writes back xlsx, trimming a csv writes back csv. Format
                # never changes underneath the caller.
                new_name = (
                    f"dashboard_{existing_start.isoformat()}_to_"
                    f"{new_end.isoformat()}{path.suffix}"
                )
                new_path = path.with_name(new_name)
                try:
                    path.rename(new_path)
                    log.info(
                        "case_sources.dashboard: trimmed overlapping staged "
                        "file %s → %s: %d → %d rows (dropped %d rows on/after "
                        "%s so the new fetch %s → %s doesn't double-count)",
                        path.name,
                        new_name,
                        before,
                        after,
                        before - after,
                        from_date,
                        from_date,
                        to_date,
                    )
                except OSError as exc:
                    log.warning(
                        "case_sources.dashboard: trimmed %s in place but could not "
                        "rename to %s: %s — filename date range is now stale",
                        path.name,
                        new_name,
                        exc,
                    )
        return removed

    def _consolidate_staged(self) -> None:
        """Collapse every ``dashboard_*.xlsx`` in the staging dir into a single
        canonical file, deduping rows by patient-id.

        The historical design tried to keep the staging dir tidy via a
        trim/delete/rename dance in :meth:`_delete_fully_contained_staged`. In
        practice that dance had two failure modes that let files pile up:
        rename collisions (two different sources trimmed to the same target
        filename silently overwrite each other on POSIX), and coverage-tracking
        drift (once filenames stop reflecting their real content, subsequent
        fetch-range computations request oversized windows and write yet
        another cumulative snapshot). On production caller nodes this manifested
        as 200+ heavily-overlapping snapshots per state, each containing the
        same real cases, which inflated aggregated case counts by 20-100×.

        Consolidation kills that class of bug at the source: after every run
        the staging dir contains exactly ONE ``dashboard_<min>_to_<max>.xlsx``
        with unique rows. The parse step has no way to double-count because
        the duplicates are physically gone.

        Robustness notes:

        - If any file cannot be read as xlsx (partial download, mock fixture,
          corrupted archive) it is LEFT ALONE — consolidation is skipped rather
          than silently dropping data. Operator-provided external xlsx that
          don't match the ``dashboard_*.xlsx`` pattern are also left alone.
        - Dedup key preference: ``Patient Specimen Id`` (in production this is
          100% populated and unique per case), then ``Patient Transaction Id``.
          If neither is present on ≥50% of rows we skip dedup and keep the
          concat as-is (better to over-count than to silently collapse rows
          on a stable key we can't verify).
        - Atomic write: build the consolidated file at ``.tmp``, then replace,
          then delete the originals. A crash mid-consolidation leaves the
          original files intact — the next run just re-attempts.
        """
        import pandas as pd  # noqa: PLC0415

        files = sorted(p for _s, _e, p in _scan_staged_files(self.staging_dir))
        if len(files) <= 1:
            return

        frames: list[pd.DataFrame] = []
        for path in files:
            try:
                frames.append(_read_staged_file(path))
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "case_sources.dashboard: consolidate skipped — could not "
                    "read %s (%s); leaving staging dir untouched",
                    path.name,
                    exc,
                )
                return

        combined = pd.concat(frames, ignore_index=True)
        if combined.empty:
            return

        before_rows = len(combined)
        dedup_key: str | None = None
        for candidate in ("Patient Specimen Id", "Patient Transaction Id"):
            if candidate in combined.columns:
                non_null = combined[candidate].notna().sum()
                if non_null >= before_rows * 0.5:
                    dedup_key = candidate
                    break
        if dedup_key is not None:
            combined = combined.drop_duplicates(subset=[dedup_key], keep="last")

        # New filename = span of the deduped rows' dates. Fall back to the union
        # of the source filenames' spans if we can't parse a date column.
        min_d = max_d = None
        for col in self.date_cols:
            if col in combined.columns:
                parsed = pd.to_datetime(combined[col], errors="coerce")
                if parsed.notna().any():
                    min_d = parsed.min().date()
                    max_d = parsed.max().date()
                    break
        if min_d is None or max_d is None:
            spans = [(s, e) for s, e, _p in _scan_staged_files(self.staging_dir)]
            min_d = min(s for s, _e in spans)
            max_d = max(e for _s, e in spans)

        new_name = (
            f"dashboard_{min_d.isoformat()}_to_"
            f"{max_d.isoformat()}{_STAGED_FILE_SUFFIX}"
        )
        tmp = self.staging_dir / (new_name + ".tmp")
        _write_staged_file(combined, tmp)
        (self.staging_dir / new_name).unlink(missing_ok=True)
        tmp.rename(self.staging_dir / new_name)

        removed = 0
        for path in files:
            if path.name == new_name:
                continue
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                log.warning(
                    "case_sources.dashboard: consolidate could not delete "
                    "superseded file %s: %s",
                    path,
                    exc,
                )
        log.info(
            "case_sources.dashboard: consolidated %d staged file(s) → %s "
            "(%d rows in, %d rows out, dedup_key=%s)",
            len(files),
            new_name,
            before_rows,
            len(combined),
            dedup_key or "<none>",
        )

    def _ensure_staged(self) -> list[str]:
        """Fetch every missing sub-range in [date_start, date_end], stage each.

        1. Compute the list of sub-ranges we still need (gaps in coverage +
           tail backfill window) via :meth:`_compute_fetch_ranges`.
        2. For each range, delete any fully-superseded prior file, trim
           external xlsx rows that overlap, then chunked-download.
        3. Consolidate every ``dashboard_*.xlsx`` into a single deduped file
           via :meth:`_consolidate_staged` so subsequent runs never see
           accumulated overlapping snapshots.
        4. Return the merged list of all xlsx now on disk.
        """
        if self._staged_paths is not None:
            return self._staged_paths
        self.staging_dir.mkdir(parents=True, exist_ok=True)

        ranges = self._compute_fetch_ranges()
        if not ranges:
            log.info(
                "case_sources.dashboard: no fetch ranges — full window "
                "%s → %s already covered on disk.",
                self.date_start,
                self.date_end,
            )
        else:
            log.info(
                "case_sources.dashboard: %d fetch range(s) to cover — %s",
                len(ranges),
                ", ".join(f"{a}→{b}" for a, b in ranges),
            )

        for from_date, to_date in ranges:
            log.info(
                "case_sources.dashboard: processing range %s → %s "
                "(backfill_days=%d, chunk_days=%d)",
                from_date,
                to_date,
                self.backfill_days,
                self.chunk_days,
            )
            removed = self._delete_fully_contained_staged(from_date, to_date)
            if removed:
                log.info(
                    "case_sources.dashboard: removed %d fully-superseded "
                    "staged file(s) before writing the new fetch",
                    removed,
                )
            # External files (non-dashboard-pattern xlsx dropped in by the
            # operator): trim their rows on/after fetch_from so the refetch
            # can't double-count.
            fetch_from_d = _parse_date(from_date)
            for _max_d, ext_path in _scan_external_xlsx_max_dates(
                self.staging_dir, date_cols=self.date_cols
            ):
                before, after = _trim_xlsx_in_place(
                    ext_path, cutoff=fetch_from_d, date_cols=self.date_cols
                )
                if before != after:
                    log.info(
                        "case_sources.dashboard: trimmed %s: %d → %d rows "
                        "(dropped %d rows on/after %s to make room for "
                        "refetch)",
                        ext_path.name,
                        before,
                        after,
                        before - after,
                        from_date,
                    )
            self._download_chunked(from_date, to_date)
        # Collapse every dashboard_*.xlsx into one deduped snapshot so the
        # parse step can never double-count across overlapping fetches. See
        # _consolidate_staged for the failure modes this prevents.
        self._consolidate_staged()
        # Return every xlsx now on disk — both dashboard_*.xlsx (our own writes)
        # and any operator-provided external xlsx that survived trimming.
        # parse_case_data reads them all; older files stay untouched.
        owned = [p.name for _, _, p in _scan_staged_files(self.staging_dir)]
        external = [
            p.name
            for _, p in _scan_external_xlsx_max_dates(
                self.staging_dir, date_cols=self.date_cols
            )
        ]
        self._staged_paths = sorted(owned + external)
        return self._staged_paths

    def list_objects(self, prefix: str = "") -> list[str]:
        return [p for p in self._ensure_staged() if p.startswith(prefix)]

    def read(self, path: str) -> bytes:
        self._ensure_staged()
        target = self.staging_dir / path
        if not target.exists():
            raise FileNotFoundError(
                f"case_sources.dashboard: staged file not found: {target}"
            )
        return target.read_bytes()
