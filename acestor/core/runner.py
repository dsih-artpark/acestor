"""Pipeline execution engine for acestor."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict
from concurrent.futures import ThreadPoolExecutor, Future, wait, FIRST_COMPLETED

from acestor.core.context import PipelineContext
from acestor.core.dag import PipelineDAG
from acestor.core.step import build_typed_inputs
from acestor.infra import send_email


@dataclass
class RunResult:
    """Lightweight container for a single run result."""

    run_id: str
    status: str
    steps: Dict[str, Any]


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
        if logger is not None:
            logger.info("Starting run %s", self.context.run_id)

        results: Dict[str, Any] = {}
        status = "success"

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
                        results[step_name] = fut.result()

                        for child in self.dag.children(step_name):
                            in_degree[child] -= 1
                            if in_degree[child] == 0:
                                submit_step(executor, child)
        except Exception:
            status = "failed"
            if logger is not None:
                logger.exception("Run %s failed", self.context.run_id)

        end_ts = datetime.now(timezone.utc).isoformat()
        if logger is not None:
            logger.info("Finished run %s with status=%s", self.context.run_id, status)

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

        email_cfg = (
            (self.context.config.get("email") or {})
            if isinstance(self.context.config, dict)
            else {}
        )
        if email_cfg.get("enabled"):
            try:
                on = email_cfg.get("on") or []
                if status in on:
                    smtp_cfg = email_cfg.get("smtp") or {}
                    host = smtp_cfg.get("host")
                    port = int(smtp_cfg.get("port", 587))
                    username = smtp_cfg.get("username")
                    password = smtp_cfg.get("password")
                    use_tls = bool(smtp_cfg.get("use_tls", True))
                    sender = email_cfg.get("from")
                    recipients = email_cfg.get("to") or []
                    if host and sender and recipients:
                        pipeline_name = (self.context.config.get("pipeline") or {}).get(
                            "name"
                        ) or "pipeline"
                        subject = f"[acestor] {pipeline_name} run {status} (run_id={self.context.run_id})"
                        body = (
                            f"Pipeline: {pipeline_name}\n"
                            f"Run ID: {self.context.run_id}\n"
                            f"Status: {status}\n"
                            f"Started: {start_ts}\n"
                            f"Finished: {end_ts}\n"
                            f"Steps: {', '.join(results.keys())}\n"
                        )
                        send_email(
                            host=host,
                            port=port,
                            username=username,
                            password=password,
                            use_tls=use_tls,
                            sender=sender,
                            recipients=recipients,
                            subject=subject,
                            body=body,
                        )
            except Exception:
                if logger is not None:
                    logger.exception(
                        "Failed to send notification email for run %s",
                        self.context.run_id,
                    )

        return RunResult(run_id=self.context.run_id, status=status, steps=results)
