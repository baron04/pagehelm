"""The form inventory is read from the UI source, is stable, and its sample is fixed by its seed."""
from pathlib import Path
import tempfile
import unittest

from evals.yudao.write_contract import inventory
from evals.yudao.write_contract.fullstack import ScriptedModel, call, index_of, reply

FORM = """<template><el-dialog><el-form :model="formData"/></el-dialog></template>
<script setup>const formType = ref(''); const open = async (type, id) => { formType.value = type; if (id) { x = await Api.get(id) } }</script>"""
PAGE = """<template><el-button @click="openForm('update', row.id)">编辑</el-button><el-button @click="openForm('create')">新增</el-button><AForm ref="f"/></template>
<script setup>import AForm from './AForm.vue'; const openForm = (type, id) => { f.value.open(type, id) }</script>"""


def tree(names):
    root = Path(tempfile.mkdtemp())
    for name in names:
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "index.vue").write_text(PAGE)
        (folder / "AForm.vue").write_text(FORM)
    return root


class InventoryTests(unittest.TestCase):
    def test_scan_finds_editable_forms_sorted_and_the_sample_is_stable(self):
        root = tree([f"mod/page{i:02d}" for i in range(40)])
        found = inventory.scan(root)
        self.assertEqual(len(found), 40)
        self.assertEqual([row["route"] for row in found], sorted(row["route"] for row in found))
        self.assertTrue(found[0]["can_create"] and found[0]["edit_label"] == "编辑")
        again = inventory.sample(inventory.scan(root))
        self.assertEqual(again, inventory.sample(found))
        self.assertEqual(len(again), inventory.SAMPLE)

    def test_an_imported_group_editor_is_not_the_list_edit_target(self):
        source = PAGE.replace('<AForm ref="f"/>', '<AForm ref="f"/><BForm ref="group"/>').replace(
            "import AForm from './AForm.vue'", "import BForm from './BForm.vue'; import AForm from './AForm.vue'")
        bindings = inventory.editor_bindings(source)
        self.assertEqual(bindings["AForm"]["state"], "confirmed")
        self.assertEqual(bindings["BForm"]["state"], "different_editor")
        unknown = inventory.editor_bindings(source.replace('f.value.open(type, id)', 'customOpen(type, id)'))
        self.assertEqual(unknown["AForm"]["state"], "unknown")
        for inert in ("// f.value.open(type, id)\n customOpen(type, id)", "'f.value.open(type, id)'; customOpen(type, id)"):
            self.assertEqual(inventory.editor_bindings(source.replace('f.value.open(type, id)', inert))["AForm"]["state"], "unknown")

    def test_comment_braces_and_quotes_cannot_hide_a_second_editor_call(self):
        source = PAGE.replace('<AForm ref="f"/>', '<AForm ref="f"/><BForm ref="group"/>').replace(
            "import AForm from './AForm.vue'", "import BForm from './BForm.vue'; import AForm from './AForm.vue'")
        for comment in ("// }", "// '", "/* } */", "/* ' */"):
            changed = source.replace('f.value.open(type, id)',
                'f.value.open(type, id); ' + comment + '\n group.value.open(type, id)')
            bindings = inventory.editor_bindings(changed)
            self.assertEqual(bindings["AForm"]["state"], "unknown")
            self.assertEqual(bindings["BForm"]["state"], "unknown")

    def test_a_dialog_that_never_loads_a_record_is_not_an_editable_form(self):
        root = tree(["a/send"])
        (root / "a/send/AForm.vue").write_text("<el-form/><script>const formType = 1</script>")
        self.assertEqual(inventory.scan(root), [])

    def test_the_menu_decides_what_is_deployed_and_where(self):
        root = tree(["crm/contact", "crm/gone"])
        menus = [{"id": 1, "parentId": 0, "path": "/crm", "component": ""},
                 {"id": 2, "parentId": 1, "path": "contact-x", "component": "crm/contact/index"}]
        deployed = inventory.deployed(inventory.scan(root), inventory.menu_routes(menus))
        self.assertEqual([(row["page"], row["route"]) for row in deployed], [("crm/contact/index.vue", "/crm/contact-x")])


class ScriptedModelTests(unittest.TestCase):
    def test_controls_are_found_by_their_label_on_the_page_the_model_saw(self):
        request = {"messages": [{"role": "tool", "content": "页面元素\n[3]<button>编 辑 />\n*[7]<button>确 定 />\n[9]<button>编辑 />"}]}
        self.assertEqual(index_of(request, "确定"), 7)
        self.assertEqual(index_of(request, "编辑", first=True), 3)
        with self.assertRaises(LookupError):
            index_of(request, "删除")

    def test_the_endpoint_answers_from_its_script_and_records_requests(self):
        import json, urllib.request
        model = ScriptedModel(lambda body: reply(call("finish", status="done", answer="ok")))
        try:
            request = urllib.request.Request(model.url + "/chat/completions", data=json.dumps({"messages": []}).encode(),
                                             headers={"Content-Type": "application/json"})
            message = json.load(urllib.request.urlopen(request, timeout=5))["choices"][0]["message"]
            self.assertEqual(message["tool_calls"][0]["function"]["name"], "finish")
            self.assertEqual(len(model.requests), 1)
        finally:
            model.close()


if __name__ == "__main__":
    unittest.main()
