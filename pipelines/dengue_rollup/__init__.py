"""dengue_rollup — sum child predictions into parent-level predictions.

Mirror of :mod:`pipelines.dengue_downscale` but flipped: consumes a
finer-grained forecast (e.g. zone) and aggregates it up to a coarser
parent level (e.g. corp) via a simple groupby-sum on the geojson-defined
parent-child mapping.

Where downscale uses LRM to split a parent prediction into children while
preserving the total, rollup is the trivial inverse: the parent's
prediction is exactly the sum of its children's predictions. Preserves
both the raw-float and int-display invariants automatically.

Zone classification at the parent level is re-derived from parent-level
historical case data, so parent thresholds match what the main pipeline
would emit for a native run at that level.
"""
