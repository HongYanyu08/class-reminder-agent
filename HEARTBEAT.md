# Scheduled Tasks

## 例行 tick：已移交 Windows 任务计划（不要再用 cron 唤醒主会话跑 tick）

从「根治版自动化」起，每 5 分钟的例行检查由 **Windows 任务计划
ClassReminderTick** 直接运行 `scripts\tick_task.cmd`：先跑确定性 tick
（`run_scheduler.py --tick --send`：展开课表、生成提醒、到期排队、
排队超时走备用通道补发），再跑说话触发器
（`scripts\speak_trigger.py`）。旧的每 5 分钟唤醒主会话的 cron
任务 **cr-tick-5min 应停用/删除**（切换与回滚步骤见 DEPLOY.md
「根治版自动化（任务计划）」一节）。

为什么搬家：5 分钟一次的 agent 回合会把主会话越撑越大、越跑越慢，
2026-10-28 就因此堵死过一下午的 tick，体育课提醒没能排队而丢失。
确定性的活归系统定时任务，智能体只在有话要说时才被叫醒。

## 主会话职责（待念流程）：被 cr-speak-* 一次性唤醒时，只做「念」这一件事

说话触发器发现有 queued 待念提醒时，会用一条 `cr-speak-<时间戳>`
一次性 cron 把主会话唤醒。被这类唤醒叫醒时：

1. 在项目目录运行 `py scripts\run_scheduler.py --speak-pending`；
2. 把输出中 `[SESSION-SPEAK]` 区块的每一行（去掉 `[SESSION-SPEAK]
   <taskId>` 前缀后的提醒正文）**原样、逐条作为独立消息**发到当前
   微信会话，不要改动文字；
3. 全部发完后，原样运行输出末尾 `[SESSION-CONFIRM]` 行给出的
   `py scripts\run_scheduler.py --confirm-sent <id,...>` 命令回写确认。

若输出只有一行 `[SESSION-SPEAK] （无）`：**只回复 NO_REPLY，不要
输出任何其他文字**。特别禁止把「无提醒，保持沉默」之类的内部状态
语发给用户——2026-10-28 16:52 真的发生过一次，那不是提醒，是噪音。

备用通道（Server酱/PushPlus）的补发由 tick 内部自动完成，被补发
的任务状态已是 sent、不会出现在待念清单里，不要替它们做任何确认。

## 前晚预告（21:00）

night_before 提醒由 tick 按时生成并排进同一条待念链路（任务计划 →
触发器 → 主会话念出），不再有单独的 agent 定时检查。明天课程的
调课例外如有待确认项，仍在日常对话里提醒用户确认，不要编造。
