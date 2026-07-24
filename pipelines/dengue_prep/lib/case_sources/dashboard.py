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
2. ``GET {base_url}/api/cases/export.xlsx?from=<date>&to=<date>`` with
   ``Authorization: Bearer <token>`` → binary XLSX response body.

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

log = logging.getLogger(__name__)


_LOGIN_PATH = "/api/auth/login"
_EXPORT_PATH = "/api/cases/export.xlsx"

_STAGED_FILENAME_RE = re.compile(
    r"^dashboard_(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})\.xlsx$"
)

# Default candidate date columns for reading max-date out of pre-existing
# xlsx files. Ordered so the most-populated column in dashboard exports comes
# first. Overridden per-run when the step forwards ``case_parse.date_column``.
_DEFAULT_DATE_CANDIDATE_COLS: tuple[str, ...] = (
    "Test Performed Date",
    "Date Of Onset",
    "Sample Collected Date",
)


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


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


def _read_max_date_from_xlsx(
    path: Path, date_cols: tuple[str, ...] = _DEFAULT_DATE_CANDIDATE_COLS
) -> date | None:
    """Return the max date across candidate date columns in an xlsx, or None."""
    import pandas as pd  # noqa: PLC0415

    try:
        df = pd.read_excel(path)
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


def _trim_xlsx_in_place(
    path: Path, cutoff: date, date_cols: tuple[str, ...] = _DEFAULT_DATE_CANDIDATE_COLS
) -> tuple[int, int]:
    """Drop rows whose max(row date) >= cutoff, save via atomic replace.

    Returns (rows_before, rows_after). Rows are dropped when *any* configured
    date column on that row falls at or after ``cutoff`` — conservative, so
    the incremental refetch of the [cutoff, today] window can't produce a
    duplicate. If none of the candidate columns are present, the file is
    left alone.
    """
    import pandas as pd  # noqa: PLC0415

    df = pd.read_excel(path)
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
    kept.to_excel(tmp, index=False)
    # Sanity-check the write before swapping.
    try:
        pd.read_excel(tmp, nrows=1)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"case_sources.dashboard: trimmed xlsx failed re-read at {tmp}: {exc}"
        ) from exc
    os.replace(tmp, path)
    return len(df), len(kept)


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
        """GET /api/cases/export.xlsx?from=..&to=.. and return the raw bytes."""
        headers = {"Authorization": f"Bearer {self._get_access_token()}"}
        params: dict[str, str] = {"from": from_date, "to": to_date}
        if self.disease:
            params["disease"] = self.disease
        if self.selected_region_id:
            params["selected_region_id"] = self.selected_region_id
        resp = requests.get(
            self.export_url, headers=headers, params=params, timeout=300, stream=False
        )
        resp.raise_for_status()
        content = resp.content
        if not content:
            raise RuntimeError(
                f"case_sources.dashboard: export endpoint {self.export_url!r} "
                f"returned zero bytes for from={from_date} to={to_date}."
            )
        # Quick sniff — XLSX is a ZIP archive; its first two bytes are 'PK'.
        if not content.startswith(b"PK"):
            raise RuntimeError(
                f"case_sources.dashboard: export endpoint returned non-XLSX bytes "
                f"(first 64: {content[:64]!r}). Auth or endpoint issue?"
            )
        return content

    _CHUNK_FLOOR_DAYS = 30  # don't halve below this — deeper indicates a real problem

    def _download_chunked(self, from_date: str, to_date: str) -> None:
        """Walk [from_date, to_date] in chunks of ``self.chunk_days`` and stage each.

        On timeout / 502 / 503 / 504, halve the current chunk and retry the same
        start date with the shorter end. Below ``_CHUNK_FLOOR_DAYS`` the failure
        is re-raised — that indicates a real backend problem, not a size issue.
        """
        cur = _parse_date(from_date)
        end = _parse_date(to_date)
        while cur <= end:
            chunk_days = self.chunk_days
            while True:
                chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
                c_from = cur.isoformat()
                c_to = chunk_end.isoformat()
                log.info(
                    "case_sources.dashboard: chunk %s → %s (%d days)",
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
                filename = f"dashboard_{c_from}_to_{c_to}.xlsx"
                out_path = self.staging_dir / filename
                out_path.write_bytes(content)
                log.info(
                    "case_sources.dashboard: wrote %d bytes → %s",
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
        """Decide which sub-range to fetch this run.

        - First run (nothing staged): fetch the full [date_start, date_end].
        - Subsequent runs: fetch from ``max(date_start, latest_end - backfill_days + 1)``
          through ``date_end``. This re-pulls the last N days to catch late-
          arriving cases in the reporting lag window while leaving older files
          untouched.
        """
        end_d = _parse_date(self.date_end)
        start_d = _parse_date(self.date_start)
        # Anchor from BOTH sources: dashboard_*.xlsx filenames (cheap) and any
        # other .xlsx already staged (pandas-read max date). The latter lets
        # an operator drop their raw_case export into the staging dir and get
        # incremental fetching without renaming or re-downloading history.
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
        # Clamp: never fetch a from > date_end.
        if fetch_from > end_d:
            fetch_from = end_d
        return fetch_from.isoformat(), self.date_end

    def _delete_overlapping_staged(self, from_date: str, to_date: str) -> int:
        """Remove any existing staged file whose date range overlaps the new fetch.

        Overlap = ``existing_end >= fetch_from AND existing_start <= fetch_to``.
        Prevents parse_case_data from double-counting cases that live in both
        files.
        """
        fetch_from_d = _parse_date(from_date)
        fetch_to_d = _parse_date(to_date)
        removed = 0
        for existing_start, existing_end, path in _scan_staged_files(self.staging_dir):
            if existing_end >= fetch_from_d and existing_start <= fetch_to_d:
                try:
                    path.unlink()
                    removed += 1
                    log.info(
                        "case_sources.dashboard: removed overlapping staged file %s "
                        "(covers %s → %s)",
                        path.name,
                        existing_start.isoformat(),
                        existing_end.isoformat(),
                    )
                except OSError as exc:
                    log.warning(
                        "case_sources.dashboard: could not delete %s: %s", path, exc
                    )
        return removed

    def _ensure_staged(self) -> list[str]:
        """Fetch (possibly just the backfill window), stage the XLSX, remember it.

        Behavior on repeat runs: only re-fetches the last ``backfill_days`` days
        of the range (+ any dates past the last staged file's end). Existing
        files that cover older, out-of-backfill dates stay untouched — the
        parse_case_data step will read every ``dashboard_*.xlsx`` in the dir.
        """
        if self._staged_paths is not None:
            return self._staged_paths
        self.staging_dir.mkdir(parents=True, exist_ok=True)

        from_date, to_date = self._compute_fetch_window()
        log.info(
            "case_sources.dashboard: fetch window %s → %s (backfill_days=%d)",
            from_date,
            to_date,
            self.backfill_days,
        )
        removed = self._delete_overlapping_staged(from_date, to_date)
        if removed:
            log.info(
                "case_sources.dashboard: removed %d overlapping staged file(s) "
                "before writing the new fetch",
                removed,
            )
        # For external files (non-dashboard-pattern xlsx dropped in by the
        # operator), don't delete — trim their rows in the [fetch_from, ...]
        # window so the fresh refetch doesn't double-count.
        fetch_from_d = _parse_date(from_date)
        for _max_d, ext_path in _scan_external_xlsx_max_dates(
            self.staging_dir, date_cols=self.date_cols
        ):
            before, after = _trim_xlsx_in_place(
                ext_path, cutoff=fetch_from_d, date_cols=self.date_cols
            )
            if before != after:
                log.info(
                    "case_sources.dashboard: trimmed %s: %d → %d rows (dropped "
                    "%d rows on/after %s to make room for refetch)",
                    ext_path.name,
                    before,
                    after,
                    before - after,
                    from_date,
                )

        # Chunked download: the dashboard export can 504 on multi-year windows.
        # Walk the [from_date, to_date] range in chunks of self.chunk_days, and
        # on timeout/504 halve the chunk (down to a floor) and retry.
        self._download_chunked(from_date, to_date)
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
