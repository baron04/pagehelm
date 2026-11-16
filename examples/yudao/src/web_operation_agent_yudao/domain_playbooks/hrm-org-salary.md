# HRM 人事与薪资域（hrm-org-salary）

- **实体边界**：
  - HRM 员工花名册（`hrm/employee`）使用 `name` 字段，**不同于**系统账号（`system/user`）的 `nickname`（显示名）与 `username`。
  - 招聘岗位（`hrm/recruit-post`）不同于系统岗位（`system/post`）。
  - 薪资档案（`hrm/employee/salary-card`）、定调薪记录（`hrm/salary/change-record`）、月度工资表（`hrm/salary/month-record`）、月度社保表（`hrm/insurance/month-record`）与考勤统计各自独立。
- **状态与拒绝规则口径**：
  - 月度工资表已归档/历史状态（`HISTORY`）禁止删除或重算，且系统仅允许删除最新一期月度工资表并至少保留一张（用 `entity_rules` 核对）。
  - 部门筛选在页面上可通过部门树（`ElTree` / `ElTreeSelect`）选择节点。
