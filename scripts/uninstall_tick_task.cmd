@echo off
rem 删除 Windows 任务计划 ClassReminderTick（根治版自动化的回滚/清理用）。
schtasks /Delete /TN "ClassReminderTick" /F
if errorlevel 1 (
  echo [失败] 删除失败：任务可能不存在，或权限不足。
  exit /b 1
)
echo [完成] 已删除任务计划 ClassReminderTick。
