@echo off
rem ============================================================
rem  Register the Windows scheduled task ClassReminderTick
rem  (every 5 minutes). The target path derives from this
rem  script's location (%~dp0), so re-running it after moving
rem  the project folder re-registers the new path. /F
rem  overwrites an existing task of the same name, so this
rem  script is safe to run repeatedly.
rem ============================================================
schtasks /Create /TN "ClassReminderTick" /TR "\"%~dp0tick_task.cmd\"" /SC MINUTE /MO 5 /F
if errorlevel 1 (
  echo [FAILED] Task registration failed. Check the window permissions and retry.
  exit /b 1
)
echo [DONE] Scheduled task ClassReminderTick registered (runs tick_task.cmd every 5 minutes).
echo        View:    schtasks /Query /TN "ClassReminderTick"
echo        Run now: schtasks /Run /TN "ClassReminderTick"
