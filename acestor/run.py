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

    args = parser.parse_args(argv)

    config = PipelineConfig.from_yaml(args.config)
    run_id = args.run_id or str(uuid.uuid4())

    builder = _load_builder(args.pipeline)
    dag = builder(config)

    context = PipelineContext.from_config(config, run_id=run_id)
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
