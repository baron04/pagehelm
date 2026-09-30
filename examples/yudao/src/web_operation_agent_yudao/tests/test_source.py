"""yudao's source rules: a control's visibility and its operation's refusals, cited."""
from __future__ import annotations

import hashlib
import re
import tempfile
import unittest
from pathlib import Path

from web_operation_agent_yudao.source import EntitySource, YudaoSource

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


if __name__ == "__main__":
    unittest.main()
