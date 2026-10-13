"""Optional business-operation port; the existing read GoalPort is unchanged."""
from typing import Protocol, runtime_checkable

from ..business.tools.registry import ToolRegistry
from ..skills import SkillRegistry


@runtime_checkable
class BusinessOperationPort(Protocol):
    @property
    def tools(self) -> ToolRegistry: ...
    @property
    def skills(self) -> SkillRegistry: ...
    def invalidate_reads(self, entities: frozenset[str]) -> None: ...


class RegisteredOperations:
    def __init__(self, tools: ToolRegistry, skills: SkillRegistry, invalidate_reads, *,
                 observed_mutation_operations: frozenset[str] = frozenset()):
        if not isinstance(tools, ToolRegistry) or not isinstance(skills, SkillRegistry):
            raise TypeError("operations require installed tools and Skills")
        self.tools, self.skills, self._invalidate_reads = tools, skills, invalidate_reads
        writes = frozenset(d.name for d in tools.definitions if d.effect == "write")
        if (not isinstance(observed_mutation_operations, frozenset)
                or not observed_mutation_operations <= writes):
            raise ValueError("observed mutation operations must name registered writes")
        self.observed_mutation_operations = observed_mutation_operations
    def invalidate_reads(self, entities): self._invalidate_reads(entities)
