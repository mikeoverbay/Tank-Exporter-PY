"""
Export the loaded vehicle by spawning Blender as a subprocess.

The viewer side dumps the mesh data + texture sidecar folder, then
launches Blender headless with `_blender_runner.py` to build the scene
and write the requested format (FBX / GLB / GLTF / OBJ).

Public API:
    export_vehicle(viewer, output_path, blender_exe=None)
        -- format inferred from output_path extension
"""

import os
import json
import shutil
import subprocess
import tempfile
import time

from .blender_locator import find_blender_executable
from .common          import collect_payload


_RUNNER_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              '_blender_runner.py')
_IMPORTER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              '_blender_importer.py')

_VALID_FORMATS = ('fbx', 'glb', 'gltf', 'obj')


def export_vehicle(viewer, output_path, blender_exe=None, on_log=None):
    """Build + run the Blender subprocess.

    Args:
        viewer (Viewer):  live viewer with viewer.meshes populated
        output_path (str): destination file.  Extension must be one of
                          .fbx / .glb / .gltf / .obj.
        blender_exe (str | None): override.  None -> auto-detect via
                          find_blender_executable().
        on_log (callable | None): optional callback(str) for streaming
                          status lines (UI / console).

    Returns:
        (success: bool, message: str)
    """
    log = on_log or (lambda s: print(f"[export] {s}"))

    # 1. Validate the requested format
    fmt = os.path.splitext(output_path)[1].lstrip('.').lower()
    if fmt not in _VALID_FORMATS:
        return False, f"unsupported format: .{fmt} (use {_VALID_FORMATS})"

    # 2. Locate Blender
    blender = find_blender_executable(blender_exe)
    if not blender:
        return False, ("Blender not found.  Install Blender or set "
                       "config['blender_exe'] to a blender.exe path.")
    log(f"using Blender: {blender}")

    # 3. Make sure the output dir exists
    out_dir = os.path.dirname(os.path.abspath(output_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    # 4. Build the JSON payload + copy textures into a sidecar folder
    log("collecting mesh data...")
    payload = collect_payload(viewer, output_path)
    log(f"  meshes:   {len(payload['meshes'])}")
    log(f"  tex_dir:  {payload['tex_dir']}")

    # 5. Stage the payload to a temp JSON file (Blender reads it)
    fd, payload_path = tempfile.mkstemp(prefix='tankviewer_export_',
                                         suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh)
        log(f"payload: {payload_path} ({os.path.getsize(payload_path)} bytes)")

        # 6. Spawn Blender headless
        cmd = [
            blender,
            '--background',
            '--python', _RUNNER_PATH,
            '--',
            '--input',  payload_path,
            '--output', os.path.abspath(output_path),
            '--format', fmt,
        ]
        log("running Blender (--background)...")
        t0 = time.perf_counter()
        result = subprocess.run(cmd,
                                capture_output=True,
                                text=True,
                                check=False)
        elapsed = time.perf_counter() - t0

        # Always show Blender's output -- it's where errors land
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

        if not os.path.isfile(output_path):
            return False, "Blender ran but output file is missing"

        size_kb = os.path.getsize(output_path) / 1024.0
        return True, (f"wrote {output_path} ({size_kb:.1f} KB) "
                      f"in {elapsed:.1f}s")

    finally:
        # Clean up the temp payload (textures stay -- they sit next to
        # the output and the user may want to keep them).
        try:
            os.unlink(payload_path)
        except OSError:
            pass


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
    fd, payload_path = tempfile.mkstemp(prefix='tankviewer_import_',
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
