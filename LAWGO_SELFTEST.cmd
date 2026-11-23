@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title LAWGO v3 local self-test
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"
if not exist "runtime\python.exe" (
  echo [FAIL] runtime\python.exe is missing.
  pause
  exit /b 1
)
"%~dp0runtime\python.exe" "%~dp0portable_selftest.py"
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo [OK] Self-test passed.) else (echo [FAIL] Self-test failed. Copy the error text above.)
pause
exit /b %RC%
