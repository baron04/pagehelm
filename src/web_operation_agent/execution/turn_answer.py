"""Evidence-backed answer state and assembly for one agent turn."""
from __future__ import annotations

import hashlib
import json
import re

from ..answer import answer_protocol
from ..answer.goal_scope import AnswerGoal
from ..business.runtime import BusinessToolResult, BusinessToolRuntime
from ..answer.evidence import EvidenceUnknown, QueryEvidence, TurnEvidence
from ..state.ledger import SessionLedger
from ..privacy import redact_sensitive_assignments


class AnswerState:
    """Expose selected ``TurnAnswer`` state on its owning session without copying it."""

    def __init__(self, name: str, default=None):
        self.name = name
        self.default = default
        self.fallback = f"_turn_answer_{name}"

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        answers = instance.__dict__.get("_answers")
        return (getattr(answers, self.name) if answers is not None
                else instance.__dict__.get(self.fallback, self.default))

    def __set__(self, instance, value) -> None:
        answers = instance.__dict__.get("_answers")
        if answers is None:
            instance.__dict__[self.fallback] = value
        else:
            setattr(answers, self.name, value)


class TurnAnswer:
    """Own what this turn may state, cite, clarify, and project as its answer."""

    def __init__(self, *, ledger: SessionLedger, run_id: str, prompt: str,
                 business_tools: BusinessToolRuntime | None,
                 plain_business_answer_min_page_actions: int | None):
        self.ledger = ledger
        self.run_id = run_id
        self.prompt = prompt
        self.business_tools = business_tools
        self.plain_business_answer_min_page_actions = plain_business_answer_min_page_actions
        self.latest_business_tool_result: BusinessToolResult | None = None
        self.evidence = TurnEvidence()
        # Source citations are stable within a turn: asking the same question twice must
        # not mint two authorities for the same exact bytes.  The source lookup, not an
        # application adapter, owns these IDs.
        self.source_evidence_ids: dict[str, str] = {}
        self.source_evidence_count = 0
        self.source_history_evidence_ids: dict[str, str] = {}
        self.source_history_evidence_count = 0
        self.skill_evidence_count = 0
        self.evidence_feedback = ""
        self.capability_ref = ""
        self.capability_withheld = False
        self.clarification_choices: dict[str, dict] = {}
        self.refused_reads: list[str] = []
        self.refused_detail = ""
        self.clarification_feedback = ""
        self.answer_repairs = 0
        # An attempted answer beside an observed action cannot disappear on repair.
        self.action_answer_required = False
        self.answer_mode = False
        self.page_answer_digest = ""
        self.page_answer_window_digest = ""
        self.action_receipt: dict | None = None
        self.latest_business_query_page_actions: int | None = None
        self.answer_goals: tuple[AnswerGoal, ...] = ()
        self.first_business_read_seen = False
        # The host, not an adapter-provided text label, binds each query reference to
        # the exact registered tool call that produced it.
        self.query_tools: dict[str, str] = {}
        self.generated_goal_metric_refs: set[str] = set()

    def collect_evidence(self, tool_result, *, step: int,
                         read_tool: str = "") -> tuple[str, ...]:
        """Capture a typed result's evidence and current clarification candidates."""
        items = getattr(tool_result, "evidence", ()) or ()
        self.evidence_feedback = ""
        self.clarification_choices = {}
        self.clarification_feedback = ""
        if items:
            self.answer_mode = True
        recorded, refused = [], []
        try:
            recorded = list(self.evidence.capture(items, namespace=f"call{step}"))
        except (ValueError, TypeError, EvidenceUnknown) as exc:
            refused.append(str(exc)[:200])
        if recorded:
            if read_tool:
                self.query_tools.update(
                    {ref: read_tool for ref in recorded
                     if isinstance(self.evidence.get(ref), QueryEvidence)})
            self.evidence_feedback = answer_protocol.evidence_index(self.evidence, tuple(recorded))
        elif refused:
            self.evidence_feedback = "ANSWER EVIDENCE UNAVAILABLE: narrow or repeat the read."
        if recorded or refused:
            self.ledger.append("turn_evidence", step=step,
                               recorded=recorded, refused=refused,
                               held=len(self.evidence))
        cards = []
        for option in getattr(tool_result, "selection_options", ()) or ():
            label = redact_sensitive_assignments(option.label).strip()
            record_ref = redact_sensitive_assignments(option.record_ref).strip()
            seed = json.dumps(
                {"run_id": self.run_id, "step": step, "record_ref": record_ref},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            choice_id = "choice-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
            choice = {"choice_id": choice_id, "label": label,
                      "record_ref": record_ref}
            self.clarification_choices[choice_id] = choice
            cards.append({"choice_id": choice_id, "label": label})
        if cards:
            self.clarification_feedback = (
                "CLARIFICATION CANDIDATES (current application facts; selecting one grants "
                "no route or write authority):\n" + json.dumps(
                    cards, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            self.ledger.append(
                "clarification_candidates_ready", step=step, count=len(cards),
                choice_ids=[item["choice_id"] for item in cards],
                label_digests=[hashlib.sha256(item["label"].encode("utf-8")).hexdigest()
                               for item in cards])
        return tuple(recorded)

    def with_labels(self, verified: dict | None) -> dict | None:
        """Attach only the application labels used by this verified answer."""
        tables = getattr(self.business_tools, "answer_labels", None) or {}
        if verified is None or not tables:
            return verified
        used: set[str] = set()
        for item in verified.get("conclusions", ()):
            used.update(value for key in ("field_name", "scope")
                        if isinstance(value := item.get(key), str))
            used.update(item.get("fields") or ())
            for condition in (item.get("condition"), item.get("filter")):
                used.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(condition or "")))
        labels = {language: {key: value for key, value in table.items()
                             if key in used or key.partition("=")[0] in used}
                  for language, table in tables.items()}
        labels = {language: table for language, table in labels.items() if table}
        return {**verified, "labels": labels} if labels else verified

    def with_refused_reads(self, verified: dict | None) -> dict | None:
        """State what this account could not read and lower answer coverage."""
        if not verified or not self.refused_reads:
            return verified
        verified["coverage"] = ("undetermined"
                                if verified["coverage"] == "undetermined" else "partial")
        verified["lowered_because"] = tuple(verified.get("lowered_because") or ()) + tuple(
            f"{label or 'a read'}: refused by this account's authority"
            for label in self.refused_reads)
        verified["refused_reads"] = tuple(self.refused_reads)
        return verified

    def operation_label(self, tool: str) -> str:
        tables = getattr(self.business_tools, "answer_labels", None) or {}
        language = "zh" if re.search(r"[\u3400-\u9fff]", self.prompt) else "en"
        return str((tables.get(language) or {}).get(tool, ""))[:80]

    def render_action(self) -> str:
        if not self.answer_mode or self.action_receipt is None:
            return ""
        return ("COMPLETED ACTION RECEIPT (server-observed; not proof of other task goals):\n"
                + json.dumps(self.action_receipt, ensure_ascii=False)
                + '\nTo report this action, call finish with {"outcome":"acted"}. '
                "Do not turn a URL, record name or action claim into a field evidence reference. "
                "If the user also requested data, include validated evidence conclusions "
                "in the optional answer field of that same finish call.")

    def business_answer_is_projectable(self, page_actions: int) -> bool:
        result = self.latest_business_tool_result
        return bool(
            self.plain_business_answer_min_page_actions is not None
            and result is not None
            and result.query_answer is not None
            and self.latest_business_query_page_actions == page_actions
            and page_actions >= self.plain_business_answer_min_page_actions)
