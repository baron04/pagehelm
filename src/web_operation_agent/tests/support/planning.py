"""Shared planning fixtures and model doubles; contains no test cases."""
from pathlib import Path

from web_operation_agent.planning.contract import _segment_from_template
from web_operation_agent.tests.support.chat_port_contract import CancellableChatDouble
from web_operation_agent.tests.support.route_fixture import FIXTURE_ROUTE_PORT
from web_operation_agent.tests.support.source_fixture import fixture_source_map


TASK = "The tray in bay four needs a new cultivar sprouted into it."
# The fixtures are relation triples rather than one application's descriptors, so the
# segment comes from the synthetic route grammar.
SEGMENT = _segment_from_template(FIXTURE_ROUTE_PORT.contract_route_template)


def _map(tmp: Path):
    return fixture_source_map(tmp)


class _StubModel(CancellableChatDouble):
    """Return queued answers and record the prompts and token budgets received."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.budgets = []

        class _Meter:
            def report(self):
                return {"provider_calls": 0}
        self.meter = _Meter()

    def complete(self, messages, tools, run_id, max_tokens=None, options=None,
                 timeout_s=None):
        self.calls.append([dict(message) for message in messages])
        self.max_tokens = max_tokens
        self.budgets.append(max_tokens)
        reply = self.replies.pop(0)
        if isinstance(reply, dict):
            return {"content": "", "finish_reason": "stop", "reasoning": "", **reply}
        return {"content": reply, "finish_reason": "stop", "reasoning": ""}
