"""Fixed zero-model policies for P7.4a apparatus qualification.

A scripted policy stands where a model would, behind the provider gateway, so the product, the
browser, the ledger and the evaluator all run for real while the decisions are fixed. Its
program is written per qualification sample from the sample's own specification and seeded
ids; it never reads the evaluator's input or the expected truth.

WOA policies are short programs of steps. Each step resolves the values a model would read
off the request -- Skill candidates, evidence references, returned routes, clarification
choice ids -- from the request body itself, so the product's real numbering and routing are
exercised rather than guessed. A step whose value cannot be resolved ends the turn with
``finish unable``, which the evaluator then scores; a script never invents a reference.

PageAgent policies choose DOM element indices from PageAgent's own browser-state text, which
is the only thing a browser-only agent has.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

Step = Callable[["RequestView"], tuple[str, dict[str, Any]]]


def _json_after(text: str, label: str) -> Any:
    """The first JSON value that follows ``label`` in ``text``; None when absent."""
    start = text.rfind(label)
    if start < 0:
        return None
    decoder = json.JSONDecoder()
    index = start + len(label)
    while index < len(text) and text[index] not in "[{":
        index += 1
    try:
        value, _ = decoder.raw_decode(text, index)
    except json.JSONDecodeError:
        return None
    return value


class RequestView:
    """Read-only accessors over one chat-completion request the product sent."""

    def __init__(self, body: dict[str, Any]):
        self.body = body
        self.messages = body.get("messages") or []

    @property
    def task(self) -> str:
        for message in self.messages:
            if message.get("role") == "user":
                match = re.search(r"TASK: (.*)", message.get("content") or "")
                if match:
                    return match.group(1).strip()
        return ""

    @property
    def tool_names(self) -> set[str]:
        return {tool["function"]["name"] for tool in self.body.get("tools") or []}

    @property
    def finish_takes_answer(self) -> bool:
        for tool in self.body.get("tools") or []:
            if tool["function"]["name"] == "finish":
                return "answer" in tool["function"]["parameters"].get("properties", {})
        return False

    def candidates(self) -> list[dict[str, Any]]:
        for message in self.messages:
            if message.get("role") == "user":
                value = _json_after(message.get("content") or "", "BUSINESS SKILL CANDIDATES")
                if isinstance(value, list):
                    return value
        return []

    def _tool_texts(self) -> list[str]:
        return [message.get("content") or "" for message in self.messages
                if message.get("role") == "tool"]

    def evidence(self) -> list[dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for text in self._tool_texts():
            value = _json_after(text, "ANSWER EVIDENCE")
            if isinstance(value, dict):
                for row in value.get("evidence") or []:
                    rows[row["ref"]] = row
        return list(rows.values())

    def last_result(self) -> dict[str, Any] | None:
        for text in reversed(self._tool_texts()):
            value = _json_after(text, "BUSINESS TOOL RESULT")
            if isinstance(value, dict):
                return value
        return None

    def clarification_choices(self) -> list[dict[str, Any]]:
        for text in reversed(self._tool_texts()):
            value = _json_after(text, "CLARIFICATION CANDIDATES")
            if isinstance(value, list):
                return value
        return []

    def last_tool_text(self) -> str:
        texts = self._tool_texts()
        return texts[-1] if texts else ""


def _call(name: str, args: dict[str, Any], index: int) -> dict[str, Any]:
    return {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": f"p74-call-{index}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}}]}


class Unresolved(Exception):
    """A step could not find what it needs in the request; the turn ends unable."""


# -- WOA steps -----------------------------------------------------------------------------

def load_skill_with(tool: str, *, writes: bool = False) -> Step:
    def step(view: RequestView):
        options = [item for item in view.candidates() if tool in item.get("tools", [])
                   and (writes or item.get("read_only"))]
        if not options:
            options = [item for item in view.candidates() if tool in item.get("tools", [])]
        if not options:
            raise Unresolved(f"no Skill candidate offers {tool}")
        options.sort(key=lambda item: (len(item.get("tools", [])), item["skill_id"]))
        return "load_skill", {"skill_id": options[0]["skill_id"]}
    return step


def call(tool: str, args: dict[str, Any]) -> Step:
    return lambda _view: (tool, dict(args))


def _latest(view: RequestView, kind: str) -> dict[str, Any]:
    rows = [row for row in view.evidence() if row.get("kind") == kind]
    if not rows:
        raise Unresolved(f"no {kind} in the request")
    return rows[-1]


def compute(operation: str, *, field: str = "", comparison: str = "", threshold: str = "",
            choices: Sequence[str] = (), over: str = "QueryEvidence") -> Step:
    def step(view: RequestView):
        args: dict[str, Any] = {"operation": operation,
                                "dataset_ref": _latest(view, over)["ref"]}
        if field:
            args["field"] = field
        if comparison:
            args["comparison"] = comparison
        if threshold:
            args["threshold"] = threshold
        if choices:
            args["choices"] = list(choices)
        return "compute", args
    return step


def _answer(view: RequestView, conclusions: list[dict[str, Any]], *, outcome: str = "answered",
            coverage: str = "complete", presentation: str | None = None):
    if not view.finish_takes_answer:
        raise Unresolved("finish does not accept an answer in this request")
    answer: dict[str, Any] = {"version": 1, "coverage": coverage, "conclusions": conclusions}
    if presentation:
        answer["presentation"] = presentation
    return "finish", {"outcome": outcome, "answer": answer}


def finish_fields(record_value: str, fields: Sequence[str]) -> Step:
    """Cite ``fields`` of the record whose ``name`` field equals ``record_value``."""
    def step(view: RequestView):
        rows = view.evidence()
        ids = {row["record_id"] for row in rows if row.get("kind") == "RecordEvidence"
               and row.get("field_name") == "name" and row.get("value") == record_value}
        if len(ids) != 1:
            raise Unresolved("the named record is not uniquely in evidence")
        record_id = ids.pop()
        refs = [row["ref"] for row in rows if row.get("kind") == "RecordEvidence"
                and row.get("record_id") == record_id and row.get("field_name") in fields]
        if len(refs) != len(fields):
            raise Unresolved("a requested field is not in evidence")
        return _answer(view, [{"kind": "field_value", "evidence_ref": ref} for ref in refs])
    return step


def finish_aggregate() -> Step:
    return lambda view: _answer(view, [{"kind": "aggregate",
                                        "evidence_ref": _latest(view, "ComputationEvidence")["ref"]}])


def finish_selection(fields: Sequence[str], *, over: str = "SelectionEvidence") -> Step:
    return lambda view: _answer(view, [{"kind": "selection", "fields": list(fields),
                                        "evidence_ref": _latest(view, over)["ref"]}],
                                presentation="list")


def finish_not_found() -> Step:
    return lambda view: _answer(view, [{"kind": "not_found",
                                        "evidence_ref": _latest(view, "QueryEvidence")["ref"]}])


def goto_route_of(name: str, *, key: str = "name") -> Step:
    """Open the exact ``ui_path`` the latest business result returned for ``name``."""
    def step(view: RequestView):
        result = view.last_result() or {}
        for value in result.values():
            if isinstance(value, list):
                for row in value:
                    if isinstance(row, dict) and row.get(key) == name and row.get("ui_path"):
                        return "goto", {"url": row["ui_path"]}
        raise Unresolved("no returned route for the named record")
    return step


def goto_collection() -> Step:
    def step(view: RequestView):
        route = (view.last_result() or {}).get("collection_ui_path")
        if not route:
            raise Unresolved("no returned collection route")
        return "goto", {"url": route}
    return step


def ask_choices(question: str) -> Step:
    def step(view: RequestView):
        choices = view.clarification_choices()
        if len(choices) < 2:
            raise Unresolved("no clarification candidates to offer")
        return "ask_user", {"question": question, "response_kind": "single_choice",
                            "choice_ids": [item["choice_id"] for item in choices]}
    return step


def finish_acted(why: str) -> Step:
    """Report a completed action; ``why`` is the prose a model would write when finish has
    no answer schema in this request."""
    def step(view: RequestView):
        if view.finish_takes_answer:
            return "finish", {"outcome": "acted"}
        return "finish", {"done": True, "why": why}
    return step


def finish_acted_with(kind: str, *, over: str) -> Step:
    """Report a completed navigation together with one evidence-backed conclusion."""
    def step(view: RequestView):
        if not view.finish_takes_answer:
            raise Unresolved("finish does not accept an answer in this request")
        return "finish", {"outcome": "acted", "answer": {
            "version": 1, "coverage": "complete",
            "conclusions": [{"kind": kind, "evidence_ref": _latest(view, over)["ref"]}]}}
    return step


def finish_refused(why: str) -> Step:
    def step(view: RequestView):
        if view.finish_takes_answer:
            return "finish", {"outcome": "unable"}
        return "finish", {"done": False, "why": why}
    return step


@dataclass(frozen=True)
class WoaProgram:
    """Per-turn step lists, keyed by the exact user message of each turn."""
    turns: dict[str, tuple[Step, ...]]

    def policy(self) -> Callable[[dict[str, Any], int], dict[str, Any]]:
        progress: dict[str, int] = {}

        def respond(body: dict[str, Any], index: int) -> dict[str, Any]:
            view = RequestView(body)
            # A continuation turn may carry earlier context around its own message, so the
            # longest scripted message contained in the task selects the turn.
            matches = sorted((message for message in self.turns if message in view.task),
                             key=len, reverse=True)
            key = matches[0] if matches else view.task
            steps = self.turns.get(key)
            position = progress.get(key, 0)
            progress[key] = position + 1
            try:
                if steps is None or position >= len(steps):
                    raise Unresolved("the script has no further step for this turn")
                name, args = steps[position](view)
            except Unresolved:
                name, args = (("finish", {"outcome": "unable"}) if view.finish_takes_answer
                              else ("finish", {"done": False, "why": "unresolved"}))
            return _call(name, args, index)

        return respond


# -- PageAgent ------------------------------------------------------------------------------

def agent_output(action: dict[str, Any], index: int) -> dict[str, Any]:
    arguments = {"evaluation_previous_goal": "Scripted.", "memory": "Scripted policy.",
                 "next_goal": "Scripted action.", "action": action}
    return {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": f"p74-agent-{index}", "type": "function",
            "function": {"name": "AgentOutput", "arguments": json.dumps(arguments)}}]}}]}


def pageagent_text(body: dict[str, Any]) -> str:
    return "\n".join(message.get("content") or "" for message in body.get("messages") or []
                     if isinstance(message.get("content"), str))


def element_index(body: dict[str, Any], pattern: str) -> int | None:
    """The index of the last element line in PageAgent's browser state matching ``pattern``."""
    text = pageagent_text(body)
    state = text[text.rfind("Interactive elements"):] if "Interactive elements" in text else text
    matches = [int(match.group(1)) for match in re.finditer(
        r"\[(\d+)\]<[^\n]*?" + pattern, state)]
    return matches[-1] if matches else None


