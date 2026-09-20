"""One contract every model-facing implementation must satisfy.

`ServerAgentSession` reaches the provider through exactly one boundary. Before this module
existed, that boundary was the concrete `ChatModel` class plus two layers of `getattr`
capability probing, so a test double could satisfy the type check by subclassing the
concrete provider without running its constructor, inherit the real `complete_cancellable`,
and fail with `AttributeError` on the first model call. This module is the shared suite that
makes such an implementation fail as an implementation, not as a run.

The suite is deliberately not a `TestCase`: `run_all.py` discovers `test_*.py`, so mixing it
into a real case in the product tree and in `evals/woa` keeps one definition of the contract.
"""
from __future__ import annotations

from web_operation_agent.modeling.client import ProviderError, require_chat_port

MESSAGES = [{"role": "user", "content": "contract"}]


class ChatPortContract:
    """Mix into a `unittest.TestCase` and implement `make_chat`.

    `make_provider_error_chat` and `make_deadline_chat` may return `None` when the
    implementation does not own a transport that can produce that outcome. Returning `None`
    is recorded as an explicit non-applicability, never as a pass: `test_chat_port.py`
    separately asserts that the real `ChatModel` supplies both.
    """

    def make_chat(self):
        raise NotImplementedError

    def make_provider_error_chat(self):
        return None

    def make_deadline_chat(self):
        return None

    # -- shape ---------------------------------------------------------------------------
    def test_the_implementation_satisfies_the_chat_port(self):
        require_chat_port(self.make_chat(), role="contract subject")

    def test_a_plain_completion_returns_a_reply_object(self):
        reply = self.make_chat().complete(MESSAGES, None, "contract-plain")
        self.assertIsInstance(reply, dict)

    def test_a_cancellable_completion_with_a_live_run_returns_the_same_shape(self):
        reply = self.make_chat().complete_cancellable(
            MESSAGES, None, "contract-live", cancelled=lambda: False)
        self.assertIsInstance(reply, dict)

    # -- cancellation --------------------------------------------------------------------
    def test_cancellation_is_refused_rather_than_silently_ignored(self):
        """An already-cancelled turn must not receive a normal reply from any implementation."""
        with self.assertRaises(ProviderError) as raised:
            self.make_chat().complete_cancellable(
                MESSAGES, None, "contract-cancelled", cancelled=lambda: True)
        self.assertEqual(raised.exception.code, "provider_cancelled")
        self.assertFalse(raised.exception.retryable)

    def test_the_cancellation_hook_is_keyword_only_and_mandatory(self):
        """`complete_cancellable` without a hook is a programming error, not a plain call."""
        with self.assertRaises(TypeError):
            self.make_chat().complete_cancellable(MESSAGES, None, "contract-missing-hook")

    # -- failure surfaces ----------------------------------------------------------------
    def test_a_provider_failure_reaches_the_caller_as_a_provider_error(self):
        chat = self.make_provider_error_chat()
        if chat is None:
            self.skipTest("implementation owns no transport that can fail")
        with self.assertRaises(ProviderError):
            chat.complete_cancellable(
                MESSAGES, None, "contract-failure", cancelled=lambda: False)

    def test_a_deadline_is_reported_and_never_silently_retried(self):
        chat = self.make_deadline_chat()
        if chat is None:
            self.skipTest("implementation owns no socket whose deadline it can enforce")
        with self.assertRaises(ProviderError):
            chat.complete_cancellable(
                MESSAGES, None, "contract-deadline", cancelled=lambda: False, timeout_s=0.05)


class CancellableChatDouble:
    """Give a scripted double the cancellation half of `ChatPort`.

    Mix into a double that already implements `complete`. Delegating keeps a subclass that
    overrides only `complete` correct, and refusing a cancelled turn -- rather than answering
    it -- is the behaviour the contract above asserts of every implementation, so a double
    cannot pass a test the real provider would fail.
    """

    model = "scripted-double"

    def complete_cancellable(self, messages, tools, run_id, *, cancelled,
                             max_tokens=None, options=None, timeout_s=None):
        if not callable(cancelled):
            raise TypeError("cancelled must be callable")
        if cancelled():
            raise ProviderError("the user stopped this task", code="provider_cancelled",
                                retryable=False)
        return self.complete(messages, tools, run_id, max_tokens=max_tokens,
                             options=options, timeout_s=timeout_s)
