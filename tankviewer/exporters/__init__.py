"""
Tank-mesh exporters.

Currently the only path is `blender_bridge` -- spawns a headless Blender
subprocess to convert the in-memory tank into FBX / GLB / GLTF / OBJ.
That gives us every format Blender supports for free without copying
any GPL'd code.

The mirror IMPORT path lives in `tankviewer.importers` (separate
subpackage so each direction has its own home).

Public API:
    export_vehicle(viewer, output_path, blender_exe=None, on_log=None)
        -- export-side entry point.  Format inferred from extension.

    find_blender_executable(override=None)
        -- re-exported for convenience.  Locates blender.exe via
           registry / PATH / install-dir scan.  Lives in
           `tankviewer.blender_locator`; both importers and exporters
           pull from there so there's a single source of truth.
"""

from .blender_bridge      import export_vehicle
from ..blender_locator    import find_blender_executable

__all__ = ['export_vehicle', 'find_blender_executable']
