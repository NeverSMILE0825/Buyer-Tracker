@echo off
setlocal
cd /d "%~dp0" || goto fail

call :configured_python
if errorlevel 1 (
  echo The saved Python environment cannot find its Python install.
  echo Looking for another Python and creating a new environment...
  call :find_python
  if errorlevel 1 goto nopython
  call :create_venv
  if errorlevel 1 goto fail
) else (
  if not exist ".venv\Scripts\python.exe" (
    echo Creating a local Python environment...
    call :create_venv
    if errorlevel 1 goto fail
  )
)

set "VIRTUAL_ENV=%CD%\.venv"
set "PATH=%CD%\.venv\Scripts;%PATH%"
set "__PYVENV_LAUNCHER__=%CD%\.venv\Scripts\python.exe"

"%BASE_PY%" -c "import fastapi,uvicorn,httpx" >nul 2>&1
if errorlevel 1 (
  echo Installing Buyer Tracker packages...
  "%BASE_PY%" -m pip install -r "%CD%\requirements.txt"
  if errorlevel 1 goto fail
)

"%BASE_PY%" "%CD%\app.py"
if errorlevel 1 goto fail
goto end

:configured_python
set "BASE_PY="
set "HOME_DIR="
if not exist ".venv\pyvenv.cfg" exit /b 1
for /f "usebackq tokens=1,* delims==" %%A in (".venv\pyvenv.cfg") do (
  set "CFG_KEY=%%A"
  set "CFG_VAL=%%B"
  call :capture_python_line
)
if defined HOME_DIR if not defined BASE_PY set "BASE_PY=%HOME_DIR%\python.exe"
call :trim_var BASE_PY
if not defined BASE_PY exit /b 1
if not exist "%BASE_PY%" exit /b 1
"%BASE_PY%" -c "import sys" >nul 2>&1
exit /b %ERRORLEVEL%

:capture_python_line
if /i "%CFG_KEY%"=="home" goto use_home
if /i "%CFG_KEY%"=="home " goto use_home
if /i "%CFG_KEY%"=="executable" goto use_exe
if /i "%CFG_KEY%"=="executable " goto use_exe
exit /b 0

:use_home
call :assign_trimmed HOME_DIR "%CFG_VAL%"
exit /b 0

:use_exe
call :assign_trimmed BASE_PY "%CFG_VAL%"
exit /b 0

:assign_trimmed
set "%~1=%~2"
call :trim_var %~1
exit /b 0

:trim_var
set "TRIM_NAME=%~1"
call set "TRIM_VAL=%%%TRIM_NAME%%%"
:trim_loop
if not defined TRIM_VAL goto trim_done
if "%TRIM_VAL:~0,1%"==" " (
  set "TRIM_VAL=%TRIM_VAL:~1%"
  goto trim_loop
)
:trim_done
set "%TRIM_NAME%=%TRIM_VAL%"
set "TRIM_NAME="
set "TRIM_VAL="
exit /b 0

:find_python
set "BASE_PY="
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%ProgramFiles%\Python313\python.exe"
  "%ProgramFiles%\Python314\python.exe"
  "%ProgramFiles%\Python312\python.exe"
  "%ProgramFiles%\Python311\python.exe"
) do (
  if not defined BASE_PY call :try_candidate "%%~P"
)
if defined BASE_PY exit /b 0
for /f "delims=" %%P in ('where python 2^>nul') do (
  if not defined BASE_PY call :try_candidate "%%~P"
)
if not defined BASE_PY exit /b 1
exit /b 0

:try_candidate
set "CANDIDATE=%~1"
echo %CANDIDATE% | find /i "\WindowsApps\" >nul && exit /b 0
if not exist "%CANDIDATE%" exit /b 0
"%CANDIDATE%" -c "import sys" >nul 2>&1 || exit /b 0
set "BASE_PY=%CANDIDATE%"
exit /b 0

:create_venv
if exist ".venv" rmdir /s /q ".venv"
"%BASE_PY%" -m venv "%CD%\.venv"
if errorlevel 1 exit /b 1
if not exist ".venv\Scripts\python.exe" exit /b 1
exit /b 0

:nopython
echo.
echo Python was not found. Install Python 3.11 or newer from
echo https://www.python.org/downloads/
echo and turn on "Add python.exe to PATH", then run this file again.
goto fail

:fail
echo.
echo Buyer Tracker stopped because of an error.
pause
:end
endlocal
