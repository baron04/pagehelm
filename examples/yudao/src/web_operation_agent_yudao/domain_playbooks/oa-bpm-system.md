# OA、BPM 与系统管理域（oa-bpm-system）

- **实体边界**：
  - **消息与公告三区分**：OA 公告（`oa/announcement`）是行政发布；系统通知公告（`system/notice`）是后台通知模板/记录；站内信（`system/notify-message`）是用户收件箱消息（`my-page` 查本人站内信）。
  - OA 日程（`oa/schedule`）与会议（`oa/meeting`）各自独立。
  - BPM 流程分类（`bpm/category`）、流程定义、我的流程（`bpm/process-instance/my-page`）、待办/已办任务（`bpm/task/todo-page`、`done-page`）。
  - 系统账号（`system/user`，显示名为 `nickname`、部门为 `deptId`）、系统部门（`system/dept`）、系统岗位（`system/post`）、角色（`system/role`）、登录日志/操作日志。
- **可用类型化写入**：`oa-announcement-publication`、`system-notice-maintenance`、`system-dept-creation`、`system-post-creation`、`bpm-category-maintenance`、`system_dept_update`、`system_post_update`、`oa_schedule_create`、`oa_schedule_update`。
