@echo off
:: ============================================================================
::  Tank Exporter PY -- uninstall
::
::  Removes every Python package go.bat installs.  Leaves the project
::  source tree, the permanent backup at
::  `resources\requirements_backup\`, and the user's tankExporterPy.json
::  config alone -- this script ONLY un-pip's the dependencies.
::
::  Companion to go.bat / reinstall.bat.
:: ============================================================================

setlocal
cd /d "%~dp0"

:: Prefer `py -3` over bare `python` -- on Windows `python` often
:: resolves to the WindowsApps Store stub rather than the real install.
:: See go.bat for the long version of this gotcha.
where py >nul 2>&1
if not errorlevel 1 (
    set "PY=py -3"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Neither `py` nor `python` is on PATH -- nothing to uninstall.
        pause
        exit /b 1
    )
    set "PY=python"
)

echo.
echo Uninstalling Tank Exporter PY runtime dependencies ...
echo (This does NOT remove anything in the project folder.)
echo.

:: -y skips per-package y/N prompts.  Order is irrelevant because none
:: of these depend on each other in the install graph.
%PY% -m pip uninstall -y pygame PyOpenGL numpy Pillow

echo.
echo Done.  resources\requirements_backup\ has been preserved so you
echo can run reinstall.bat to put everything back later.
echo.
pause
endlocal
