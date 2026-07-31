"""Dashboard geojson source — GET /api/regions/{scope_id}?level={region_type}.

The dashboard returns a full GeoJSON ``FeatureCollection`` with per-region
geometry. This source unpacks it into ``{base_path}/{region_type}s/{region_id}.geojson``
files — matching the on-disk layout every acestor step already reads from.

**Cache policy is permanent + gap-filling**: on every run this source first
hits ``/api/regions/lite`` (a cheap 36 KB listing) to learn which region_ids
should exist at the requested level, then compares against files already on
disk. If everything is present, no full fetch happens. If anything is
missing (a new region was added upstream, or the cache dir is fresh), the
full FeatureCollection is pulled once and *only* the missing files are
written — cached ones are left alone. Geographic boundaries change on the
order of years, so set ``refresh: true`` in the ``data.geojson`` config
block to force re-download of everything.

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
_REGIONS_LITE_PATH = "/api/regions/lite"


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

        # Step 1: ask the dashboard which region_ids should exist at this level
        # via the lightweight listing endpoint (36 KB / <200 ms for a state).
        # We use this to detect *which* regions are missing from the local
        # cache rather than blindly refetching the full FeatureCollection.
        expected_ids = self._list_expected_region_ids(region_type)

        # Step 2: figure out what we already have and what's missing.
        existing_ids = {p.stem for p in target.glob("*.geojson")}
        cached_expected = expected_ids & existing_ids
        missing_ids = expected_ids - existing_ids

        if not self.refresh and not missing_ids:
            log.info(
                "geojson_sources.dashboard: all %d expected %s geojson(s) already "
                "cached in %s — skipping full fetch (set data.geojson.refresh=true "
                "to re-download)",
                len(cached_expected),
                region_type,
                target,
            )
            return GeojsonFetchResult(
                region_type=region_type,
                output_dir=str(target),
                files_written=0,
                files_cached=len(cached_expected),
            )

        # Step 3: something is missing (or refresh was requested). The dashboard
        # has no per-region-id endpoint, so we have to fetch the whole
        # FeatureCollection — but we still only write the files we actually
        # need (missing_ids), or all of them if refresh=True.
        if self.refresh:
            log.info(
                "geojson_sources.dashboard: refresh=true — refetching all %d "
                "expected %s geojson(s) for scope=%r",
                len(expected_ids),
                region_type,
                self.scope_id,
            )
        else:
            log.info(
                "geojson_sources.dashboard: %d/%d %s geojson(s) missing from cache "
                "(%s) — fetching full FeatureCollection to fill gaps",
                len(missing_ids),
                len(expected_ids),
                region_type,
                sorted(missing_ids)[:5] + (["..."] if len(missing_ids) > 5 else []),
            )

        url = f"{self.base_url}{_REGIONS_PATH.format(scope_id=self.scope_id)}"
        headers = {"Authorization": f"Bearer {self._get_access_token()}"}
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

        # Only write the ones we're missing (or all, when refresh=True).
        want_ids = expected_ids if self.refresh else missing_ids
        written = 0
        skipped_no_id = 0
        for feat in features:
            props = feat.get("properties") or {}
            rid = props.get("region_id") or feat.get("id")
            if not rid:
                log.warning(
                    "geojson_sources.dashboard: feature has no region_id — skipping. "
                    "properties keys: %s",
                    sorted(props.keys()),
                )
                skipped_no_id += 1
                continue
            if rid not in want_ids:
                continue
            fc = {"type": "FeatureCollection", "features": [feat]}
            (target / f"{rid}.geojson").write_text(json.dumps(fc), encoding="utf-8")
            written += 1

        log.info(
            "geojson_sources.dashboard: wrote %d %s geojson(s) to %s "
            "(%d previously cached, %d feature(s) skipped for missing region_id)",
            written,
            region_type,
            target,
            len(cached_expected) if not self.refresh else 0,
            skipped_no_id,
        )
        return GeojsonFetchResult(
            region_type=region_type,
            output_dir=str(target),
            files_written=written,
            files_cached=len(cached_expected) if not self.refresh else 0,
        )

    def _list_expected_region_ids(self, region_type: str) -> set[str]:
        """Fetch /api/regions/lite and return the region_ids at ``region_type``.

        Cheap (~36 KB, <200 ms for a state-sized scope). Lets us detect
        missing files without pulling the full FeatureCollection.
        """
        url = f"{self.base_url}{_REGIONS_LITE_PATH}"
        headers = {"Authorization": f"Bearer {self._get_access_token()}"}
        resp = requests.get(
            url, headers=headers, params={"scope_id": self.scope_id}, timeout=30
        )
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, list):
            raise RuntimeError(
                f"geojson_sources.dashboard: expected a list from {url}, got "
                f"{type(payload).__name__}"
            )
        ids = {
            str(item["id"])
            for item in payload
            if isinstance(item, dict)
            and item.get("level") == region_type
            and item.get("id")
        }
        if not ids:
            raise RuntimeError(
                f"geojson_sources.dashboard: /api/regions/lite returned no "
                f"regions at level={region_type!r} for scope={self.scope_id!r} "
                f"({len(payload)} total items). Check the scope + level are correct."
            )
        return ids
