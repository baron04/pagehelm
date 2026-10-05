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
    def test_radio_literals_preserve_boolean_values_and_known_mode_gate(self):
        radio = '<el-radio-group v-model="formData.remark"><el-radio :value="false">私有</el-radio><el-radio :value="true">公开</el-radio></el-radio-group>'
        self.path.write_text(_SOURCE.replace('<el-input v-model="formData.remark" />', radio)
            .replace('label="合成备注"', 'v-if="formType === \'create\'" label="合成备注"'))
        fact = self.reader.read(_PATH).fields[-1]
        self.assertEqual((fact.input_type, fact.choices, fact.modes),
                         ('radio', (('false', '私有'), ('true', '公开')), ('create',)))
        self.assertTrue(fact.conditional)
        for bad in (radio.replace('公开', '私有'), radio.replace(':value="true"', ':value="expression()"'),
                    radio.replace('>公开', '@change="persist">公开'), radio.replace('>公开', 'v-if="canEdit">公开')):
            self.path.write_text(_SOURCE.replace('<el-input v-model="formData.remark" />', bad))
            self.assertNotIn('remark', [f.reference for f in self.reader.read(_PATH).fields])

    def test_radio_shallow_constant_import_is_fresh_and_never_executed(self):
        dependency = self.root/'ui/src/views/demo/constants.ts'
        dependency.write_text('export const Mode = { NORMAL: 1, AGILE: 2 } as const\n')
        body = _SOURCE.replace('<el-input v-model="formData.remark" />',
            '<el-radio-group v-model="formData.remark"><el-radio-button :value="Mode.NORMAL">普通</el-radio-button><el-radio-button :value="Mode.AGILE">敏捷</el-radio-button></el-radio-group>')
        body = body.replace('// Do not execute', "import { Mode } from '@/views/demo/constants'\n// Do not execute")
        self.path.write_text(body)
        first = self.reader.read(_PATH)
        self.assertEqual(first.fields[-1].choices, (('1', '普通'), ('2', '敏捷')))
        self.assertTrue(self.tree.verify(first.dependencies[0]))
        dependency.write_text(dependency.read_text().replace('AGILE: 2', 'AGILE: 3'))
        second = self.reader.read(_PATH)
        self.assertEqual(second.fields[-1].choices[-1][0], '3')
        self.assertNotEqual(first.binding_digest, second.binding_digest)
        for content in ['export const Mode = { NORMAL: 1, AGILE: run() } as const',
                        'export { Mode } from "./other"']:
            dependency.write_text(content)
            self.assertNotIn('remark', [f.reference for f in self.reader.read(_PATH).fields])
        dependency.unlink()
        self.assertNotIn('remark', [f.reference for f in self.reader.read(_PATH).fields])

    def test_literal_single_choices_and_dynamic_multiple_or_duplicate_choices(self):
        select = '<el-select v-model="formData.remark"><el-option label="普通" value="normal"/><el-option label="加急" :value="2"/></el-select>'
        self.path.write_text(_SOURCE.replace('<el-input v-model="formData.remark" />', select))
        result = self.reader.read(_PATH)
        self.assertEqual(result.fields[-1].choices, (("normal", "普通"), ("2", "加急")))
        self.assertTrue(self.tree.verify(result.fields[-1].evidence))
        for bad in (select.replace('v-model=', 'multiple v-model='),
                    select.replace('label="加急"', 'label="普通"'),
                    select.replace('label="加急"', ':label="dynamic"'),
                    select.replace(':value="2"', ':value="expr()"')):
            self.path.write_text(_SOURCE.replace('<el-input v-model="formData.remark" />', bad))
            self.assertNotIn("remark", [f.reference for f in self.reader.read(_PATH).fields])

    def test_dictionary_dependency_is_allowlisted_fresh_and_release_bound(self):
        dictionary = self.root/'ui/src/utils/dict.ts'
        dictionary.parent.mkdir(parents=True)
        dictionary.write_text("export enum DICT_TYPE {\n SYNTHETIC = 'synthetic',\n}\n")
        select = '<el-select v-model="formData.remark"><el-option v-for="dict in getIntDictOptions(DICT_TYPE.SYNTHETIC)" :label="dict.label" :value="dict.value"/></el-select>'
        body = _SOURCE.replace('<el-input v-model="formData.remark" />', select).replace(
            '// Do not execute', "import { DICT_TYPE, getIntDictOptions } from '@/utils/dict'\n// Do not execute")
        self.path.write_text(body)
        first = self.reader.read(_PATH)
        self.assertEqual(first.fields[-1].choice_reference, "synthetic")
        self.assertTrue(self.tree.verify(first.dependencies[0]))
        dictionary.write_text(dictionary.read_text().replace("'synthetic'", "'changed'"))
        second = self.reader.read(_PATH)
        self.assertEqual(second.fields[-1].choice_reference, "changed")
        self.assertNotEqual(first.binding_digest, second.binding_digest)
        dictionary.unlink()
        self.assertNotIn("remark", [f.reference for f in self.reader.read(_PATH).fields])

    def test_user_relation_requires_the_fixed_zero_argument_read_and_scalar_ids(self):
        select = '<el-select v-model="formData.remark"><el-option v-for="item in userOptions" :label="item.nickname" :value="item.id"/></el-select>'
        body = _SOURCE.replace('<el-input v-model="formData.remark" />', select).replace(
            '// Do not execute', "import * as UserApi from '@/api/system/user'\nuserOptions.value = await UserApi.getSimpleUserList()\n// Do not execute")
        self.path.write_text(body)
        self.assertEqual(self.reader.read(_PATH).fields[-1].choice_source, 'users')
        for old, new in [('getSimpleUserList()', 'getSimpleUserList(query)'),
                         ('item.id', 'item'), ('item.nickname', 'item.displayName'),
                         ("@/api/system/user", '@/api/private/users'),
                         ('v-model=', ':multiple.prop="true" v-model=')]:
            self.path.write_text(body.replace(old, new))
            self.assertNotIn('remark', [f.reference for f in self.reader.read(_PATH).fields])

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

    def test_literal_numeric_constraints_and_single_iso_day_preserve_source_evidence(self):
        self.path.write_text(_SOURCE.replace(
            '<el-input v-model="formData.remark" />',
            '<el-input-number v-model="formData.remark" :min="0.01" :max="999" :precision="2" />')
            .replace('<el-input type="textarea" v-model="formData.description" maxlength="500" />',
                     '<el-date-picker v-model="formData.description" type="date" value-format="x" />'))
        result = self.reader.read(_PATH)
        date, number = result.fields[1:]
        self.assertEqual(date.input_type, "date")
        self.assertEqual((number.input_type, number.minimum, number.maximum, number.precision),
                         ("number", "0.01", "999", 2))
        self.assertTrue(self.tree.verify(number.evidence))
        self.assertTrue(self.tree.verify(date.evidence))

    def test_dynamic_numeric_constraints_and_non_iso_or_range_dates_are_not_compiled(self):
        controls = [
            '<el-input-number :min="limit" />',
            '<el-input-number :precision="scale" />',
            '<el-input-number :precision="16" />',
            '<el-input-number :min="0" min="1" />',
            '<el-input-number :min.prop="0" />',
            '<el-input-number step-strictly />',
            '<el-date-picker type="daterange" />',
            '<el-date-picker type="datetime" />',
            '<el-date-picker :format="fmt" />',
            '<el-date-picker format="DD/MM/YYYY" />',
            '<el-date-picker value-format="YYYY/MM/DD" />',
        ]
        for control in controls:
            with self.subTest(control=control):
                bound = control.replace(' />', ' v-model="formData.remark" />')
                self.path.write_text(_SOURCE.replace('<el-input v-model="formData.remark" />', bound))
                self.assertEqual([f.reference for f in self.reader.read(_PATH).fields],
                                 ["name", "description"])


if __name__ == "__main__":
    unittest.main()
