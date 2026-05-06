"""
Tank-mesh importers.

Currently the only path is `blender_bridge` -- spawns a headless Blender
subprocess that reads an FBX / GLB / GLTF / OBJ via `_blender_importer.py`
and returns a JSON-serialisable payload describing every mesh.  The
viewer feeds that payload to `Viewer.load_imported_payload()`.

Public API:
    import_vehicle(input_path, blender_exe=None, on_log=None)
        -- import-side entry point.  Returns (success, payload_or_message).

    find_blender_executable(override=None)
        -- re-exported for convenience.  Locates blender.exe via
           registry / PATH / install-dir scan.  Lives in
           `tankExporterPy.blender_locator`; both importers and exporters
           pull from there so there's a single source of truth.
"""

from .blender_bridge      import import_vehicle
from ..blender_locator    import find_blender_executable

__all__ = ['import_vehicle', 'find_blender_executable']
