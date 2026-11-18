"""Deployed Vue record tabs are hints, with no added capability or click authority."""
from pathlib import Path
import tempfile
import unittest
from web_operation_agent_yudao.rules.ui_source import UiSource
from web_operation_agent_yudao.goal_compat.goal import YudaoGoalData
from web_operation_agent_yudao.tests.test_goal import _Client, DOCS

ROUTER="""path: '/crm', children: [{ path: 'customer/detail/:id',
meta: { activeMenu: '/crm/customer' }, component: () => import('@/views/crm/customer/detail/index.vue') }]"""

class RecordSections(unittest.TestCase):
    def test_literal_component_labels_are_bounded_and_dynamic_comments_are_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);src=root/'src'
            for path,text in ((src/'utils/dict.ts',''),(src/'router/modules/remaining.ts',ROUTER),
                              (src/'views/crm/customer/detail/index.vue','''<template>
<el-tab-pane label="联系人"/><el-tab-pane label="合同" lazy/><el-tab-pane :label="title"/>
<!-- <el-tab-pane label="隐藏注释"/> --><el-tab-pane label="不固定" v-for="t in tabs" />
<el-tab-pane label="合同"/><el-tab-pane label="动态覆盖" :label="title"/></template>''')):
                path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
            ui=UiSource.read(root)
            self.assertEqual(ui.record_sections,{'/crm/customer/detail/{id}':('联系人','合同')})
            self.assertEqual(UiSource.none().record_sections,{})
            for enabled in (False,True):
                data=YudaoGoalData(_Client(),DOCS,ui=ui,native_sections=enabled)
                e=data.index().entity('crm/customer')
                self.assertEqual(e.sections,('联系人','合同') if enabled else ())
                self.assertEqual('sections' in data.capabilities(),enabled)

    def test_a_component_outside_views_cannot_supply_tabs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);src=root/'src'
            for path,text in ((src/'utils/dict.ts',''),(src/'router/modules/remaining.ts',ROUTER.replace('crm/customer/detail/index.vue','../uploads/index.vue')),
                              (src/'uploads/index.vue','<el-tab-pane label="秘密"/>')):
                path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
            (src/'views').mkdir(exist_ok=True)
            self.assertEqual(UiSource.read(root).record_sections,{})

if __name__=='__main__':unittest.main()
