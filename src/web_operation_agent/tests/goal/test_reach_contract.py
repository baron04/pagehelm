"""The bounded page loop: the model steps, the server decides arrival, nothing is entered."""
import json
from types import SimpleNamespace
import unittest

from web_operation_agent.goal.reach import EndReach, page_text, reach
from web_operation_agent.modeling.provider import ProviderError
from web_operation_agent.schemas import ActionReceipt, ElementRef, PageObservation


def page(epoch=1, *, content="", extra=(), path="/list", state="ready", read=None):
    elements = [ElementRef("e1", "textbox", "请输入名称", "input", epoch,
                           {"page_control": "query", "field_label": "名称", "query_field": "name"}),
                ElementRef("e2", "button", "搜索", "button", epoch, {"page_control": "query"}),
                ElementRef("e3", "button", "编辑", "button", epoch),
                ElementRef("e4", "button", "更多", "button", epoch, {"revealable": "true"}),
                ElementRef("e5", "textbox", "备注", "input", epoch, {"field_label": "备注"}),
                *extra]
    return PageObservation(schema_version=1, epoch=epoch, url="https://app.test" + path, title="列表",
                           landmarks=[], elements=elements, content=content or f"page {epoch}",
                           render_state=state, page_read=read)


class Chat:
    def __init__(self, steps):
        self.steps, self.asked = list(steps), []

    def complete_cancellable(self, messages, tools, run_id, **kwargs):
        self.asked.append(messages[1]["content"])
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return {"tool_calls": [{"id": "s", "function": {"name": "page_step", "arguments": json.dumps(
            {"why": "test", **step}, ensure_ascii=False)}}]}


class Turn:
    """An executor and turn state that answer every dispatch with a fresh page."""

    def __init__(self, steps, *, policy=None, changes=True):
        self.calls, self.events = [], []
        self.page_reach_policy = policy
        self.changes = changes
        self.s = SimpleNamespace(
            terminal_reason="", obs=page(), step=0, chat=Chat(steps), run_id="r", cancelled=lambda: False,
            budget=SimpleNamespace(remaining=lambda: 30.0),
            ledger=SimpleNamespace(append=lambda event, **kw: self.events.append((event, kw))))

    def _dispatch(self, s, action, args, **kwargs):
        self.calls.append((action, {k: v for k, v in args.items() if k != "epoch"}, kwargs))
        epoch = s.obs.epoch + 1
        s.obs = page(epoch, content=f"page {epoch}" if self.changes and action != "wait_for" else s.obs.content)
        return ActionReceipt(1, "c" + str(epoch), action, {}, True, observation=s.obs)

    def actions(self):
        return [(a, args) for a, args, _ in self.calls if a != "wait_for"]


class Admit:
    def __init__(self, refusal=""):
        self.refusal, self.asked = refusal, []

    def decide(self, observation, element):
        self.asked.append(element.name)
        return self.refusal


class PageText(unittest.TestCase):
    def test_a_long_stretch_without_controls_keeps_only_its_ends(self):
        menu = [f"menu {i}" for i in range(200)]
        obs = page(content="\n".join(["[0]<a href=/>home />", *menu, "[3]<button>编辑 />", "row", "[4]<button>更多 />"]),
                   read={"path": "/list", "query_digest": "0" * 64, "query_fields": {}, "revision": 1,
                         "page": 1, "page_size": 10, "rows": 3, "total": 3})
        text = page_text(obs)
        self.assertIn("path: /list", text)
        self.assertIn("list: page 1, 10 per page, 3 rows shown, 3 in all", text)
        self.assertIn("the list's search fields: [1] 名称", text)
        obs.elements[0].attrs["value"] = "苏州备件仓"
        self.assertIn('the list\'s search fields: [1] 名称 = "苏州备件仓"', page_text(obs))
        self.assertIn("menu 0", text)
        self.assertIn("menu 199", text)        # the lines nearest the next control stay
        self.assertNotIn("menu 100", text)
        self.assertIn("lines of text with no control left out", text)
        self.assertIn("[3]<button>编辑 />\nrow\n[4]<button>更多 />", text)

    def test_session_parameters_are_redacted_and_a_page_without_text_lists_its_controls(self):
        obs = page(content="open /x?token=abcdef0123456789abcdef&id=2")
        self.assertNotIn("abcdef0123456789abcdef", page_text(obs))
        bare = page()
        bare.content = ""
        self.assertIn('[3] button "编辑"', page_text(bare))


