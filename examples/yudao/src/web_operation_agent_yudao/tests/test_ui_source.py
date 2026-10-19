"""The admin UI's source, read for the facts the adapter takes from it."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from web_operation_agent_yudao.ui_source import UiSource, _literal_column_labels, _shown_units, _query_choices

_PAGE = """<template>
  <el-table-column label="流程名称" prop="name" />
</template>
<script setup lang="ts">
import * as ProcessInstanceApi from '@/api/bpm/processInstance'
import { CategoryApi } from '@/api/bpm/category'
const getList = async () => {
  loading.value = true
  try {
    const data = await ProcessInstanceApi.getProcessInstanceMyPage(queryParams)
    list.value = data.list
  } finally {
    loading.value = false
  }
}
</script>
"""
_ITEMS = """<template>
  <el-table :data="formData">
    <el-table-column label="序号" type="index" width="60" />
    <el-table-column label="产品名称" min-width="180">
      <template #default="{ row, $index }">
        <el-form-item :prop="`${$index}.productId`"><el-select v-model="row.productId" /></el-form-item>
      </template>
    </el-table-column>
    <el-table-column label="数量" prop="count" min-width="140">
      <template #default="{ row }"><el-input-number v-model="row.count" /></template>
    </el-table-column>
  </el-table>
</template>
"""
_API = """import request from '@/config/axios'
export const getProcessInstanceMyPage = async (params: any) => {
  return await request.get({ url: '/bpm/process-instance/my-page', params })
}
"""


class UiSourceTests(unittest.TestCase):
    def test_search_options_are_owned_by_the_exact_form_field(self):
        form = '<el-form :model="queryParams"><el-form-item prop="stage"><el-select v-model="queryParams.stage"><el-option label="Not received" value="0" /><el-option label="Received" :value="2" /></el-select></el-form-item></el-form>'
        self.assertEqual(_query_choices('<template>' + form + '</template>', ''),
                         ((("stage", (("0", "Not received"), ("2", "Received"))),), ()))
        for invalid in (form.replace('queryParams.stage','formData.stage'),
                        form.replace('label="Received"','label="Received" v-if="allowed"'),
                        form.replace('value="0"','value="2"'), form + form):
            self.assertEqual(_query_choices('<template>' + invalid + '</template>', ''), ((), ()))
        unrelated = '<el-select v-model="queryParams.other"><el-option label="Wrong" value="9" /></el-select>'
        self.assertEqual(_query_choices('<template>' + unrelated + '</template>', ''), ((), ()))

    def test_line_dictionaries_are_column_bound_and_conflicts_remain_unresolved(self):
        table = '<el-table-column label="单位"><dict-tag :type="DICT_TYPE.UNIT" :value="row.measure" /></el-table-column>'
        status = '<el-table-column label="状态"><dict-tag :type="DICT_TYPE.STATUS" :value="scope.row.status" /></el-table-column>'
        outside = '<dict-tag :type="DICT_TYPE.UNIT" :value="row.unrelated" />'
        unknown = '<el-table-column label="未知"><dict-tag :type="DICT_TYPE.ABSENT" :value="row.unknown" /></el-table-column>'
        with tempfile.TemporaryDirectory() as root:
            src = Path(root) / "src"
            for path, text in (("utils/dict.ts", "UNIT = 'line_unit'\nSTATUS = 'line_status'\nOTHER = 'other_status'"),
                               ("router/modules/remaining.ts", ""),
                               ("views/orders/detail/Items.vue", table + status + outside + unknown),
                               ("views/orders/detail/Other.vue", status.replace('DICT_TYPE.STATUS', 'DICT_TYPE.OTHER')),
                               ("views/orders-extra/Items.vue", table.replace('DICT_TYPE.UNIT', 'DICT_TYPE.OTHER'))):
                file = src / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(text, encoding="utf-8")
            source = UiSource.read(root)
        self.assertEqual(source.line_dicts("orders"), {"measure": "line_unit"})
        self.assertNotIn("unrelated", source.table_dicts["orders/detail/Items"])
        self.assertEqual(UiSource.none().line_dicts("orders"), {})

    def test_a_page_reads_the_list_its_get_list_awaits(self):
        with tempfile.TemporaryDirectory() as root:
            src = Path(root) / "src"
            for path, text in ((src / "utils" / "dict.ts", ""),
                               (src / "router" / "modules" / "remaining.ts", ""),
                               (src / "views" / "bpm" / "processInstance" / "index.vue", _PAGE),
                               (src / "api" / "bpm" / "processInstance" / "index.ts", _API),
                               (src / "views" / "bpm" / "processInstance" / "ItemForm.vue", _ITEMS)):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            source = UiSource.read(root)
            view = source.views["bpm/processInstance/index"]
        self.assertEqual(view.endpoint, "/bpm/process-instance/my-page")
        # A line table's columns name the row fields their bodies bind.
        self.assertEqual(source.line_labels("bpm/processInstance"),
                         {"productId": "产品名称", "count": "数量"})
        self.assertEqual(view.columns, ("name",))
        self.assertEqual(view.column_labels, (("name", "流程名称"),))

    def test_named_loader_and_function_api_resolve_the_main_list(self):
        page = """<template><el-table :data="projectList" /></template>
