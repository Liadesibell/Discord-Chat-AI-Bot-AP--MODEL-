@echo off
setlocal
cd /d "%~dp0"

if not exist ".env" (
    echo ERROR: .env was not found in the project folder.
    pause
    exit /b 1
)

if not exist "main.py" (
    echo ERROR: main.py was not found in the project folder.
    pause
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py
) else (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 main.py
    ) else (
        where python >nul 2>nul
        if errorlevel 1 (
            echo ERROR: Python was not found. Install Python or create the .venv environment.
            pause
            exit /b 1
        )
        python main.py
    )
)

if errorlevel 1 (
    echo.
    echo The bot stopped because of an error. Read the message above.
)
pause
