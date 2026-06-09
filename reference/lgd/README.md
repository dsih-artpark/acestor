# LGD code lookups

Committed snapshot of `{region_id, lgd_code, name}` per spatial level, used by the predictions writers to add an `lgdCode` column to every output CSV.

## Files

One CSV per (state, spatial level):

```
ap_district.csv    AP districts (28)
ap_mandal.csv      AP mandals  (688)
```

Schema: `region_id, lgd_code, name`. All values are strings.

## Source of truth

The geojsons under `<state>_datasets/geojsons/geojsons_<STATE>/` are the source. The CSVs here are extracted from them, so they're reproducible — not magic frozen data.

LGD field by spatial level (see `scripts/extract_lgd_codes.py`):

- **district** — the numeric suffix of `region_id` (e.g. `district_515` → `515`). The IHIP parser maps LGD district codes directly into `region_id`, so the suffix IS the LGD district code. District geojsons don't carry an India LGD property explicitly.
- **mandal** — `DMCodeInd` from the mandal geojson. Full state+district+mandal LGD subdistrict code (e.g. `55105206`).
- **ward / village** — to be added with `VWCodeInd` (or equivalent) once those geojsons are wired in.

## Regenerating

```bash
uv run python scripts/extract_lgd_codes.py --state AP
uv run python scripts/extract_lgd_codes.py --state OD
```

Run this after updating any geojson, then commit the regenerated CSVs.

## Consumed by

- `pipelines/dengue/lib/lgd.py` — `lgd_code_lookup()` and `add_lgd_column()`
- Predictions writers in `pipelines/dengue/steps/train_and_predict.py` and `pipelines/dengue_downscale/steps/downscale_predictions.py` call `add_lgd_column()` right before `to_csv`, so every predictions CSV carries `lgdCode`.
