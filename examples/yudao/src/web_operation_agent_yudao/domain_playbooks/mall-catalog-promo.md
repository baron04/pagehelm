# 商城商品与营销域（mall-catalog-promo）

- **实体边界**：
  - 商城商品 SPU（`product/spu`）/ SKU 不同于 ERP 产品（`erp/product`）与 CRM 产品（`crm/product`）。
  - 商品分类（`product/category`）：`parentId=0` 为一级分类；新建分类（`product-category-creation`）仅可复用同源已有图片 URL；部分营销活动指定分类范围时要求二级及以下启用分类（以 `entity_rules` 为准）。
  - 营销活动：`promotion/coupon-template`（优惠券模板）、`promotion/seckill-activity`（秒杀活动）、`promotion/reward-activity`（满减送）、`promotion/combination-activity`（拼团）、`promotion/bargain-activity`（砍价）。
- **单位与状态口径**：
  - **关键量纲**：商城商品价格、优惠券金额（`discountPrice`、`usePrice`、`discountLimitPrice`）接口字段单位均为**分（fen）**；答复展示为元时，即便行表已内联（≤30 行）也须通过 `sql`（`÷ 100.0`）换算为元后再引用该回执，确保衍生金额可核对。
  - 启用状态 `status`：`0`=开启、`1`=关闭（`CommonStatusEnum`）；秒杀活动未关闭/未结束前禁止删除。
- **可用类型化写入**：`product-category-creation`、`product-brand-maintenance`（含品牌维护、`promotion_coupon_template_status` 优惠券模板启停与 `promotion_seckill_activity_close` 秒杀活动关闭）。
