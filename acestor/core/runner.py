"""Pipeline execution engine for acestor."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict
from concurrent.futures import ThreadPoolExecutor, Future, wait, FIRST_COMPLETED

from acestor.core.context import PipelineContext
from acestor.core.dag import PipelineDAG
from acestor.core.step import build_typed_inputs


@dataclass
class RunResult:
    """Lightweight container for a single run result."""

    run_id: str
    status: str
    steps: Dict[str, Any]
    failure_detail: str = ""
    failed_step: str = ""


@dataclass
class PipelineRunner:
    """Coordinates execution of a PipelineDAG."""

    dag: PipelineDAG
    context: PipelineContext

    def run(self) -> RunResult:
        """Execute the DAG.

        Runs independent steps concurrently using threads, respecting DAG dependencies.
        """
        logger = self.context.logger
        start_ts = datetime.now(timezone.utc).isoformat()
        self.context.run_started_at = start_ts
        self.context.completed_steps = []
        if logger is not None:
            logger.info("Starting run %s", self.context.run_id)

        results: Dict[str, Any] = {}
        status = "success"
        failure_detail = ""
        failed_step = ""

        step_start_times: Dict[str, float] = {}

        try:
            name_to_step = {s.name: s for s in self.dag.steps}

            in_degree: Dict[str, int] = {name: 0 for name in self.dag.edges}
            for parent, children in self.dag.edges.items():
                for child in children:
                    in_degree[child] = in_degree.get(child, 0) + 1

            ready = [name for name, deg in in_degree.items() if deg == 0]
            futures: Dict[Future, str] = {}

            def submit_step(executor: ThreadPoolExecutor, step_name: str) -> None:
                if logger is not None:
                    logger.info("Running step %s", step_name)
                step_start_times[step_name] = time.monotonic()
                step = name_to_step[step_name]
                parent_names = self.dag.parents(step_name)
                upstream_results = {p: results[p] for p in parent_names}
                inputs = build_typed_inputs(step.impl, upstream_results)
                fut = executor.submit(step.impl.run, self.context, inputs)
                futures[fut] = step_name

            with ThreadPoolExecutor() as executor:
                for name in ready:
                    submit_step(executor, name)

                while futures:
                    done, _ = wait(set(futures.keys()), return_when=FIRST_COMPLETED)
                    for fut in list(done):
                        step_name = futures.pop(fut)
                        try:
                            results[step_name] = fut.result()
                        except Exception:
                            elapsed = time.monotonic() - step_start_times.get(
                                step_name, time.monotonic()
                            )
                            failed_step = step_name
                            if logger is not None:
                                # logger.exception captures the full traceback,
                                # so the underlying file:line is in run.log.
                                logger.exception(
                                    "Step %s failed after %.2fs",
                                    step_name,
                                    elapsed,
                                )
                            raise
                        elapsed = time.monotonic() - step_start_times.get(
                            step_name, time.monotonic()
                        )
                        if logger is not None:
                            logger.info("Finished step %s in %.2fs", step_name, elapsed)
                        self.context.completed_steps.append(step_name)

                        for child in self.dag.children(step_name):
                            in_degree[child] -= 1
                            if in_degree[child] == 0:
                                submit_step(executor, child)
        except Exception as exc:
            status = "failed"
            failure_detail = f"{type(exc).__name__}: {exc}"
            # When failed_step is set, the inner step handler already logged
            # the traceback via logger.exception. Otherwise this is a non-step
            # failure (DAG bookkeeping, build_typed_inputs, etc.) and we need
            # to log here or lose the trace entirely.
            if logger is not None and not failed_step:
                logger.exception(
                    "Run %s failed before step dispatch", self.context.run_id
                )

        end_ts = datetime.now(timezone.utc).isoformat()
        if logger is not None:
            if status == "failed":
                logger.info(
                    "Finished run %s with status=failed at step=%s detail=%s",
                    self.context.run_id,
                    failed_step or "<unknown>",
                    failure_detail,
                )
            else:
                logger.info(
                    "Finished run %s with status=%s",
                    self.context.run_id,
                    status,
                )

        runs_storage = self.context.storages.get("runs")
        if runs_storage is not None:
            pipeline_name = (self.context.config.get("pipeline") or {}).get(
                "name"
            ) or "pipeline"
            run_meta = {
                "run_id": self.context.run_id,
                "pipeline": pipeline_name,
                "status": status,
                "started_at": start_ts,
                "finished_at": end_ts,
                "steps": list(results.keys()),
                "failed_step": failed_step,
                "failure_detail": failure_detail,
            }
            try:
                runs_storage.write_text(
                    json.dumps(run_meta, indent=2), f"{self.context.run_id}/run.json"
                )
            except Exception:
                if logger is not None:
                    logger.exception(
                        "Failed to write run metadata for %s", self.context.run_id
                    )

        return RunResult(
            run_id=self.context.run_id,
            status=status,
            steps=results,
            failure_detail=failure_detail,
            failed_step=failed_step,
        )
