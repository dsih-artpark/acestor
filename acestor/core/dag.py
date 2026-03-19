"""DAG data structures and Airflow-style wiring for acestor."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Set

from .step import PipelineStep


@dataclass
class PipelineDAG:
    """Minimal representation of a directed acyclic graph of steps."""

    steps: List[PipelineStep]
    edges: Dict[str, Set[str]] = field(default_factory=dict)  # parent -> {children}

    @classmethod
    def from_steps(cls, steps: Sequence[PipelineStep]) -> "PipelineDAG":
        """Construct a DAG from a sequence of steps using their wired edges."""
        step_list = list(steps)
        name_to_step = {s.name: s for s in step_list}
        if len(name_to_step) != len(step_list):
            raise ValueError("Step names must be unique within a DAG.")

        edges: Dict[str, Set[str]] = {s.name: set() for s in step_list}
        for step in step_list:
            for child in step.downstream:
                if child.name not in name_to_step:
                    raise ValueError(
                        f"Step {child.name!r} is not included in from_steps(...) list."
                    )
                edges[step.name].add(child.name)

        dag = cls(steps=step_list, edges=edges)
        dag._validate_acyclic()
        return dag

    # --- Introspection helpers ---

    def parents(self, name: str) -> Set[str]:
        return {parent for parent, children in self.edges.items() if name in children}

    def children(self, name: str) -> Set[str]:
        return set(self.edges.get(name, set()))

    def topological_order(self) -> List[str]:
        """Return a topological ordering of step names."""
        # Kahn's algorithm
        in_degree: Dict[str, int] = {name: 0 for name in self.edges}
        for parent, children in self.edges.items():
            for child in children:
                in_degree[child] = in_degree.get(child, 0) + 1

        queue: List[str] = [name for name, deg in in_degree.items() if deg == 0]
        order: List[str] = []

        while queue:
            node = queue.pop(0)
            order.append(node)
            for child in self.edges.get(node, set()):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

        if len(order) != len(in_degree):
            raise ValueError("DAG contains a cycle.")
        return order

    # --- Internal helpers ---

    def _validate_acyclic(self) -> None:
        # This will raise if there is a cycle.
        self.topological_order()
