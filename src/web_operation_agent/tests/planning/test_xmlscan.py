import unittest

from web_operation_agent.planning.xmlscan import XMLScanError, parse


class Parsing(unittest.TestCase):
    def test_tags_attrs_and_line_numbers_are_recorded(self):
        root = parse("<a>\n<b x=\"1\">\n<c/>\n</b>\n</a>")
        a = root.first("a")
        self.assertEqual(a.start_line, 1)
        b = a.first("b")
        self.assertEqual(b.attrs, {"x": "1"})
        self.assertEqual(b.start_line, 2)
        self.assertEqual(b.first("c").start_line, 3)
        self.assertEqual(b.end_line, 4)

    def test_find_all_walks_every_descendant_in_document_order(self):
        root = parse("<a><b><c/></b><c/></a>")
        found = root.find_all("c")
        self.assertEqual(len(found), 2)

    def test_kids_returns_only_direct_children(self):
        root = parse("<a><b/><c><b/></c></a>")
        a = root.first("a")
        self.assertEqual([node.tag for node in a.kids("b")], ["b"])

    def test_a_namespace_prefix_is_stripped(self):
        root = parse('<a xmlns:x="urn:example"><x:b/></a>')
        self.assertIsNotNone(root.first("a").first("b"))

    def test_malformed_xml_raises_xmlscanerror(self):
        with self.assertRaises(XMLScanError):
            parse("<a><b></a>")


if __name__ == "__main__":
    unittest.main()
