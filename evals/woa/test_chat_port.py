"""The WOA evaluation double must satisfy the same model boundary as the product.

At `61603fe` this suite would have caught the regression that made 22 of 36 WOA cases report
`apparatus_invalid`: `ScriptedWoaChat` subclassed the concrete `ChatModel` to pass the
runner's `isinstance` check, skipped its constructor, and inherited a `complete_cancellable`
that raised `AttributeError` on the first model call.
"""
import unittest

from web_operation_agent.model import require_chat_port
from web_operation_agent.tests.chat_port_contract import MESSAGES, ChatPortContract

from evals.woa.scripted import ScriptedWoaChat


def _replies():
    return [("finish", {"done": True, "why": "contract"})]


class ScriptedWoaChatContract(ChatPortContract, unittest.TestCase):
    def make_chat(self):
        return ScriptedWoaChat(_replies())

    def make_provider_error_chat(self):
        return ScriptedWoaChat(_replies(), failures={1: "provider_transport"})


class TheDoubleDoesNotBorrowTheConcreteProvider(unittest.TestCase):
    def test_it_is_not_a_chatmodel_subclass(self):
        from web_operation_agent.model import ChatModel

        self.assertNotIsInstance(ScriptedWoaChat(_replies()), ChatModel)

    def test_the_runner_boundary_accepts_it_by_contract(self):
        require_chat_port(ScriptedWoaChat(_replies()), role="scripted chat")

    def test_accounting_stays_content_free_and_reports_a_model(self):
        chat = ScriptedWoaChat(_replies())
        chat.complete(MESSAGES, None, "run")
        self.assertTrue(chat.model.strip() == chat.model and chat.model)
        self.assertEqual(chat.meter.run_report("run")["model_calls"], 1)


if __name__ == "__main__":
    unittest.main()
