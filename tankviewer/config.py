"""
Persistent configuration for Tank Viewer.

Settings are stored in tankviewer.json next to tankExporterPy.py.
Any key can be overridden from the command line; the new value is
written back to disk so subsequent runs use it automatically.

Keys
----
pkg_dir   (str) : absolute path to res/packages/ (WoT .pkg archives)
res_mods  (str) : absolute path to res_mods/<version>/ folder

Both default to '' (empty), which tells the viewer to auto-detect from
the loaded file's path.
"""

import json
import os

# Config file lives next to tankExporterPy.py (two directories above this file)
_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'tankviewer.json',
)

_DEFAULTS = {
    'pkg_dir':    '',     # e.g. C:\Games\World_of_Tanks_NA\res\packages
    'res_mods':   '',     # e.g. C:\Games\World_of_Tanks_NA\res_mods\2.2.1.2
    'lookup_xml': '',     # path to TheItemList.xml; auto-discovered when empty
    'blender_exe': '',    # path override for FBX/GLB/OBJ export.  Empty
                          # = auto-detect via registry / PATH / install dirs.
    'info_panel_collapsed': False,  # remember the left-panel collapse state
    'light_value':   0.10,  # Light slider (direct sun brightness)
    'ambient_value': 0.50,  # Ambient slider (flat ambient fill)
    'smoke_start_size': 0.10,  # billboard size at particle birth
    'smoke_end_size':   0.25,  # billboard size at particle death
    'smoke_speed':      2.0,   # initial velocity along emitter forward
    # Frame-based alpha fade-out range (smoke flipbook is 91 frames).
    # Defaults fade across the middle third of each particle's life so
    # smoke dissipates well before the flipbook ends.  Both ends are
    # exposed as sliders ("Sm FadeS" / "Sm FadeE") in the smoke panel.
    'smoke_fade_start_frame': 30.0,
    'smoke_fade_end_frame':   60.0,
    # Hardpoint-marker visibility (orange spheres + cyan direction
    # vectors at exhaust nodes).  Toggled by the "Show HP" checkbox in
    # the smoke control panel.  Default off -- markers are diagnostic.
    'show_hardpoints':        False,
}


# ---------------------------------------------------------------------------

def load():
    """Return the config dict (all keys guaranteed, missing ones filled from defaults)."""
    cfg = dict(_DEFAULTS)
    if os.path.isfile(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, 'r', encoding='utf-8') as fh:
                on_disk = json.load(fh)
            cfg.update({k: v for k, v in on_disk.items() if k in _DEFAULTS})
        except Exception as exc:
            print(f"[config] Could not read {_CONFIG_PATH}: {exc}")
    return cfg


def save(cfg):
    """Write *cfg* to disk (only known keys are written)."""
    safe = {k: cfg.get(k, _DEFAULTS[k]) for k in _DEFAULTS}
    try:
        with open(_CONFIG_PATH, 'w', encoding='utf-8') as fh:
            json.dump(safe, fh, indent=2)
        print(f"[config] Saved -> {_CONFIG_PATH}")
    except Exception as exc:
        print(f"[config] Could not write {_CONFIG_PATH}: {exc}")


def config_path():
    """Return the absolute path of the JSON config file."""
    return _CONFIG_PATH
