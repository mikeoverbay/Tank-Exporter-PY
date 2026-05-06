"""
Locate a Blender executable on the local machine.

Strategy (first hit wins):
    1. Caller-supplied override (e.g. config['blender_exe'])
    2. PATH lookup (`blender` / `blender.exe`)
    3. Windows registry: .blend file association
            HKEY_CLASSES_ROOT\\blendfile\\shell\\open\\command
       (most reliable on Windows -- Blender always registers this)
    4. Windows registry: BlenderFoundation install key
            HKLM/HKCU\\Software\\BlenderFoundation\\Blender <version>
       'Install_Dir' value points at the install root
    5. Standard install dirs (Program Files / /Applications / /usr/bin)

All strategies tolerate missing keys / broken paths and fall through.

Public API:
    find_blender_executable(override=None) -> str | None
"""

import os
import sys
import shutil


def find_blender_executable(override=None):
    """Return the absolute path to a usable blender.exe, or None.

    Args:
        override (str | None): explicit path supplied by config or
            command line.  If valid, returned directly without scanning.
    """
    # 1. Caller override
    if override:
        if os.path.isfile(override):
            return os.path.abspath(override)
        # Caller supplied a path that doesn't exist -- fall through to
        # auto-detect rather than failing outright.
        print(f"[blender] override not found: {override}")

    # 2. PATH
    for name in ('blender', 'blender.exe'):
        p = shutil.which(name)
        if p and os.path.isfile(p):
            return os.path.abspath(p)

    # 3 + 4: Windows registry
    if sys.platform == 'win32':
        path = _find_via_windows_registry()
        if path:
            return path

    # 5. Platform-specific standard install dirs
    if sys.platform == 'win32':
        path = _find_in_program_files()
        if path:
            return path
    elif sys.platform == 'darwin':
        for path in (
            '/Applications/Blender.app/Contents/MacOS/Blender',
            os.path.expanduser('~/Applications/Blender.app/Contents/MacOS/Blender'),
        ):
            if os.path.isfile(path):
                return path
    else:  # linux / *bsd
        for path in (
            '/usr/bin/blender', '/usr/local/bin/blender',
            '/opt/blender/blender', '/snap/bin/blender',
        ):
            if os.path.isfile(path):
                return path

    return None


# ---------------------------------------------------------------------------
# Windows registry helpers
# ---------------------------------------------------------------------------

def _find_via_windows_registry():
    """Try every known Blender registry signature and return the first
    install dir that actually has a blender.exe in it."""
    try:
        import winreg
    except ImportError:
        return None

    # Strategy A: .blend file association.  Blender registers
    # HKCR\blendfile\shell\open\command with a value like
    #     "C:\Program Files\Blender Foundation\Blender 4.2\blender-launcher.exe" "%1"
    # We try the full set of paths Windows might use.
    assoc_paths = [
        (winreg.HKEY_CLASSES_ROOT, r'blendfile\shell\open\command'),
        (winreg.HKEY_CURRENT_USER,
            r'Software\Classes\blendfile\shell\open\command'),
        (winreg.HKEY_LOCAL_MACHINE,
            r'Software\Classes\blendfile\shell\open\command'),
    ]
    for root, sub in assoc_paths:
        cmd = _read_reg_default(winreg, root, sub)
        if not cmd:
            continue
        path = _parse_cmd_executable(cmd)
        if path and os.path.isfile(path):
            # The launcher and blender.exe live side by side; use whichever
            # one we got but normalise to plain blender.exe for headless work
            return _prefer_blender_exe(path)

    # Strategy B: BlenderFoundation install key.  Versioned subkeys hold
    # an Install_Dir value pointing at the install root.
    foundation_paths = [
        (winreg.HKEY_CURRENT_USER,  r'Software\BlenderFoundation'),
        (winreg.HKEY_LOCAL_MACHINE, r'Software\BlenderFoundation'),
        (winreg.HKEY_LOCAL_MACHINE,
            r'Software\WOW6432Node\BlenderFoundation'),
    ]
    for root, sub in foundation_paths:
        path = _scan_foundation_key(winreg, root, sub)
        if path:
            return path

    return None


def _read_reg_default(winreg, root, sub):
    """Read the default ('') value at root\\sub.  Returns the string, or
    None on any error.  Closes the key automatically."""
    try:
        with winreg.OpenKey(root, sub) as key:
            value, _ = winreg.QueryValueEx(key, '')
            return value
    except OSError:
        return None


def _scan_foundation_key(winreg, root, sub):
    """Walk every versioned subkey under HKLM/HKCU\\Software\\BlenderFoundation
    and return the first valid blender.exe found."""
    try:
        bf_key = winreg.OpenKey(root, sub)
    except OSError:
        return None

    found = None
    try:
        i = 0
        while True:
            try:
                version_name = winreg.EnumKey(bf_key, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(bf_key, version_name) as ver_key:
                    install_dir, _ = winreg.QueryValueEx(ver_key, 'Install_Dir')
                exe = os.path.join(install_dir, 'blender.exe')
                if os.path.isfile(exe):
                    found = exe
                    break
            except OSError:
                continue
    finally:
        winreg.CloseKey(bf_key)
    return found


def _parse_cmd_executable(cmd):
    """Pull the executable path out of a registry command string.

        '"C:\\Program Files\\Blender 4.2\\blender.exe" "%1"' -> 'C:\\Program Files\\Blender 4.2\\blender.exe'
        'C:\\foo\\blender.exe %1'                            -> 'C:\\foo\\blender.exe'
    """
    cmd = cmd.strip()
    if not cmd:
        return None
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        if end < 0:
            return None
        return cmd[1:end]
    return cmd.split(' ', 1)[0]


def _prefer_blender_exe(path):
    """Given a path that might point at blender-launcher.exe or
    blender.exe, return blender.exe in the same directory.  The launcher
    is what's registered for double-clicks; the bare blender.exe is what
    we want for headless / --background work.
    """
    if os.path.basename(path).lower() == 'blender.exe':
        return path
    sibling = os.path.join(os.path.dirname(path), 'blender.exe')
    if os.path.isfile(sibling):
        return sibling
    return path


# ---------------------------------------------------------------------------
# Standard-install fallback (Windows only -- mac/linux handled in main fn)
# ---------------------------------------------------------------------------

def _find_in_program_files():
    """Look under Program Files / Program Files (x86) for the highest-
    versioned `Blender Foundation\\Blender X.Y\\blender.exe`."""
    candidates = []
    for env in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432'):
        base = os.environ.get(env)
        if not base:
            continue
        bf = os.path.join(base, 'Blender Foundation')
        if not os.path.isdir(bf):
            continue
        try:
            entries = os.listdir(bf)
        except OSError:
            continue
        for entry in entries:
            exe = os.path.join(bf, entry, 'blender.exe')
            if os.path.isfile(exe):
                candidates.append(exe)
    if not candidates:
        return None
    # Prefer the lexicographically largest path (puts "Blender 4.2" after
    # "Blender 3.6").  Not a perfect version sort but good enough.
    return sorted(candidates, reverse=True)[0]
