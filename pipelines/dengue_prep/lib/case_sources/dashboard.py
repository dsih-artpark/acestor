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
    source_mode:  dashboard
    source_path:  "./cache/dashboard_cases"   # staging dir for downloaded XLSX
    base_url:     "https://dashboard.example.com"  # falls back to DASHBOARD_URL env
    date_start:   "2024-01-01"                 # sent as `?from=`
    date_end:     ""                            # empty → today; sent as `?to=`
    disease:      "Dengue"                     # optional filter passed through

Environment variables:
    DASHBOARD_URL           — base URL (fallback if base_url not in YAML)
    DASHBOARD_CLIENT_ID     — sent as `email` on /api/auth/login
    DASHBOARD_CLIENT_SECRET — sent as `password` on /api/auth/login
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import requests

from pipelines.dengue_prep.lib.case_sources import CaseSource

log = logging.getLogger(__name__)


_LOGIN_PATH = "/api/auth/login"
_EXPORT_PATH = "/api/cases/export.xlsx"


class Source(CaseSource):
    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        staging_dir: Path,
        date_start: str,
        date_end: str,
        disease: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.staging_dir = staging_dir
        self.date_start = date_start
        self.date_end = date_end
        self.disease = disease
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
        date_start = str(config.get("date_start", "")).strip()
        if not date_start:
            raise ValueError(
                "case_sources.dashboard: data.case_download.date_start is required "
                "(the /api/cases/export.xlsx endpoint requires `from`)."
            )
        date_end = str(config.get("date_end", "")).strip() or date.today().isoformat()
        return cls(
            base_url=base_url,
            client_id=client_id,
            client_secret=client_secret,
            staging_dir=staging,
            date_start=date_start,
            date_end=date_end,
            disease=str(config.get("disease", "")).strip() or None,
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

    def _download_xlsx(self) -> bytes:
        """GET /api/cases/export.xlsx?from=..&to=.. and return the raw bytes."""
        headers = {"Authorization": f"Bearer {self._get_access_token()}"}
        params: dict[str, str] = {"from": self.date_start, "to": self.date_end}
        if self.disease:
            params["disease"] = self.disease
        resp = requests.get(
            self.export_url, headers=headers, params=params, timeout=300, stream=False
        )
        resp.raise_for_status()
        content = resp.content
        if not content:
            raise RuntimeError(
                f"case_sources.dashboard: export endpoint {self.export_url!r} "
                f"returned zero bytes for from={self.date_start} to={self.date_end}."
            )
        # Quick sniff — XLSX is a ZIP archive; its first two bytes are 'PK'.
        if not content.startswith(b"PK"):
            raise RuntimeError(
                f"case_sources.dashboard: export endpoint returned non-XLSX bytes "
                f"(first 64: {content[:64]!r}). Auth or endpoint issue?"
            )
        return content

    # ------------------------------------------------------------------
    # CaseSource interface
    # ------------------------------------------------------------------

    def _ensure_staged(self) -> list[str]:
        """Fetch once (per Source instance), write XLSX, remember its path."""
        if self._staged_paths is not None:
            return self._staged_paths
        self.staging_dir.mkdir(parents=True, exist_ok=True)

        content = self._download_xlsx()
        filename = f"dashboard_{self.date_start}_to_{self.date_end}.xlsx"
        out_path = self.staging_dir / filename
        out_path.write_bytes(content)
        log.info(
            "case_sources.dashboard: wrote %d bytes → %s",
            len(content),
            out_path,
        )
        self._staged_paths = [out_path.name]
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
