@echo off
setlocal EnableExtensions
cd /d "%~dp0" || exit /b 1
title TIRD Newsletter Collector

if not exist "tird_collector.py" (
    echo [ERROR] tird_collector.py not found.
    pause
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    where py.exe >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON=py -3"
    ) else (
        set "PYTHON=python"
    )
)

%PYTHON% tird_collector.py --ui
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
    echo.
    echo [ERROR] TIRD exited with code %RESULT%.
    echo Install dependencies: %PYTHON% -m pip install -r requirements.txt
    pause
)
exit /b %RESULT%
