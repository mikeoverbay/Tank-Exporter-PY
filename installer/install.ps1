# ============================================================================
#  Tank Exporter PY -- installer (PowerShell)
#
#  Replaces the C .exe installer: an unsigned download-and-run .exe trips
#  Windows Defender's trojan-downloader heuristic and gets quarantined on
#  sight (2026-10-23).  A readable script does the same job and is not
#  auto-deleted.  Normally launched by install.bat (double-click), which
#  runs it with -ExecutionPolicy Bypass.
#
#  Fully automatic by design -- the target user may be non-technical, so
#  there are no questions: it installs what is missing, downloads the app,
#  makes a Desktop shortcut, and launches.  The only interactive moment is
#  Windows' own permission (UAC) prompt for the Blender install, which is
#  announced in plain words just before it appears.
#
#  Params (all optional; install.bat passes none):
#    -InstallDir <path>   where to put the app (default %USERPROFILE%\TankExporterPY)
#    -NoBlender           skip the Blender install
#    -NoShortcut          skip the Desktop shortcut
#    -NoLaunch            don't launch at the end
# ============================================================================
param(
    [string]$InstallDir = (Join-Path $env:USERPROFILE 'TankExporterPY'),
    [switch]$NoBlender,
    [switch]$NoShortcut,
    [switch]$NoLaunch
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'   # keeps iwr fast on big files

$RepoZip    = 'https://github.com/mikeoverbay/Tank-Exporter-PY/archive/refs/heads/master.zip'
$RepoSubdir = 'Tank-Exporter-PY-master'
$PythonUrl  = 'https://www.python.org/ftp/python/3.13.1/python-3.13.1-amd64.exe'
$BlenderUrl = 'https://download.blender.org/release/Blender4.2/blender-4.2.14-windows-x64.msi'

function Say($msg)  { Write-Host $msg -ForegroundColor Cyan }
function Good($msg) { Write-Host $msg -ForegroundColor Green }
function Warn($msg) { Write-Host $msg -ForegroundColor Yellow }

Say ''
Say '====================================='
Say '   Tank Exporter PY -- installer'
Say '====================================='
Say ''

# -- 1. Python --------------------------------------------------------------
function Test-Python {
    try { & py -3 --version *> $null; if ($LASTEXITCODE -eq 0) { return $true } } catch { }
    try { & python --version *> $null; if ($LASTEXITCODE -eq 0) { return $true } } catch { }
    return $false
}

if (Test-Python) {
    Good 'Python: already installed.'
} else {
    Say 'Python is not installed -- downloading and installing it now.'
    $pyExe = Join-Path $env:TEMP 'python-setup.exe'
    Invoke-WebRequest -UseBasicParsing $PythonUrl -OutFile $pyExe
    Say 'Installing Python (a small progress window will appear)...'
    # Per-user, PATH added, py launcher + tcl/tk included so TEPY's file
    # dialogs work.  /passive = progress bar, no questions.
    $p = Start-Process $pyExe -Wait -PassThru -ArgumentList @(
        '/passive', 'InstallAllUsers=0', 'PrependPath=1',
        'Include_launcher=1', 'Include_tcltk=1')
    Remove-Item $pyExe -ErrorAction SilentlyContinue
    if ($p.ExitCode -eq 0) { Good 'Python: installed.' }
    else { Warn 'Python installer did not finish cleanly -- continuing.' }
}

# -- 2. Blender (optional, for FBX/GLB/OBJ import+export) --------------------
function Test-Blender {
    if (Get-Command blender -ErrorAction SilentlyContinue) { return $true }
    foreach ($root in @(
            "$env:ProgramFiles\Blender Foundation",
            "${env:ProgramFiles(x86)}\Blender Foundation")) {
        if (Test-Path $root) {
            if (Get-ChildItem $root -Directory -ErrorAction SilentlyContinue |
                    Where-Object { Test-Path (Join-Path $_.FullName 'blender.exe') }) {
                return $true
            }
        }
    }
    return $false
}

if ($NoBlender) {
    Say 'Skipping Blender (as requested).'
} elseif (Test-Blender) {
    Good 'Blender: already installed.'
} else {
    Say 'Blender is not installed -- downloading it (about 350 MB, this'
    Say 'may take a few minutes).  Blender is only needed for importing and'
    Say 'exporting 3D files; everything else works without it.'
    try {
        $msi = Join-Path $env:TEMP 'blender-setup.msi'
        Invoke-WebRequest -UseBasicParsing $BlenderUrl -OutFile $msi
        Warn ''
        Warn '>> Windows will now ask for permission to install Blender.'
        Warn '>> Click YES on that prompt.'
        Warn ''
        $p = Start-Process 'msiexec.exe' -Verb RunAs -Wait -PassThru `
                 -ArgumentList @('/i', "`"$msi`"", '/qb')
        Remove-Item $msi -ErrorAction SilentlyContinue
        if ($p.ExitCode -eq 0) { Good 'Blender: installed.' }
        else { Warn 'Blender install was cancelled or failed -- that is OK, you can add it later.' }
    } catch {
        Warn "Blender step skipped ($($_.Exception.Message)) -- you can add it later."
    }
}

# -- 3. Download + extract the app ------------------------------------------
Say ''
Say 'Downloading Tank Exporter PY...'
$zip = Join-Path $env:TEMP 'tepy_master.zip'
Invoke-WebRequest -UseBasicParsing $RepoZip -OutFile $zip

Say "Installing to: $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
# Expand-Archive won't overwrite; clear a previous extraction first.
$appDir = Join-Path $InstallDir $RepoSubdir
if (Test-Path $appDir) { Remove-Item $appDir -Recurse -Force }
Expand-Archive -Path $zip -DestinationPath $InstallDir -Force
Remove-Item $zip -ErrorAction SilentlyContinue

$goBat = Join-Path $appDir 'go.bat'
if (-not (Test-Path $goBat)) {
    Warn 'Something went wrong -- go.bat is missing after extraction.'
    Warn 'Please run this installer again.'
    Read-Host 'Press Enter to close'
    exit 1
}
Good "Installed to: $appDir"

# -- 4. Desktop shortcut ----------------------------------------------------
if (-not $NoShortcut) {
    try {
        $desktop = [Environment]::GetFolderPath('Desktop')
        $lnk = Join-Path $desktop 'Tank Exporter PY.lnk'
        $ws  = New-Object -ComObject WScript.Shell
        $sc  = $ws.CreateShortcut($lnk)
        $sc.TargetPath       = $goBat
        $sc.WorkingDirectory = $appDir
        $sc.Description      = 'Tank Exporter PY'
        $sc.Save()
        Good 'A "Tank Exporter PY" shortcut is on your Desktop.'
    } catch {
        Warn 'Could not make the Desktop shortcut -- you can start the app'
        Warn "by opening go.bat in $appDir."
    }
}

Say ''
Good 'Done!'
Say 'The first time it starts it sets up a few things (about a minute),'
Say 'then the viewer opens.  In the app, click "Set Paths" and point it'
Say 'at your World of Tanks folder.'
Say ''

# -- 5. Launch --------------------------------------------------------------
if (-not $NoLaunch) {
    Say 'Starting Tank Exporter PY...'
    Start-Process -FilePath $goBat -WorkingDirectory $appDir
}
