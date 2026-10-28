@echo off
rem ============================================================
rem  上课提醒 · 定时 tick 入口（根治版自动化）
rem  由 Windows 任务计划 ClassReminderTick 每 5 分钟调用本脚本。
rem  路径全部由本脚本位置推导，项目搬家不需要改这里。
rem ============================================================
rem  第 1 步：确定性 tick —— 展开课表、生成提醒、到期排队（session
rem  通道）、排队超时未确认自动走备用通道补发。全部在脚本内完成，
rem  不依赖小课主会话醒着没有。--from 缺省即 term 时区的今天。
rem  第 2 步：说话触发器 —— 只有真有 queued 待念提醒时才一次性
rem  唤醒主会话让小课念出来；没有待念就静默退出，不打扰。
rem  顺序不能反：触发器只看 tick 刚写入的 queued 状态。
rem  两步互不阻断：tick 失败也照样跑触发器，因为队列里可能还有
rem  上一轮遗留的待念任务正等着被念出来。
cd /d %~dp0..
py scripts\run_scheduler.py --tick --send
py scripts\speak_trigger.py
