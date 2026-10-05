"""Bounded, literal page evidence; no business identity or completeness is inferred."""
import re
from dataclasses import dataclass, replace

from .evidence import Origin, PageEvidence
from ..schemas import PageObservation

MAX_PAGE_ITEMS = 32
MAX_PAGE_EXCERPT_CHARS = 300
MAX_PAGE_QUERY_CHARS = 120
MAX_PAGE_OFFSET = 10000

SYSTEM = """For factual questions about what the page displays, use observe with
answer_evidence=true and cite the resulting page_observation or undetermined references.
Quote the observed text/value; do not infer that a label means an empty field. Explanations
without claims about current data can use ordinary prose. Prefer a suitable typed read for
business facts, and do not repeat page observations after a sufficient API read. A control
with an explicit selected state or tab-control text supports UI information, not business
data or source knowledge. Such evidence has saw=ui_state. If the answer evidence
window omits the needed content, use answer_query for a literal visible word or label,
or answer_offset for the next window. These search only this redacted observation;
no match does not prove a business record or field is absent."""

OBSERVE_TOOL = {"type": "function", "function": {
    "name": "observe", "description": (
        "Re-read the current page without changing it. Set answer_evidence=true when "
        "answering from page text or controls: returns bounded current page references "
        "for page_observation/undetermined conclusions. A label alone is not an empty value. "
        "Use answer_query to find a literal word/label and neighbouring text, or "
        "answer_offset to read another evidence window. No match is not proof of absence. "
        "Do not observe again for questions already answered by a typed API read."),
    "parameters": {"type": "object", "additionalProperties": False,
                   "properties": {
                       "answer_evidence": {"type": "boolean"},
                       "answer_query": {"type": "string", "maxLength": MAX_PAGE_QUERY_CHARS},
                       "answer_offset": {"type": "integer", "minimum": 0,
                                         "maximum": MAX_PAGE_OFFSET}}}}}


def observation_options(args: dict) -> tuple[bool, str, int]:
    """Server-only options; none are sent to the browser Runtime."""
    enabled, query, offset = (args.get("answer_evidence", False),
                              args.get("answer_query", ""), args.get("answer_offset", 0))
    if (set(args) - {"answer_evidence", "answer_query", "answer_offset"}
            or not isinstance(enabled, bool)
            or not isinstance(query, str) or len(query) > MAX_PAGE_QUERY_CHARS
            or re.search(r"[\x00-\x1f\x7f]", query)
            or isinstance(offset, bool) or not isinstance(offset, int)
            or not 0 <= offset <= MAX_PAGE_OFFSET
            or (not enabled and ("answer_query" in args or "answer_offset" in args))):
        raise ValueError("observe accepts boolean answer_evidence; answer_query and "
                         "answer_offset require answer_evidence=true, a bounded literal "
                         "query and a nonnegative integer offset")
    return enabled, query.strip(), offset


@dataclass(frozen=True)
class PageEvidenceWindow:
    items: tuple[PageEvidence, ...]
    matched_items: int
    next_offset: int | None


def _has_page_text(line: str) -> bool:
    """Do not spend the excerpt window on the controller's layout and empty markup.

    This only selects lines. The evidence retains the original line, without inventing
    field associations or rewriting application text as parsed business values.
    """
    text = line.strip()
    if (not text or text in ("[Start of page]", "[End of page]")
            or text.startswith(("Current Page: [", "Page info: ",
                                "Interactive elements from top layer of the current page"))):
        return False
    candidate = re.sub(r"^\[\d+\]", "", text)
    candidate = re.sub(r"</?[A-Za-z][^>]*>", "", candidate)
    candidate = re.sub(r"\s*/>$", "", candidate)
    return bool(candidate.strip())


def _text_kind(line: str) -> str:
    """Keep controller-serialized tab/selection facts distinct from document content.

    Links/buttons can carry business names, so their labels are not universally
    UI-only. Only an explicit tab or selected-state attribute qualifies here.
    """
    match = re.match(r"\s*\[\d+\]<([A-Za-z][\w-]*)([^>]*)>", line)
    if match is not None:
        attributes = match.group(2)
        if re.search(r"\baria-selected\s*=", attributes, re.I) or re.search(
                r"\brole\s*=\s*(?:\"tab\"|'tab'|tab(?=[\s/>]))", attributes, re.I):
            return "ui_state"
    return "text"


