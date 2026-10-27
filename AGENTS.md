# AGENTS.md

## 团队（MVP 只用一个 agent，结构预留多 agent）
- @class-reminder：唯一的智能体，负责对话、课表管理与提醒发送

## 数据流
1. 用户输入（文字/图片/通知转发）→ 对应 Skill 解析 → 写入 data/*.json（需用户确认）
2. HEARTBEAT 唤醒 → scripts/run_scheduler.py --tick
   → expand 展开未来 14 天 Occurrence → scheduler 生成 ReminderTask → 到期发送
3. 用户回复“收到/请假了/ snooze 10分钟”→ scripts/handle_reply.py（src/ack.py 确定性
   解析+定位，默认预览、--apply 才回写）→ 更新对应那次课的 ReminderTask 状态
   （acknowledged / snoozed，请假时同次课未发的任务转 cancelled）；已发送未确认
   超过宽限期的，scheduler 在 tick 里生成一条 follow_up 追问（一次课一条）
4. 用户说“课前提前20分钟/关掉前晚提醒/高数出门提前50分钟”→ scripts/configure.py
   （src/config_edit.py 确定性解析出变更计划，默认预览、用户确认后 --apply 才写入
   user.json/courses.json 并备份）→ scheduler 按 preferences.disabledTypes 与单课
   reminderOverride.disabledTypes 生成任务，被关的类型不生成

## 分工原则
- LLM（DeepSeek）：只做解析与文案
- 确定性代码（src/）：算周次、展开课程、算提醒时间、状态机流转 —— 必须可测试
