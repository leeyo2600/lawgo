@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"
if not exist "runtime\python.exe" (
  echo [ERROR] Bundled Python not found. Extract the entire ZIP.
  pause
  exit /b 1
)
if not exist "input\input.json" (
  echo [INFO] This optional CLI test requires input\input.json.
  pause
  exit /b 2
)
"%~dp0runtime\python.exe" "%~dp0run_full.py" --limit 10 --llm-mode rules --offline-api --output output\offline_10.xlsx
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
