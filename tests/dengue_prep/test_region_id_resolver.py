"""Tests for the pre-resolved region_id resolver path in ihip.py.

When ``region_id_column`` is set and present in the file, the parser trusts
that column verbatim and skips LGD / geocode / spatial join entirely.
"""

from __future__ import annotations

import logging

import pandas as pd

from pipelines.dengue_prep.lib.ihip import parse_ihip_files


def test_region_id_column_used_when_present(tmp_path, caplog):
    case_dir = tmp_path / "cases"
    case_dir.mkdir()
    df = pd.DataFrame(
        {
            "Sample Collected Date": ["01/01/2024", "01/01/2024", "02/01/2024"],
            "Region Id": ["ward_gba-1", "ward_gba-1", "ward_gba-2"],
        }
    )
    df.to_csv(case_dir / "cases.csv", index=False)

    with caplog.at_level(logging.INFO):
        out = parse_ihip_files(
            folder=str(case_dir),
            region_type="ward",
            geojson_base=str(tmp_path / "geo-doesnt-exist"),
            region_id_column="Region Id",
        )

    assert out["case_count"].sum() == 3
    assert set(out["region_id"]) == {"ward_gba-1", "ward_gba-2"}
    assert any(
        "using pre-resolved region_id column" in r.getMessage() for r in caplog.records
    )


def test_region_id_column_drops_null_and_mismatched_prefix(tmp_path, caplog):
    case_dir = tmp_path / "cases"
    case_dir.mkdir()
    df = pd.DataFrame(
        {
            "Sample Collected Date": ["01/01/2024"] * 5,
            "Region Id": [
                "ward_gba-1",
                "",  # blank → dropped
                None,  # NaN → dropped
                "district_502",  # wrong prefix → dropped
                "ward_gba-2",
            ],
        }
    )
    df.to_csv(case_dir / "cases.csv", index=False)

    with caplog.at_level(logging.WARNING):
        out = parse_ihip_files(
            folder=str(case_dir),
            region_type="ward",
            geojson_base=str(tmp_path / "geo-doesnt-exist"),
            region_id_column="Region Id",
        )

    assert out["case_count"].sum() == 2
    assert set(out["region_id"]) == {"ward_gba-1", "ward_gba-2"}
    assert any("could not be rolled up" in r.getMessage() for r in caplog.records)


def test_region_id_takes_priority_over_lgd_and_geocode(tmp_path):
    """If region_id_column is set, neither LGD nor geocode is invoked even when their columns exist."""
    case_dir = tmp_path / "cases"
    case_dir.mkdir()
    df = pd.DataFrame(
        {
            "Sample Collected Date": ["01/01/2024", "02/01/2024"],
            "Region Id": ["ward_gba-1", "ward_gba-2"],
            "District Code": [999, 998],  # would normally trigger LGD path
            "Patient Address": ["addr1", "addr2"],
            "Village Or Ward": ["v1", "v2"],
        }
    )
    df.to_csv(case_dir / "cases.csv", index=False)

    out = parse_ihip_files(
        folder=str(case_dir),
        region_type="ward",
        geojson_base=str(tmp_path / "geo-doesnt-exist"),
        region_id_column="Region Id",
        lgd_code_column="District Code",
    )
    # If LGD path had been used the IDs would be ward_999 / ward_998, not ward_gba-*
    assert set(out["region_id"]) == {"ward_gba-1", "ward_gba-2"}


def test_region_id_rolls_up_via_geojson_parent_chain(tmp_path, caplog):
    """ward IDs in the file roll up to corp via the geojson parent chain."""
    import json

    geo_root = tmp_path / "geo"
    (geo_root / "wards").mkdir(parents=True)
    (geo_root / "zones").mkdir(parents=True)
    (geo_root / "corps").mkdir(parents=True)

    def write(folder, region_id, parent):
        (geo_root / folder / f"{region_id}.geojson").write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"region_id": region_id, "parent": parent},
                            "geometry": None,
                        }
                    ],
                }
            )
        )

    write("wards", "ward_gba-1", "zone_gba-c-1")
    write("wards", "ward_gba-2", "zone_gba-c-1")
    write("wards", "ward_gba-3", "zone_gba-e-1")
    write("zones", "zone_gba-c-1", "corp_gba-5")
    write("zones", "zone_gba-e-1", "corp_gba-3")
    write("corps", "corp_gba-5", "gulb_gba")
    write("corps", "corp_gba-3", "gulb_gba")

    case_dir = tmp_path / "cases"
    case_dir.mkdir()
    pd.DataFrame(
        {
            "Sample Collected Date": ["01/01/2024"] * 4,
            "Region Id": ["ward_gba-1", "ward_gba-2", "ward_gba-3", "ward_gba-3"],
        }
    ).to_csv(case_dir / "cases.csv", index=False)

    with caplog.at_level(logging.INFO):
        out = parse_ihip_files(
            folder=str(case_dir),
            region_type="corp",
            geojson_base=str(geo_root),
            region_id_column="Region Id",
        )

    # All 4 rows roll up: ward_gba-1/2 → corp_gba-5 (count=2), ward_gba-3 → corp_gba-3 (count=2)
    assert out["case_count"].sum() == 4
    by_region = dict(zip(out["region_id"], out["case_count"]))
    assert by_region == {"corp_gba-5": 2, "corp_gba-3": 2}
    assert any("rolled up" in r.getMessage() for r in caplog.records)


def test_region_id_rolls_up_to_zone_as_well(tmp_path):
    """Same parent chain, target = zone — wards stop at their zone, not corp."""
    import json

    geo_root = tmp_path / "geo"
    (geo_root / "wards").mkdir(parents=True)
    (geo_root / "zones").mkdir(parents=True)

    def write(folder, region_id, parent):
        (geo_root / folder / f"{region_id}.geojson").write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"region_id": region_id, "parent": parent},
                            "geometry": None,
                        }
                    ],
                }
            )
        )

    write("wards", "ward_gba-1", "zone_gba-c-1")
    write("wards", "ward_gba-2", "zone_gba-c-1")
    write("zones", "zone_gba-c-1", "corp_gba-5")

    case_dir = tmp_path / "cases"
    case_dir.mkdir()
    pd.DataFrame(
        {
            "Sample Collected Date": ["01/01/2024"] * 2,
            "Region Id": ["ward_gba-1", "ward_gba-2"],
        }
    ).to_csv(case_dir / "cases.csv", index=False)

    out = parse_ihip_files(
        folder=str(case_dir),
        region_type="zone",
        geojson_base=str(geo_root),
        region_id_column="Region Id",
    )
    assert dict(zip(out["region_id"], out["case_count"])) == {"zone_gba-c-1": 2}


def test_region_id_column_missing_falls_through_to_next_resolver(tmp_path, caplog):
    """Configured column missing → log warning + fall through to LGD (or further)."""
    case_dir = tmp_path / "cases"
    case_dir.mkdir()
    df = pd.DataFrame(
        {
            "Sample Collected Date": ["01/01/2024"],
            "District Code": [502],
        }
    )
    df.to_csv(case_dir / "cases.csv", index=False)

    with caplog.at_level(logging.WARNING):
        out = parse_ihip_files(
            folder=str(case_dir),
            region_type="district",
            geojson_base=str(tmp_path / "geo-doesnt-exist"),
            region_id_column="Region Id",  # configured but absent in file
            lgd_code_column="District Code",  # should pick this up instead
        )
    assert set(out["region_id"]) == {"district_502"}
    assert any(
        "missing configured region_id_column" in r.getMessage() for r in caplog.records
    )
