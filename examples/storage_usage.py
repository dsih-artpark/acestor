"""Example: using storages['raw'] and storages['runs'] in a step."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from acestor import (
    PipelineConfig,
    PipelineContext,
    PipelineStep,
    PipelineDAG,
    PipelineRunner,
)


@dataclass
class CopyFromRawToRuns:
    def run(self, context: PipelineContext, inputs: Dict[str, Any]) -> Dict[str, Any]:
        raw_store = context.storages.get("raw")
        runs_store = context.storages.get("runs")
        if raw_store is None or runs_store is None:
            raise RuntimeError("Both 'raw' and 'runs' storages must be configured.")

        text = raw_store.read_text("hello.txt")
        out_path = runs_store.write_text(text, "copied/hello.txt")
        return {"copied_path": out_path}


def main() -> None:
    config = PipelineConfig.from_yaml("example.yaml")
    context = PipelineContext.from_config(config, run_id="storage-example-run")

    copy_step = PipelineStep(name="copy", impl=CopyFromRawToRuns())
    dag = PipelineDAG.from_steps([copy_step])

    runner = PipelineRunner(dag=dag, context=context)
    result = runner.run()
    print(result)


if __name__ == "__main__":
    main()