<script setup lang="ts">
import * as ProjectApi from '@/api/pms/pm/project'
async function getProjectList() {
  const data = await ProjectApi.getProjectPage(queryParams)
  projectList.value = data.list
}
async function getFavoriteProjectList() {
  favoriteProjectList.value = await ProjectApi.getFavoriteProjectList()
}
</script>"""
        api = """export function getProjectPage(params: PageParam) {
  return request.get({ url: '/pms/pm/project/page', params })
}
export function getFavoriteProjectList() {
  return request.get({ url: '/pms/pm/project/favorite-list' })
}"""
        with tempfile.TemporaryDirectory() as root:
            src = Path(root) / "src"
            for path, content in ((src / "utils/dict.ts", ""),
                                  (src / "router/modules/remaining.ts", ""),
                                  (src / "views/pms/pm/project/index.vue", page),
                                  (src / "api/pms/pm/project/index.ts", api)):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            source = UiSource.read(root)
        self.assertEqual(source.views["pms/pm/project/index"].endpoint,
                         "/pms/pm/project/page")

    def test_loader_follows_its_list_helper_past_a_conditional_detail_read(self):
        page = """<script setup lang="ts">
import * as DefinitionApi from '@/api/bpm/definition'
import * as ProcessApi from '@/api/bpm/process'
const getList = async () => {
  await getDefinitionList()
  if (copyId) await ProcessApi.getProcess(copyId)
}
const getDefinitionList = async () => {
  definitionList.value = await DefinitionApi.getDefinitionList({ suspensionState: 1 })
}
</script>"""
        with tempfile.TemporaryDirectory() as root:
            src = Path(root) / "src"
            for path, content in ((src / "utils/dict.ts", ""),
                                  (src / "router/modules/remaining.ts", ""),
                                  (src / "views/bpm/process/index.vue", page),
                                  (src / "api/bpm/process/index.ts", """export const getProcess = (id) => {
  return request.get({ url: '/bpm/process/get' })
}"""),
                                  (src / "api/bpm/definition/index.ts", """export const getDefinitionList = () => {
  return request.get({ url: '/bpm/definition/list' })
}""")):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            source = UiSource.read(root)
        self.assertEqual(source.views["bpm/process/index"].endpoint,
                         "/bpm/definition/list")
        self.assertEqual(source.views["bpm/process/index"].fixed_params,
                         (("suspensionState", 1),))

    def test_load_named_other_than_get_list_can_supply_a_record_list(self):
        page = """<script setup lang="ts">
