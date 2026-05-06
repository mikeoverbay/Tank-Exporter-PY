"""FBX-version probe + auto-conversion via Autodesk FBX Converter 2013.

Blender's FBX importer (`io_scene_fbx`) rejects binary FBX files
with a header version < 7100 (FBX 7.1).  Older content packs and
the legacy WoT tank exporter both wrote FBX 6.1, which the Blender
importer outright refuses to load.  Rather than make the user
shuttle every file through a converter UI by hand, we probe the
header on import and, if it's too old, run Autodesk's free FBX
Converter 2013 in headless mode to upgrade in place.

Public API
----------
    read_fbx_version(path)                  -> int|None
    fbx_version_pretty(version_code)        -> str
    find_fbx_converter(override=None)       -> str|None
    convert_fbx_to_modern(src, exe, dst)    -> (ok, dst|err)
    ensure_modern_fbx(src, override=None)   -> (final_path, action, message)

`ensure_modern_fbx` is the one the import flow calls -- it returns
`action='no_op'` for already-modern FBXes (and other content), or
`action='converted'` once the upgrade has run.  `action='error'`
when the converter is absent or the conversion failed; the caller
should surface the message and abort.
"""

import os
import struct
import subprocess


# Blender's importer tests `if version < 7100: raise` -- so 7100 is
# the floor.  The 2013 FBX Converter writes 7.3 max (`/f201300`),
# which sits comfortably above the floor without forcing the user
# onto the much heavier FBX SDK 2020.
MIN_BLENDER_FBX_VERSION = 7100
TARGET_FBX_VERSION_FLAG = '/f201300'   # FBX 7.3 binary

# Common Autodesk FBX Converter install paths (Windows).  The 2013
# release ships with a CLI binary at <install>/bin/FbxConverter.exe.
# We probe the standard installer locations + the (x86) variant for
# users who picked the 32-bit installer on a 64-bit OS.
_FBX_CONVERTER_CANDIDATES = (
    r'C:\Program Files\Autodesk\FBX\FBX Converter\2013.3\bin\FbxConverter.exe',
    r'C:\Program Files\Autodesk\FBX\FBX Converter\2013.2\bin\FbxConverter.exe',
    r'C:\Program Files\Autodesk\FBX\FBX Converter\2013.1\bin\FbxConverter.exe',
    r'C:\Program Files (x86)\Autodesk\FBX\FBX Converter\2013.3\bin\FbxConverter.exe',
    r'C:\Program Files (x86)\Autodesk\FBX\FBX Converter\2013.2\bin\FbxConverter.exe',
    r'C:\Program Files (x86)\Autodesk\FBX\FBX Converter\2013.1\bin\FbxConverter.exe',
)


# ---------------------------------------------------------------------------

def read_fbx_version(path):
    """Return the FBX-binary version code (e.g. 6100, 7300, 7400) or None.

    Reads the 27-byte FBX-binary header.  The magic string is
    'Kaydara FBX Binary  ' (20 bytes) + 0x00 0x1a 0x00 (3 bytes),
    then a little-endian uint32 with the version.

    Returns None for ASCII FBX, non-FBX content, or files too short
    to inspect.  An ASCII FBX would need a different probe -- we
    treat it as 'modern enough' since Blender handles ASCII without
    version checks.
    """
    try:
        with open(path, 'rb') as fh:
            head = fh.read(32)
    except OSError:
        return None
    if not head.startswith(b'Kaydara FBX Binary'):
        return None
    if len(head) < 27:
        return None
    return struct.unpack_from('<I', head, 23)[0]


def fbx_version_pretty(version_code):
    """Format e.g. 7300 -> 'FBX 7.3', 6100 -> 'FBX 6.1', None -> 'unknown'."""
    if not version_code:
        return 'unknown'
    major, rest = divmod(version_code, 1000)
    minor = rest // 100
    return f'FBX {major}.{minor}'


def find_fbx_converter(override_path=None):
    """Locate FbxConverter.exe.  Returns absolute path or None.

    Search order:
        1. `override_path` (from config / explicit caller arg).
        2. The hardcoded install-path candidates above.

    No registry probe -- the candidates cover every standard
    Autodesk installer layout, and the override slot exists for
    anyone who unzipped the archive into a non-default location.
    """
    if override_path:
        override_path = override_path.strip()
        if override_path and os.path.isfile(override_path):
            return override_path
    for cand in _FBX_CONVERTER_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return None


