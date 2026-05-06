"""
Export the loaded vehicle by spawning Blender as a subprocess.

The viewer side dumps the mesh data + texture sidecar folder, then
launches Blender headless with `_blender_runner.py` to build the scene
and write the requested format (FBX / GLB / GLTF / OBJ).

This file holds the EXPORT path only.  The mirror import path lives at
`tankExporterPy/importers/blender_bridge.py`; the two stay symmetric.

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

from ..blender_locator import find_blender_executable
from .common           import collect_payload


_RUNNER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '_blender_runner.py')

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
    fd, payload_path = tempfile.mkstemp(prefix='tankExporterPy_export_',
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

