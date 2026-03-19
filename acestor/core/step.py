"""Step interface definitions for acestor."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, Generic, Protocol, Sequence, TypeVar

StepInputT = TypeVar("StepInputT")
StepOutputT = TypeVar("StepOutputT")


class NoInputs:
    """Sentinel type for steps that have no upstream dependencies."""


class BaseStep(Generic[StepInputT, StepOutputT]):
    """Base class for typed step implementations.

    Type parameters:
        StepInputT — a frozen dataclass whose fields are named after upstream step names,
            typed with the upstream step's result dataclass.  Use ``NoInputs``
            for root steps that have no upstream dependencies.
        StepOutputT — a frozen dataclass returned by ``run()``.

    Subclasses must set ``input_type`` as a ClassVar so the runner can
    auto-construct the typed inputs object from upstream results.
    """

    input_type: ClassVar[type] = NoInputs

    def run(
        self, context: Any, inputs: StepInputT
    ) -> StepOutputT:  # pragma: no cover - interface only
        raise NotImplementedError


class StepImpl(Protocol):
    """Protocol for user-defined step implementations (duck-typing fallback)."""

    def run(
        self, context: Any, inputs: Any
    ) -> Any:  # pragma: no cover - interface only
        ...


def build_typed_inputs(step_impl: Any, upstream_results: Dict[str, Any]) -> Any:
    """Construct the typed inputs object for a step from upstream results.

    If the step declares ``input_type = NoInputs`` (or has no ``input_type``),
    returns a ``NoInputs()`` instance.  Otherwise inspects the dataclass fields
    of ``input_type`` and populates them from ``upstream_results``.
    """
    input_cls = getattr(step_impl, "input_type", NoInputs)
    if input_cls is NoInputs:
        return NoInputs()

    if not dataclasses.is_dataclass(input_cls):
        return NoInputs()

    kwargs: Dict[str, Any] = {}
    for f in dataclasses.fields(input_cls):
        if f.name in upstream_results:
            kwargs[f.name] = upstream_results[f.name]
        elif f.default is not dataclasses.MISSING:
            pass  # field has a default (e.g. Optional = None)
        elif f.default_factory is not dataclasses.MISSING:
            pass  # field has a default_factory
        else:
            raise ValueError(
                f"Step input field {f.name!r} on {input_cls.__name__} "
                f"has no upstream result and no default value. "
                f"Available upstream: {set(upstream_results)}"
            )
    return input_cls(**kwargs)


@dataclass(eq=False)
class PipelineStep:
    """Wrapper for a single logical step in a pipeline."""

    name: str
    impl: Any
    upstream: set["PipelineStep"] = field(default_factory=set, repr=False)
    downstream: set["PipelineStep"] = field(default_factory=set, repr=False)

    def __hash__(self) -> int:
        return id(self)

    # --- DAG wiring operators (Airflow-style) ---

    def _link(self, other: "PipelineStep") -> None:
        self.downstream.add(other)
        other.upstream.add(self)

    def __rshift__(self, other: "PipelineStep | Sequence[PipelineStep]"):
        if isinstance(other, PipelineStep):
            self._link(other)
            return other
        if isinstance(other, (list, tuple)):
            for step in other:
                if not isinstance(step, PipelineStep):
                    raise TypeError(
                        "All elements on the right-hand side must be PipelineStep instances."
                    )
                self._link(step)
            return other
        raise TypeError(
            "Right-hand side of >> must be a PipelineStep or a sequence of PipelineStep."
        )

    def __rrshift__(self, other: "PipelineStep | Sequence[PipelineStep]"):
        if isinstance(other, PipelineStep):
            other._link(self)
            return self
        if isinstance(other, (list, tuple)):
            for step in other:
                if not isinstance(step, PipelineStep):
                    raise TypeError(
                        "All elements on the left-hand side must be PipelineStep instances."
                    )
                step._link(self)
            return self
        raise TypeError(
            "Left-hand side of >> must be a PipelineStep or a sequence of PipelineStep."
        )