def convert_fbx_to_modern(src_path, converter_exe, out_path=None):
    """Convert an old FBX to FBX 7.3 binary via FbxConverter.exe.

    Args:
        src_path      (str): the original FBX file
        converter_exe (str): path to FbxConverter.exe
                              (typically from `find_fbx_converter`)
        out_path      (str|None): destination path; defaults to
                              `<src_no_ext>_v73.fbx` next to the
                              original so we never overwrite the
                              user's source.

    Returns:
        (ok, result) -- on success result is the absolute output
        path; on failure result is an error message string.

    The 2013 converter requires the `/f<verCode>` flag whenever
    both endpoints are FBX (else it just prints usage and exits 0).
    `/v` enables verbose log to stdout; we capture but mostly
    ignore it (the caller logs via TEPY's own console).
    """
    if out_path is None:
        base = os.path.splitext(src_path)[0]
        out_path = base + '_v73.fbx'

    # Hide the console window we'd otherwise spawn on Windows.
    creation_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    cmd = [converter_exe, src_path, out_path,
           TARGET_FBX_VERSION_FLAG, '/v']
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=120, creationflags=creation_flags)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f'FbxConverter call failed: {exc}'

    # The converter often returns 0 even when it bails -- the only
    # reliable success signal is "did the output file appear and is
    # it non-trivial in size".
    if not os.path.isfile(out_path) or os.path.getsize(out_path) < 64:
        stderr = (result.stderr or '').strip()[:200]
        stdout = (result.stdout or '').strip()[:200]
        return False, (f'FbxConverter exited {result.returncode} but no '
                       f'output produced.  stdout: {stdout!r}  '
                       f'stderr: {stderr!r}')
    return True, out_path


def ensure_modern_fbx(src_path, converter_override=None):
    """Probe `src_path`'s FBX version; auto-convert if Blender can't read it.

    The single entry point the import flow should call.  Returns a
    3-tuple `(final_path, action, message)`:

        * `action` is one of:
              'no_op'      -- file is fine to feed Blender as-is
              'converted'  -- a new FBX 7.3 file was written; use it
              'error'      -- conversion was needed but failed; the
                              import flow should abort

        * `final_path` is the path to feed Blender:
              - 'no_op'     -> `src_path` unchanged
              - 'converted' -> the new converted file's path
              - 'error'     -> `src_path` (caller decides; Blender
                                will most likely reject it)

        * `message` is a human-readable summary suitable for a
          notification popup.

    The converter location may be overridden via `converter_override`
    (typically `cfg.get('fbx_converter_exe')`).  When omitted, the
    default search paths are used.
    """
    ver = read_fbx_version(src_path)
    if ver is None:
        # ASCII FBX, non-FBX content, or unreadable file.  Pass
        # through and let Blender decide.
        return src_path, 'no_op', 'no binary FBX header (skipping version check)'
    if ver >= MIN_BLENDER_FBX_VERSION:
        return src_path, 'no_op', f'{fbx_version_pretty(ver)} (Blender-ready)'

    # Old version.  Look for the converter.
    converter = find_fbx_converter(converter_override)
    if converter is None:
        return src_path, 'error', (
            f"This file is {fbx_version_pretty(ver)}, which Blender "
            f"can't read directly (Blender requires FBX 7.1 or newer).\n\n"
            f"Autodesk FBX Converter 2013 isn't installed in any of "
            f"the standard locations.  Either install it (free from "
            f"Autodesk's archives) or set `fbx_converter_exe` in "
            f"tankExporterPy.json to the absolute path of "
            f"FbxConverter.exe.")

    ok, result = convert_fbx_to_modern(src_path, converter)
    if not ok:
        return src_path, 'error', (
            f"FBX upgrade failed:\n{result}")

    new_ver = read_fbx_version(result)
    return result, 'converted', (
        f"{fbx_version_pretty(ver)} is too old for Blender; "
        f"auto-upgraded to {fbx_version_pretty(new_ver)}.\n\n"
        f"Source: {src_path}\n"
        f"Converted: {result}\n\n"
        f"The original FBX is unchanged; the converted copy "
        f"will be loaded.")
