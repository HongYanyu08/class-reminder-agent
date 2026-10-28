@echo off
rem ============================================================
rem  Class reminder - scheduled tick entry (event-driven setup).
rem  Called by Windows Task Scheduler job ClassReminderTick
rem  every 5 minutes. All paths derive from this script's own
rem  location, so moving the project folder needs no edits here.
rem ============================================================
rem  Step 1: deterministic tick - expand the timetable, build
rem  reminders, queue the due ones (session channel), escalate
rem  stale queued ones through the fallback channel. All of it
rem  runs inside the script; it never depends on the main
rem  session being awake. --from defaults to today in the
rem  term timezone.
rem  Step 2: speak trigger - only when queued reminders are
rem  actually waiting does it fire a one-shot wake for the main
rem  session to speak them; nothing queued means silent exit.
rem  Order matters: the trigger reads the queued state that
rem  the tick has just written.
rem  The two steps do not block each other: even if the tick
rem  fails, the trigger still runs, because the queue may hold
rem  speak-pending items left over from a previous round.
cd /d %~dp0..
py scripts\run_scheduler.py --tick --send
py scripts\speak_trigger.py
