# ERP 财务结算域（erp-finance）

- **实体边界**：
  - 结算账户（`erp/account`）、收款单（`erp/finance-receipt`）、付款单（`erp/finance-payment`）属于 ERP 进销存资金结算，不同于 FMS 财务总账（账套/凭证/科目）。
  - CRM 回款（`crm/receivable`）不同于 ERP 收款单（`erp/finance-receipt`）。
- **单据与核销口径**：
  - 收/付款单为表头 + `items` 明细结构（关联采购入库/退货或销售出库/退货），`totalPrice` 为实际收/付款金额（单位：元）。
  - `status`：`10`=未审核、`20`=已审核；修改表头字段或审批状态时严禁篡改原 `accountId` 与明细行。
- **可用类型化写入**：`erp_account_create`/`update`、以及 `erp_finance_receipt_status` / `erp_finance_payment_status` 状态流转。
