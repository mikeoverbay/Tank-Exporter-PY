"""
Tank-mesh exporters.

Currently the only path is `blender_bridge` -- spawns a headless Blender
subprocess to convert the in-memory tank into FBX / GLB / GLTF / OBJ.
That gives us every format Blender supports for free without copying
any GPL'd code.

Public API:
    export_vehicle(viewer, output_path, blender_exe=None, on_log=None)
        -- export-side entry point.  Format inferred from extension.

    import_vehicle(input_path, blender_exe=None, on_log=None)
        -- import-side entry point.  Returns a payload dict the viewer
           feeds to load_imported_payload.

    find_blender_executable(override=None)
        -- locate blender.exe via registry / PATH / install dirs.
"""

from .blender_bridge   import export_vehicle, import_vehicle
from .blender_locator  import find_blender_executable

__all__ = ['export_vehicle', 'import_vehicle', 'find_blender_executable']
