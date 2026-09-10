"""Engine-neutral evidence contract shared by every Twenty runner.

A runner host calls these functions with the public user answer, the routes and companies
that its own successful business-tool results disclosed, and text sampled from the final
rendered page. Nothing here repairs an answer, reads database truth, or accepts a model's
tool arguments as a substitute for observed facts.
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any


MAX_VISIBLE_BYTES = 512 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{2,159}\Z")
_DOMAIN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class EvidenceContractError(ValueError):
    """The runner input or the deterministic evidence contract is not satisfied."""


def bounded_text(value: Any, name: str, maximum: int) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or _CONTROL.search(value)):
        raise EvidenceContractError(f"{name} must be a bounded display string")
    return value.strip()


def normalize_domain(value: Any) -> str:
    if value in (None, ""):
        return ""
    selected = bounded_text(value, "company domain", 300).casefold()
    selected = re.sub(r"^https?://", "", selected).rstrip("/")
    if not _DOMAIN.fullmatch(selected):
        raise EvidenceContractError("company domain is invalid")
    return selected


def tool_domain(value: Any) -> str:
    """Normalize a tool-returned domain for comparison without rejecting the tool result."""
    try:
        return normalize_domain(value)
    except EvidenceContractError:
        return str(value).casefold().strip()


def failed_evidence() -> dict[str, Any]:
    return {"artifact_version": 1, "execution_status": "failed"}


def indeterminate_evidence() -> dict[str, Any]:
    return {"artifact_version": 1, "execution_status": "indeterminate"}


def validate_execution_case(value: Any, *,
                            allowed_kinds: Sequence[str] = ("query", "open")) -> dict[str, Any]:
    """Validate one instantiated execution case and add its ``task_kind``."""
    fields = {
        "artifact_version", "kind", "set_id", "split", "task_id",
        "template_family_digest", "task_digest", "prompt", "evaluator_task",
    }
    if (not isinstance(value, dict) or set(value) != fields
            or value.get("artifact_version") != 1
            or value.get("kind") != "twenty_execution_case"):
        raise EvidenceContractError("execution case header is invalid")
    for name in ("template_family_digest", "task_digest"):
        if not isinstance(value.get(name), str) or not _DIGEST.fullmatch(value[name]):
            raise EvidenceContractError(f"execution case {name} is invalid")
    for name in ("set_id", "task_id"):
        if not isinstance(value.get(name), str) or not _ID.fullmatch(value[name]):
            raise EvidenceContractError(f"execution case {name} is invalid")
    if value.get("split") not in {"development", "test"}:
        raise EvidenceContractError("execution case split is invalid")
    prompt = bounded_text(value.get("prompt"), "execution case prompt", 20_000)
    evaluator_task = value.get("evaluator_task")
    if not isinstance(evaluator_task, dict) or evaluator_task.get("artifact_version") != 1:
        raise EvidenceContractError("execution case evaluator task is invalid")
    task_kind = evaluator_task.get("kind")
    if task_kind not in set(allowed_kinds):
        raise EvidenceContractError(
            "runner supports " + " and ".join(allowed_kinds) + " only")
    expected = {"artifact_version", "kind"} if task_kind == "query" else {
        "artifact_version", "kind", "company_id"}
    if task_kind == "create":
        expected = {"artifact_version", "kind", "name", "domain", "require_open"}
    if set(evaluator_task) != expected:
        raise EvidenceContractError("execution case evaluator task has unexpected fields")
    return {**value, "prompt": prompt, "task_kind": task_kind}


def case_projection(case: dict[str, Any]) -> dict[str, Any]:
    """The part of an execution case a runner artifact may carry: identity and task kind."""
    return {
        **{key: case[key] for key in (
            "set_id", "split", "task_id", "task_digest", "template_family_digest")},
        "evaluator_task": {"kind": case["evaluator_task"]["kind"]},
    }


def disclosed_companies(results: Sequence[dict[str, Any]]) -> tuple[set[tuple[str, str]], bool]:
    """Return the company identities disclosed by successful tool results and whether any
    result reported a complete, untruncated scan."""
    identities: set[tuple[str, str]] = set()
    complete = False
    for result in results:
        rows = result.get("companies") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("name"), str):
                identities.add((row["name"].strip().casefold(), tool_domain(row.get("domain"))))
        completeness = result.get("completeness")
        if (isinstance(completeness, dict)
                and completeness.get("source_complete") is True
                and completeness.get("result_truncated") is False):
            complete = True
    return identities, complete


def disclosed_routes(results: Sequence[dict[str, Any]]) -> list[str]:
    """Every exact ``ui_path`` a successful business result disclosed, in order."""
    routes: list[str] = []
    for result in results:
        collection_route = (result.get("collection_ui_path")
                            if isinstance(result, dict) else None)
        if isinstance(collection_route, str) and collection_route not in routes:
            routes.append(collection_route)
        for collection in ("companies", "people", "opportunities"):
            rows = result.get(collection) if isinstance(result, dict) else None
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and isinstance(row.get("ui_path"), str):
                        if row["ui_path"] not in routes:
                            routes.append(row["ui_path"])
        company = result.get("company") if isinstance(result, dict) else None
        if isinstance(company, dict) and isinstance(company.get("ui_path"), str):
            if company["ui_path"] not in routes:
                routes.append(company["ui_path"])
    return routes


def parse_query_evidence(answer: Any) -> dict[str, Any]:
    """Parse a user-visible company answer without repairing or completing it.

    Browser-only runners have no business result against which to pre-check the answer.  They
    use this schema-only projection and leave correctness to the isolated state evaluator.
    Skill/API runners add the stricter same-run ToolResult check in :func:`query_evidence`.
    """
    answer = bounded_text(answer, "public answer", 20_000)
    try:
        value = json.loads(answer)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise EvidenceContractError("query answer is not one strict JSON object") from exc
    fields = {"artifact_version", "execution_status", "complete", "companies"}
    if (not isinstance(value, dict) or set(value) != fields
            or value.get("artifact_version") != 1
            or value.get("execution_status") != "completed"
            or not isinstance(value.get("complete"), bool)
            or not isinstance(value.get("companies"), list)
            or len(value["companies"]) > 1_000):
        raise EvidenceContractError("query answer does not satisfy the evidence schema")
    companies = []
    for row in value["companies"]:
        if not isinstance(row, dict) or set(row) != {"name", "domain"}:
            raise EvidenceContractError("query answer company has unexpected fields")
        companies.append({
            "name": bounded_text(row.get("name"), "company name", 300),
            "domain": normalize_domain(row.get("domain")),
        })
    identities = [(row["name"].casefold(), row["domain"]) for row in companies]
    if len(identities) != len(set(identities)):
        raise EvidenceContractError("query answer contains duplicate companies")
    return {**value, "companies": companies}


def query_evidence(answer: Any, business_results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Require a parsed answer to restate the same-run business results exactly."""
    value = parse_query_evidence(answer)
    identities = [(row["name"].casefold(), row["domain"]) for row in value["companies"]]
    if not business_results:
        raise EvidenceContractError("query answer has no business result to be checked against")
    disclosed, disclosed_complete = disclosed_companies(business_results)
    if set(identities) != disclosed:
        raise EvidenceContractError("query answer does not restate the business results")
    if value["complete"] is not disclosed_complete:
        raise EvidenceContractError("query answer completeness contradicts the business results")
    return value


