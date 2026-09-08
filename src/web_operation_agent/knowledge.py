"""Small, auditable application Skills for knowledge the current page cannot expose.

Skills live on the server.  A browser receives only the resulting generic ``goto`` command;
it never receives the catalog, matching rules, provenance, or route templates.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit


_SKILL_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_PARAMETER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")


def _strings(value: object, name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of strings")
    rows = tuple(item.strip() for item in value)
    if (not allow_empty and not rows) or any(not item for item in rows):
        raise ValueError(f"{name} must contain non-empty strings")
    return rows


@dataclass(frozen=True)
class AppSkill:
    skill_id: str
    title: str
    intent: str
    triggers: tuple[str, ...]
    current_webapp: str
    route_template: str
    parameters: tuple[str, ...]
    expected_text: tuple[str, ...]
    provenance: tuple[str, ...]
    read_only: bool
    artifact_path: str
    artifact_sha256: str

    @classmethod
    def load(cls, path: str | Path) -> "AppSkill":
        artifact = Path(path)
        raw = artifact.read_bytes()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid Skill JSON: {artifact}") from exc
        if not isinstance(data, dict):
            raise ValueError("Skill must be a JSON object")
        required = {
            "schema_version", "skill_id", "title", "intent", "triggers",
            "current_webapp", "route_template", "parameters", "expected_text",
            "provenance", "read_only",
        }
        if set(data) != required or data.get("schema_version") != 1:
            raise ValueError("Skill fields or schema_version do not match schema 1")
        skill_id = data["skill_id"]
        if not isinstance(skill_id, str) or not _SKILL_ID.fullmatch(skill_id):
            raise ValueError("skill_id is invalid")
        for name in ("title", "intent", "current_webapp", "route_template"):
            if not isinstance(data[name], str) or not data[name].strip():
                raise ValueError(f"{name} must be a non-empty string")
        webapp = data["current_webapp"]
        if not re.fullmatch(r"[A-Za-z0-9_]+", webapp):
            raise ValueError("current_webapp is invalid")
        route = data["route_template"]
        parsed = urlsplit(route)
        if (not route.startswith(f"/{webapp}/control/") or route.startswith("//")
                or parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
                or "\\" in route or "%" in parsed.path or ".." in parsed.path.split("/")):
            raise ValueError("route_template must be a plain same-webapp control path")
        parameters = _strings(data["parameters"], "parameters", allow_empty=True)
        if len(parameters) > 16 or len(set(parameters)) != len(parameters) \
                or any(not _PARAMETER.fullmatch(item) for item in parameters):
            raise ValueError("parameters are invalid")
        if data["read_only"] is not True:
            raise ValueError("only read-only Skills are supported")
        return cls(
            skill_id=skill_id, title=data["title"].strip(), intent=data["intent"].strip(),
            triggers=_strings(data["triggers"], "triggers"), current_webapp=webapp,
            route_template=route, parameters=parameters,
            expected_text=_strings(data["expected_text"], "expected_text"),
            provenance=_strings(data["provenance"], "provenance"), read_only=True,
            artifact_path=str(artifact), artifact_sha256=hashlib.sha256(raw).hexdigest(),
        )

    def public_card(self) -> dict:
        """The bounded facts the model may use; route and provenance remain server-side."""
        return {
            "skill_id": self.skill_id,
            "title": self.title,
            "intent": self.intent,
            "requires_current_webapp": self.current_webapp,
            "parameters": list(self.parameters),
            "expected_text": list(self.expected_text),
        }

    def open_url(self, bindings: object, current_url: str) -> tuple[str, str]:
        if not isinstance(bindings, dict):
            return "", "bindings must be an object"
        current = urlsplit(current_url)
        current_parts = current.path.strip("/").split("/")
        if not current_parts or current_parts[0] != self.current_webapp:
            return "", f"open the {self.current_webapp} application from the current page first"
        if set(bindings) != set(self.parameters):
            return "", "bindings must contain exactly the declared parameters"
        if any(not isinstance(value, str) for value in bindings.values()):
            return "", "every binding value must be a string"
        if any(len(value) > 1000 or re.search(r"[\x00-\x1f\x7f]", value)
               for value in bindings.values()):
            return "", "a binding is too long or contains control characters"
        query = urlencode([(name, bindings[name]) for name in self.parameters])
        return self.route_template + (("?" + query) if query else ""), ""


class KnowledgeRouter:
    """Select and resolve a small allowlisted catalog without model-side path invention."""

    def __init__(self, skills: tuple[AppSkill, ...]):
        ids = [skill.skill_id for skill in skills]
        if len(ids) != len(set(ids)):
            raise ValueError("Skill IDs must be unique")
        self._skills = {skill.skill_id: skill for skill in skills}

    @classmethod
    def from_paths(cls, paths: list[str | Path] | tuple[str | Path, ...]) -> "KnowledgeRouter":
        return cls(tuple(AppSkill.load(path) for path in paths))

    def relevant(self, task: str, limit: int = 3) -> tuple[AppSkill, ...]:
        task_folded = task.casefold()
        ranked: list[tuple[int, str, AppSkill]] = []
        for skill in self._skills.values():
            score = sum(1 for trigger in skill.triggers if trigger.casefold() in task_folded)
            if score:
                ranked.append((-score, skill.skill_id, skill))
        return tuple(row[2] for row in sorted(ranked)[:limit])

    def resolve(self, skill_id: object, bindings: object, current_url: str,
                allowed_ids: set[str]) -> tuple[str, str, AppSkill | None]:
        if not isinstance(skill_id, str) or skill_id not in allowed_ids:
            return "", "skill_id was not supplied for this task", None
        skill = self._skills.get(skill_id)
        if skill is None:
            return "", "skill_id is no longer in the server catalog", None
        url, error = skill.open_url(bindings, current_url)
        return url, error, skill

    def app_link(self, skill_id: object, observation: object,
                 allowed_ids: set[str]) -> tuple[str, str, AppSkill | None]:
        """Find the browser-local app handoff link without exporting its secret query value."""
        if not isinstance(skill_id, str) or skill_id not in allowed_ids:
            return "", "skill_id was not supplied for this task", None
        skill = self._skills.get(skill_id)
        if skill is None:
            return "", "skill_id is no longer in the server catalog", None
        current = urlsplit(str(getattr(observation, "url", "")))
        if current.path.strip("/").split("/")[0] == skill.current_webapp:
            return "", f"already in {skill.current_webapp}; call open_skill_route", skill
        expected_path = f"/{skill.current_webapp}/control/main"
        matches: list[str] = []
        for element in getattr(observation, "elements", ()):
            if getattr(element, "role", "") != "link" or getattr(element, "tag", "") != "a":
                continue
            href = str(getattr(element, "attrs", {}).get("href", ""))
            parsed = urlsplit(urljoin(str(getattr(observation, "url", "")), href))
            if (parsed.scheme in ("http", "https") and parsed.netloc == current.netloc
                    and parsed.path == expected_path):
                matches.append(str(getattr(element, "ref", "")))
        if len(matches) != 1 or not matches[0]:
            return "", (f"the current page must expose exactly one {skill.current_webapp} "
                        "application link"), skill
        return matches[0], "", skill

    @staticmethod
    def gap_evidence(skill: AppSkill, observation: object) -> dict:
        """Measure the target-link gap on the exact fresh page delivered to the Agent."""
        current_url = str(getattr(observation, "url", ""))
        current = urlsplit(current_url)
        visible_links = []
        target_hits = []
        for element in getattr(observation, "elements", ()):
            if getattr(element, "role", "") != "link" or getattr(element, "tag", "") != "a":
                continue
            visible_links.append(element)
            href = str(getattr(element, "attrs", {}).get("href", ""))
            parsed = urlsplit(urljoin(current_url, href))
            if parsed.netloc == current.netloc and parsed.path == skill.route_template:
                target_hits.append(str(getattr(element, "ref", "")))
        webapp = current.path.strip("/").split("/")[0] if current.path.strip("/") else ""
        return {
            "page_url": current_url,
            "required_webapp": skill.current_webapp,
            "webapp_match": webapp == skill.current_webapp,
            "visible_link_count": len(visible_links),
            "target_visible_link_refs": target_hits,
            "target_visible_link_absent": not target_hits,
        }
