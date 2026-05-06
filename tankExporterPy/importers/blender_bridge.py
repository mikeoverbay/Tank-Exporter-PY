"""
Import an FBX / GLB / GLTF / OBJ tank by spawning Blender as a subprocess.

The viewer side asks Blender (headless) to open the input file and dump
every mesh into a JSON payload via `_blender_importer.py`; we then
swizzle the data back to OpenGL Y-up and rebuild Mesh objects.

This file holds the IMPORT path only.  The mirror export path lives at
`tankExporterPy/exporters/blender_bridge.py`; the two stay symmetric.

Public API:
    import_vehicle(input_path, blender_exe=None, on_log=None)
        -- format inferred from input_path extension.  Returns
           (success, payload_dict) on success or (False, error_msg).
"""

import os
import json
import subprocess
import tempfile
import time

from ..blender_locator import find_blender_executable


_IMPORTER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              '_blender_importer.py')

_VALID_FORMATS = ('fbx', 'glb', 'gltf', 'obj')


def import_vehicle(input_path, blender_exe=None, on_log=None):
    """Run Blender headless to read an FBX/GLB/GLTF/OBJ and return a
    JSON-serialisable payload describing every mesh.

    The returned payload schema matches what the import side of the
    viewer expects (see Viewer.load_imported_payload):

        {
          'name':   str,                 # filename stem
          'meshes': [
            {
              'name':         str,
              'positions':    [(x,y,z), ...]      # Blender Z-up
              'normals':      [(x,y,z), ...]
              'uvs':          [(u,v),   ...]
              'uvs2':         [(u,v),   ...] | None      # 2nd UV layer
              'indices':      [int, ...]
              'tangents':     [(x,y,z), ...] | None      # decoded from WoTTangent
              'binormals':    [(x,y,z), ...] | None      # decoded from WoTBinormal
              'bone_indices': [(i,i,i,i), ...] | None    # decoded from WoTBoneIdx
              'bone_weights': [(w,w,w,w), ...] | None    # decoded from WoTBoneWeight
              'model_matrix': [16 floats]                # Blender Z-up world,
                                                         #   row-major, kept SEPARATE
                                                         #   from the mesh-local
                                                         #   vertex data so the
                                                         #   original layout can be
                                                         #   written back out as a
                                                         #   WoT .primitives_processed.
              'diffuse_path': absolute path to AM image, or None
            },
            ...
          ],
        }

    Args:
        input_path  (str): file to import
        blender_exe (str | None): override; None = auto-detect
        on_log      (callable | None): line-by-line status reporter

    Returns:
        (success: bool, message_or_payload)
            success=True  -> message_or_payload is the dict above
            success=False -> message_or_payload is a human error string
    """
    log = on_log or (lambda s: print(f"[import] {s}"))

    if not os.path.isfile(input_path):
        return False, f"file not found: {input_path}"

    fmt = os.path.splitext(input_path)[1].lstrip('.').lower()
    if fmt not in _VALID_FORMATS:
        return False, f"unsupported format: .{fmt} (use {_VALID_FORMATS})"

    blender = find_blender_executable(blender_exe)
    if not blender:
        return False, ("Blender not found.  Install Blender or set "
                       "config['blender_exe'] to a blender.exe path.")
    log(f"using Blender: {blender}")

    # Stage a temp file for Blender to write the payload into
    fd, payload_path = tempfile.mkstemp(prefix='tankExporterPy_import_',
                                         suffix='.json')
    os.close(fd)

    try:
        cmd = [
            blender,
            '--background',
            '--python', _IMPORTER_PATH,
            '--',
            '--input',  os.path.abspath(input_path),
            '--output', payload_path,
        ]
        log(f"running Blender (--background) on {os.path.basename(input_path)}...")
        t0 = time.perf_counter()
        result = subprocess.run(cmd,
                                capture_output=True,
                                text=True,
                                check=False)
        elapsed = time.perf_counter() - t0

        if result.stdout:
            for line in result.stdout.splitlines():
                if line.strip():
                    log(f"  bpy: {line}")
        if result.stderr:
            for line in result.stderr.splitlines():
                if line.strip():
                    log(f"  bpy! {line}")

        if result.returncode != 0:
            return False, (f"Blender exited with code {result.returncode} "
                           f"after {elapsed:.1f}s")
        if not os.path.isfile(payload_path):
            return False, "Blender ran but payload file is missing"

        with open(payload_path, 'r', encoding='utf-8') as fh:
            payload = json.load(fh)
        log(f"loaded payload: {len(payload.get('meshes', []))} meshes "
            f"in {elapsed:.1f}s")
        return True, payload
    finally:
        try:
            os.unlink(payload_path)
        except OSError:
            pass