def browser_open_evidence(*, path: Any, visible_projection: Any) -> dict[str, Any]:
    """Project only facts a browser-only host can observe.

    The runner deliberately does not receive the hidden target row.  It therefore preserves
    the final visible page projection for the isolated evaluator, which checks the target and
    discards the text when it projects the public trial row.
    """
    path = bounded_text(path, "final page path", 1_000)
    if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        raise EvidenceContractError("final page path is not canonical")
    visible = bounded_text(
        visible_projection, "visible page projection", MAX_VISIBLE_BYTES)
    return {
        "artifact_version": 1,
        "execution_status": "completed",
        "page": {"path": path, "visible_projection": visible},
    }


def create_evidence(*, business_results: Sequence[dict[str, Any]],
                    mutation_dispatch_count: int, approvals_requested: int,
                    approvals_approved: int, approvals_denied: int, require_open: bool,
                    path: Any = "", visible_projection: Any = "",
                    permitted_routes: Sequence[str] = ()) -> dict[str, Any]:
    """Project one acknowledged, same-actor mutation and optional rendered-page proof."""
    if (approvals_requested, approvals_approved, approvals_denied) != (1, 1, 0):
        raise EvidenceContractError("create evidence requires one approved fresh decision")
    if type(mutation_dispatch_count) is not int or mutation_dispatch_count != 1:
        raise EvidenceContractError("create evidence requires exactly one mutation dispatch")
    created = []
    for result in business_results:
        company = result.get("company") if isinstance(result, dict) else None
        if (isinstance(result, dict) and result.get("created") is True
                and result.get("readback_verified") is True
                and isinstance(company, dict)):
            created.append(company)
    if len(created) != 1:
        raise EvidenceContractError("create evidence requires one acknowledged readback")
    company = created[0]
    name = bounded_text(company.get("name"), "created company name", 300)
    domain = normalize_domain(company.get("domain"))
    route = bounded_text(company.get("ui_path"), "created company route", 1_000)
    result: dict[str, Any] = {
        "artifact_version": 1,
        "execution_status": "completed",
        "write_receipt": {
            "dispatch_count": 1,
            "acknowledged": True,
            "same_actor_readback": True,
        },
    }
    if require_open:
        selected_path = bounded_text(path, "final page path", 1_000)
        visible = bounded_text(
            visible_projection, "visible page projection", MAX_VISIBLE_BYTES)
        if selected_path != route or route not in set(permitted_routes):
            raise EvidenceContractError("created company route was not opened with a permit")
        folded = visible.casefold()
        if name.casefold() not in folded or (domain and domain not in folded):
            raise EvidenceContractError("created company facts are not visible")
        result["page"] = {"path": route, "name": name, "domain": domain}
    return result


