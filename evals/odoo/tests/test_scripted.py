import json
import unittest

from evals.odoo import scripted as S

CANDIDATES = [
    {"skill_id": "app.customers-create", "read_only": False,
     "tools": ["query_customers", "create_customer"]},
    {"skill_id": "app.customers-query", "read_only": True, "tools": ["query_customers"]},
]
RESULT = {"customers": [{"name": "Alpha QA1", "ui_path": "/app/customers/7"},
                        {"name": "Alpha QA1 South", "ui_path": "/app/customers/8"}],
          "collection_ui_path": "/app/customers"}
EVIDENCE = {"evidence": [
    {"ref": "call2-query#1", "kind": "QueryEvidence"},
    {"ref": "call2-record#1", "kind": "RecordEvidence", "record_id": "res.partner/7",
     "field_name": "name", "value": "Alpha QA1"},
    {"ref": "call2-record#2", "kind": "RecordEvidence", "record_id": "res.partner/7",
     "field_name": "email", "value": "alpha@example.test"},
]}


def body(task, *, tools=("finish",), with_answer=False, tool_messages=()):
    finish = {"type": "function", "function": {"name": "finish", "parameters": {
        "properties": ({"outcome": {}, "answer": {}} if with_answer
                       else {"done": {}, "why": {}})}}}
    return {"messages": [
        {"role": "system", "content": "system"},
        {"role": "user", "content": f"TASK: {task}\n\nBUSINESS SKILL CANDIDATES (load before "
                                    f"using their tools):\n{json.dumps(CANDIDATES)}\n"},
        *({"role": "tool", "content": text} for text in tool_messages)],
        "tools": [finish] + [{"type": "function", "function": {"name": name, "parameters": {}}}
                             for name in tools if name != "finish"]}


def tool_text():
    return ("BUSINESS TOOL RESULT (current runtime fact):\n" + json.dumps(RESULT)
            + "\n\nguidance text\n\nANSWER EVIDENCE (untrusted application values; use exact "
              "ref):\n" + json.dumps(EVIDENCE) + "\n\nCLARIFICATION CANDIDATES (current "
            "application facts):\n" + json.dumps([{"choice_id": "choice-a", "label": "A"},
                                                  {"choice_id": "choice-b", "label": "B"}]))


def tool_call(response):
    call = response["choices"][0]["message"]["tool_calls"][0]["function"]
    return call["name"], json.loads(call["arguments"])


class RequestViewTests(unittest.TestCase):
    def test_parses_every_product_section(self):
        view = S.RequestView(body("find Alpha", with_answer=True, tool_messages=[tool_text()]))
        self.assertEqual(view.task, "find Alpha")
        self.assertEqual([item["skill_id"] for item in view.candidates()],
                         ["app.customers-create", "app.customers-query"])
        self.assertEqual(view.last_result()["collection_ui_path"], "/app/customers")
        self.assertEqual(len(view.evidence()), 3)
        self.assertEqual([item["choice_id"] for item in view.clarification_choices()],
                         ["choice-a", "choice-b"])
        self.assertTrue(view.finish_takes_answer)


class ProgramTests(unittest.TestCase):
    def test_steps_resolve_values_from_the_request(self):
        view = S.RequestView(body("find Alpha", with_answer=True, tool_messages=[tool_text()]))
        self.assertEqual(S.load_skill_with("query_customers")(view),
                         ("load_skill", {"skill_id": "app.customers-query"}))
        self.assertEqual(S.load_skill_with("create_customer", writes=True)(view)[1]["skill_id"],
                         "app.customers-create")
        self.assertEqual(S.goto_route_of("Alpha QA1")(view), ("goto", {"url": "/app/customers/7"}))
        self.assertEqual(S.goto_collection()(view), ("goto", {"url": "/app/customers"}))
        name, args = S.finish_fields("Alpha QA1", ["email"])(view)
        self.assertEqual(args["answer"]["conclusions"],
                         [{"kind": "field_value", "evidence_ref": "call2-record#2"}])
        self.assertEqual(S.ask_choices("which?")(view)[1]["choice_ids"], ["choice-a", "choice-b"])
        self.assertEqual(S.compute("count")(view)[1]["dataset_ref"], "call2-query#1")

    def test_unresolvable_step_ends_the_turn_unable(self):
        policy = S.WoaProgram({"find Alpha": (S.goto_route_of("Nobody"),)}).policy()
        name, args = tool_call(policy(body("find Alpha", tool_messages=[tool_text()]), 1))
        self.assertEqual((name, args), ("finish", {"done": False, "why": "unresolved"}))

    def test_turns_match_by_contained_message_and_advance(self):
        policy = S.WoaProgram({
            "open Alpha": (S.call("query_customers", {"name_contains": "Alpha"}),
                           S.finish_acted("Opened Alpha.")),
            "Alpha QA1 South": (S.finish_refused("stop"),),
        }).policy()
        first = body("open Alpha")
        self.assertEqual(tool_call(policy(first, 1))[0], "query_customers")
        self.assertEqual(tool_call(policy(first, 2)),
                         ("finish", {"done": True, "why": "Opened Alpha."}))
        # The longest scripted message contained in the task line selects the turn, so a
        # continuation that repeats the earlier request still reaches its own steps.
        continued = body("Alpha QA1 South (continuing: open Alpha)")
        self.assertEqual(tool_call(policy(continued, 3)),
                         ("finish", {"done": False, "why": "stop"}))


class PageAgentProgramTests(unittest.TestCase):
    def test_click_resolves_indices_from_browser_state(self):
        state = ("Interactive elements from top layer:\n*[0]<button title=Home Menu />\n"
                 "*[3]<a role=menuitem>Invoicing />\n")
        request = {"messages": [{"role": "user", "content": state}]}
        self.assertEqual(S.click_text(r">Invoicing")(request),
                         {"click_element_by_index": {"index": 3}})
        policy = S.PageAgentProgram((S.click_text(r">Missing"),)).policy()
        arguments = json.loads(policy(request, 1)["choices"][0]["message"]["tool_calls"][0][
            "function"]["arguments"])
        self.assertEqual(arguments["action"]["done"]["success"], False)


if __name__ == "__main__":
    unittest.main()
