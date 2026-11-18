"""yudao's source rules: a control's visibility and its operation's refusals, cited."""
from __future__ import annotations

import hashlib
import re
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from web_operation_agent_yudao.rules.source import EntitySource, YudaoSource, _mask, _methods

_JAVA = "server/yudao-module-crm/src/main/java/demo"
_FILES = {
    f"{_JAVA}/enums/ErrorCodeConstants.java": """public interface ErrorCodeConstants {
    ErrorCode CONTRACT_NOT_EXISTS = new ErrorCode(1_020_000_000, "合同不存在");
    ErrorCode CONTRACT_SUBMIT_FAIL_NOT_DRAFT = new ErrorCode(1_020_000_002, "合同提交审核失败，原因：合同没处在未提交状态");
    ErrorCode CONTRACT_DELETE_FAIL = new ErrorCode(1_020_000_005, "删除合同失败，原因：有被回款所使用");
    ErrorCode CONTRACT_UPDATE_FAIL = new ErrorCode(1_020_000_006, "修改合同失败，原因：只有草稿状态可以修改");
}
""",
    f"{_JAVA}/controller/CrmContractController.java": """@RestController
@RequestMapping("/crm/contract")
@Validated
public class CrmContractController {

    @Resource
    private CrmContractService contractService;

    @PutMapping("/submit")
    @Operation(summary = "提交合同审批")
    @PreAuthorize("@ss.hasPermission('crm:contract:update')")
    public CommonResult<Boolean> submitContract(@RequestParam("id") Long id) {
        contractService.submitContract(id, getLoginUserId());
        return success(true);
    }

    @DeleteMapping("/delete")
    @Operation(summary = "删除合同")
    @PreAuthorize("@ss.hasPermission('crm:contract:delete')")
    public CommonResult<Boolean> deleteContract(@RequestParam("id") Long id) {
        contractService.deleteContract(id);
        return success(true);
    }

    @PutMapping("/update")
    @Operation(summary = "修改合同")
    @PreAuthorize("@ss.hasPermission('crm:contract:update')")
    public CommonResult<Boolean> updateContract(@Valid @RequestBody CrmContractSaveReqVO reqVO) {
        contractService.updateContract(reqVO);
        return success(true);
    }
}
""",
    f"{_JAVA}/service/CrmContractServiceImpl.java": """public class CrmContractServiceImpl implements CrmContractService {

    @Override
    public void submitContract(Long id, Long userId) {
        CrmContractDO contract = validateContractExists(id);
        if (ObjUtil.notEqual(contract.getAuditStatus(), 0)) {
            throw exception(CONTRACT_SUBMIT_FAIL_NOT_DRAFT);
        }
    }

    @Override
    public void updateContract(CrmContractSaveReqVO reqVO) {
        CrmContractDO contract = validateContractExists(reqVO.getId());
        if (ObjUtil.notEqual(contract.getAuditStatus(), 0)) {
            throw exception(CONTRACT_UPDATE_FAIL);
        }
    }

    @Override
    public void deleteContract(Long id) {
        CrmContractDO contract = validateContractExists(id);
        checkReceivables(contract);
    }

    private void checkReceivables(CrmContractDO contract) {
        if (receivableService.getReceivableCountByContractId(contract.getId()) > 0) {
            throw exception(CONTRACT_DELETE_FAIL);
        }
    }

    private CrmContractDO validateContractExists(Long id) {
        CrmContractDO contract = contractMapper.selectById(id);
        if (contract == null) {
            throw exception(CONTRACT_NOT_EXISTS);
        }
        return contract;
    }
}
""",
    "ui/src/views/crm/contract/ContractForm.vue": """<template>
  <el-form :model="formData">
    <el-form-item label="负责人" prop="ownerUserId">
      <el-select v-model="formData.ownerUserId" :disabled="formType !== 'create'" />
    </el-form-item>
    <el-form-item label="客户签约人" prop="signContactId">
      <el-select v-model="formData.signContactId" :disabled="!formData.customerId" />
    </el-form-item>
    <el-form-item label="合同名称" prop="name">
      <el-input v-model="formData.name" :disabled="formLoading" />
    </el-form-item>
  </el-form>
</template>
<script setup lang="ts">
import * as ContractApi from '@/api/crm/contract'
const submitForm = async () => {
  await ContractApi.updateContract(formData.value)
}
</script>
""",
    "ui/src/api/crm/contract/index.ts": """import request from '@/config/axios'
export const submitContract = async (id: number) => {
  return await request.put({ url: `/crm/contract/submit?id=${id}` })
}
export const deleteContract = async (id: number) => {
  return await request.delete({ url: `/crm/contract/delete?id=` + id })
}
export const updateContract = (data: ContractVO) => {
  return request.put({ url: '/crm/contract/update', data })
}
""",
    "ui/src/views/crm/contract/index.vue": """<template>
  <el-table-column label="操作">
    <template #default="scope">
      <el-button
        v-if="scope.row.auditStatus === 0"
        v-hasPermi="['crm:contract:update']"
        @click="handleSubmit(scope.row)"
      >
        提交审核
      </el-button>
      <el-button v-else @click="handleProcessDetail(scope.row)">查看审批</el-button>
      <el-button v-hasPermi="['crm:contract:delete']" @click="handleDelete(scope.row.id)">删除</el-button>
      <el-button v-hasPermi="['crm:contract:update']" @click="openForm('update', scope.row.id)">修改</el-button>
    </template>
  </el-table-column>
</template>
<script setup lang="ts">
import ContractForm from './ContractForm.vue'
import * as ContractApi from '@/api/crm/contract'
const formRef = ref()
const openForm = (type: string, id?: number) => {
  formRef.value.open(type, id)
}
const handleSubmit = async (row) => {
  await ContractApi.submitContract(row.id)
}
const handleDelete = async (id: number) => {
  await ContractApi.deleteContract(id)
}
</script>
""",
}


class YudaoSourceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for relative, text in _FILES.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        entity = EntitySource(key="crm/contract", component_dir="crm/contract",
                              api_base="crm/contract",
                              labels={"auditStatus": "审批状态", "customerId": "客户"},
                              choices={"auditStatus": {"0": "未提交"}})
        self.rules, self.controls = YudaoSource(self.root, release="v2026.08").rules((entity,))

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_control_shows_under_its_condition_and_permission(self):
        submit = next(r for r in self.rules["crm/contract"]
                      if r.kind == "visibility" and r.about == "提交审核")
        self.assertIn("审批状态 === 0（未提交）", submit.text)
        self.assertIn("crm:contract:update", submit.text)
        self.assertEqual(submit.subject, "crm/contract/submit")
        other = next(r for r in self.rules["crm/contract"] if r.about == "查看审批")
        self.assertIn("!(审批状态 === 0（未提交）)", other.text)
        # A control listed is one with a permission or an operation behind it.
        self.assertEqual(self.controls["crm/contract"], ("提交审核", "删除", "修改"))

    def test_a_button_that_opens_a_form_is_tied_to_the_operation_its_form_calls(self):
        # The release writes its api functions as `(data) => { return request.put({...}) }`,
        # neither async nor await. A pattern that required both resolved no URL at all, so
        # every button's operation stayed unknown and the rules of one operation -- when the
        # control shows, what the operation refuses -- were answered as two islands.
        modify = next(r for r in self.rules["crm/contract"]
                      if r.kind == "visibility" and r.about == "修改")
        self.assertEqual(modify.subject, "crm/contract/update")
        refusal = next(r for r in self.rules["crm/contract"]
                       if r.kind == "refusal" and r.subject == "crm/contract/update")
        self.assertIn("只有草稿状态可以修改", refusal.text)

    def test_an_operation_refuses_as_its_service_says_under_the_same_subject(self):
        refusals = {r.about: r for r in self.rules["crm/contract"] if r.kind == "refusal"}
        self.assertIn("没处在未提交状态", refusals["提交审核"].text)
        self.assertEqual(refusals["提交审核"].subject, "crm/contract/submit")
        # Through a method of the same class; a record that is not there is no rule.
        self.assertIn("有被回款所使用", refusals["删除"].text)
        self.assertNotIn("合同不存在", " ".join(r.text for r in self.rules["crm/contract"]))

    def test_a_form_field_is_locked_under_its_condition_on_the_record(self):
        locked = {r.about: r.text for r in self.rules["crm/contract"] if r.kind == "editability"}
        self.assertEqual(locked, {"负责人": "不可修改条件：修改已有记录时（只在新建时可填）",
                                  "客户签约人": "不可修改条件：未填客户"})

    def test_java_cache_rehashes_bytes_additions_deletions_and_does_not_cache_ui_labels(self):
        entity=EntitySource("crm/contract","crm/contract","crm/contract", labels={"auditStatus":"New label"})
        with patch.object(YudaoSource,"_refusals",side_effect=AssertionError("unexpected parse")):
            cached,_=YudaoSource(self.root,release="v2026.08").rules((entity,))
        self.assertTrue(any("New label" in r.text for r in cached[entity.key]))
        path=self.root/f"{_JAVA}/enums/ErrorCodeConstants.java"
        path.write_text(path.read_text().replace("没处在未提交状态","状态不是草稿"))
        changed,_=YudaoSource(self.root,release="v2026.08").rules((entity,))
        self.assertTrue(any("状态不是草稿" in r.text for r in changed[entity.key]))
        added=self.root/f"{_JAVA}/enums/ExtraEnum.java";added.write_text("enum ExtraEnum { ONE(1, \"one\") }")
        original_parse = YudaoSource._refusals
        with patch.object(YudaoSource,"_refusals",autospec=True,side_effect=original_parse) as parse:
            YudaoSource(self.root,release="v2026.08").rules((entity,))
            self.assertEqual(parse.call_count,1)
        added.unlink()
        with patch.object(YudaoSource,"_refusals",side_effect=AssertionError("unchanged earlier bytes")):
            YudaoSource(self.root,release="v2026.08").rules((entity,))
        path.unlink()
        removed,_=YudaoSource(self.root,release="v2026.08").rules((entity,))
        self.assertFalse(any("状态不是草稿" in r.text for r in removed[entity.key]))

    def test_ui_cache_rehashes_current_controls_and_keeps_actor_labels_fresh(self):
        entity = EntitySource("crm/contract", "crm/contract", "crm/contract", labels={"auditStatus": "Actor label"})
        with patch.object(YudaoSource, "_buttons", side_effect=AssertionError("unchanged UI parsed")):
            rules, _ = YudaoSource(self.root, release="v2026.08").rules((entity,))
        self.assertTrue(any("Actor label" in r.text for r in rules[entity.key]))
        path = self.root / "ui/src/views/crm/contract/index.vue"
        path.write_text(path.read_text().replace("提交审核", "申请审核"))
        rules, controls = YudaoSource(self.root, release="v2026.08").rules((entity,))
        self.assertIn("申请审核", controls[entity.key])
        self.assertNotIn("提交审核", controls[entity.key])
        path.unlink()
        _, controls = YudaoSource(self.root, release="v2026.08").rules((entity,))
        self.assertNotIn("申请审核", controls.get(entity.key, ()))

    def test_out_of_module_import_is_never_cached_even_when_api_file_does_not_exist(self):
        path = self.root / "ui/src/views/crm/contract/index.vue"
        path.write_text(path.read_text().replace("@/api/crm/contract", "@/api/other/contract"))
        entity = EntitySource("crm/contract", "crm/contract", "crm/contract")
        source = YudaoSource(self.root, release="v2026.08", modules=("crm",))
        source.rules((entity,))
        self.assertFalse(source._ui_cache_complete)
        api = self.root / "ui/src/api/other/contract/index.ts"
        api.parent.mkdir(parents=True, exist_ok=True)
        original = next(text for name, text in _FILES.items() if name.startswith("ui/src/api/"))
        api.write_text(original)
        source = YudaoSource(self.root, release="v2026.08", modules=("crm",))
        rules, _ = source.rules((entity,))
        self.assertFalse(source._ui_cache_complete)
        self.assertTrue(any(r.subject == "crm/contract/submit" for r in rules[entity.key]))

    def test_cached_java_input_cannot_be_replaced_with_a_symlink(self):
        original=self.root/f"{_JAVA}/enums/ErrorCodeConstants.java"
        other=self.root/"private.java";other.write_text(original.read_text())
        original.unlink();original.symlink_to(other)
        with self.assertRaises(ValueError): YudaoSource(self.root,release="v2026.08").rules(())

    def test_every_citation_hashes_the_lines_it_names(self):
        for rule in self.rules["crm/contract"]:
            path, start, end, release, digest = re.fullmatch(
                r"(.+):(\d+)-(\d+)@(\S+) sha256:([0-9a-f]{64})", rule.citation).groups()
            lines = (self.root / path).read_bytes().splitlines(keepends=True)[int(start) - 1:int(end)]
            self.assertEqual(hashlib.sha256(b"".join(lines)).hexdigest(), digest)
            self.assertEqual(release, "v2026.08")
        refusal = next(r for r in self.rules["crm/contract"] if r.about == "提交审核" and r.kind == "refusal")
        path, start, end = re.match(r"(.+):(\d+)-(\d+)@", refusal.citation).groups()
        cited = (self.root / path).read_text(encoding="utf-8").splitlines()[int(start) - 1:int(end)]
        self.assertTrue(cited[0].strip().startswith("if (") and "throw exception" in cited[-1])


