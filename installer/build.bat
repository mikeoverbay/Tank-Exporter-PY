@echo off
:: ============================================================================
::  Tank Exporter PY -- installer build script
::
::  Compiles tepy_setup.c into a single static tepy_setup.exe with no
::  runtime dependencies (/MT links the C runtime in).  Needs any
::  Visual Studio with the C++ build tools; the loop below checks the
::  usual install locations for VsDevCmd.bat.
::
::  Output: installer\tepy_setup.exe  (NOT committed -- attach it to a
::  GitHub release instead; README points users at the latest release.)
:: ============================================================================
setlocal
cd /d "%~dp0"

set "VSDEV="
for %%E in (Community Professional Enterprise BuildTools) do (
    for %%V in (18 2022 2019) do (
        if not defined VSDEV (
            if exist "C:\Program Files\Microsoft Visual Studio\%%V\%%E\Common7\Tools\VsDevCmd.bat" (
                set "VSDEV=C:\Program Files\Microsoft Visual Studio\%%V\%%E\Common7\Tools\VsDevCmd.bat"
            )
        )
    )
)
if not defined VSDEV (
    echo ERROR: VsDevCmd.bat not found -- install Visual Studio C++ build tools.
    exit /b 1
)

call "%VSDEV%" -arch=x64 -no_logo
cl /nologo /W3 /O1 /MT tepy_setup.c urlmon.lib wininet.lib /Fe:tepy_setup.exe
if errorlevel 1 (
    echo BUILD FAILED
    exit /b 1
)
del tepy_setup.obj >nul 2>&1
echo.
echo Built: %~dp0tepy_setup.exe
endlocal
