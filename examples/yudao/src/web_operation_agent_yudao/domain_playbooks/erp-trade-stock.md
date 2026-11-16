# ERP 进销存域（erp-trade-stock）

- **实体边界**：
  - ERP 客户（`erp/customer`）、ERP 供应商（`erp/supplier`）、ERP 产品（`erp/product`）均为进销存主档，与 CRM、商城、WMS 规格库存各自独立。
  - 单据区分：采购订单（`erp/purchase-order`）/ 采购入库（`erp/purchase-in`）/ 采购退货（`erp/purchase-return`）；销售订单（`erp/sale-order`）/ 销售出库（`erp/sale-out`）/ 销售退货（`erp/sale-return`）；其它入库（`erp/stock-in`）/ 其它出库（`erp/stock-out`）/ 库存调拨（`erp/stock-move`）/ 库存盘点（`erp/stock-check`）。
- **状态与单位口径**：
  - 单据审批状态 `status`：`10`=未审核、`20`=已审核；已审核单据通常禁止直接修改或删除（用 `entity_rules` 核对）。
  - 价格、金额单位为**元**（不换算为分）；重量单位为 kg，保质期为天。
  - 采购/销售单据按单号 `no` 查询或定位，明细行在 `/get` 详情的 `items` 中。
- **可用类型化写入**：`erp-supplier-maintenance`、`erp-customer-maintenance`、`erp-product-maintenance`（含 ERP 产品与计量单位 `erp_product_unit_create`/`update`）、`erp-warehouse-maintenance`（含仓库、结算账户与 12 类单据状态流转 `erp_document_status`）。
