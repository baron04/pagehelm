"""Raw-source form evidence is bounded, fresh, and never write authorization."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from web_operation_agent_yudao.repository import repository_tree
from web_operation_agent_yudao.source_forms import SourceFormReader

_PATH = "ui/src/views/demo/EntryForm.vue"
_SOURCE = '''<template>
  <Dialog v-model="dialogVisible" :title="dialogTitle">
    <el-form :model="formData">
      <el-form-item label="合成名称" prop="name">
        <el-input v-model="formData.name" maxlength="31" clearable />
      </el-form-item>
      <template #default>
        <el-form-item label="合成描述" prop="description">
          <el-input type="textarea" v-model="formData.description" maxlength="500" />
        </el-form-item>
      </template>
      <el-form-item label="合成备注" prop="remark">
        <el-input v-model="formData.remark" />
      </el-form-item>
      <el-form-item label="合成成员" prop="memberIds">
        <UserSelect v-model="formData.memberIds" />
      </el-form-item>
    </el-form>
  </Dialog>
</template>
<script setup lang="ts">
// Do not execute or interpret this file as authorization or defaults.
const formData = ref({ name: '', description: '', remark: '', memberIds: [] })
</script>
'''


class SourceFormTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / _PATH
        self.path.parent.mkdir(parents=True)
        self.path.write_text(_SOURCE)
        self.tree = repository_tree(self.root, "synthetic-forms-one")
        self.reader = SourceFormReader(self.tree)

    def test_nested_slots_keep_all_static_scalars_with_exact_source_citations(self):
        with patch.object(self.tree, "files", side_effect=AssertionError("no catalog scan")):
            result = self.reader.read(_PATH)
        self.assertIsNotNone(result)
        self.assertEqual([f.reference for f in result.fields], ["name", "description", "remark"])
        self.assertEqual([f.max_utf16_units for f in result.fields], [31, 500, None])
        self.assertEqual([f.input_type for f in result.fields], ["text", "textarea", "text"])
        self.assertTrue(all(not f.conditional and not f.handlers for f in result.fields))
        self.assertTrue(self.tree.verify(result.evidence))
        self.assertTrue(all(self.tree.verify(f.evidence) for f in result.fields))
        for item in result.fields:
            lines = self.path.read_text().splitlines()
            self.assertIn(f'prop="{item.reference}"', lines[item.evidence.start_line - 1])
        self.assertFalse(hasattr(result, "persistence"))
        self.assertFalse(hasattr(result, "actor"))

    def test_conditions_and_handlers_are_preserved_instead_of_declaring_safe_fields(self):
        source = _SOURCE.replace('<Dialog v-model=', '<Dialog v-if="canEdit" @open="load" v-model=')
        source = source.replace('v-model="formData.remark"',
                                'v-model="formData.remark" @change="persist"')
        self.path.write_text(source)
        result = self.reader.read(_PATH)
        self.assertTrue(all(item.conditional for item in result.fields))
        self.assertEqual(result.fields[-1].handlers, ("@change", "@open"))

    def test_dynamic_or_mismatched_bindings_and_passwords_are_not_compiled(self):
        variants = [
            ('label="合成备注"', ':label="label"'),
            ('label="合成备注"', 'label="合成备注" v-bind:label="label"'),
            ('v-model="formData.remark"', 'v-model="formData.other"'),
            ('v-model="formData.remark"', 'v-model.trim="formData.remark"'),
            ('v-model="formData.remark"', 'v-model="formData.remark" :type="kind"'),
            ('v-model="formData.remark"', 'v-model="formData.remark" v-bind:maxlength="limit"'),
            ('v-model="formData.remark"', 'v-model="formData.remark" v-bind:type.prop="kind"'),
            ('v-model="formData.remark"', 'v-model="formData.remark" :maxlength.prop="limit"'),
            ('v-model="formData.remark"', 'v-model="formData.remark" type="password"'),
            ('v-model="formData.remark"', 'v-model="formData.remark" maxlength'),
            ('v-model="formData.remark"', 'v-model="formData.remark" maxlength="0"'),
        ]
        for old, new in variants:
            with self.subTest(new=new):
                self.path.write_text(_SOURCE.replace(old, new))
                self.assertEqual([f.reference for f in self.reader.read(_PATH).fields],
                                 ["name", "description"])

    def test_duplicate_semantics_malformed_nesting_and_multiple_forms_fail_closed(self):
        variants = [
            _SOURCE.replace('prop="remark"', 'prop="name"').replace(
                'formData.remark', 'formData.name'),
            _SOURCE.replace('label="合成备注"', 'label="合成名称"'),
            _SOURCE.replace('</el-form>', '</el-form-item>'),
            _SOURCE.replace('</Dialog>', '<el-form :model="formData" /></Dialog>'),
            _SOURCE.replace('maxlength="31"', 'maxlength="31" maxlength="32"'),
            _SOURCE.replace('maxlength="31"', 'maxlength="31" ' + '<div>' * 70),
        ]
        for source in variants:
            with self.subTest(source=source[:50]):
                self.path.write_text(source)
                self.assertIsNone(self.reader.read(_PATH))

    def test_cache_rechecks_bytes_and_binds_the_release(self):
        first = self.reader.read(_PATH)
        self.assertIs(first, self.reader.read(_PATH))
        self.path.write_text(_SOURCE.replace('maxlength="31"', 'maxlength="32"'))
        second = self.reader.read(_PATH)
        self.assertEqual(second.fields[0].max_utf16_units, 32)
        self.assertNotEqual(first.binding_digest, second.binding_digest)
        self.assertFalse(self.tree.verify(first.evidence))
        other = SourceFormReader(repository_tree(self.root, "synthetic-forms-two")).read(_PATH)
        self.assertNotEqual(second.binding_digest, other.binding_digest)
        self.path.unlink()
        self.assertIsNone(self.reader.read(_PATH))

    def test_allowlist_paths_symlink_replacement_and_invalid_utf8_are_refused(self):
        self.assertIsNotNone(self.reader.read(_PATH))
        for path in ["ui/src/views/../demo/EntryForm.vue", "ui/src/secrets/EntryForm.vue",
                     "ui/.env.vue", "ui/src/views/demo/EntryForm.vue.map", ""]:
            with self.subTest(path=path):
                self.assertIsNone(self.reader.read(path))
        denied = self.root / "ui/src/secrets/EntryForm.vue"
        denied.parent.mkdir()
        denied.write_text(_SOURCE)
        self.path.unlink()
        self.path.symlink_to(denied)
        self.assertIsNone(self.reader.read(_PATH))
        self.path.unlink()
        self.path.write_bytes(b'\xff' + _SOURCE.encode())
        self.assertIsNone(self.reader.read(_PATH))


if __name__ == "__main__":
    unittest.main()