def route_candidates(results: Sequence[dict[str, Any]], path: str) -> list[dict[str, str]]:
    candidates = []
    for result in results:
        rows = result.get("companies") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("ui_path") == path:
                candidates.append({
                    "name": bounded_text(row.get("name"), "company name", 300),
                    "domain": normalize_domain(row.get("domain")),
                })
    unique = {(row["name"], row["domain"]): row for row in candidates}
    return list(unique.values())


def open_evidence(*, path: Any, visible_projection: Any,
                  business_results: Sequence[dict[str, Any]],
                  permitted_routes: Sequence[str]) -> dict[str, Any]:
    """Require route facts from a tool result, a host permit for that exact route, and the
    presence of the same facts in the rendered page."""
    path = bounded_text(path, "final page path", 1_000)
    if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        raise EvidenceContractError("final page path is not a canonical record path")
    visible = bounded_text(visible_projection, "visible page projection", MAX_VISIBLE_BYTES)
    if path not in set(permitted_routes):
        raise EvidenceContractError("final route was not authorized by the route Host")
    candidates = route_candidates(business_results, path)
    if len(candidates) != 1:
        raise EvidenceContractError(
            "final route does not identify one company disclosed by a business result")
    company = candidates[0]
    folded = visible.casefold()
    if company["name"].casefold() not in folded:
        raise EvidenceContractError("opened company name is not visible")
    if company["domain"] and company["domain"] not in folded:
        raise EvidenceContractError("opened company domain is not visible")
    return {
        "artifact_version": 1,
        "execution_status": "completed",
        "page": {"path": path, **company},
    }


_VISIBLE_PROJECTION_SCRIPT = r"""() => {
  const visible = element => {
    const style = getComputedStyle(element)
    const rect = element.getBoundingClientRect()
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0
  }
  const pieces = [document.body?.innerText || '']
  for (const element of document.querySelectorAll('input, textarea, select, a, [aria-label], [title]')) {
    if (!visible(element)) continue
    for (const value of [element.value, element.textContent, element.getAttribute('aria-label'), element.getAttribute('title'), element.getAttribute('href')]) {
      if (typeof value === 'string' && value.trim()) pieces.push(value.trim())
    }
  }
  return pieces.join('\n')
}"""


def visible_page_projection(page: Any) -> str:
    """Sample the text a user can currently see in a trusted Playwright page."""
    value = page.evaluate(_VISIBLE_PROJECTION_SCRIPT)
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_VISIBLE_BYTES:
        raise EvidenceContractError("visible page projection is invalid or oversized")
    return value
