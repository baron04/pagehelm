"""Stable public entry point for the product's versioned schemas.

Implementations are grouped by trust boundary; callers import the shared contract from this
package rather than depending on a monolithic module.
"""

from .common import SCHEMA_VERSIONS, SchemaError, canonical_json, digest, parse_event_id
from .terminal import (INDETERMINATE_TERMINAL_REASONS, INVALIDATING_TERMINAL_REASONS,
                       SCORABLE_TERMINAL_REASONS, TERMINAL_REASONS,
                       assert_terminal_reason_contract, classify_terminal)
from .planning import (RELATION_KINDS, ContractStep, EvidenceRef, MutationSignature,
                       OperationContract, SourceMap, SourceRelation, empty_contract)
from .browser import (BROWSER_ACTIONS, INTERNAL_BROWSER_ACTIONS,
                      INTRINSICALLY_READ_ONLY_ACTIONS,
                      MAX_OBSERVATION_CONTENT_CHARS, MAX_OBSERVATION_ELEMENTS,
                      MAX_SCROLL_PAGES, POTENTIALLY_WRITING_ACTIONS, RECEIPT_ERRORS,
                      ActionCommand, ActionReceipt, ElementRef, PageObservation)
from .protocol import (ACTION_RESULT_ERRORS, ACTION_RESULT_STATUSES, APPROVAL_KINDS,
                       TASK_EVENT_KINDS, ActionEnvelope, ActionResultEnvelope,
                       ApprovalDecision, ApprovalRequest, CancellationRequest, ClientHello,
                       ObservationEnvelope, ReconciliationRequest, TaskEvent)
from .control import ControlPlan, ControlTicket

__all__ = [name for name in globals() if not name.startswith("_")]
