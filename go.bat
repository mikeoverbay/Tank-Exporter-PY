@echo off
:: ============================================================================
::  Tank Exporter PY -- launcher
::
::  go.bat
::      1. Verify Python is reachable.
::      2. Try to import every runtime dependency we need
::         (pygame / PyOpenGL / numpy / PIL).
::      3. If any are missing:
::            - back up the live `requirements\` folder to
::              `resources\requirements_backup\` (first install only),
::            - install from `requirements\requirements.txt` (using any
::              wheels in `requirements\` as a `--find-links` source),
::            - re-verify imports,
::            - delete `requirements\` once everything passes.
::      4. Launch tank_viewer.py with whatever args the user passed in.
::
::  Once the install has happened once, this script just verifies imports
::  and launches -- the full install path runs only when something is
::  actually missing, so steady-state startup is fast.
::
::  Companion scripts:
::      uninstall.bat   -- pip-uninstalls every package go.bat installs
::      reinstall.bat   -- restores requirements\ from the backup and
::                         force-reinstalls everything
:: ============================================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "REQ_DIR=%~dp0requirements"
set "BACKUP_DIR=%~dp0resources\requirements_backup"

:: -------- 1. Python availability --------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: Python is not on PATH.
    echo Install Python 3.10 or newer from https://www.python.org/downloads/
    echo and re-run go.bat.
    echo.
    pause
    exit /b 1
)

:: -------- 2. Quick import probe ---------------------------------------------
::  Print nothing on success; non-zero exit triggers the install path.
python -c "import pygame, OpenGL, numpy, PIL" >nul 2>&1
if not errorlevel 1 goto :launch

echo.
echo One or more runtime dependencies are missing -- running install...
echo.

:: -------- 3a. Locate the install source -------------------------------------
::  Prefer the live `requirements\` folder; fall back to the backup if
::  someone deleted requirements\ without running uninstall first.
set "INSTALL_SRC="
if exist "%REQ_DIR%\requirements.txt" (
    set "INSTALL_SRC=%REQ_DIR%"
) else if exist "%BACKUP_DIR%\requirements.txt" (
    echo No live requirements\ folder; restoring from backup...
    if exist "%REQ_DIR%" rmdir /S /Q "%REQ_DIR%"
    xcopy /E /I /Y "%BACKUP_DIR%" "%REQ_DIR%" >nul
    set "INSTALL_SRC=%REQ_DIR%"
) else (
    echo ERROR: no requirements\ folder and no resources\requirements_backup\
    echo Either run reinstall.bat (if you have a backup elsewhere) or
    echo restore the project tree from a fresh download.
    pause
    exit /b 1
)

:: -------- 3b. Permanent backup ----------------------------------------------
::  Created exactly once, on the first install that ever runs.  Never
::  overwritten -- if someone hand-edited the backup we trust their copy.
if not exist "%BACKUP_DIR%\requirements.txt" (
    echo Backing up requirements\ to resources\requirements_backup\ ...
    if not exist "%~dp0resources" mkdir "%~dp0resources"
    xcopy /E /I /Y "%REQ_DIR%" "%BACKUP_DIR%" >nul
)

:: -------- 3c. Install -------------------------------------------------------
::  --upgrade pip first so old setuptools doesn't choke on a manylinux/wheel
::  spec we hit later.  --find-links makes pre-bundled wheels in
::  requirements\ take precedence over PyPI -- safe to leave on even when
::  there are no wheels (find-links missing files just no-ops).
echo.
echo Upgrading pip ...
python -m pip install --upgrade pip
if errorlevel 1 (
    echo WARNING: pip upgrade failed -- continuing with the existing pip.
)

echo.
echo Installing dependencies ...
python -m pip install --find-links "%INSTALL_SRC%" -r "%INSTALL_SRC%\requirements.txt"
if errorlevel 1 (
    echo.
    echo ERROR: pip install failed.  See output above.
    echo The requirements\ folder has been left in place so you can retry.
    pause
    exit /b 1
)

:: -------- 3d. Verify --------------------------------------------------------
python -c "import pygame, OpenGL, numpy, PIL" >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: install reported success but imports are still failing.
    echo Run uninstall.bat, then reinstall.bat to start over.
    pause
    exit /b 1
)

:: -------- 3e. Cleanup -------------------------------------------------------
::  Backup is the source of truth from this point on.  Reinstall.bat will
::  recreate requirements\ when needed.
echo.
echo Removing requirements\ (backup preserved at resources\requirements_backup\)
rmdir /S /Q "%REQ_DIR%"

:launch
echo.
echo Launching Tank Exporter PY ...
echo.
python tank_viewer.py %*
endlocal
