# 商城交易与售后域（mall-trade-aftersale）

- **实体边界**：
  - 交易订单（`trade/order`）与售后退款单（`trade/after-sale`）属于商城交易域，不同于 ERP 销售订单/退货单。
  - 会员体系：`member/user`（会员）、`member/tag`（会员标签）、`member/group`（会员分组）、`member/level`（会员等级）。
- **状态与单位口径**：
  - 售后单 `status`：`10`=申请中（待审核）、`20`=待退货、`30`=待收货、`40`=等待退款、`50`=已完成、`61`=买家取消、`62`=卖家拒绝、`63`=卖家拒收货。
  - 订单与退款金额字段（`payPrice`、`refundPrice`）接口单位均为**分（fen）**；答复展示为元或汇总时须用 `sql`（`÷ 100.0`）换算并引用该计算回执。
- **可用类型化写入**：`member-tag-creation`、`member-group-creation`（覆盖新建；修改已有分组/标签/等级备注或属性、订单及售后页内操作等无已装 Skill 操作时走对应页面原生表单或行按钮，切勿因无更新 Skill 而拒绝）。
