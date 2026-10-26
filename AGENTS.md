# AGENTS.md

## 团队（MVP 只用一个 agent，结构预留多 agent）
- @class-reminder：唯一的智能体，负责对话、课表管理与提醒发送

## 数据流
1. 用户输入（文字/图片/通知转发）→ 对应 Skill 解析 → 写入 data/*.json（需用户确认）
2. HEARTBEAT 唤醒 → scripts/run_scheduler.py --tick
   → expand 展开未来 14 天 Occurrence → scheduler 生成 ReminderTask → 到期发送
3. 用户回复“收到/请假了/ snooze 10分钟”→ 更新对应 ReminderTask / Occurrence 状态

## 分工原则
- LLM（DeepSeek）：只做解析与文案
- 确定性代码（src/）：算周次、展开课程、算提醒时间、状态机流转 —— 必须可测试
