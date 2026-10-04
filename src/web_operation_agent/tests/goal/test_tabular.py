import unittest

from web_operation_agent.goal.data import DataRefused
from web_operation_agent.goal.tabular import table_rows


class TabularTests(unittest.TestCase):
    def test_duplicates_are_rows_and_handles_bind_contents_and_query(self):
        rows = [{"Name":"Cash", "Amount":10}, {"Name":"Cash", "Amount":10},
                {"Name":"Cash", "Amount":20}]
        first = table_rows(rows,binding="actor-a:query-a",max_rows=4)
        self.assertEqual(3,len({row.reference for row in first}))
        self.assertEqual(first,table_rows(rows,binding="actor-a:query-a",max_rows=4))
        for binding in ("actor-b:query-a", "actor-a:query-b", "actor-a:credential-2"):
            self.assertTrue(set(row.reference for row in first).isdisjoint(
                row.reference for row in table_rows(rows,binding=binding,max_rows=4)))
        changed = table_rows([{**rows[0],"Amount":11}],binding="actor-a:query-a",max_rows=4)
        self.assertNotEqual(first[0].reference,changed[0].reference)
        rows[0]["Amount"] = 999
        self.assertEqual(10,first[0].values["Amount"])
        self.assertEqual((),table_rows([],binding="a",max_rows=1))

    def test_invalid_or_incomplete_shapes_do_not_return_partial_tables(self):
        for rows in (None,{},[{}]*3,[{},None],[{1:"invalid key"}],
                     [{"Amount":float("nan")}],[{"Amount":float("inf")}],[{"x":object()}]):
            with self.subTest(rows=rows),self.assertRaises(DataRefused) as error:
                table_rows(rows,binding="a",max_rows=2)
            self.assertTrue(error.exception.read_failed)
        for args in ({"binding":"","max_rows":1},{"binding":"a","max_rows":True}):
            with self.assertRaises(ValueError):
                table_rows([],**args)
