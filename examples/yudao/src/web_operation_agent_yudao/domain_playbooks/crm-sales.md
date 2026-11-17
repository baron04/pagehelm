# CRM 销售域（crm-sales）

- **实体边界**：CRM 客户（`crm/customer`）不同于 ERP 销售往来客户（`erp/customer`）；CRM 联系人（`crm/contact`）是独立联系人档案；CRM 产品（`crm/product`）不同于 ERP 产品（`erp/product`）与商城商品（`product/spu`）。
- **页签与范围口径**：
  - `sceneType`：`1`=我负责的、`2`=我参与的、`3`=下属负责的；问“全部/全公司”时不传 `sceneType`（仅限当前账号可见）。
  - **公海客户**：`crm/customer/page` 传 `pool=true`；领取用 `crm_customer_receive`、放入公海用 `crm_customer_put_pool`；公海时间列按页面可见列核对。
- **核心口径**：
  - 合同 `auditStatus`：`0`=未提交、`10`=审核中、`20`=审核通过（成交）、`30`=审核不通过；新签合同按 `orderDate`（下单日期）统计。
  - **金额单位**：CRM 合同、回款、回款计划、商机与产品接口金额字段（`totalPrice`、`totalReceivablePrice`、`price`）单位均为**元（yuan）**（与 ERP 一致，切勿当作分除以 100；仅商城/会员/支付域使用分），合同待回款差额 = `totalPrice - totalReceivablePrice`（多行差额或汇总用 `sql` 计算）。
- **可用类型化写入**：`crm-customer-*`、`crm-contact-*`、`crm-clue-maintenance`、`crm-follow-up`、`crm-receivable-planning`。
