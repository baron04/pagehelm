# HRM 人事与薪资域（hrm-org-salary）

- **实体边界**：
  - HRM 员工花名册（`hrm/employee`）使用 `name` 字段，**不同于**系统账号（`system/user`）的 `nickname`（显示名）与 `username`。
  - 招聘岗位（`hrm/recruit/post`，字段 `postName`、`recruitNum` 招聘人数、`hasEntryNum` 已入职人数、`recruitSchedule` 招聘进度）、候选人（`hrm/recruit/candidate`）、面试（`hrm/recruit/interview`）不同于系统岗位（`system/post`）；HRM 域新增或流转操作无已装 Skill 操作时走对应页面原生表单或行按钮。
  - 员工薪资档案/信息（`hrm/salary/employee-info`）、员工工资卡/银行卡（`hrm/employee/salary-card`）、定调薪记录（`hrm/salary/change-record`）、月度工资表汇总（`hrm/salary/month-record`）、员工月度工资明细（`hrm/salary/month-employee-record`）与月度社保表（`hrm/insurance/month-record`）各自独立，切勿把工资银行卡当作薪资档案。
- **状态与拒绝规则口径**：
  - 月度工资表已归档/历史状态（`HISTORY`）禁止删除或重算，且系统仅允许删除最新一期月度工资表并至少保留一张（用 `entity_rules` 核对）。
  - 部门筛选在页面上可通过部门树（`ElTree` / `ElTreeSelect`）选择节点。
