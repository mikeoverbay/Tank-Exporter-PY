@echo off
:: ============================================================================
::  Tank Exporter PY -- reinstall
::
::  "Things went south" recovery path.  Restores the live requirements\
::  folder from the permanent backup at resources\requirements_backup\,
::  then runs pip install with --force-reinstall so every package is
::  re-fetched fresh -- handy when an installed package was corrupted
::  or when a Python version change broke the existing wheels.
::
::  Does NOT pre-uninstall.  --force-reinstall handles upgrades cleanly
::  and any stale package metadata gets overwritten in place.  Run
::  uninstall.bat first if you want a truly clean slate.
::
::  Companion to go.bat / uninstall.bat.
:: ============================================================================

setlocal
cd /d "%~dp0"

set "REQ_DIR=%~dp0requirements"
set "BACKUP_DIR=%~dp0resources\requirements_backup"

:: Prefer `py -3` over bare `python` -- on Windows `python` often
:: resolves to the WindowsApps Store stub rather than the real install.
:: See go.bat for the long version of this gotcha.
where py >nul 2>&1
if not errorlevel 1 (
    set "PY=py -3"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Neither `py` nor `python` is on PATH.
        pause
        exit /b 1
    )
    set "PY=python"
)

if not exist "%BACKUP_DIR%\requirements.txt" (
    echo.
    echo ERROR: no backup found at resources\requirements_backup\
    echo.
    echo If this is a fresh checkout, run go.bat first -- it sets up
    echo the backup the first time it sees a live requirements\ folder.
    echo.
    pause
    exit /b 1
)

:: -------- Restore requirements\ from backup ---------------------------------
if exist "%REQ_DIR%" (
    echo Replacing existing requirements\ folder ...
    rmdir /S /Q "%REQ_DIR%"
)
echo Restoring requirements\ from resources\requirements_backup\ ...
xcopy /E /I /Y "%BACKUP_DIR%" "%REQ_DIR%" >nul

:: -------- Force reinstall every package -------------------------------------
echo.
echo Force-reinstalling every dependency ...
echo.
%PY% -m pip install --upgrade --force-reinstall ^
    --find-links "%REQ_DIR%" ^
    -r "%REQ_DIR%\requirements.txt"
if errorlevel 1 (
    echo.
    echo ERROR: reinstall failed.  See output above.
    echo The requirements\ folder has been left in place so you can retry.
    pause
    exit /b 1
)

:: -------- Verify ------------------------------------------------------------
%PY% -c "import pygame, OpenGL, numpy, PIL" >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: reinstall succeeded but imports are still failing.
    echo Check that you're running the right Python (where python).
    pause
    exit /b 1
)

echo.
echo Reinstall complete.  Run go.bat to launch.
echo (go.bat will delete requirements\ again on its next run.)
echo.
pause
endlocal
