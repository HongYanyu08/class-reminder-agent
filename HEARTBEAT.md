# Scheduled Tasks

## Scheduler Tick
- Schedule: Every 5 minutes
- Agent: @class-reminder
- Message: 运行 scripts/run_scheduler.py --tick --send：展开未来14天课表、生成提醒任务、发送所有到期且未确认的提醒。若输出含 [SESSION-SPEAK] 行（session 主会话投递通道），必须逐条把该行文字（去掉 `[SESSION-SPEAK] <taskId>` 前缀后的提醒正文）作为独立消息原样发到当前会话，不要改动文字；全部发完后，原样运行输出里给出的 `python3 scripts/run_scheduler.py --confirm-sent <id,...>` 命令回写确认。无 [SESSION-SPEAK] 行时不发送任何消息。若部署时开启了备用通道（Server酱/PushPlus），排队超时未确认的提醒由 tick 内部自动补发，输出会有 [备用通道] 日志行——朗读协议不变，agent 不需要为此做任何额外动作，被补发的任务也不会出现在 [SESSION-SPEAK] 里。

## Nightly Check
- Schedule: Every day at 21:00
- Agent: @class-reminder
- Message: 检查明天课程，发送前一晚预告（night_before），并校验明天数据是否有待确认的调课例外，有则提醒用户确认。
