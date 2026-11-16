# FMS 财务总账域（fms-accounting）

- **实体边界**：
  - 财务账套（`fms/config/account-set`）、结账会计期间（`fms/closing/period`）、会计科目（`fms/config/subject` 或 `search_api` 查具体路径）、记账凭证（`fms/voucher`）、总账/明细账/资产负债表/利润表属于 FMS 总账域，不同于 ERP 资金结算单（`erp/finance-*`）。
- **必带上下文口径**：
  - 查询会计科目、记账凭证、账簿或财务报表时，先用 `search_api` 核对接口声明的必带查询参数（如 `accountSetId` 及已发布的月份/期间参数 `month`、`startMonth`、`endMonth` 等）；先读当前账套与期间列表再带精确参数查询，不猜测期间参数名或值。
  - 财务报表行读取需核对报表模板与期间，金额单位与方向（借/贷）按接口与页面列头核验。
