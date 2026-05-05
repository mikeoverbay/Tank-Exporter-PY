"""
Build an export payload from a live Viewer.

Walks Viewer.meshes, copies referenced textures into a sidecar folder
next to the eventual output file, and returns a dict that's small,
JSON-serialisable, and contains everything the format-specific exporter
(blender_bridge / native FBX writer / etc.) needs to recreate the scene.

Public API:
    collect_payload(viewer, output_path) -> dict
"""

import os
import shutil
import numpy as np


def collect_payload(viewer, output_path):
    """Snapshot the loaded vehicle for export.

    Args:
        viewer (Viewer): the live viewer.  viewer.meshes must be populated.
        output_path (str): destination file (e.g. /path/to/Sherman.fbx).
                           A '<basename>_textures/' sidecar folder is
                           created next to it for the texture copies.

    Returns:
        dict with structure:
            {
              'name':      str,        # vehicle name (filename stem)
              'tex_dir':   str,        # absolute path to sidecar tex folder
              'meshes':    [{
                  'name':         str,
                  'positions':    [[x, y, z], ...],
                  'normals':      [[x, y, z], ...],
                  'uvs':          [[u, v], ...],
                  'indices':      [int, int, ...],   # flat triangle list
                  'model_matrix': [16 floats],       # row-major
                  'material': {
                      'name':         str,
                      'diffuse':      str | None,    # path inside tex_dir
                      'normal':       str | None,
                      'ao':           str | None,
                      'gmm':          str | None,
                      'double_sided': bool,
                      'alpha_test':   bool,
                  },
                  'bone_indices': [[i,i,i,i], ...] | None,
                  'bone_weights': [[w,w,w,w], ...] | None,
              }, ...],
            }

    Texture files are copied (not moved) into the sidecar folder so
    the export is self-contained even if the WoT temp-extraction
    cleanup runs.
    """
    if not viewer.meshes:
        raise RuntimeError("Nothing loaded -- nothing to export")

    base, _ = os.path.splitext(os.path.abspath(output_path))
    tex_dir = base + '_textures'
    os.makedirs(tex_dir, exist_ok=True)

    name = os.path.basename(base)
    tex_dedupe = {}    # source abspath -> destination basename
    meshes_out = []

    for i, mesh in enumerate(viewer.meshes):
        # Skip hidden meshes -- user toggled them off in the mesh window
        if not getattr(mesh, 'visible', True):
            continue

        material = {
            'name':         f"{getattr(mesh, 'identifier', '') or mesh.name}_{i}",
            'diffuse':      _stage_texture(mesh.diffuse_path, tex_dir, tex_dedupe),
            'normal':       _stage_texture(mesh.normal_path,  tex_dir, tex_dedupe),
            'ao':           _stage_texture(mesh.ao_path,      tex_dir, tex_dedupe),
            'gmm':          _stage_texture(mesh.gmm_path,     tex_dir, tex_dedupe),
            'double_sided': bool(getattr(mesh, 'double_sided',      False)),
            'alpha_test':   bool(getattr(mesh, 'alpha_test_enable', False)),
        }

        # Convert numpy arrays to plain Python lists so json.dump works.
        # Every per-vertex value WoT carries goes in here verbatim --
        # the Blender runner stashes tangents / binormals / bone arrays
        # as named FLOAT_COLOR vertex attributes so FBX / glTF round-trip
        # preserves the full vertex stream.
        #
        # Display-name fallback: WoT primitive-group section names
        # ('mesh.name') are often empty -- e.g. for hull/turret -- which
        # would produce useless '_4'/'_5' object names in Blender.  Prefer
        # the WoT material identifier (e.g. 'tank_hull_01') when present.
        display_name = (getattr(mesh, 'identifier', '')
                        or getattr(mesh, 'name', '')
                        or f'mesh_{i}')
        out = {
            'name':         f"{display_name}_{i}",
            'positions':    _arr_to_list(mesh.positions),
            'normals':      _arr_to_list(mesh.normals),
            'tangents':     _arr_to_list(mesh.tangents),
            'binormals':    _arr_to_list(mesh.binormals),
            'uvs':          _arr_to_list(mesh.uv0),
            # Optional second UV channel (lightmap / detail-routing).
            # None when the WoT data didn't carry one -- the Blender
            # runner only creates a 2nd UV layer when this is non-None
            # so static-hull-only exports stay clean.
            'uvs2':         _arr_to_list(getattr(mesh, 'uv1', None)),
            'indices':      _arr_to_list(mesh.indices.astype(np.int32).reshape(-1)),
            'model_matrix': _arr_to_list(mesh.model_matrix.reshape(-1)),
            'material':     material,
        }

        # Optional skinning data (None for non-skinned meshes; the
        # runner only adds the color attributes when both arrays exist).
        if mesh.bone_indices is not None and mesh.bone_weights is not None:
            out['bone_indices'] = _arr_to_list(mesh.bone_indices.astype(np.int32))
            out['bone_weights'] = _arr_to_list(mesh.bone_weights)
        else:
            out['bone_indices'] = None
            out['bone_weights'] = None

        meshes_out.append(out)

    # If the live viewer knows the WoT tank XML name (set by
    # load_vehicle), pass it through so a future Import can re-resolve
    # exhaust emitters / armor color etc.  Falls back to None for
    # standalone .primitives_processed loads.
    source_tank = getattr(viewer, 'source_tank_name', None)

    return {
        'name':        name,
        'tex_dir':     tex_dir,
        'source_tank': source_tank,
        'meshes':      meshes_out,
    }


# ---------------------------------------------------------------------------

def _arr_to_list(arr):
    """numpy/list -> nested Python list (for JSON)."""
    if arr is None:
        return None
    if hasattr(arr, 'tolist'):
        return arr.tolist()
    return list(arr)


def _stage_texture(src_path, tex_dir, dedupe_map):
    """Copy `src_path` into tex_dir and return the relative filename
    (or None when src_path is missing / not a file).

    `dedupe_map` is a {abs_src_path: basename_in_tex_dir} cache so the
    same source isn't copied twice when many sub-meshes share the same
    texture (which is the common case for AM/NM/AO/GMM packs).
    """
    if not src_path:
        return None
    if not os.path.isfile(src_path):
        return None
    abspath = os.path.abspath(src_path)
    if abspath in dedupe_map:
        return dedupe_map[abspath]
    dest_name = os.path.basename(abspath)
    dest = os.path.join(tex_dir, dest_name)
    # If a different source already claimed this basename, suffix to
    # avoid stomping (rare).
    if os.path.isfile(dest) and dest not in dedupe_map.values():
        stem, ext = os.path.splitext(dest_name)
        n = 1
        while True:
            candidate = f"{stem}_{n}{ext}"
            if not os.path.isfile(os.path.join(tex_dir, candidate)):
                dest_name = candidate
                dest      = os.path.join(tex_dir, dest_name)
                break
            n += 1
    try:
        shutil.copy2(abspath, dest)
    except Exception as exc:
        print(f"[export] could not copy texture {abspath}: {exc}")
        return None
    dedupe_map[abspath] = dest_name
    return dest_name
