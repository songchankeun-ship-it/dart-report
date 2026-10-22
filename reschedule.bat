@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$a = New-ScheduledTaskAction -Execute '%~dp0run_daily.bat' -Argument 'quiet' -WorkingDirectory '%~dp0';" ^
  "$t = New-ScheduledTaskTrigger -Daily -At 7:00;" ^
  "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 2);" ^
  "Register-ScheduledTask -TaskName 'DART_Daily_Report' -Action $a -Trigger $t -Settings $s -Force | Out-Null;" ^
  "Get-ScheduledTask -TaskName 'DART_Daily_Report' | Get-ScheduledTaskInfo | Format-List LastRunTime,LastTaskResult,NextRunTime"
echo.
echo (LastTaskResult 0 = ok, 267011 = never ran yet)
pause