import * as InsuranceApi from '@/api/hrm/insurance'
async function init() {
  allRecords.value = (await InsuranceApi.getInsuranceRecordList()) || []
}
</script>"""
        with tempfile.TemporaryDirectory() as root:
            src = Path(root) / "src"
            for path, content in ((src / "utils/dict.ts", ""),
                                  (src / "router/modules/remaining.ts", ""),
                                  (src / "views/hrm/insurance/index.vue", page),
                                  (src / "api/hrm/insurance/index.ts", """export const getInsuranceRecordList = () => {
  return request.get({ url: '/hrm/insurance/record/list' })
}""")):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            source = UiSource.read(root)
        self.assertEqual(source.views["hrm/insurance/index"].endpoint,
                         "/hrm/insurance/record/list")

    def test_single_local_list_component_supplies_wrapped_page(self):
        parent = """<template><ClockRecordList /></template>
<script setup lang="ts">
import ClockRecordList from './ClockRecordList.vue'
</script>"""
        child = """<template><el-table-column label="姓名" prop="name" /></template>
<script setup lang="ts">
import * as ClockApi from '@/api/hrm/clock'
async function getList() {
  list.value = (await ClockApi.getClockPage(queryParams)).list
}
</script>"""
        with tempfile.TemporaryDirectory() as root:
            src = Path(root) / "src"
            for path, content in ((src / "utils/dict.ts", ""),
                                  (src / "router/modules/remaining.ts", ""),
                                  (src / "views/hrm/clock/index.vue", parent),
                                  (src / "views/hrm/clock/ClockRecordList.vue", child),
                                  (src / "api/hrm/clock/index.ts", """export const getClockPage = (params) => {
  return request.get({ url: '/hrm/clock/page', params })
}""")):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            source = UiSource.read(root)
        self.assertEqual(source.views["hrm/clock/index"].endpoint, "/hrm/clock/page")
        self.assertEqual(source.views["hrm/clock/index"].columns, ("name",))

    def test_a_columns_label_states_the_unit_only_of_a_value_shown_as_read(self):
        column = '<el-table-column label="价格（元）" prop="price" :formatter="erpPriceTableColumnFormatter" />'
        self.assertEqual(_shown_units(column), (("price", "元"),))
        self.assertEqual(_shown_units(column.replace("（元）", "(分)")), (("price", "分"),))
        # The page converts before showing, or renders the cell itself: the label is not
        # the unit of what was read.
        self.assertEqual(_shown_units(column.replace("erpPriceTableColumnFormatter", "fenToYuanFormat")), ())
        self.assertEqual(_shown_units('<el-table-column label="价格（元）" prop="price">'
                                      '<template #default="{ row }">{{ fenToYuan(row.price) }}</template>'
                                      '</el-table-column>'), ())
        # No unit in the label, a bound label, or two columns that disagree.
        self.assertEqual(_shown_units('<el-table-column label="价格" prop="price" />'), ())
        self.assertEqual(_shown_units('<el-table-column :label="caption" prop="price" />'), ())
        self.assertEqual(_shown_units(column + column.replace("（元）", "（分）")), ())


if __name__ == "__main__":
    unittest.main()

    def test_column_vocabulary_requires_unique_literal_bindings(self):
        source = '<el-table-column prop="totalPrice" label="含税金额" :formatter="formatter" />'
        self.assertEqual(_literal_column_labels(source), (("totalPrice", "含税金额"),))
        for attrs in ('prop="a" :label="caption"', ':prop="field" label="金额"',
                      'prop="a" label="金额" v-bind="attrs"'):
            self.assertEqual(_literal_column_labels(f'<el-table-column {attrs} />'), ())
        self.assertEqual(_literal_column_labels(source + source.replace('totalPrice', 'taxPrice')), ())
        self.assertEqual(_literal_column_labels(source + source.replace('含税金额', '税额')), ())
