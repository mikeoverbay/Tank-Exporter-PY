@echo off
REM ==========================================================================
REM  Tank Exporter PY -- one-click installer (double-click this file)
REM
REM  This is the single file a user downloads and double-clicks.  It fetches
REM  the PowerShell installer (install.ps1) from the project and runs it.
REM  We ship a .bat + .ps1 instead of a compiled .exe because an unsigned
REM  download-and-run .exe is quarantined on sight by Windows Defender's
REM  trojan-downloader heuristic (2026-10-23); a readable script is not.
REM
REM  If install.ps1 is sitting next to this file (e.g. you downloaded the
REM  whole project), it runs that copy; otherwise it downloads the latest
REM  from GitHub.
REM ==========================================================================
setlocal
cd /d "%~dp0"

echo.
echo Starting the Tank Exporter PY installer...
echo.

set "LOCAL_PS=%~dp0install.ps1"
if exist "%LOCAL_PS%" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%LOCAL_PS%" %*
    goto :done
)

REM Not alongside us -- fetch the latest installer from GitHub.
set "PS1=%TEMP%\tepy_install.ps1"
set "URL=https://raw.githubusercontent.com/mikeoverbay/Tank-Exporter-PY/master/installer/install.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -UseBasicParsing '%URL%' -OutFile '%PS1%' } catch { Write-Host ('Download failed: ' + $_.Exception.Message) -ForegroundColor Red; exit 1 }"
if errorlevel 1 (
    echo.
    echo Could not download the installer.  Check your internet connection
    echo and try again.
    echo.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
del "%PS1%" >nul 2>&1

:done
echo.
echo You can close this window.
pause
endlocal
