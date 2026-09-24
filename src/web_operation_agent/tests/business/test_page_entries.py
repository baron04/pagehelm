"""Page candidates are application-neutral and only qualified routes are authority."""
from __future__ import annotations

import unittest

from web_operation_agent.business.page_entries import (
    FIND_PAGE_ENTRIES_SCHEMA, PageEntry, PageEntryCatalog, PageEntryTools, PagePart)
from web_operation_agent.business.runtime import (
    BusinessToolContractError, BusinessToolRequestError)


class _Port:
    def __init__(self):
        self.complete = True

    def catalog(self, *, context=None):
        return PageEntryCatalog(
            binding_digest="1" * 64,
            entries=(
                PageEntry(reference="inventory", label="Inventory",
                          search_text="stock products 库存", route="/inventory",
                          views=("list",)),
                PageEntry(reference="inventory-context", label="Inventory adjustment",
                          search_text="stock products 库存",
                          unavailable_reason="requires_record_context"),
                PageEntry(reference="dashboards", label="Dashboards",
                          search_text="reports of sales", route="/dashboards"),
            ),
            complete=self.complete,
            scope="pages visible to this actor",
        )

    def parts(self, entry, *, context=None):
        return (PagePart(reference="notes", label="Internal Notes", kind="tab"),
                PagePart(reference="terms", label="Payment Terms", kind="field"),
                PagePart(reference="payment", label="Online payment", kind="field"))


class PageEntryTests(unittest.TestCase):
    def test_only_qualified_entry_returns_navigation_authority(self):
        result = PageEntryTools(_Port(), application_id="fixture").execute({
            "query": "inventory"})
        self.assertEqual(result.content["returned_count"], 2)
        self.assertTrue(result.content["candidates"][0]["openable"])
        self.assertEqual(result.content["candidates"][0]["ui_path"], "/inventory")
        self.assertFalse(result.content["candidates"][1]["openable"])
        self.assertNotIn("ui_path", result.content["candidates"][1])
        self.assertEqual(result.content["candidates"][1]["unavailable_reason"],
                         "requires_record_context")
        self.assertEqual(result.allowed_routes, ("/inventory",))
        self.assertNotIn("inventory-context", result.render())

    def test_schema_cannot_supply_native_route_or_method(self):
        props = FIND_PAGE_ENTRIES_SCHEMA["function"]["parameters"]["properties"]
        self.assertEqual(set(props), {"query", "detail_query", "limit"})
        with self.assertRaises(BusinessToolRequestError):
            PageEntryTools(_Port(), application_id="fixture").execute({
                "query": "inventory", "route": "/admin"})

    def test_a_shared_stopword_does_not_fabricate_a_page_match(self):
        result = PageEntryTools(_Port(), application_id="fixture").execute({
            "query": "Units of Measure Categories"})
        self.assertEqual(result.content["candidates"], [])

    def test_detail_query_returns_bounded_hints_not_selectors(self):
        result = PageEntryTools(_Port(), application_id="fixture").execute({
            "query": "inventory", "detail_query": "Internal Notes"})
        candidate = result.content["candidates"][0]
        self.assertTrue(candidate["part_metadata_checked"])
        self.assertEqual([(part["kind"], part["label"])
                          for part in candidate["part_matches"]],
                         [("tab", "Internal Notes")])
        self.assertFalse(result.content["candidates"][1]["part_metadata_checked"])
        self.assertTrue(result.content["part_matches_are_hints"])
        self.assertEqual(result.allowed_routes, ("/inventory",))
        self.assertNotIn("notes", result.render())
        terms = PageEntryTools(_Port(), application_id="fixture").execute({
            "query": "inventory", "detail_query": "Payment Terms"})
        self.assertEqual([part["label"] for part in
                          terms.content["candidates"][0]["part_matches"]],
                         ["Payment Terms"])

    def test_equal_names_prioritize_an_openable_entry(self):
        class DuplicatePort:
            def catalog(self, *, context=None):
                return PageEntryCatalog(
                    binding_digest="2" * 64,
                    entries=(
                        PageEntry(reference="a", label="Product Variants",
                                  search_text="products",
                                  unavailable_reason="not_reviewed_for_navigation"),
                        PageEntry(reference="b", label="Product Variants",
                                  search_text="products", route="/variants"),
                    ), complete=True, scope="actor pages")

            def parts(self, entry, *, context=None):
                return ()

        result = PageEntryTools(DuplicatePort(), application_id="fixture").execute({
            "query": "Product Variants"})
        self.assertEqual(result.content["candidates"][0]["ui_path"], "/variants")

    def test_invalid_catalog_and_route_are_refused(self):
        with self.assertRaises(ValueError):
            PageEntry(reference="bad", label="Bad", search_text="",
                      route="//other.invalid/")
        with self.assertRaises(ValueError):
            PageEntry(reference="bad", label="Bad", search_text="",
                      unavailable_reason="requires_record_context", route="/bad")

        class MalformedPort:
            def catalog(self, *, context=None):
                return {"entries": []}

            def parts(self, entry, *, context=None):
                return ()

        with self.assertRaises(BusinessToolContractError):
            PageEntryTools(MalformedPort(), application_id="fixture").execute({
                "query": "inventory"})


if __name__ == "__main__":
    unittest.main()
