@echo off
setlocal
cd /d "%~dp0"
if not exist logs mkdir logs
echo [%date% %time%] install start >> logs\install.log
echo ============================================
echo  DART disclosure report - one-time setup
echo ============================================

rem ---- 0. API key -> .env (created locally on this PC)
:askkey
if exist ".env" goto :haskey
if exist "dart.env" goto :haskey
echo.
echo  DART API key is needed (opendart.fss.or.kr - 40 characters).
echo  Right-click in this window to paste, then press Enter.
set "DARTKEY="
set /p "DARTKEY=  Key: "
if not defined DARTKEY ( echo  (nothing entered - try again) & goto :askkey )
> ".env" echo DART_API_KEY=%DARTKEY%
>> ".env" echo ANTHROPIC_API_KEY=
>> ".env" echo TELEGRAM_BOT_TOKEN=
>> ".env" echo TELEGRAM_CHAT_ID=
echo  .env created.
:haskey

rem ---- 1. find Python (python / py launcher / default install path), install via winget if missing
set "PY="
python -c "import sys" >nul 2>&1 && set "PY=python"
if not defined PY ( py -3 -c "import sys" >nul 2>&1 && set "PY=py -3" )
if not defined PY if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PY (
  echo [1/5] Python not found - installing Python 3.12 with winget...
  winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
  set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
)
echo [1/5] Python: %PY%

rem ---- 2. virtual env + packages
if not exist ".venv\Scripts\python.exe" (
  echo [2/5] creating virtual environment...
  %PY% -m venv .venv || goto :fail
)
echo [3/5] installing packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q || goto :fail

rem ---- 3. offline self-test
echo [4/5] self-test...
set PYTHONIOENCODING=utf-8
for %%t in (test_offline test_parsers test_parsers2 test_weekly_offline test_alerts_offline) do (
  ".venv\Scripts\python.exe" %%t.py >nul 2>&1 && echo    %%t OK || echo    %%t FAILED
)

rem ---- 4. daily task 07:00 (runs later if PC was off at 07:00)
echo [5/5] registering daily task 07:00 ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$a = New-ScheduledTaskAction -Execute '%~dp0run_daily.bat' -Argument 'quiet' -WorkingDirectory '%~dp0';" ^
  "$t = New-ScheduledTaskTrigger -Daily -At 7:00;" ^
  "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 1);" ^
  "Register-ScheduledTask -TaskName 'DART_Daily_Report' -Action $a -Trigger $t -Settings $s -Force | Out-Null" || goto :fail
echo    task 'DART_Daily_Report' registered.

rem ---- 5. first run now (last 7 days)
echo.
echo First run (last 7 days)... this takes a few minutes the first time.
call "%~dp0run_daily.bat" first
start "" "%~dp0reports\daily\index.html"
echo.
echo DONE. Reports: %~dp0reports\daily\index.html
pause
exit /b 0

:fail
echo [%date% %time%] FAILED >> logs\install.log
echo.
echo *** SETUP FAILED - see messages above ***
pause
exit /b 1
