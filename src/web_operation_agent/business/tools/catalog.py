"""Skill discovery and capability selection over a validated business catalog."""
from __future__ import annotations

import hashlib
import json

from ..text import _trigger_matches, trigger_requests_action
from .skills import (BUSINESS_SKILL_MATCHER_VERSION, BusinessSkill,
                     BusinessSkillSelection, _without_redundant_reads)


class BusinessSkillCatalog:
    """Read-only view of installed Skills and their typed tool schemas."""

    def __init__(self, *, skills: dict[str, BusinessSkill], schemas: dict[str, dict],
                 effects: dict[str, str], required_scopes: dict[str, str],
                 route_skill_hints: tuple, withheld_skills: tuple[BusinessSkill, ...]):
        self._skills = skills
        self._schemas = schemas
        self._effects = effects
        self._required_scopes = required_scopes
        self._route_skill_hints = route_skill_hints
        self._withheld_skills = withheld_skills

    @property
    def catalog_manifest(self) -> dict:
        """Return a versioned, non-secret description of installed Skills and tools."""
        skills = [{
            "skill_id": skill.skill_id,
            "artifact_sha256": skill.artifact_sha256,
            "read_only": skill.read_only,
            "tools": list(skill.tool_names),
        } for skill in sorted(self._skills.values(), key=lambda item: item.skill_id)]
        tools = [{
            "name": name,
            "effect": self._effects[name],
            "required_scope": self._required_scopes.get(name, ""),
            "schema_sha256": hashlib.sha256(json.dumps(
                self._schemas[name], ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")).hexdigest(),
        } for name in sorted(self._schemas)]
        route_hints = [{"pattern": pattern, "skill_id": skill_id}
                       for _compiled, pattern, skill_id in self._route_skill_hints]
        return {"schema_version": 1, "skills": skills, "tools": tools,
                "route_skill_hints": route_hints}

    @property
    def catalog_sha256(self) -> str:
        encoded = json.dumps(
            self.catalog_manifest, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def resolve_capability(self, goal: str, *, allow_mutations: bool,
                           effect: str = "write", extra: tuple[str, ...] = (),
                           actor_scopes: tuple[str, ...] | None = None) -> tuple[str, ...]:
        """Search registered tools; Skills only supply optional search vocabulary.

        This is a lexical hint, not a proof that no semantically similar capability exists.
        Dispatch independently checks the actor, arguments and exact effect.
        """
        if not isinstance(allow_mutations, bool):
            raise ValueError("allow_mutations must be a boolean")
        if effect not in ("write", "any"):
            raise ValueError("a capability search looks for write capabilities or any")
        folded = str(goal or "").casefold()
        if not folded.strip():
            raise ValueError("a capability search needs the goal it searched for")
        scopes = None if actor_scopes is None else set(actor_scopes)
        found: list[str] = []
        for name in sorted(self._schemas):
            if (self._effects[name] == "write" and not allow_mutations
                    or effect == "write" and self._effects[name] != "write"
                    or scopes is not None and self._required_scopes.get(name)
                    and self._required_scopes[name] not in scopes):
                continue
            if (name in folded or name.replace("_", " ") in folded
                    or any(name in skill.tool_names
                           and any(_trigger_matches(trigger, folded)
                                   for trigger in skill.triggers)
                           for skill in self._skills.values())):
                found.append(name)
        found += [name for name in extra
                  if isinstance(name, str) and name.strip() and name not in found]
        return tuple(dict.fromkeys(found))

    def requested_writes(self, task: str) -> frozenset[str]:
        """The write tools this request itself asks to use.

        A write is asked for when the request words it as something to do, not as a
        description ("create Ada", never "list my contacts" or "the ones created today"):
        by the tool's own name or the action it begins with, or by a trigger of a Skill
        that declares the tool. Records, pages and model output never count; they are not
        the person's request.
        """
        text = str(task or "")
        found = set()
        for name in self._schemas:
            if self._effects[name] != "write":
                continue
            words = [name.replace("_", " "), name.split("_")[0]] + [
                trigger for skill in self._skills.values() if name in skill.tool_names
                for trigger in skill.triggers]
            if any(trigger_requests_action(word, text) for word in words):
                found.add(name)
        return frozenset(found)

    def withheld_capability(self, goal: str) -> tuple[str, ...]:
        """Tool names the adapter says this actor's grant withheld, not Skill grants."""
        folded = str(goal or "").casefold()
        if not folded.strip():
            raise ValueError("a capability search needs the goal it searched for")
        found: list[str] = []
        for skill in sorted(self._withheld_skills, key=lambda item: item.skill_id):
            if (any(_trigger_matches(trigger, folded) for trigger in skill.triggers)
                    or any(name.replace("_", " ") in folded or name in folded
                           for name in skill.tool_names)):
                found.extend(name for name in skill.tool_names
                             if name not in self._schemas)
        return tuple(dict.fromkeys(found))

    def relevant(self, task: str, limit: int = 3) -> tuple[BusinessSkill, ...]:
        folded = task.casefold()
        ranked: list[tuple[int, str, BusinessSkill]] = []
        for skill in self._skills.values():
            score = sum(1 for trigger in skill.triggers
                        if _trigger_matches(trigger, folded))
            if score:
                ranked.append((-score, skill.skill_id, skill))
        selected = [row[2] for row in sorted(ranked)]
        # A mutation Skill often contains the read tools needed for duplicate checks and
        # verification.  In that case a broader read-only Skill adds a second, ambiguous
        # route to a strict subset of the same tools.  Remove only that redundant candidate;
        # unrelated read Skills remain available for mixed-intent tasks.
        return tuple(_without_redundant_reads(selected)[:limit])

    def capability_skills(self, *, allow_mutations: bool) -> tuple[BusinessSkill, ...]:
        """Return the installed user-facing capability catalog in stable order.

        This is descriptive only: exposing a mutation Skill here neither loads its tools nor
        grants approval.  Keeping the catalog on the runtime avoids teaching the generic
        session about an application's entities.
        """
        if not isinstance(allow_mutations, bool):
            raise ValueError("allow_mutations must be a boolean")
        return tuple(
            skill for skill in sorted(self._skills.values(), key=lambda item: item.skill_id)
            if skill.read_only or allow_mutations)

    def select(self, task: str, *, allow_mutations: bool,
               trusted_entry_path: str = "",
               limit: int = 5) -> BusinessSkillSelection:
        """Select task candidates and use a bounded capability-aware empty fallback.

        Trigger matching is the primary high-precision route. If it finds nothing eligible,
        expose read-only Skills plus mutation Skills only when this turn has a real approval
        path. This preserves the user-text-only injection boundary while preventing one missed
        paraphrase from making the application's entire typed tool surface disappear.
        """
        if not isinstance(allow_mutations, bool):
            raise ValueError("allow_mutations must be a boolean")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("Skill candidate limit must be an integer from 1 to 20")
        if trusted_entry_path and (
                not isinstance(trusted_entry_path, str)
                or not trusted_entry_path.startswith("/")
                or len(trusted_entry_path) > 1_000):
            raise ValueError("trusted_entry_path must be a bounded absolute path")
        matched = self.relevant(task, limit=max(limit, len(self._skills)))
        eligible = [skill for skill in matched if skill.read_only or allow_mutations]
        route_context_used = False
        if not eligible and trusted_entry_path:
            context_ids = []
            for pattern, _source, skill_id in self._route_skill_hints:
                if pattern.fullmatch(trusted_entry_path) and skill_id not in context_ids:
                    context_ids.append(skill_id)
            eligible = [self._skills[skill_id] for skill_id in context_ids]
            route_context_used = bool(eligible)
        # Trigger phrases rank the catalog; they do not decide what this turn is able to do.
        # A write trigger is a verb and its object ("confirm quotation", "确认报价单"); the
        # read trigger beside it is usually the object alone ("quotation", "报价单"). Every
        # sentence matching the write therefore matches the read, and none of the reverse.
        # When word order breaks the write phrase -- "把报价单 S00002 确认掉" puts the verb
        # last -- the read Skill still matches, `matched` is not empty, and the fallback below
        # never runs, because it only ever fired on *no* match. The turn is then left with no
        # write tool and refuses correctly for the wrong reason, which reads exactly like the
        # product holding its line. Measured on the Odoo catalog: 10 of 21 ordinary phrasings
        # reached the write Skill through triggers alone.
        #
        # The catalog itself says which write belongs to which read: a mutation Skill carries
        # the read tools its own preflight needs, so its tool set is a strict superset of the
        # narrower read-only Skill about the same subject. When triggers land on that read
        # Skill and this turn can write, offer both and let the model say which it meant.
        # Widening no further is the point -- "list every product" gains nothing, because
        # `products-query` has no write Skill extending it.
        widened = False
        if (allow_mutations and eligible and not route_context_used
                and all(skill.read_only for skill in eligible)):
            selected_tools = [set(skill.tool_names) for skill in eligible]
            present = {skill.skill_id for skill in eligible}
            # Subsuming *every* matched read Skill is what makes this safe. "Create a person
            # named Bob at Acme company" matches both people and companies, and company
            # creation extends only one of them -- so the sentence is about more than that
            # write, and nothing is added. A subject named alone, whose whole read surface one
            # mutation Skill already carries, is the only case triggers cannot resolve.
            addition = [
                skill for skill in sorted(self._skills.values(),
                                          key=lambda item: item.skill_id)
                if (not skill.read_only and skill.skill_id not in present
                    and all(tools < set(skill.tool_names) for tools in selected_tools))]
            widened = bool(addition)
            eligible += addition
        # Pruning the read-only Skill here would undo the widening: it was added on that
        # Skill's account, and the reader may well have meant the read.
        if not widened:
            eligible = _without_redundant_reads(eligible)
        if eligible:
            return BusinessSkillSelection(
                BUSINESS_SKILL_MATCHER_VERSION, tuple(eligible[:limit]),
                tuple(skill.skill_id for skill in matched), False,
                mutation_surface_widened=widened,
                route_context_used=route_context_used)

        fallback = [
            skill for skill in sorted(self._skills.values(), key=lambda item: item.skill_id)
            if skill.read_only or allow_mutations
        ]
        fallback = _without_redundant_reads(fallback)
        return BusinessSkillSelection(
            BUSINESS_SKILL_MATCHER_VERSION, tuple(fallback[:limit]),
            tuple(skill.skill_id for skill in matched), bool(fallback))

    def load(self, skill_id: object, allowed_ids: set[str]) -> tuple[BusinessSkill | None, str]:
        if not isinstance(skill_id, str) or skill_id not in allowed_ids:
            return None, "skill_id was not selected for this task"
        skill = self._skills.get(skill_id)
        if skill is None:
            return None, "skill_id is no longer in the server catalog"
        return skill, ""
