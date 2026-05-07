@echo off
:: ============================================================================
::  Tank Exporter PY -- skip-deps launcher
::
::  launch_skip_deps.bat is the "I know my deps are installed, just
::  run the app" entry point.  No import probe, no install path, no
::  requirements\ folder shuffling -- just python tankExporterPy.py
::  and any args you pass through.  Use this only after a successful
::  go.bat has installed the runtime packages once.
::
::  >>> If you're a new user or unsure whether deps are installed,
::  >>> use go.bat instead.  That one verifies + installs on demand
::  >>> before launching.  This script will fail with a Python
::  >>> ImportError if pygame / PyOpenGL / numpy / Pillow aren't
::  >>> already present.
::
::  History: this file was called `start.bat` until v1.67.3.  Renamed
::  because the obvious-sounding name was tricking fresh installers
::  into clicking it before go.bat had a chance to bootstrap the
::  environment.
::
::  Errors land in this window; pause-on-exit so you can read the
::  traceback if Python bails before the GL window appears.
:: ============================================================================

cd /d "%~dp0"

:: Prefer `py -3` over bare `python` -- on Windows `python` often
:: resolves to the WindowsApps Store stub rather than the real
:: install (see go.bat for the long version of this gotcha).
where py >nul 2>&1
if not errorlevel 1 (
    set "PY=py -3"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo.
        echo ERROR: Neither `py` nor `python` is on PATH.
        echo Install Python 3.10+ or run go.bat.
        echo.
        pause
        exit /b 1
    )
    set "PY=python"
)

%PY% tankExporterPy.py %*

if errorlevel 1 (
    echo.
    echo TEPY exited with an error -- see the traceback above.
    pause
)
