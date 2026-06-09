# LGD code lookups

Committed snapshot of `{region_id, lgd_code, name}` per (state, spatial level), used by the predictions writers to add an `lgdCode` column to every output CSV.

## Files

One CSV per (state, spatial level):

```text
ap_district.csv    AP districts  (28)
ap_mandal.csv      AP mandals    (688)
ap_block.csv       AP blocks     (668)
ap_village.csv     AP villages   (17,957)
```

Schema: `region_id, lgd_code, name`. All values are strings.

## Source of truth

These are extracted from `<state>_datasets/lgd_normalized/` — the canonical LGD-portal export. The committed copies here decouple the pipeline from the gitignored `<state>_datasets/` tree.

`region_id` is zero-padded to match the format the pipeline emits (e.g. mandal LGD code `4731` → `mandal_04731` in predictions CSV). `lgd_code` is the canonical unpadded value from the LGD portal.

## Regenerating

```bash
uv run python scripts/extract_lgd_codes.py --state AP
uv run python scripts/extract_lgd_codes.py --state OD
```

Run this whenever `<state>_datasets/lgd_normalized/` changes, then commit the regenerated CSVs.

## Consumed by

- `pipelines/dengue/lib/lgd.py` — `lgd_code_lookup()` and `add_lgd_column()`
- Predictions writers in `pipelines/dengue/steps/train_and_predict.py` and `pipelines/dengue_downscale/steps/downscale_predictions.py` call `add_lgd_column()` right before `to_csv`, so every predictions CSV carries `lgdCode`.
