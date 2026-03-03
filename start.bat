@echo off
setlocal

cd /d "%~dp0"

echo Starting ComfyUI Model Library Manager...
echo.

if not exist ".env" (
    echo ERROR: .env file was not found in this folder.
    echo Copy .env.example to .env, then set LOCAL_MODELS_ROOT and LAKE_MODELS_ROOT.
    echo.
    pause
    exit /b 1
)

findstr /r /c:"^LOCAL_MODELS_ROOT=." ".env" >nul
if errorlevel 1 (
    echo ERROR: LOCAL_MODELS_ROOT is missing or blank in .env
    echo.
    pause
    exit /b 1
)

findstr /r /c:"^LAKE_MODELS_ROOT=." ".env" >nul
if errorlevel 1 (
    echo ERROR: LAKE_MODELS_ROOT is missing or blank in .env
    echo.
    pause
    exit /b 1
)

echo Server URL:
echo   http://localhost:8420
echo Press Ctrl+C to stop the server.
echo.

where uv >nul 2>nul
if not errorlevel 1 goto run_with_uv

if exist ".venv\Scripts\python.exe" goto run_with_venv

echo ERROR: Could not find uv on PATH or .venv\Scripts\python.exe
echo Install dependencies first with:
echo   uv pip install -e .
echo.
pause
exit /b 1

:run_with_uv
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8420
goto finished

:run_with_venv
".venv\Scripts\python.exe" -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8420

:finished
if errorlevel 1 (
    echo.
    echo Server exited with an error.
    pause
    exit /b 1
)

endlocal
