# CRM 销售域（crm-sales）

- **实体边界**：CRM 客户（`crm/customer`）不同于 ERP 销售往来客户（`erp/customer`）；CRM 联系人（`crm/contact`）是独立联系人档案；CRM 产品（`crm/product`）不同于 ERP 产品（`erp/product`）与商城商品（`product/spu`）。
- **页签与范围口径**：
  - `sceneType`：`1`=我负责的、`2`=我参与的、`3`=下属负责的；问“全部/全公司”时不传 `sceneType`（仅限当前账号可见）。
  - **公海客户**：`crm/customer/pool-page`（或 `pool=true`），领取用 `receive`、放入公海用 `put-pool`；公海时间列按页面可见列核对。
- **核心口径**：
  - 合同 `auditStatus`：`0`=未提交、`10`=审核中、`20`=审核通过（成交）、`30`=审核不通过；新签合同按 `orderDate`（下单日期）统计。
  - 合同应收余额 = 合同总金额 `totalPrice` - 已回款金额 `totalReceivablePrice`。
- **可用类型化写入**：`crm-customer-*`、`crm-contact-*`、`crm-clue-maintenance`、`crm-follow-up`、`crm-receivable-planning`。
