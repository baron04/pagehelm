"""The application's own Chinese names come from its zh_CN catalogues, and only from them."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from web_operation_agent_odoo.translations import odoo_translations, parse_po

_CATALOGUE = '''
#. module: account
#: model_terms:ir.ui.view,arch_db:account.view_move_form
msgid "Source Document"
msgstr "源单据"

#: model:ir.model.fields,help:account.field_account_move__invoice_origin
msgid ""
"The document(s) that generated the invoice, used for traceability "
"and reporting."
msgstr ""
"生成发票的单据，用于追溯。"

#: model:ir.model.fields.selection,name:account.selection__account_move__state__posted
msgid "Posted"
msgstr "已过账"

msgid "Untranslated"
msgstr ""

msgid "Invoice %s"
msgstr "发票 %s"
'''


class TranslationsTests(unittest.TestCase):
    def test_a_catalogue_is_read_with_its_multi_line_strings(self):
        pairs = dict(parse_po(_CATALOGUE))
        self.assertEqual(pairs["Source Document"], "源单据")
        self.assertEqual(pairs["The document(s) that generated the invoice, used for "
                               "traceability and reporting."], "生成发票的单据，用于追溯。")
        self.assertNotIn("Untranslated", pairs)

    def test_only_zh_cn_catalogues_are_read_and_only_names_are_kept(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            (base / "addons/account/i18n").mkdir(parents=True)
            (base / "addons/account/i18n/zh_CN.po").write_text(_CATALOGUE, encoding="utf-8")
            # Another language and a catalogue outside i18n are not the application's
            # Chinese names and are never read.
            (base / "addons/account/i18n/fr.po").write_text(
                'msgid "Posted"\nmsgstr "Comptabilisé"\n', encoding="utf-8")
            (base / "addons/account/models").mkdir(parents=True)
            (base / "addons/account/models/zh_CN.po").write_text(
                'msgid "Draft"\nmsgstr "伪造"\n', encoding="utf-8")
            names = odoo_translations(base, release="18.0-test")
        self.assertEqual(names.of("Source Document"), ("源单据",))
        self.assertEqual(names.back("已过账"), ("Posted",))
        self.assertEqual(names.of("Draft"), ())
        # A help sentence and a formatted message are not names.
        self.assertEqual(names.of("Invoice %s"), ())
        self.assertNotIn("生成发票的单据，用于追溯。", names.english)

    def test_no_catalogues_means_no_names(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(odoo_translations(root, release="18.0-test").labels, {})


if __name__ == "__main__":
    unittest.main()
