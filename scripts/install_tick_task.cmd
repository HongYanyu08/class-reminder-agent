@echo off
rem ============================================================
rem  注册 Windows 任务计划：ClassReminderTick（每 5 分钟）
rem  运行对象由本脚本位置（%~dp0）推导，项目搬家后重跑即可；
rem  /F 表示同名任务已存在时覆盖更新，本脚本可重复运行。
rem ============================================================
schtasks /Create /TN "ClassReminderTick" /TR "\"%~dp0tick_task.cmd\"" /SC MINUTE /MO 5 /F
if errorlevel 1 (
  echo [失败] 任务注册失败，请确认窗口权限后重试。
  exit /b 1
)
echo [完成] 已注册任务计划 ClassReminderTick（每 5 分钟运行 tick_task.cmd）。
echo        查看：schtasks /Query /TN "ClassReminderTick"
echo        立即试跑：schtasks /Run /TN "ClassReminderTick"
