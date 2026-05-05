@echo off
:: ============================================================================
::  Tank Exporter PY -- uninstall
::
::  Removes every Python package go.bat installs.  Leaves the project
::  source tree, the permanent backup at
::  `resources\requirements_backup\`, and the user's tankviewer.json
::  config alone -- this script ONLY un-pip's the dependencies.
::
::  Companion to go.bat / reinstall.bat.
:: ============================================================================

setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not on PATH -- nothing to uninstall.
    pause
    exit /b 1
)

echo.
echo Uninstalling Tank Exporter PY runtime dependencies ...
echo (This does NOT remove anything in the project folder.)
echo.

:: -y skips per-package y/N prompts.  Order is irrelevant because none
:: of these depend on each other in the install graph.
python -m pip uninstall -y pygame PyOpenGL numpy Pillow

echo.
echo Done.  resources\requirements_backup\ has been preserved so you
echo can run reinstall.bat to put everything back later.
echo.
pause
endlocal
