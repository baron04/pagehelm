from pathlib import Path
import tempfile
import unittest

from web_operation_agent.planning.product_knowledge import ProductKnowledgeLookup
from web_operation_agent.planning.sourcetree import SourceTree
from web_operation_agent_yudao.workflow_knowledge import compile_workflow_guides


FORM = '''<template>
  <el-form :model="formData" :rules="formRules">
    <el-tab-pane label="基础设置" name="info">
      <el-form-item label="名称" prop="name"><el-input v-model="formData.name" /></el-form-item>
      <el-form-item label="单位" prop="unit"><el-input v-model="formData.unit" /></el-form-item>
    </el-tab-pane>
    <el-tab-pane :label="runtimeLabel" />
    <el-form-item v-if="enabled" label="备注" prop="remark"><el-input /></el-form-item>
    <el-button @click="submitForm">确定</el-button>
  </el-form>
</template>
<script setup>
/** 仓储商品 表单 */
defineOptions({ name: 'ItemForm' })
const formRules = reactive({
  name: [{ required: true, message: '名称不能为空' }]
})
async function submitForm() {
  await formRef.value.validate()
  await ItemApi.createItem(formData.value)
}
</script>
'''
LIST = '''<template>
  <el-button @click="openForm('create')">新增</el-button>
  <ItemForm ref="formRef" />
</template>
<script setup>
import ItemForm from './ItemForm.vue'
const openForm = (type) => {
  formRef.value.open(type)
}
</script>
'''


class WorkflowKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tree = SourceTree({"repository_id": "example/ui", "release": "build-2",
            "allow_globs": ["ui/src/views/**", "ui/src/router/**"],
            "deny_globs": ["**/.env*", "**/tests/**"]}, self.root)
        self.put("ui/src/views/wms/item/ItemForm.vue", FORM)
        self.put("ui/src/views/wms/item/index.vue", LIST)

    def put(self, path, text):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)

    def test_entry_labels_rules_and_named_function_are_original_evidence(self):
        guides = compile_workflow_guides(self.tree)
        self.assertEqual(1, len(guides))
        lookup = ProductKnowledgeLookup(self.tree, guides)
        sections = guides[0].sections
        summaries = " ".join(s.summary for s in sections)
        excerpts = "\n".join(r.excerpt for s in sections for r in s.evidence)
        self.assertIn("基础设置", summaries)
        self.assertNotIn("runtimeLabel", summaries)
        self.assertIn("备注(remark)[条件展示]", summaries)
        self.assertIn("await ItemApi.createItem", excerpts)
        self.assertIn("required: true", excerpts)
        self.assertNotIn("unit: [{ required", excerpts)
        self.assertTrue(any(r.path.endswith("index.vue") for s in sections for r in s.evidence))
        self.assertTrue(all(lookup.verify(h.evidence) for h in lookup.search("WMS 仓储商品")))

    def test_mentions_and_unrendered_imports_are_not_creation_workflows(self):
        self.put("ui/src/views/member/order/index.vue", '<template><div>商品</div></template><script>const product = 1</script>')
        self.put("ui/src/views/pay/index.vue", "<template><div>商品</div></template><script>import ItemForm from '../wms/item/ItemForm.vue'</script>")
        guides = compile_workflow_guides(self.tree)
        self.assertEqual(["ui/src/views/wms/item/ItemForm.vue"], [g.key for g in guides])
        self.assertFalse(any("/pay/" in r.path for s in guides[0].sections for r in s.evidence))

    def test_changed_optional_field_and_conditional_label_are_not_guessed(self):
        self.put("ui/src/views/wms/item/ItemForm.vue", FORM.replace('label="单位"', ':label="unitLabel"'))
        guide = compile_workflow_guides(self.tree)[0]
        self.assertNotIn("单位", " ".join(s.summary for s in guide.sections))

    def test_commented_rule_end_and_multiline_validator_remain_bound_to_original_code(self):
        body = FORM.replace("})\nasync function", "}) // rules\nasync function")
        body = body.replace("name: [{ required: true, message: '名称不能为空' }]", "name: [{ required: true, message: '名称不能为空', validator: validateName }]")
        body = body.replace("</script>", "function validateName(\n  rule,\n  value,\n  callback\n) {\n  if (value.length < 2) callback(new Error('too short'))\n}\n</script>")
        self.put("ui/src/views/wms/item/ItemForm.vue", body)
        guide = compile_workflow_guides(self.tree)[0]
        self.assertIn("名称(name)", " ".join(s.summary for s in guide.sections))
        self.assertIn("too short", " ".join(r.excerpt for s in guide.sections for r in s.evidence))

    def test_a_search_form_calling_a_create_utility_is_not_a_creation_form(self):
        self.put("ui/src/views/mall/list/index.vue", '<template><el-form :model="queryParams" /></template><script>createImageViewer({})</script>')
        self.assertFalse(any('/mall/list/' in g.key for g in compile_workflow_guides(self.tree)))

    def test_broken_template_and_secret_import_fail_closed(self):
        self.put("ui/src/views/wms/item/ItemForm.vue", FORM.replace('</el-form>', '</div>'))
        self.assertFalse(compile_workflow_guides(self.tree))

    def test_named_route_connects_separate_form_to_actual_parent(self):
        self.put("ui/src/views/wms/item/index.vue", '''<template><el-button @click="handleAdd">新增</el-button></template>
<script>
const handleAdd = () => {
  push({ name: 'ItemCreate' })
}
</script>''')
        self.put("ui/src/router/modules/remaining.ts", "component: () => import('@/views/wms/item/ItemForm.vue'),\nname: 'ItemCreate',\n")
        guide = compile_workflow_guides(self.tree)[0]
        self.assertTrue(any(r.path.endswith('index.vue') for s in guide.sections for r in s.evidence))

    def test_related_forms_require_actual_import_and_template_usage(self):
        self.put("ui/src/views/wms/item/Child.vue", FORM.replace('label="名称"', 'label="子名称"'))
        text = FORM.replace('<el-form :', '<Child />\n  <el-form :').replace('<script setup>', "<script setup>\nimport Child from './Child.vue'")
        self.put("ui/src/views/wms/item/ItemForm.vue", text)
        root = next(g for g in compile_workflow_guides(self.tree) if g.key.endswith('/ItemForm.vue'))
        self.assertTrue(any(r.path.endswith('Child.vue') for s in root.sections for r in s.evidence))


if __name__ == "__main__":
    unittest.main()