@dataclass(frozen=True)
class PageAgentProgram:
    """A fixed sequence of PageAgent actions; element indices are resolved per request.

    A step whose element is not in the page's state yet waits and asks again rather than ending
    the run: the application renders the menu a click opened after that click returns, and the
    state captured in between holds neither the old menu nor the new one. The waiting is bounded
    for the whole program, so a program cannot wait its way to an element that is never there,
    and each wait is an ordinary step of the run -- counted, billed and inside the same budgets.
    """
    steps: tuple[Callable[[dict[str, Any]], dict[str, Any]], ...]
    #: How many times, over the whole program, a step may wait for the page to render.
    waits: int = 3
    #: The seconds each of those waits asks the engine for.
    wait_s: int = 1

    def policy(self) -> Callable[[dict[str, Any], int], dict[str, Any]]:
        progress = {"position": 0, "waited": 0}

        def respond(body: dict[str, Any], index: int) -> dict[str, Any]:
            position = progress["position"]
            if position >= len(self.steps):
                return agent_output({"done": {"text": "Script exhausted.", "success": False}},
                                    index)
            try:
                action = self.steps[position](body)
            except Unresolved:
                if progress["waited"] >= self.waits:
                    return agent_output(
                        {"done": {"text": "Unresolved element.", "success": False}}, index)
                progress["waited"] += 1
                return agent_output({"wait": {"seconds": self.wait_s}}, index)
            progress["position"] += 1
            return agent_output(action, index)
        return respond


def click_text(pattern: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def step(body: dict[str, Any]) -> dict[str, Any]:
        found = element_index(body, pattern)
        if found is None:
            raise Unresolved(f"no element matching {pattern}")
        return {"click_element_by_index": {"index": found}}
    return step


def done_text(text: str, *, success: bool = True) -> Callable[[dict[str, Any]], dict[str, Any]]:
    return lambda _body: {"done": {"text": text, "success": success}}


def wait_seconds(seconds: int) -> Callable[[dict[str, Any]], dict[str, Any]]:
    return lambda _body: {"wait": {"seconds": seconds}}
