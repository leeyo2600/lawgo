@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title LAWGO v3 - Python Bundled

rem Use only the Python executable bundled in this folder.
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"
set "HF_HOME=%~dp0runtime_cache\huggingface"
set "SENTENCE_TRANSFORMERS_HOME=%~dp0runtime_cache\sentence_transformers"
set "LAWGO_EMBEDDING_CACHE=%~dp0runtime_cache\embedding"
set "PYEXE=%~dp0runtime\python.exe"

if not exist "%PYEXE%" (
    echo [ERROR] Bundled Python is missing: runtime\python.exe
    echo Extract the entire ZIP before running LAWGO_RUN.cmd.
    pause
    exit /b 1
)
if not exist "%~dp0runtime\python313._pth" (
    echo [ERROR] Bundled Python search paths are missing.
    echo Re-extract the full ZIP to a regular Windows folder.
    pause
    exit /b 1
)

"%PYEXE%" -c "import sys, flask, waitress, requests, xlsxwriter; assert sys.version_info[:2] == (3,13); print('Embedded Python and required libraries: OK')"
if errorlevel 1 (
    echo [ERROR] Bundled Python or a required library cannot load.
    echo Run LAWGO_SELFTEST.cmd and send its message to the developer.
    pause
    exit /b 1
)

if not exist "config.json" if exist "config.example.json" copy /y "config.example.json" "config.json" >nul
if not exist "runtime_cache" mkdir "runtime_cache" >nul 2>nul

echo ============================================================
echo LAWGO v3 - Python 3.13.3 included

echo Open http://127.0.0.1:8000 if the browser does not appear.
echo Keep this command window open while using LAWGO.
echo ============================================================

rem Start default browser shortly after server launch.
start "" /b powershell.exe -NoLogo -NoProfile -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8000'" >nul 2>nul
"%PYEXE%" "%~dp0web_app.py"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo LAWGO stopped with error code %RC%.
    pause
)
exit /b %RC%
