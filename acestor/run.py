"""CLI entrypoint for running acestor.

Usage (from project root):

    python -m acestor.run --pipeline module.path:build_function --config path/to/config.yaml

The referenced callable must accept a ``PipelineConfig`` and return a
``PipelineDAG``. The CLI builds a ``PipelineContext`` and ``PipelineRunner``
and executes the graph.
"""

from __future__ import annotations

import argparse
import importlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent.parent / ".env", override=False)
except ImportError:
    pass

from acestor import PipelineConfig, PipelineContext, PipelineDAG, PipelineRunner
from acestor.infra.run_notification import send_run_notification_email_if_configured


def _load_builder(spec: str) -> Callable[[PipelineConfig], PipelineDAG]:
    """Load a build_pipeline(config) -> PipelineDAG function from a module spec."""
    if ":" not in spec:
        raise ValueError(
            "pipeline spec must be of the form 'module.path:callable_name'"
        )
    module_name, func_name = spec.split(":", 1)
    module = importlib.import_module(module_name)
    builder = getattr(module, func_name, None)
    if builder is None:
        raise ValueError(f"Module {module_name!r} has no attribute {func_name!r}")
    return builder


def _apply_overrides(raw: dict, overrides: list[str]) -> dict:
    """Apply --set key.path=value overrides to a raw config dict in-place."""
    import yaml as _yaml

    for item in overrides:
        if "=" not in item:
            raise ValueError(f"--set value must be key=value, got: {item!r}")
        key_path, _, raw_value = item.partition("=")
        keys = key_path.strip().split(".")
        # Parse the value as YAML so booleans, ints, lists work naturally
        value = _yaml.safe_load(raw_value)
        target = raw
        for k in keys[:-1]:
            target = target.setdefault(k, {})
        target[keys[-1]] = value
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run acestor.")
    parser.add_argument(
        "--pipeline",
        required=True,
        help="Pipeline builder in the form 'module.path:callable_name'. "
        "The callable must accept a PipelineConfig and return a PipelineDAG.",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to YAML configuration file.",
    )
    parser.add_argument(
        "--run-id",
        required=False,
        help="Optional run identifier. If omitted, a UUID will be generated.",
    )
    parser.add_argument(
        "--set",
        metavar="KEY=VALUE",
        action="append",
        default=[],
        dest="overrides",
        help="Override a config value using dot-notation, e.g. --set model_configs.rf.tune=true. "
        "Values are parsed as YAML (booleans, ints, lists all work). Repeatable.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help=(
            "Wipe the run-id's artifact subtree before the DAG executes. Default "
            "off — when re-using a run-id deliberately (e.g. re-running only a "
            "downstream step), omit this. Set it when iterating on configs to "
            "stop files from a previous run shadowing the current one (e.g. an "
            "orphan predictions_<model>.csv from a since-removed model)."
        ),
    )

    args = parser.parse_args(argv)

    if args.overrides:
        import yaml as _yaml
        from pathlib import Path as _Path
        from acestor.core.config import _resolve_env_recursive

        with open(args.config) as f:
            raw_config = _yaml.safe_load(f) or {}
        _apply_overrides(raw_config, args.overrides)
        config = PipelineConfig(
            path=_Path(args.config), raw=_resolve_env_recursive(raw_config)
        )
    else:
        config = PipelineConfig.from_yaml(args.config)
    run_id = args.run_id or str(uuid.uuid4())

    builder = _load_builder(args.pipeline)
    dag = builder(config)

    context = PipelineContext.from_config(config, run_id=run_id, clean=args.clean)

    runner = PipelineRunner(dag=dag, context=context)

    result = runner.run()
    if result.status == "failed":
        print(f"\n[FAILED] Run '{result.run_id}' did not complete.")
        print(f"  Reason: {result.failure_detail}")
        print()
    else:
        print(f"Run {result.run_id} finished with status={result.status}")

    # Failure notifications: the DAG may not reach ``notify_run``, so handle here.
    if result.status == "failed":
        cfg = context.config or {}
        email_cfg = cfg.get("email") or {}
        if email_cfg.get("enabled") and "failed" in (email_cfg.get("on") or []):
            end_ts = datetime.now(timezone.utc).isoformat()
            start_ts = context.run_started_at or end_ts
            send_run_notification_email_if_configured(
                config=cfg,
                run_id=context.run_id,
                status="failed",
                start_ts=start_ts,
                end_ts=end_ts,
                step_names=sorted(context.completed_steps),
                failure_detail=result.failure_detail,
                logger=context.log,
            )

    return 0 if result.status == "success" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
