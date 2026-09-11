"""A synthetic `ApplicationKnowledgePort` for core session tests.

`knowledge.py` is a four-method Protocol; the implementation that satisfies it lives in
whichever application distribution owns the route grammar. Core tests previously borrowed
the OFBiz one, which made the core suite depend on that distribution *and* checked every
Skill-routing behaviour against `/{webapp}/control/{request}` only.

This implementation is deliberately the same shape and a different grammar: it composes and
recognises routes through `FIXTURE_ROUTE_PORT`, so a session behaviour that only works for
one application's URL spelling fails here. It is a test double, not a second product
implementation: no artifact format, no catalog loading, no ranking beyond trigger matching.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlencode, urljoin, urlsplit

from web_operation_agent.tests.route_fixture import FIXTURE_ROUTE_PORT


def _entry_route(webapp: str) -> str:
    return FIXTURE_ROUTE_PORT.compose_contract_route(webapp, "main")


@dataclass(frozen=True)
class FixtureSkill:
    """One read-only navigation Skill the server resolves on the model's behalf."""

    skill_id: str
    title: str
    intent: str
    triggers: tuple[str, ...]
    current_webapp: str
    request: str
    parameters: tuple[str, ...] = ()
    expected_text: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()
    read_only: bool = True

    @property
    def route_template(self) -> str:
        return FIXTURE_ROUTE_PORT.compose_contract_route(self.current_webapp, self.request)

    @property
    def artifact_path(self) -> str:
        """Where this Skill came from. A real router loads a file; this one is inline."""
        return f"fixture://{self.skill_id}"

    @property
    def artifact_sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(self.public_card(), sort_keys=True).encode("utf-8")).hexdigest()

    def public_card(self) -> dict:
        """The bounded facts the model may use; the route stays server-side."""
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
        parts = urlsplit(current_url).path.strip("/").split("/")
        if not parts or parts[0] != self.current_webapp:
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


@dataclass(frozen=True)
class FixtureKnowledgeRouter:
    """Select bounded cards and resolve opaque targets, over the synthetic grammar."""

    skills: tuple[FixtureSkill, ...] = field(default_factory=tuple)

    def _by_id(self, skill_id: object, allowed_ids: set[str]):
        if not isinstance(skill_id, str) or skill_id not in allowed_ids:
            return None, "skill_id was not supplied for this task"
        for skill in self.skills:
            if skill.skill_id == skill_id:
                return skill, ""
        return None, "skill_id is no longer in the server catalog"

    def relevant(self, task: str, limit: int = 3) -> tuple[FixtureSkill, ...]:
        folded = task.casefold()
        ranked = [(-sum(1 for trigger in skill.triggers if trigger.casefold() in folded),
                   skill.skill_id, skill) for skill in self.skills]
        return tuple(row[2] for row in sorted(ranked) if row[0] < 0)[:limit]

    def resolve(self, skill_id: object, bindings: object, current_url: str,
                allowed_ids: set[str]):
        skill, error = self._by_id(skill_id, allowed_ids)
        if skill is None:
            return "", error, None
        url, error = skill.open_url(bindings, current_url)
        return url, error, skill

    def app_link(self, skill_id: object, observation: object, allowed_ids: set[str]):
        """Find the browser-local application link without exporting its secret query."""
        skill, error = self._by_id(skill_id, allowed_ids)
        if skill is None:
            return "", error, None
        current_url = str(getattr(observation, "url", ""))
        current = urlsplit(current_url)
        if current.path.strip("/").split("/")[0] == skill.current_webapp:
            return "", f"already in {skill.current_webapp}; call open_skill_route", skill
        expected = _entry_route(skill.current_webapp)
        matches = [str(getattr(element, "ref", ""))
                   for element in getattr(observation, "elements", ())
                   if getattr(element, "role", "") == "link"
                   and getattr(element, "tag", "") == "a"
                   and _same_path(current_url, current.netloc, element, expected)]
        if len(matches) != 1 or not matches[0]:
            return "", (f"the current page must expose exactly one {skill.current_webapp} "
                        "application link"), skill
        return matches[0], "", skill

    @staticmethod
    def gap_evidence(skill: FixtureSkill, observation: object) -> dict:
        """Measure the target-link gap on the exact fresh page delivered to the Agent."""
        current_url = str(getattr(observation, "url", ""))
        current = urlsplit(current_url)
        links = [element for element in getattr(observation, "elements", ())
                 if getattr(element, "role", "") == "link"
                 and getattr(element, "tag", "") == "a"]
        hits = [str(getattr(element, "ref", "")) for element in links
                if _same_path(current_url, current.netloc, element, skill.route_template)]
        parts = current.path.strip("/").split("/")
        return {
            "page_url": current_url,
            "required_webapp": skill.current_webapp,
            "webapp_match": (parts[0] if parts and parts[0] else "") == skill.current_webapp,
            "visible_link_count": len(links),
            "target_visible_link_refs": hits,
            "target_visible_link_absent": not hits,
        }


def _same_path(current_url: str, netloc: str, element, expected_path: str) -> bool:
    href = str(getattr(element, "attrs", {}).get("href", ""))
    parsed = urlsplit(urljoin(current_url, href))
    return (parsed.scheme in ("http", "https") and parsed.netloc == netloc
            and parsed.path == expected_path)