class Reach(unittest.TestCase):
    def test_arrival_is_the_servers_check_and_ends_the_loop_without_another_question(self):
        turn = Turn([{"action": "click", "element": 3}, {"action": "click", "element": 3}], policy=Admit())
        result = reach(turn, turn.s, goal="open the editor", steps=5,
                       arrived=lambda obs: obs.epoch >= 2)
        self.assertTrue(result.arrived)
        self.assertEqual(result.steps, 1)
        self.assertEqual(turn.actions(), [("click", {"ref": "e3"})])
        self.assertEqual(len(turn.s.chat.asked), 1)
        self.assertTrue(turn.calls[0][2].get("admitted"))

    def test_a_page_already_there_asks_nothing(self):
        turn = Turn([], policy=Admit())
        self.assertTrue(reach(turn, turn.s, goal="g", steps=3, arrived=lambda obs: True).arrived)
        self.assertEqual((turn.calls, turn.s.chat.asked), ([], []))

    def test_no_policy_or_a_refusing_policy_presses_nothing_and_says_so_to_the_model(self):
        for policy in (None, Admit("that control submits the open form")):
            with self.subTest(policy=policy):
                turn = Turn([{"action": "click", "element": 3}, {"action": "stop"}], policy=policy)
                result = reach(turn, turn.s, goal="g", steps=5, arrived=lambda obs: False)
                self.assertFalse(result.arrived)
                self.assertEqual(turn.actions(), [])
                self.assertIn("refused", turn.s.chat.asked[1])

    def test_only_a_declared_name_goes_into_a_lists_own_search_field(self):
        turn = Turn([{"action": "type", "element": 1, "name": "别的名字"},
                     {"action": "type", "element": 5, "name": "苏州备件仓"},
                     {"action": "type", "element": 1, "name": "苏州备件仓"},
                     {"action": "stop"}], policy=Admit())
        result = reach(turn, turn.s, goal="g", steps=6, arrived=lambda obs: False, names=("苏州备件仓",))
        self.assertEqual(turn.actions(), [("fill", {"ref": "e1", "value": "苏州备件仓"})])
        self.assertIn("NAMES\n- 苏州备件仓", turn.s.chat.asked[0])
        # A name that went in is said to be in, with what searching takes; never "nothing new".
        self.assertIn("typed, it is in the field now", turn.s.chat.asked[3])
        self.assertNotIn("typed, it is in the field now; the list is searched when its search button "
                         "is pressed; the page shows nothing new", turn.s.chat.asked[3])
        self.assertEqual(turn.s.chat.asked[2].count("refused: only a name from NAMES"), 2)
        self.assertEqual(result.why, "test")

    def test_a_collapsed_menu_is_opened_as_a_menu_and_never_offered_to_press(self):
        pressed = []
        turn = Turn([{"action": "click", "element": 4}, {"action": "stop"}])
        reach(turn, turn.s, goal="g", steps=3, arrived=lambda obs: False,
              press=lambda element: pressed.append(element) or "pressed")
        self.assertEqual(turn.actions(), [("reveal", {"ref": "e4"})])
        self.assertEqual(pressed, [])

    def test_a_callers_press_decides_its_own_admission_and_may_end_the_search(self):
        def press(element):
            raise EndReach("a form opened that is not the one asked for")
        turn = Turn([{"action": "click", "element": 3}, {"action": "click", "element": 3}])
        result = reach(turn, turn.s, goal="g", steps=5, arrived=lambda obs: False, press=press)
        self.assertEqual((result.arrived, result.steps), (False, 1))
        self.assertIn("not the one asked for", result.why)

    def test_the_same_step_on_an_unchanged_page_is_refused_once_then_ends_the_loop(self):
        turn = Turn([{"action": "scroll", "direction": "down"}] * 5, policy=Admit(), changes=False)
        result = reach(turn, turn.s, goal="g", steps=6, arrived=lambda obs: False)
        self.assertEqual(result.why, "the same step was repeated without effect")
        self.assertEqual(len(turn.actions()), 1)

    def test_the_step_limit_a_missing_element_and_an_unusable_reply_all_end_honestly(self):
        turn = Turn([{"action": "click", "element": 99}, {"action": "scroll"}, {"action": "scroll", "direction": "up"}],
                    policy=Admit())
        result = reach(turn, turn.s, goal="g", steps=3, arrived=lambda obs: False)
        self.assertEqual((result.arrived, result.steps, result.why), (False, 3, "the step limit was reached"))
        self.assertIn("there is no such element", turn.s.chat.asked[1])
        for bad in (ProviderError("down"), {"action": "submit"}):
            turn = Turn([bad], policy=Admit())
            result = reach(turn, turn.s, goal="g", steps=3, arrived=lambda obs: False)
            self.assertEqual((result.arrived, turn.actions()), (False, []))
            self.assertEqual(result.why, "no usable step was chosen")

    def test_a_turn_that_ended_asks_and_presses_nothing_more(self):
        turn = Turn([{"action": "click", "element": 3}], policy=Admit())
        turn.s.terminal_reason = "user_cancelled"
        result = reach(turn, turn.s, goal="g", steps=3, arrived=lambda obs: True)
        self.assertEqual((result.arrived, turn.calls, turn.s.chat.asked), (False, [], []))


if __name__ == "__main__":
    unittest.main()
