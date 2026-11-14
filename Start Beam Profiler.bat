@echo off
rem Starts the beam profiler with the Thorlabs camera (Windows).
rem Extra options are passed on, e.g.:  "Start Beam Profiler.bat" --lan
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo First run: creating the Python environment. This takes a minute...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Python was not found. Install Python 3.9 or newer from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" in the installer, then run this file again.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Installing the Python packages failed. Check the internet connection and run this file again.
    rmdir /s /q .venv
    pause
    exit /b 1
  )
)

rem The Thorlabs SDK is found in the ThorCam installation automatically on start-up.
".venv\Scripts\python.exe" -m beamprofiler %*
pause
