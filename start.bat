@echo off
:: ============================================================================
::  Tank Exporter PY -- minimal launcher
::
::  start.bat is the bare-bones "just run the app" entry point.  Use it
::  when you know the dependencies are already installed and you just
::  want to spin up the viewer fast.  No import probe, no install path,
::  no requirements\ folder shuffling -- just python tankExporterPy.py and
::  any args you pass through.
::
::  If you're not sure whether the deps are installed, use go.bat
::  instead -- that one verifies and installs on demand before launching.
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
