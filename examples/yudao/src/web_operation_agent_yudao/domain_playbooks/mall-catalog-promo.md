# 商城商品与营销域（mall-catalog-promo）

- **实体边界**：
  - 商城商品 SPU（`product/spu`）/ SKU 不同于 ERP 产品（`erp/product`）与 CRM 产品（`crm/product`）。
  - 商品分类（`product/category`）：`parentId=0` 为一级分类；创建 SPU 须使用二级及以下且启用状态的分类，图片 URL 仅可复用同源已有图片。
  - 营销活动：`promotion/coupon-template`（优惠券模板）、`promotion/seckill-activity`（秒杀活动）、`promotion/reward-activity`（满减送）、`promotion/combination-activity`（拼团）、`promotion/bargain-activity`（砍价）。
- **单位与状态口径**：
  - **关键量纲**：商城商品价格、优惠券金额（`discountPrice`、`usePrice`、`discountLimitPrice`）接口字段单位均为**分（fen）**，展示金额需按已核验单位换算为元（÷100）。
  - 启用状态 `status`：`0`=开启、`1`=关闭（`CommonStatusEnum`）；秒杀活动未关闭/未结束前禁止删除。
- **可用类型化写入**：`product-brand-maintenance`、`product-category-creation`、`promotion_coupon_template_status`、`promotion_seckill_activity_close`。
