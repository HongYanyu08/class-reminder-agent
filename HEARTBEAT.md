# Scheduled Tasks

## Scheduler Tick
- Schedule: Every 5 minutes
- Agent: @class-reminder
- Message: 运行 scripts/run_scheduler.py --tick：展开未来14天课表、生成提醒任务、发送所有到期且未确认的提醒；无到期任务时不发送任何消息。

## Nightly Check
- Schedule: Every day at 21:00
- Agent: @class-reminder
- Message: 检查明天课程，发送前一晚预告（night_before），并校验明天数据是否有待确认的调课例外，有则提醒用户确认。
