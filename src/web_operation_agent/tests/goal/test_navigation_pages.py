"""Direct actor-index pages carry navigation, without fabricating record entities."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from web_operation_agent.goal.data import EntitySchema, PageSchema, Schema
from web_operation_agent.goal.index import ApplicationIndex, IndexConvention, IndexEntity, IndexPage
from web_operation_agent.goal.narrow import KnownReference, neighbourhood, summaries
from web_operation_agent.goal.resolve.indexing import index_from_schema
from web_operation_agent.goal.resolve.resolver import Resolver
from web_operation_agent.goal.ports import PagePlan
from web_operation_agent.goal.spec import GoalSpec, scope_from_json


class NavigationPageContractTests(unittest.TestCase):
    def test_destination_identity_plan_is_opt_in_and_requires_an_expected_path(self):
        self.assertFalse(PagePlan("/contacts", "Contacts").requires_page_route)
        for value in (1, "yes", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PagePlan("/help", "Help", "/help", requires_page_route=value)
        with self.assertRaises(ValueError):
            PagePlan("/help", "Help", requires_page_route=True)

    def setUp(self):
        self.schema = Schema(entities=(EntitySchema("contact", "Contacts"),), pages=(
            PageSchema("contacts", "contact", "Contacts"),
            PageSchema("help", "", "Help", opens_records=False)))
        self.index = index_from_schema(self.schema, breadcrumbs={"help": "Workspace / Help"})
        self.port = SimpleNamespace(page_address=Mock(side_effect=lambda key, **kw: "/" + key),
                                    find=Mock(), count=Mock(), rows=Mock())

    def resolve(self, value):
        return Resolver(self.port, self.index, self.schema).resolve(
            GoalSpec.from_json({"deliverables": [value]}))

    def test_navigation_only_page_has_no_record_vocabulary(self):
        self.assertEqual("", self.index.page("help").entity)
        self.assertIn("help: Workspace / Help | navigation only (no records)", self.index.render())
        self.assertIn("contacts: Contacts | entity contact | views: list", self.index.render())
        for metadata in ({"views": ("list",)}, {"filters": ("Mine",)},
                         {"groups": ("Name",)}, {"scope": ("Customers",)},
                         {"operations": ("create",)}, {"sections": ("Edit",)},
                         {"filters_as": "contacts"}):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                IndexPage("help", "Help", "Workspace / Help", **metadata)

    def test_staged_page_choice_survives_an_empty_entity_neighbourhood(self):
        parts, *_ = scope_from_json({"parts": [{"kind": "page", "page": "help"}]})
        self.assertEqual({"kind": "page", "entity": "", "about": "", "page": "help"}, parts[0])
        text = summaries(self.index, "Open Help", ())
        self.assertIn("NAVIGATION PAGES", text)
        self.assertIn("help: Workspace / Help", text)
        focused = neighbourhood(self.index, (), page_keys=(parts[0]["page"],))
        self.assertEqual((), focused.entities)
        self.assertEqual((self.index.page("help"),), focused.pages)
        self.assertEqual((), neighbourhood(self.index, (), page_keys=("invented",)).pages)

    def test_navigation_resolves_without_business_reads_and_data_pages_still_open(self):
        for key in ("help", "contacts"):
            result = self.resolve({"kind": "page", "page": {"key": key}})
            self.assertFalse(result.deliverables[0].problem)
            self.assertEqual("/" + key, result.deliverables[0].page.url)
            self.assertEqual(key == "help", result.deliverables[0].page.requires_page_route)
            self.assertEqual((), result.reads)
        self.port.find.assert_not_called()
        self.port.count.assert_not_called()
        self.port.rows.assert_not_called()

    def test_entityless_page_refuses_data_filters_records_and_forms_without_reads(self):
        declarations = (
            {"kind": "fact", "fields": ["Email"]},
            {"kind": "aggregate", "metric": {"op": "count"}},
            {"kind": "list"}, {"kind": "handoff", "action": "create"},
            {"kind": "handoff", "field_values": [{"field": "Name", "value": "Test"}]},
            {"kind": "page", "entity": "contact"}, {"kind": "page", "record": "Ada"},
            {"kind": "page", "page": {"filters_on": ["Mine"]}},
            {"kind": "page", "page": {"view": "list"}},
            {"kind": "page", "page": {"search": "Ada"}},
            {"kind": "page", "rules": ["unverified-rule"]},
        )
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                value = {**declaration, "page": {"key": "help", **declaration.get("page", {})}}
                result = self.resolve(value)
                settled = result.deliverables[0]
                self.assertIsNone(settled.page)
                self.assertIsNone(settled.aggregate)
                self.assertFalse(settled.facts)
                self.assertEqual("refused", settled.read_failure)
                self.assertEqual(declaration["kind"], settled.read_operation)
                self.assertIn("navigation only", settled.problem)
                self.assertEqual((), result.reads)
        self.port.find.assert_not_called()
        self.port.count.assert_not_called()
        self.port.rows.assert_not_called()

    def test_an_index_page_still_needs_a_current_application_page_address(self):
        self.index = replace(self.index, pages=self.index.pages + (IndexPage("invented", "Other", "Other"),))
        result = self.resolve({"kind": "page", "page": {"key": "invented"}})
        self.assertIsNone(result.deliverables[0].page)
        self.port.page_address.assert_not_called()

    def test_relevant_navigation_pages_are_reserved_before_large_entity_directories(self):
        for size in (100, 500, 1000):
            with self.subTest(size=size):
                entities = tuple(IndexEntity(f"module-{n}/record", f"Record type {n}",
                    aliases=("Unrelated actor-visible alias " + str(n),) * 5) for n in range(size))
                pages = tuple(IndexPage(f"nav-{n}", f"Settings {n}", f"Workspace / Settings {n}")
                              for n in range(100)) + (IndexPage("help", "Workspace help", "Workspace / Help"),)
                index = ApplicationIndex(entities=entities, pages=pages,
                    conventions=(IndexConvention(("book",), "book means indexed record type 0",
                                                  entity=entities[0].key),))
                text = summaries(index, "Open Workspace help and R123 in the book",
                    (KnownReference("R123", entities[0].key, "Scoped record R123"),))
                self.assertIn("help: Workspace / Help", text)
                self.assertLess(text.index("help: Workspace / Help"), text.index("LIKELY ENTITIES"))
                self.assertIn("R123 -> module-0/record: Scoped record R123", text)
                self.assertIn("book means indexed record type 0", text)
                self.assertLessEqual(len(text), 12_000)
                self.assertLessEqual(text.count("nav-"), 19)

    def test_navigation_only_catalog_is_bounded_and_needs_no_entity(self):
        pages = tuple(IndexPage(f"nav-{n}", f"Settings {n}", f"Workspace / Settings {n}")
                      for n in range(1000)) + (IndexPage("help", "Workspace help", "Workspace / Help"),)
        text = summaries(ApplicationIndex(pages=pages), "Open Workspace help", ())
        self.assertIn("help: Workspace / Help", text)
        self.assertIn("20 of 1001 shown", text)
        self.assertLessEqual(len(text), 12_000)
        self.assertLessEqual(text.count("nav-"), 19)


if __name__ == "__main__":
    unittest.main()