def _candidates(observation: PageObservation, *, observed_at: str, query: str = ""):
    """The caller supplies a redacted observation from an actual successful receipt."""
    origin = Origin(kind="page", reference=observation.content_digest,
                    observed_at=observed_at,
                    scope=f"observed page only, epoch {observation.epoch}; not an API or complete collection")
    for element in observation.elements:
        if element.role not in ("textbox", "combobox", "checkbox", "radio", "spinbutton"):
            continue
        # Only an explicit value in the observation is a value. Missing attrs and a
        # placeholder do not establish that a control is empty. Passwords are never values.
        value = element.attrs.get("value")
        readable = (isinstance(value, str)
                    and element.attrs.get("type") != "password"
                    and value != "[redacted]" and len(value) <= MAX_PAGE_EXCERPT_CHARS)
        yield PageEvidence(
            evidence_id="page#1", origin=origin, page_route=observation.url,
            saw="value" if readable else "control_only",
            detail=f"{element.ref}: {element.name or element.role}"[:MAX_PAGE_EXCERPT_CHARS],
            value=value if readable else None)
    # A selected tab is a current UI fact, not a business result. It can be
    # reported without showing raw markup or trusting a model's paraphrase.
    for element in observation.elements:
        if element.attrs.get("aria-selected") != "true":
            continue
        yield PageEvidence(
            evidence_id="page#1", origin=origin,
            page_route=observation.url, saw="ui_state",
            detail=f"{element.name or element.role} ({element.role})"[:MAX_PAGE_EXCERPT_CHARS],
            value="selected")
    # Exact bounded excerpts, not inferred key/value pairs or extracted table records.
    # The model can cite a line, but cannot rename it into an API field or aggregate it.
    for line_number, line in enumerate(observation.content.splitlines(), 1):
        if not _has_page_text(line):
            continue
        # A serialized paragraph/table can be one long line. A literal focus may
        # expose another original span of that line, never bytes outside the observation.
        if query and len(line) > MAX_PAGE_EXCERPT_CHARS:
            last_end = 0
            found = False
            for match in re.finditer(re.escape(query), line, re.I):
                if match.end() <= last_end:
                    continue
                found = True
                start = max(0, match.start() - 80)
                end = min(len(line), start + MAX_PAGE_EXCERPT_CHARS)
                yield PageEvidence(evidence_id="page#1", origin=origin,
                    page_route=observation.url, saw=_text_kind(line),
                    detail=f"page text line {line_number}, chars {start}-{end} (bounded excerpt)",
                    value=line[start:end])
                last_end = end
            if found:
                continue
        yield PageEvidence(
            evidence_id="page#1", origin=origin, page_route=observation.url,
            saw=_text_kind(line), detail=f"page text line {line_number} (bounded excerpt)",
            value=line[:MAX_PAGE_EXCERPT_CHARS])


def collect_window(observation: PageObservation, *, observed_at: str,
                   query: str = "", offset: int = 0) -> PageEvidenceWindow:
    """Select literal excerpts without changing their provenance or inferring facts.

    Search visible values/text within this observation, not hidden attributes or
    omitted observation bytes. Adjacent text preserves a label/value split across separate lines;
    it does not certify that the two belong to the same field or record.
    """
    _, query, offset = observation_options({"answer_evidence": True,
                                           "answer_query": query, "answer_offset": offset})
    candidates = tuple(_candidates(observation, observed_at=observed_at, query=query))
    selected = set()
    if query:
        needle = query.casefold()
        for index, item in enumerate(candidates):
            text = item.value or ""
            if item.saw != "text":
                text = item.detail + "\n" + text
            if needle not in text.casefold():
                continue
            selected.add(index)
            if item.saw == "text":
                for neighbour in (index - 1, index + 1):
                    if (0 <= neighbour < len(candidates)
                            and candidates[neighbour].saw == "text"):
                        selected.add(neighbour)
    else:
        selected = set(range(len(candidates)))
    indices = sorted(selected)
    window = indices[offset:offset + MAX_PAGE_ITEMS]
    items = tuple(replace(candidates[index], evidence_id=f"page#{number}")
                  for number, index in enumerate(window, 1))
    following = offset + len(items)
    return PageEvidenceWindow(items, len(indices),
                              following if following < len(indices) else None)


def collect(observation: PageObservation, *, observed_at: str) -> tuple[PageEvidence, ...]:
    """The default first window, retained for existing callers."""
    return collect_window(observation, observed_at=observed_at).items
