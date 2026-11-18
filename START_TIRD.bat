@echo off
setlocal EnableExtensions
cd /d "%~dp0" || exit /b 1
if not exist "tird_collector.py" (
    echo [ERROR] Missing tird_collector.py
    pause
    exit /b 1
)
set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=%CD%\.venv\Scripts\python.exe"
if /I not "%~1"=="--inside" (
    where wt.exe >nul 2>&1
    if not errorlevel 1 (
        wt.exe --window new --fullscreen new-tab --title "TIRD Collector" --startingDirectory "%CD%" cmd.exe /k ""%~f0" --inside"
        exit /b 0
    )
)
"%PYTHON%" tird_collector.py --ui
if errorlevel 1 (
    echo.
    echo [ERROR] TIRD UI exited with an error.
    pause
)
endlocal
