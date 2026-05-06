"""
Persistent configuration for TEPY (Tank Exporter PY).

Settings are stored in tankExporterPy.json next to tankExporterPy.py.
Any key can be overridden from the command line; the new value is
written back to disk so subsequent runs use it automatically.

Filename history: pre-v1.48.2 the file was called tankExporterPy.json
(matching the package-internal name).  It was renamed for symmetry
with the launcher script + GitHub repo (Tank-Exporter-PY) + the
TEPY brand.  `load()` migrates the old name on first run if the
new one isn't present yet.

Keys
----
pkg_dir   (str) : absolute path to res/packages/ (WoT .pkg archives)
res_mods  (str) : absolute path to res_mods/<version>/ folder

Both default to '' (empty), which tells the viewer to auto-detect
from the loaded file's path.
"""

import json
import os

# Project root (where tankExporterPy.py lives) -- two dirs above this
# file inside the tankExporterPy/ package.
_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))

# Active config file (post-rename).  See _migrate_legacy_filename
# for the one-time tankExporterPy.json -> tankExporterPy.json move.
_CONFIG_PATH        = os.path.join(_PROJECT_ROOT, 'tankExporterPy.json')
_LEGACY_CONFIG_PATH = os.path.join(_PROJECT_ROOT, 'tankExporterPy.json')

_DEFAULTS = {
    'pkg_dir':    '',     # e.g. C:\Games\World_of_Tanks_NA\res\packages
    'res_mods':   '',     # e.g. C:\Games\World_of_Tanks_NA\res_mods\2.2.1.2
    'lookup_xml': '',     # path to TheItemList.xml; auto-discovered when empty
    'blender_exe': '',    # path override for FBX/GLB/OBJ export.  Empty
                          # = auto-detect via registry / PATH / install dirs.
    'fbx_converter_exe': '',  # path override for Autodesk FBX
                              # Converter 2013 (FbxConverter.exe).
                              # Used by importers/fbx_version.py to
                              # auto-upgrade pre-7.1 binary FBXes that
                              # Blender's importer rejects outright.
                              # Empty = search the standard install
                              # paths under Program Files.
    'info_panel_collapsed': False,  # remember the left-panel collapse state
    'light_value':   0.10,  # Light slider (direct sun brightness)
    'ambient_value': 0.50,  # Ambient slider (flat ambient fill)
    # Per-engine-class smoke / fire settings.  Filled in by the
    # viewer at startup with built-in defaults merged with whatever's
    # on disk; saved as nested dicts.  See Viewer._SMOKE_GROUP_DEFAULTS
    # / _FIRE_GROUP_DEFAULTS for the inner field schema.
    'smoke_groups': {},
    'fire_groups':  {},
    # Hardpoint-marker visibility (orange spheres + cyan direction
    # vectors at exhaust nodes).  Toggled by the "Show HP" checkbox in
    # the smoke control panel.  Default off -- markers are diagnostic.
    'show_hardpoints':        False,
}


# ---------------------------------------------------------------------------

def _migrate_legacy_filename():
    """Move tankExporterPy.json -> tankExporterPy.json on first run after
    the rename.  Idempotent: skips if the new file already exists or
    if the legacy file doesn't.  Doesn't touch contents -- the viewer
    handles per-key migration (legacy smoke_* keys -> smoke_groups
    dict) inside Viewer._migrate_legacy_smoke_fire_config.
    """
    if os.path.isfile(_CONFIG_PATH):
        return
    if not os.path.isfile(_LEGACY_CONFIG_PATH):
        return
    try:
        os.rename(_LEGACY_CONFIG_PATH, _CONFIG_PATH)
        print(f"[config] migrated {os.path.basename(_LEGACY_CONFIG_PATH)} "
              f"-> {os.path.basename(_CONFIG_PATH)}")
    except Exception as exc:
        print(f"[config] could not rename legacy config: {exc}")


def load():
    """Return the config dict.

    Every key in _DEFAULTS is guaranteed to be present.  Extra keys
    found on disk are also kept (per-tank smoke_groups dicts, etc.),
    so future schema additions don't require touching this file.
    Legacy single-slot keys (`smoke_start_size`, `fire_fps`, ...) are
    left in the dict for the viewer to migrate at startup.
    """
    _migrate_legacy_filename()

    cfg = dict(_DEFAULTS)
    if os.path.isfile(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, 'r', encoding='utf-8') as fh:
                on_disk = json.load(fh)
            # No whitelist -- preserve whatever the file carries.
            # The viewer's loaders treat unknown keys as no-ops and
            # the cleanup() path uses _LEGACY_*_KEYS to drop the few
            # we know are obsolete.
            if isinstance(on_disk, dict):
                cfg.update(on_disk)
        except Exception as exc:
            print(f"[config] Could not read {_CONFIG_PATH}: {exc}")
    return cfg


def save(cfg):
    """Write *cfg* to disk in full -- no key whitelist."""
    try:
        with open(_CONFIG_PATH, 'w', encoding='utf-8') as fh:
            json.dump(cfg, fh, indent=2)
        print(f"[config] Saved -> {_CONFIG_PATH}")
    except Exception as exc:
        print(f"[config] Could not write {_CONFIG_PATH}: {exc}")


def config_path():
    """Return the absolute path of the JSON config file."""
    return _CONFIG_PATH
