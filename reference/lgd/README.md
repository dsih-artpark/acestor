# LGD code lookups

Committed snapshot of `{region_id, lgd_code, name}` per (state, spatial level), used by the predictions writers to add an `lgdCode` column to every output CSV.

## Layout

```text
reference/lgd/
├── ap/
│   ├── districts.csv      (28)
│   ├── mandals.csv        (688)
│   ├── blocks.csv         (668)
│   └── villages.csv       (17,957)
├── od/                    (when wired)
│   └── …
└── ka/                    (when wired)
    └── …
```

Schema: `region_id, lgd_code, name`. All values are strings. Mirrors `<state>_datasets/lgd_normalized/<level>s.csv` 1:1.

## How the pipeline finds these

The run config must declare `state` at the top level:

```yaml
state: "AP"
```

The predictions writers (main dengue + downscale) read this and load `reference/lgd/<state>/<spatial_res>s.csv`. If `state` is missing in config, the run aborts with a clear remediation message.

If the CSV doesn't exist for a (state, spatial_res) pair, the writer logs a warning and skips `lgdCode` — the forecast still publishes. Regenerate to fix:

```bash
uv run python scripts/extract_lgd_codes.py --state AP
```

`region_id` is zero-padded to match the format the pipeline emits (e.g. mandal LGD code `4731` → `mandal_04731`). `lgd_code` is the canonical unpadded value from the LGD portal.

## Source of truth

`<state>_datasets/lgd_normalized/` — canonical LGD-portal export. The CSVs here are a committed snapshot, so the pipeline doesn't depend on the gitignored `<state>_datasets/` tree.

## Regenerating

```bash
uv run python scripts/extract_lgd_codes.py --state AP
uv run python scripts/extract_lgd_codes.py --state OD
```

Re-run whenever `<state>_datasets/lgd_normalized/` changes, then commit the regenerated CSVs.

## Consumed by

- `pipelines/dengue/lib/lgd.py` — `lgd_code_lookup()`, `add_lgd_column()`, `require_state()`
- `pipelines/dengue/steps/train_and_predict.py` — main predictions writer
- `pipelines/dengue_downscale/steps/downscale_predictions.py` — downscale predictions writer