_DEMO = "server/yudao-module-mall/yudao-module-demo/src/main/java/cn/iocoder/yudao/module/demo"
_EXTRA_FILES = {
    f"{_DEMO}/enums/ErrorCodeConstants.java": """public interface ErrorCodeConstants {
    ErrorCode DEMO_NOT_EXISTS = new ErrorCode(1_900_000_000, "不存在");
    ErrorCode DEMO_LOCKED = new ErrorCode(1_900_000_001, "已锁定");
    ErrorCode DEMO_STATE_BAD = new ErrorCode(1_900_000_002, "状态错误");
    ErrorCode DEMO_ITEM_BAD = new ErrorCode(1_900_000_003, "明细错误");
    ErrorCode DEMO_ADMIN_FORBIDDEN = new ErrorCode(1_900_000_004, "管理员禁止");
    ErrorCode DEMO_SINGLE_BAD = new ErrorCode(1_900_000_005, "单条错误");
    ErrorCode DEMO_BATCH_BAD = new ErrorCode(1_900_000_006, "批量错误");
    ErrorCode DEMO_RISKY = new ErrorCode(1_900_000_007, "风险");
    ErrorCode DEMO_IO_BAD = new ErrorCode(1_900_000_008, "IO错误");
    ErrorCode DEMO_NESTED = new ErrorCode(1_900_000_009, "嵌套吞没");
    ErrorCode DEMO_PHANTOM = new ErrorCode(1_900_000_010, "幻影");
    ErrorCode DEMO_CODE_A = new ErrorCode(1_900_000_011, "调用方A");
    ErrorCode DEMO_CODE_B = new ErrorCode(1_900_000_012, "调用方B");
}
""",
    f"{_DEMO}/controller/admin/DemoAdminController.java": """@RestController
@RequestMapping("/demo/thing")
public class DemoAdminController {
    @Resource
    private DemoService demoService;

    @PutMapping("/update")
    @Operation(summary = "更新")
    @PreAuthorize("@ss.hasPermission('demo:thing:update')")
    public CommonResult<Boolean> update(@RequestBody DemoSaveReqVO reqVO) {
        demoService.updateThing(reqVO);
        return success(true);
    }

    @DeleteMapping("/delete")
    public CommonResult<Boolean> delete(@RequestParam("id") Long id) {
        demoService.deleteThing(id);
        return success(true);
    }
}
""",
    f"{_DEMO}/controller/app/DemoAppController.java": """@RestController
@RequestMapping("/demo/thing")
public class DemoAppController {
    @Resource
    private DemoService demoService;

    @PostMapping("/app-ping")
    public CommonResult<Boolean> ping(@RequestParam("id") Long id) {
        demoService.pingThing(id);
        return success(true);
    }
}
""",
    f"{_DEMO}/service/DemoServiceImpl.java": """package cn.iocoder.yudao.module.demo.service;
import org.flowable.engine.RuntimeService;
import jakarta.annotation.Resource;

public class DemoServiceImpl implements DemoService {
    @Resource
    private RuntimeService runtimeService;
    @Resource
    private DemoRelatedService demoRelatedService;
    @Resource
    private DemoMapper demoMapper;
    @Resource
    private DemoItemMapper demoItemMapper;
    @Resource
    private ApplicationEventPublisher eventPublisher;

    @Override
    public void updateThing(DemoSaveReqVO reqVO) {
        // throw exception(DEMO_PHANTOM); demoRelatedService.deleteRelated(1L);
        String note = "throw exception(DEMO_PHANTOM); demoRelatedService.deleteRelated(2L);";
        DemoDO demo = validateDemoExists(reqVO.getId());
        this.assertUnlocked(demo);
        validateUserNotAdmin(reqVO.getId(), DEMO_ADMIN_FORBIDDEN);
        validateBatch(reqVO.getId());
        try {
            if (reqVO.getFlag()) {
                throw exception(DEMO_RISKY);
            }
        } catch (ServiceException ignored) {
        }
        try {
            if (reqVO.getIo()) {
                throw exception(DEMO_IO_BAD);
            }
        } catch (java.io.IOException e) {
            log.warn("io", e);
        }
        try {
            try {
                if (reqVO.getNested()) {
                    throw exception(DEMO_NESTED);
                }
            } finally {
                cleanup();
            }
        } catch (Exception e) {
            log.warn("nested", e);
        }
        if (Boolean.TRUE.equals(reqVO.getFlag())) {
            throw exception(DEMO_STATE_BAD);
        }
        if (reqVO.getDanger()) throw exception(DEMO_STATE_BAD);
        demoRelatedService.updateRelated2(reqVO.getId());
        for (Long id : new ArrayList<Long>(reqVO.getIds())) {
            demoRelatedService.updateRelated(id);
        }
        if (reqVO.getCascade()) {
            demoItemMapper.deleteByThingId(reqVO.getId());
        }
        eventPublisher.publishEvent(new DemoUpdatedEvent(reqVO.getId()));
        SpringUtil.getBean(OtherService.class).notifyThing(reqVO.getId());
        getSelf().pingThing(reqVO.getId());
        runtimeService.createModelQuery().list();
        demoMapper.deleteById(reqVO.getId());
        demoRelatedService.getRelated(reqVO.getId());
    }

    private DemoService getSelf() {
        return SpringUtil.getBean(getClass());
    }

    @Override
    public void deleteThing(Long id) {
        validateDemoExists(id);
        List<DemoItemDO> items = selectItems(id);
        items.forEach(this::checkItem);
        validateBatch(new ArrayList<Long>());
    }

    @Override
    public void pingThing(Long id) {
        demoMapper.selectById(id);
    }

    DemoDO validateDemoExists(Long id) {
        DemoDO demo = demoMapper.selectById(id);
        if (demo == null) {
            throw exception(DEMO_NOT_EXISTS);
        }
        return demo;
    }

    private void assertUnlocked(DemoDO demo) {
        checkLocked(demo);
    }

    private void checkLocked(DemoDO demo) {
        if (demo.getLocked()) {
            throw exception(DEMO_LOCKED);
        }
    }

    private void checkItem(DemoItemDO item) {
        if (item == null) {
            throw exception(DEMO_ITEM_BAD);
        }
    }

    private void validateUserNotAdmin(Long id, ErrorCode code) {
        if (id == 1L) {
            throw exception(code);
        }
    }

    private void validateBatch(Long id) {
        if (id == null) {
            throw exception(DEMO_SINGLE_BAD);
        }
    }

    private void validateBatch(Collection<Long> ids) {
        if (CollUtil.isEmpty(ids)) {
            throw exception(DEMO_BATCH_BAD);
        }
    }
}
""",
    # Two callers share one parameterized-code validator: whichever caller the
    # walk reaches second must still recover its own call-site constant.
    f"{_DEMO}/service/ExtraServiceImpl.java": """package cn.iocoder.yudao.module.demo.service;

public class ExtraServiceImpl implements ExtraService {
    public void opA(Long id) {
        first(id);
        second(id);
    }

    private void first(Long id) {
        validate(id, DEMO_CODE_A);
    }

    private void second(Long id) {
        validate(id, DEMO_CODE_B);
    }

    private void validate(Long id, ErrorCode code) {
        if (id == 1L) {
            throw exception(code);
        }
    }
}
""",
}


