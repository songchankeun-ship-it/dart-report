@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs
echo ==== DART daily run  %date% %time% ====
echo (close this window only after it says DONE)
echo [%date% %time%] run start >> logs\run.log
if /i "%~1"=="first" (
  ".venv\Scripts\python.exe" weekly.py --days 7 --since-last --no-llm --prefix 老老傍矫 --out reports\daily --stocks-link 2>&1 | ".venv\Scripts\python.exe" -u tee_log.py logs\run.log
) else (
  ".venv\Scripts\python.exe" weekly.py --days 3 --since-last --no-llm --prefix 老老傍矫 --out reports\daily --stocks-link 2>&1 | ".venv\Scripts\python.exe" -u tee_log.py logs\run.log
)
if not exist ".dart_cache\industry_map.json" (
  echo building industry map - first time only, 60-90 minutes
  ".venv\Scripts\python.exe" industry.py build 2>&1 | ".venv\Scripts\python.exe" -u tee_log.py logs\run.log
)
".venv\Scripts\python.exe" riskboard.py --watchlist watchlist.txt --out reports\stocks 2>&1 | ".venv\Scripts\python.exe" -u tee_log.py logs\run.log
findstr /R "^TELEGRAM_BOT_TOKEN=.." dart.env >nul 2>&1 && ".venv\Scripts\python.exe" alerts.py --once --min-importance 3 2>&1 | ".venv\Scripts\python.exe" -u tee_log.py logs\run.log
echo [%date% %time%] run end >> logs\run.log
echo.
echo ==== DONE  %date% %time% ====   reports\daily\index.html
if /i not "%~1"=="first" if /i not "%~1"=="quiet" timeout /t 15
