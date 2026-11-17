# OA、BPM 与系统管理域（oa-bpm-system）

- **实体边界**：
  - **消息与公告三区分（与部署约定一致）**：未特指 OA 且不在 OA 页面时，“公告”指系统通知公告（`system/notice`）中 `公告类型=公告` 的记录，“通知公告”指 `system/notice` 全部记录（含通知与公告）；明确说“OA 公告”或当前正在 OA 办公协同页面时指 `oa/announcement`；站内信（`system/notify-message`，`my-page` 查本人收件箱）独立于前两者。
  - OA 日程（`oa/schedule`）、会议室档案（`oa/meeting-room`）与会议室预约（`oa/meeting-room-booking`）各自独立；PMS 项目（`pms/pm/project`）、工作项（`pms/pm/work-item`）与知识库（`pms/kb/library`、`pms/kb/document`）属于项目与知识管理域。
  - BPM 流程分类（`bpm/category`）、流程定义、我的流程（`bpm/process-instance/my-page`）、待办/已办任务（`bpm/task/todo-page`、`done-page`）。
  - 系统账号（`system/user`，显示名为 `nickname`、部门为 `deptId`）、系统部门（`system/dept`）、系统岗位（`system/post`）、角色（`system/role`）、登录日志/操作日志。
- **可用类型化写入**：`oa-announcement-publication`、`system-notice-maintenance`、`system-dept-creation`（含新建与修改 `system_dept_create`/`system_dept_update`）、`system-post-creation`（含新建与修改 `system_post_create`/`system_post_update`）、`bpm-category-maintenance`；OA 日程等无已装 Skill 操作时走页面原生表单。
