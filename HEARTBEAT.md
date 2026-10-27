# Scheduled Tasks

## Scheduler Tick
- Schedule: Every 5 minutes
- Agent: @class-reminder
- Message: 运行 scripts/run_scheduler.py --tick --send：展开未来14天课表、生成提醒任务、发送所有到期且未确认的提醒。若输出含 [SESSION-SPEAK] 行（session 主会话投递通道），必须逐条把该行文字（去掉 `[SESSION-SPEAK] <taskId>` 前缀后的提醒正文）作为独立消息原样发到当前会话，不要改动文字；全部发完后，原样运行输出里给出的 `python3 scripts/run_scheduler.py --confirm-sent <id,...>` 命令回写确认。无 [SESSION-SPEAK] 行时不发送任何消息。

## Nightly Check
- Schedule: Every day at 21:00
- Agent: @class-reminder
- Message: 检查明天课程，发送前一晚预告（night_before），并校验明天数据是否有待确认的调课例外，有则提醒用户确认。
