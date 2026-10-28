@echo off
rem Delete the Windows scheduled task ClassReminderTick
rem (rollback / cleanup for the event-driven setup).
schtasks /Delete /TN "ClassReminderTick" /F
if errorlevel 1 (
  echo [FAILED] Delete failed: the task may not exist, or permissions are insufficient.
  exit /b 1
)
echo [DONE] Scheduled task ClassReminderTick deleted.