class YudaoSourceExtractionTests(unittest.TestCase):
    """The Java extraction itself: nested modules, helper chains, overloads."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for relative, text in _EXTRA_FILES.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.source = YudaoSource(self.root, release="v2026.08")

    def tearDown(self):
        self._tmp.cleanup()

    def _codes(self, method):
        refusals = self.source._refusals(self.source._messages())
        return {r.code for r in refusals.get(("DemoService", method), ())}

    def _calls(self, method):
        cascades = self.source._cascades()
        return {(service, name) for service, name, *rest in
                cascades.get(("DemoService", method), {}).get("calls", ())}

    def _cascade_entries(self, method):
        return self.source._cascades().get(("DemoService", method), {})

    def _unknown_kinds(self, method):
        return {k for k, _p, _l in
                self._cascade_entries(method).get("untraceable", ())}

    def test_nested_module_is_discovered_and_mapped(self):
        # A nested module registers under its slash key ("mall/demo").
        self.assertIn("mall/demo", self.source.modules)
        base = self.source._module_base("mall/demo")
        self.assertEqual(base.resolve(),
                         (self.root / "server/yudao-module-mall/yudao-module-demo/src/main/java").resolve())
        operations = self.source._operations()
        self.assertIn("demo/thing/update", operations)
        service, method, _permission, _summary, verb, path = operations["demo/thing/update"]
        self.assertEqual((service, method), ("DemoService", "updateThing"))
        self.assertEqual(verb, "Put")
        self.assertIn("/controller/admin/", path)

    def test_member_controller_is_kept_out_of_admin_writes(self):
        operations = self.source._operations()
        service, method, _p, _s, verb, path = operations["demo/thing/app-ping"]
        self.assertEqual(verb, "Post")
        self.assertIn("/controller/app/", path)
        admin_writes = [url for url, op in operations.items()
                        if "/controller/admin/" in op[5] and op[4] != "Get"]
        self.assertEqual(sorted(admin_writes), ["demo/thing/delete", "demo/thing/update"])

    def test_package_private_and_chained_helpers_contribute(self):
        codes = self._codes("updateThing")
        # Package-private validateDemoExists, then assertUnlocked -> checkLocked.
        self.assertIn("DEMO_NOT_EXISTS", codes)
        self.assertIn("DEMO_LOCKED", codes)

    def test_control_keywords_are_not_parsed_as_methods(self):
        from web_operation_agent_yudao.rules.source import _methods
        text = (self.root / f"{_DEMO}/service/DemoServiceImpl.java").read_text()
        names = {name for name, *_ in _methods(text)}
        self.assertFalse(names & {"if", "for", "while", "switch", "return", "new", "throw"})

    def test_parameterized_error_code_is_recovered_at_the_call_site(self):
        self.assertIn("DEMO_ADMIN_FORBIDDEN", self._codes("updateThing"))

    def test_overloaded_helpers_resolve_by_arity_and_argument_shape(self):
        update = self._codes("updateThing")
        # updateThing calls validateBatch(reqVO.getId()): only the Long overload.
        self.assertIn("DEMO_SINGLE_BAD", update)
        self.assertNotIn("DEMO_BATCH_BAD", update)
        delete = self._codes("deleteThing")
        # deleteThing calls validateBatch(new ArrayList<>()): only the Collection overload.
        self.assertIn("DEMO_BATCH_BAD", delete)
        self.assertNotIn("DEMO_SINGLE_BAD", delete)

    def test_method_reference_is_followed(self):
        self.assertIn("DEMO_ITEM_BAD", self._codes("deleteThing"))

    def test_a_swallowed_exception_is_not_a_rule(self):
        codes = self._codes("updateThing")
        self.assertNotIn("DEMO_RISKY", codes)    # catch (ServiceException) swallows
        self.assertNotIn("DEMO_NESTED", codes)   # outer catch (Exception) swallows
        self.assertIn("DEMO_STATE_BAD", codes)
        # catch (java.io.IOException) does not cover ServiceException: kept.
        self.assertIn("DEMO_IO_BAD", codes)

    def test_comments_and_strings_do_not_fabricate_rules(self):
        codes = self._codes("updateThing")
        self.assertNotIn("DEMO_PHANTOM", codes)
        self.assertNotIn(("DemoRelatedService", "deleteRelated"),
                         {(s, m) for s, m, *_ in self._cascade_entries("updateThing")["calls"]})

    def test_shared_validator_recovers_every_callers_code(self):
        refusals = self.source._refusals(self.source._messages())
        codes = {r.code for r in refusals.get(("ExtraService", "opA"), ())}
        self.assertTrue({"DEMO_CODE_A", "DEMO_CODE_B"} <= codes, codes)

    def test_this_qualified_helper_is_followed(self):
        # updateThing reaches assertUnlocked -> checkLocked only through `this.`.
        self.assertIn("DEMO_LOCKED", self._codes("updateThing"))

    def test_cascades_keep_business_services_only(self):
        # The business service mutation and the item table's mapper write are
        # other entities; the own-record mapper, the read verb and the foreign
        # engine service stay out.
        self.assertEqual(self._calls("updateThing"),
                         {("DemoRelatedService", "updateRelated"),
                          ("DemoRelatedService", "updateRelated2"),
                          ("DemoItemMapper", "deleteByThingId")})
        # deleteThing mutates nothing else: the mapper write stays with the record.
        self.assertEqual(self._calls("deleteThing"), set())

    def test_a_cascade_under_a_branch_says_so(self):
        entries = self._cascade_entries("updateThing")["calls"]
        item = next(e for e in entries if e[1] == "deleteByThingId")
        self.assertEqual(item[5], "reqVO.getCascade()")
        plain = next(e for e in entries if e[1] == "updateRelated")
        self.assertEqual(plain[5], "")
        # An unbraced `if (c) throw ...;` ends at its `;`: the following
        # unconditional call must not inherit the condition.
        after = next(e for e in entries if e[1] == "updateRelated2")
        self.assertEqual(after[5], "")

    def test_untraceable_marks_dynamic_paths_but_not_self_lookup(self):
        kinds = self._unknown_kinds("updateThing")
        self.assertIn("event", kinds)          # eventPublisher.publishEvent(...)
        self.assertIn("dynamic-bean", kinds)   # getBean(OtherService.class)
        self.assertNotIn("async", kinds)
        # SpringUtil.getBean(getClass()) is a self lookup whose calls stay traced.
        self.assertEqual(len(kinds), 2)

    def test_cascades_land_on_every_owner_and_nowhere_without_one(self):
        two = (EntitySource(key="demo/thing", component_dir="demo/thing", api_base="demo/thing",
                            labels={}, choices={}),
               EntitySource(key="demo/thing-b", component_dir="demo/thing", api_base="demo/thing",
                            labels={}, choices={}))
        rules, _ = self.source.rules(two)
        for key in ("demo/thing", "demo/thing-b"):
            cascades = [r for r in rules[key]
                        if r.kind == "cascade" and r.subject == "demo/thing/update"]
            self.assertTrue(any("DemoRelated.updateRelated" in r.text for r in cascades))
            self.assertTrue(any("DemoItem.deleteByThingId" in r.text
                                and "满足某条件时" in r.text for r in cascades))
            self.assertTrue(any("未知" in r.text for r in cascades))
        empty_rules, _ = self.source.rules(())
        self.assertEqual(empty_rules, {})


class MaskedSourceTests(unittest.TestCase):
    """After masking, a file whose bulk is a text block or long comment still
    parses in linear time: the blanked region must not hand the signature
    pattern a quadratic runway (PersonServiceImpl's embedded CSV froze the
    audit before the bounds existed)."""

    def test_a_file_dominated_by_a_text_block_still_parses_its_methods(self):
        csv = "\n".join("            " + ",".join(
            f"cell{i}" for i in range(8)) for _ in range(400))
        text = ("""public class CsvServiceImpl {
    private static final String CSV_DATA = \"\"\"
""" + csv + """
            \"\"\";
    public Person getPersonById(Long id) {
        return personStore.get(id);
    }
    public void deletePerson(Long id) {
        personStore.remove(id);
    }
}
""")
        import time
        t0 = time.time()
        found = [name for name, *_ in _methods(_mask(text))]
        self.assertLess(time.time() - t0, 5.0)
        self.assertEqual(found, ["getPersonById", "deletePerson"])

    def test_the_type_name_gap_survives_a_wrapped_signature(self):
        text = """public class WrapServiceImpl {
    public Map<String, Integer>
            computeTotal(Long id) {
        return null;
    }
}
"""
        self.assertEqual([name for name, *_ in _methods(_mask(text))], ["computeTotal"])


if __name__ == "__main__":
    unittest.main()
