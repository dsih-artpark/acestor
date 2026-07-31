"""Dashboard geojson source — GET /api/regions/{scope_id}?level={region_type}.

The dashboard returns a full GeoJSON ``FeatureCollection`` with per-region
geometry. This source unpacks it into ``{base_path}/{region_type}s/{region_id}.geojson``
files — matching the on-disk layout every acestor step already reads from.

**Cache policy is permanent**: once a region's file is on disk, it is not
re-downloaded on subsequent runs — geographic boundaries change on the order
of years, not weeks. Set ``refresh: true`` in the ``data.geojson`` config
block to force re-download.

Auth reuses the same dashboard credentials as the case source
(``DASHBOARD_URL``, ``DASHBOARD_CLIENT_ID``, ``DASHBOARD_CLIENT_SECRET``), so
prep configs that already use ``case_download.source_mode: dashboard`` don't
need extra secrets.

Configuration
-------------
YAML (``data.geojson``):

.. code-block:: yaml

    data:
      geojson:
        base_path: "ka_datasets/geojsons"    # cache dir
        source_mode: dashboard
        scope_id: karnataka                   # or DASHBOARD_SELECTED_REGION_ID env
        refresh: false                        # true = re-download even if cached
        base_url: "https://apps.artpark.ai/disease-dashboard"  # or DASHBOARD_URL env
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

import requests

from pipelines.dengue_prep.lib.geojson_sources import (
    GeojsonFetchResult,
    GeojsonSource,
)

# See case_sources/dashboard.py for the reparenting rationale.
log = logging.getLogger("acestor.dengue_prep.geojson_sources.dashboard")


_LOGIN_PATH = "/api/auth/login"
_REGIONS_PATH = "/api/regions/{scope_id}"


class Source(GeojsonSource):
    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        scope_id: str,
        refresh: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope_id = scope_id
        self.refresh = bool(refresh)
        self._access_token: str | None = None

    @classmethod
    def build(cls, config: Mapping[str, Any]) -> "Source":
        base_url = (
            str(config.get("base_url", "")).strip()
            or os.getenv("DASHBOARD_URL", "").strip()
        )
        if not base_url:
            raise ValueError(
                "geojson_sources.dashboard: no base_url configured. Set "
                "data.geojson.base_url or DASHBOARD_URL env var."
            )
        client_id = os.getenv("DASHBOARD_CLIENT_ID", "").strip()
        client_secret = os.getenv("DASHBOARD_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise ValueError(
                "geojson_sources.dashboard: DASHBOARD_CLIENT_ID and "
                "DASHBOARD_CLIENT_SECRET env vars are required."
            )
        scope_id = (
            str(config.get("scope_id", "")).strip()
            or os.getenv("DASHBOARD_SELECTED_REGION_ID", "").strip()
        )
        if not scope_id:
            raise ValueError(
                "geojson_sources.dashboard: scope_id is required. Set "
                "data.geojson.scope_id or DASHBOARD_SELECTED_REGION_ID env var."
            )
        return cls(
            base_url=base_url,
            client_id=client_id,
            client_secret=client_secret,
            scope_id=scope_id,
            refresh=bool(config.get("refresh", False)),
        )

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _get_access_token(self) -> str:
        if self._access_token is not None:
            return self._access_token
        resp = requests.post(
            f"{self.base_url}{_LOGIN_PATH}",
            json={"email": self.client_id, "password": self.client_secret},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        token = body.get("access_token") or body.get("token")
        if not token:
            raise RuntimeError(
                f"geojson_sources.dashboard: login endpoint did not return an "
                f"access token in response body ({body!r})"
            )
        self._access_token = token
        return token

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def fetch(self, region_type: str, base_path: str) -> GeojsonFetchResult:
        target = Path(base_path) / f"{region_type}s"
        target.mkdir(parents=True, exist_ok=True)

        # Permanent cache: if the directory already has any geojsons for this
        # region_type and refresh is False, treat as complete and return
        # immediately. Boundaries change slowly; the operator has to opt in
        # to re-fetch via ``refresh: true``.
        existing = sorted(target.glob(f"{region_type}_*.geojson"))
        if existing and not self.refresh:
            log.info(
                "geojson_sources.dashboard: %d %s geojson(s) already cached in %s "
                "— skipping fetch (set data.geojson.refresh=true to re-download)",
                len(existing),
                region_type,
                target,
            )
            return GeojsonFetchResult(
                region_type=region_type,
                output_dir=str(target),
                files_written=0,
                files_cached=len(existing),
            )

        url = f"{self.base_url}{_REGIONS_PATH.format(scope_id=self.scope_id)}"
        headers = {"Authorization": f"Bearer {self._get_access_token()}"}
        log.info(
            "geojson_sources.dashboard: fetching %s regions for scope=%r "
            "from %s (refresh=%s)",
            region_type,
            self.scope_id,
            url,
            self.refresh,
        )
        resp = requests.get(
            url, headers=headers, params={"level": region_type}, timeout=120
        )
        resp.raise_for_status()
        payload = resp.json()

        if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
            raise RuntimeError(
                f"geojson_sources.dashboard: expected FeatureCollection from "
                f"{url}?level={region_type}, got payload of type "
                f"{type(payload).__name__} with top-level keys "
                f"{sorted(payload.keys()) if isinstance(payload, dict) else '<n/a>'}"
            )
        features = payload.get("features") or []
        if not features:
            raise RuntimeError(
                f"geojson_sources.dashboard: dashboard returned FeatureCollection "
                f"with zero features for scope={self.scope_id!r} level={region_type!r}. "
                f"Check that the scope exists and has {region_type}-level regions loaded."
            )

        # Split into per-region files.
        written = 0
        skipped = 0
        for feat in features:
            props = feat.get("properties") or {}
            rid = props.get("region_id") or feat.get("id")
            if not rid:
                log.warning(
                    "geojson_sources.dashboard: feature has no region_id — skipping. "
                    "properties keys: %s",
                    sorted(props.keys()),
                )
                skipped += 1
                continue
            fc = {"type": "FeatureCollection", "features": [feat]}
            out_path = target / f"{rid}.geojson"
            out_path.write_text(json.dumps(fc), encoding="utf-8")
            written += 1

        log.info(
            "geojson_sources.dashboard: wrote %d %s geojson(s) to %s "
            "(%d feature(s) skipped for missing region_id)",
            written,
            region_type,
            target,
            skipped,
        )
        return GeojsonFetchResult(
            region_type=region_type,
            output_dir=str(target),
            files_written=written,
            files_cached=0,
        )
