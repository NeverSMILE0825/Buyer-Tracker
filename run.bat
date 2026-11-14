@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Creating a local Python environment...
  python -m venv .venv
  if errorlevel 1 goto fail
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 goto fail
)
.venv\Scripts\python.exe app.py
if errorlevel 1 goto fail
goto end
:fail
echo.
echo Buyer Tracker stopped because of an error.
pause
:end
