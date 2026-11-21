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
                 observed_mutation_operations: frozenset[str] = frozenset(),
                 default_loaded_skill_ids: frozenset[str] = frozenset()):
        if not isinstance(tools, ToolRegistry) or not isinstance(skills, SkillRegistry):
            raise TypeError("operations require installed tools and Skills")
        if not isinstance(default_loaded_skill_ids, frozenset):
            raise TypeError("default_loaded_skill_ids must be a frozenset")
        for skill_id in sorted(default_loaded_skill_ids):
            skills.get(skill_id)
        self.tools, self.skills, self._invalidate_reads = tools, skills, invalidate_reads
        writes = frozenset(d.name for d in tools.definitions if d.effect == "write")
        if (not isinstance(observed_mutation_operations, frozenset)
                or not observed_mutation_operations <= writes):
            raise ValueError("observed mutation operations must name registered writes")
        self.observed_mutation_operations = observed_mutation_operations
        self.default_loaded_skill_ids = default_loaded_skill_ids
    def invalidate_reads(self, entities): self._invalidate_reads(entities)
