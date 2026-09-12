"""Bounded, literal page evidence; no business identity or completeness is inferred."""
from .evidence import Origin, PageEvidence
from .schemas import PageObservation

MAX_PAGE_ITEMS = 32
MAX_PAGE_EXCERPT_CHARS = 300

OBSERVE_TOOL = {"type": "function", "function": {
    "name": "observe", "description": (
        "Re-read the current page without changing it. Set answer_evidence=true when "
        "answering from page text or controls: returns bounded current page references "
        "for page_observation/undetermined conclusions. A label alone is not an empty value. "
        "Do not observe again for questions already answered by a typed API read."),
    "parameters": {"type": "object", "additionalProperties": False,
                   "properties": {"answer_evidence": {"type": "boolean"}}}}}


def collect(observation: PageObservation, *, observed_at: str) -> tuple[PageEvidence, ...]:
    """The caller supplies a redacted observation from an actual successful receipt."""
    origin = Origin(kind="page", reference=observation.content_digest,
                    observed_at=observed_at,
                    scope=f"observed page only, epoch {observation.epoch}; not an API or complete collection")
    items = []
    for element in observation.elements:
        if element.role not in ("textbox", "combobox", "checkbox", "radio", "spinbutton"):
            continue
        # Only an explicit value in the observation is a value. Missing attrs and a
        # placeholder do not establish that a control is empty. Passwords are never values.
        value = element.attrs.get("value")
        readable = (isinstance(value, str)
                    and element.attrs.get("type") != "password"
                    and value != "[redacted]" and len(value) <= MAX_PAGE_EXCERPT_CHARS)
        items.append(PageEvidence(
            evidence_id=f"page#{len(items) + 1}", origin=origin, page_route=observation.url,
            saw="value" if readable else "control_only",
            detail=f"{element.ref}: {element.name or element.role}"[:MAX_PAGE_EXCERPT_CHARS],
            value=value if readable else None))
        if len(items) == MAX_PAGE_ITEMS:
            return tuple(items)
    # Exact bounded excerpts, not inferred key/value pairs or extracted table records.
    # The model can cite a line, but cannot rename it into an API field or aggregate it.
    for line_number, line in enumerate(observation.content.splitlines(), 1):
        if not line.strip():
            continue
        items.append(PageEvidence(
            evidence_id=f"page#{len(items) + 1}", origin=origin, page_route=observation.url,
            saw="text", detail=f"page text line {line_number} (bounded excerpt)",
            value=line[:MAX_PAGE_EXCERPT_CHARS]))
        if len(items) == MAX_PAGE_ITEMS:
            break
    return tuple(items)
