# CRM 销售域（crm-sales）

- **实体边界**：CRM 客户（`crm/customer`）不同于 ERP 销售往来客户（`erp/customer`）；CRM 联系人（`crm/contact`）是独立联系人档案；CRM 产品（`crm/product`）不同于 ERP 产品（`erp/product`）与商城商品（`product/spu`）。
- **页签与范围口径**：
  - `sceneType`：`1`=我负责的、`2`=我参与的、`3`=下属负责的；问“全部/全公司”时不传 `sceneType`（仅限当前账号可见）。
  - **公海客户**：`crm/customer/page` 传 `pool=true`；领取用 `crm_customer_receive`、放入公海用 `crm_customer_put_pool`；公海时间列按页面可见列核对。
- **核心口径**：
  - 合同 `auditStatus`：`0`=未提交、`10`=审核中、`20`=审核通过、`30`=审核不通过；新签合同按 `orderDate`（下单日期）统计。
  - **金额单位**：CRM 合同、回款、回款计划、商机与产品接口金额字段（`totalPrice`、`totalReceivablePrice`、`price`）单位均为**元（yuan）**（与 ERP 一致，切勿当作分除以 100；仅商城/会员/支付域使用分），合同待回款差额 = `totalPrice - totalReceivablePrice`（多行差额或汇总用 `sql` 计算）。
- **可用类型化写入**：`crm-customer-*`、`crm-contact-*`、`crm-clue-maintenance`、`crm-follow-up`、`crm-receivable-planning`；实际回款新建请用 `create_record(entity="crm/receivable")`。
- **严禁越权新建前置实体**：办理下游单据（如登记实际回款 `crm/receivable`）时，若所引用的前置计划或父记录（如对应回款计划期次 `crm/receivable-plan` 或合同）不存在或当前账号无权限，必须如实向用户说明原因或使用 `finish(status="need_input")` 追问，**严禁擅自调用 `crm_receivable_plan_create` 或 `create_record` 代建回款计划/合同**；仅在用户明确要求创建回款计划时才使用 `crm_receivable_plan_create`。
