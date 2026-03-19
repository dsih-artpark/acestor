"""CLI entrypoint for running acestor pipelines.

Usage (from project root):

    python -m acestor.run --pipeline my_pkg.my_module:build_pipeline --config path/to/config.yaml

The referenced ``build_pipeline`` callable should accept a ``PipelineConfig``
instance and return a ``PipelineDAG``. The CLI will create a ``PipelineContext``
and ``PipelineRunner`` and execute the DAG.
"""

from __future__ import annotations

import argparse
import importlib
import uuid
from typing import Callable

from acestor import PipelineConfig, PipelineContext, PipelineDAG, PipelineRunner


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
    parser = argparse.ArgumentParser(description="Run an acestor pipeline.")
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
    print(f"Run {result.run_id} finished with status={result.status}")
    return 0 if result.status == "success" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
