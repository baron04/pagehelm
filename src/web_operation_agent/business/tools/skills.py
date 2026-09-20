"""Immutable Business Skill artifacts and deterministic catalog matching values."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .contracts import Citation, _provenance, _string_list


_SKILL_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
BUSINESS_SKILL_MATCHER_VERSION = "business-skill-trigger-v2"


def _without_redundant_reads(skills: list) -> list:
    """Drop a read-only Skill whose tools a mutation Skill in the same set already carries.

    A mutation Skill usually carries the read tools its own preflight needs, so offering the
    narrower read-only Skill beside it adds a second, ambiguous route to a strict subset of
    the same tools. Unrelated read Skills stay: a mixed-intent task needs them.
    """
    mutation_toolsets = [set(skill.tool_names) for skill in skills if not skill.read_only]
    if not mutation_toolsets:
        return list(skills)
    return [skill for skill in skills
            if (not skill.read_only
                or not any(set(skill.tool_names) < tools for tools in mutation_toolsets))]


@dataclass(frozen=True)
class BusinessSkill:
    skill_id: str
    title: str
    intent: str
    triggers: tuple[str, ...]
    instructions: tuple[str, ...]
    tool_names: tuple[str, ...]
    provenance: tuple[str, ...]
    read_only: bool
    artifact_path: str
    artifact_sha256: str

    @classmethod
    def load(cls, path: str | Path) -> "BusinessSkill":
        artifact = Path(path)
        return cls.from_bytes(artifact.read_bytes(), artifact_uri=str(artifact))

    @classmethod
    def from_bytes(cls, raw: bytes, *, artifact_uri: str) -> "BusinessSkill":
        """Load an immutable Skill artifact without requiring a filesystem path.

        ``artifact_uri`` is a logical identity retained for diagnostics and provenance;
        callers may use schemes such as ``package://`` or ``memory://``.  The bytes are
        parsed immediately, so no temporary resource path needs to outlive this call.
        """
        if not isinstance(raw, bytes):
            raise TypeError("Business Skill content must be bytes")
        if (not isinstance(artifact_uri, str) or not artifact_uri.strip()
                or len(artifact_uri) > 2_000
                or re.search(r"[\x00-\x1f\x7f]", artifact_uri)):
            raise ValueError("Business Skill artifact_uri is invalid")
        artifact_uri = artifact_uri.strip()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid Business Skill JSON: {artifact_uri}") from exc
        required = {
            "schema_version", "skill_id", "title", "intent", "triggers",
            "instructions", "tool_names", "provenance", "read_only",
        }
        if not isinstance(data, dict) or set(data) != required or data.get("schema_version") != 1:
            raise ValueError("Business Skill fields or schema_version do not match schema 1")
        skill_id = data.get("skill_id")
        if not isinstance(skill_id, str) or not _SKILL_ID.fullmatch(skill_id):
            raise ValueError("skill_id is invalid")
        for name in ("title", "intent"):
            if not isinstance(data.get(name), str) or not data[name].strip():
                raise ValueError(f"{name} must be a non-empty string")
        tool_names = _string_list(data.get("tool_names"), "tool_names")
        if len(set(tool_names)) != len(tool_names) \
                or any(not _TOOL_NAME.fullmatch(name) for name in tool_names):
            raise ValueError("tool_names are invalid")
        if not isinstance(data.get("read_only"), bool):
            raise ValueError("read_only must be a boolean")
        return cls(
            skill_id=skill_id,
            title=data["title"].strip(),
            intent=data["intent"].strip(),
            triggers=_string_list(data.get("triggers"), "triggers"),
            instructions=_string_list(data.get("instructions"), "instructions"),
            tool_names=tool_names,
            provenance=_provenance(data.get("provenance")),
            read_only=data["read_only"],
            artifact_path=artifact_uri,
            artifact_sha256=hashlib.sha256(raw).hexdigest(),
        )

    @property
    def artifact_uri(self) -> str:
        """Logical artifact identity; ``artifact_path`` remains as a compatibility name."""
        return self.artifact_path

    def candidate_card(self, *, loaded: bool = False) -> dict:
        return {
            "skill_id": self.skill_id,
            "title": self.title,
            "intent": self.intent,
            "read_only": self.read_only,
            "tools": list(self.tool_names),
            "loaded": loaded,
        }

    def loaded_card(self) -> dict:
        return {
            **self.candidate_card(loaded=True),
            "instructions": list(self.instructions),
            "tools": list(self.tool_names),
            "provenance": list(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class BusinessSkillSelection:
    """Bounded result of matching user intent against one installed catalog."""

    matcher_version: str
    skills: tuple[BusinessSkill, ...]
    trigger_matches: tuple[str, ...]
    fallback_used: bool
    #: Trigger phrases matched, but none of what they matched could perform a change, so the
    #: catalog's mutation Skills were added to the candidates anyway. Recorded because a
    #: widened surface is a decision, and a decision nobody can see is how the previous
    #: behaviour went unnoticed.
    mutation_surface_widened: bool = False
    #: The task had no domain trigger, so an application-declared route-to-Skill hint was
    #: used. The page path is trusted only as routing context and never grants a tool scope.
    route_context_used: bool = False

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(skill.skill_id for skill in self.skills)


def _answer_labels(tables: dict) -> dict[str, dict[str, str]]:
    """What the application calls its fields and scopes in each reader language.

    Evidence names a field and a scope the way the adapter's tools do -- `amount_total`,
    "customers visible to the delegated user" -- because that is what the verifier checks
    against. A Chinese answer that showed them as written had an English sentence and field
    names in every source line. The table is keyed by those exact strings, per language, and
    only renders; it never changes what an answer claims.
    """
    if not isinstance(tables, dict) or len(tables) > 8:
        raise ValueError("answer labels must map at most eight languages to tables")
    checked = {}
    for language, table in tables.items():
        if (not isinstance(language, str) or not re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", language)
                or not isinstance(table, dict) or len(table) > 500):
            raise ValueError("an answer label table is keyed by a language tag and bounded")
        for key, value in table.items():
            if not all(isinstance(text, str) and 0 < len(text.strip()) <= 200
                       and not re.search(r"[\x00-\x1f\x7f]", text) for text in (key, value)):
                raise ValueError("answer labels are short single-line text")
        checked[language] = dict(table)
    return checked
