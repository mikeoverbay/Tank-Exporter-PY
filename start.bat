@echo off
:: ============================================================================
::  Tank Exporter PY -- minimal launcher
::
::  start.bat is the bare-bones "just run the app" entry point.  Use it
::  when you know the dependencies are already installed and you just
::  want to spin up the viewer fast.  No import probe, no install path,
::  no requirements\ folder shuffling -- just python tank_viewer.py and
::  any args you pass through.
::
::  If you're not sure whether the deps are installed, use go.bat
::  instead -- that one verifies and installs on demand before launching.
::
::  Errors land in this window; pause-on-exit so you can read the
::  traceback if Python bails before the GL window appears.
:: ============================================================================

cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: Python is not on PATH.  Install Python 3.10+ or run go.bat.
    echo.
    pause
    exit /b 1
)

python tank_viewer.py %*

if errorlevel 1 (
    echo.
    echo TEPY exited with an error -- see the traceback above.
    pause
)
