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

    # Stage textures into a sidecar folder NAMED AFTER THE OUTPUT
    # FILE'S STEM with a '_textures' suffix.  Sits next to the FBX/
    # GLB/OBJ rather than mingling with it -- gives the user one
    # clean .fbx file at the path they picked plus an obvious
    # support folder beside it:
    #     <user_chosen_dir>/A14_T30.fbx
    #     <user_chosen_dir>/A14_T30_textures/<every referenced .dds>
    base, _ = os.path.splitext(os.path.abspath(output_path))
    tex_dir = base + '_textures'
    os.makedirs(tex_dir, exist_ok=True)

    name = os.path.basename(base)
    tex_dedupe = {}    # source abspath -> destination basename
    meshes_out = []

    for i, mesh in enumerate(viewer.meshes):
        # Skip hidden meshes -- user toggled them off in the mesh window.
        #
        # EXCEPT for the two track-ribbon classes the viewer hides
        # BY DEFAULT at load time (per the runtime's "the chain pad
        # system covers the visible role" reasoning):
        #
        #   * `_MAT` / `_mat` rubber-band meshes -- the static track-
        #     wrap geometry every WoT chassis ships alongside the
        #     skinned ribbons (`track_mat_L`, `track_mat_R`, etc.).
        #     The viewer hides these unconditionally (viewer.py
        #     ~4713-4719); without the override here they'd never
        #     ship to FBX, which broke the "FBX has the rubber-band
        #     tracks" behaviour TEPY had prior to the v1.230.x
        #     hide-by-default change.
        #
        #   * `Track_*Shape*` skinned ribbons -- the bottom-run
        #     deforming track segments that ride the road-wheel
        #     bones.  The viewer hides these on tanks that have a
        #     pad-system model (viewer.py ~4681-4690).  An FBX
        #     consumer wants the static rubber-band silhouette
        #     visible so the exported file SHOWS a tank, not a
        #     wheel-only chassis.
        #
        # Per Coffee 2026-06-04 ("i want the ribbons exported to
        # the fbx").  Hull / turret / gun / armor / etc. continue
        # to honour the visibility checkbox so the user can still
        # hide a part to omit it from the export.
        if not getattr(mesh, 'visible', True):
            mn    = (getattr(mesh, 'name',       '') or '').lower()
            mn_id = (getattr(mesh, 'identifier', '') or '').lower()
            is_mat_ribbon  = ('_mat' in mn) or ('_mat' in mn_id)
            is_skin_ribbon = (mn.startswith('track_') and 'shape' in mn) \
                          or (mn_id.startswith('track_') and 'shape' in mn_id)
            if not (is_mat_ribbon or is_skin_ribbon):
                continue

        # Preserve the original WoT identifier / mesh name as-is.  We
        # used to append '_{i}' for guaranteed uniqueness, but that
        # renamed every part the user knows by name in the source data
        # ('tank_hull_01' became 'tank_hull_01_0').  Blender's own
        # '.001' suffix handles real collisions on import without
        # mangling the canonical name on the meshes that don't collide.
        mat_base_name = (getattr(mesh, 'identifier', '')
                          or getattr(mesh, 'name', '')
                          or f'material_{i}')
        material = {
            'name':         mat_base_name,
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
        # would produce useless 'mesh_4' / 'mesh_5' object names in
        # Blender.  Prefer the WoT material identifier
        # ('exportTrackR_Shape', 'tank_hull_01', etc) when present so
        # the part name a user sees in Blender / FBX matches what the
        # game uses internally.  No '_{i}' suffix any more -- Blender
        # auto-appends '.001' on actual name collisions, which is a
        # less invasive form of disambiguation than mangling every name.
        display_name = (getattr(mesh, 'identifier', '')
                        or getattr(mesh, 'name', '')
                        or f'mesh_{i}')
        # Export the BIND model matrix (static hardpoint placement
        # snapshot at load time), NOT the per-frame `mesh.model_matrix`.
        # Per Coffee 2026-06-04 ("turret and gun rotated wrong"):
        # viewer.py:~19477-19488 rewrites `mesh.model_matrix` every
        # frame as `chassis_pose @ _yaw_mat @ _pitch_mat @
        # bind_model_matrix` (gun) or `chassis_pose @ _yaw_mat @ bind`
        # (turret) so the runtime pose follows physics + mouse aim.
        # If the user has driven the tank or moved the mouse before
        # hitting Export, those compositions get baked into the FBX
        # and the turret/gun lands at whatever angle it happened to
        # be at click-time.  `bind_model_matrix` is the canonical
        # "tank at origin, gun forward at pitch=0, turret at yaw=0"
        # transform -- which is the static-asset shape every FBX
        # consumer expects.  Fallback to live `model_matrix` only if
        # bind isn't populated (very-early-load race or a
        # standalone-prim path that didn't go through the chassis
        # composition).
        export_mat = getattr(mesh, 'bind_model_matrix', None)
        if export_mat is None:
            export_mat = mesh.model_matrix
        out = {
            'name':         display_name,
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
            'model_matrix': _arr_to_list(export_mat.reshape(-1)),
            'material':     material,
        }

        # Skinning data is intentionally NOT exported.  Per Coffee
        # 2026-06-04 ("drop exporting bones.. just use the transforms
        # from the xml file as we did before"): the FBX path is
        # rigid-mesh-only now.  Each mesh ships its `model_matrix`
        # (= the per-mesh transform the chassis XML's hardpoint
        # placement produced at load time); consuming apps see a
        # static-pose tank with hull / turret / gun / chassis at the
        # right world positions, no armature, no skin clusters, no
        # vertex-group binding required.
        #
        # The v1.243.0 attempt to also export the bone palette +
        # vertex-group binding so wheels could deform in Blender
        # broke the FBX export (`_attach_armature_modifiers`
        # attached an Armature modifier to every mesh -- skinned or
        # not -- without populating the vertex groups, so FBX
        # writers emitted skin clusters with no bindings; consumer
        # apps then collapsed or relocated those meshes per their
        # own broken-skin recovery).  Reverted at v1.242.2, then
        # this `None`-out lands at v1.242.3 to make sure no future
        # change accidentally re-enables the same path.
        #
        # If a future workstream wants to ship skinned meshes
        # again, the gate is: (a) only set these for meshes that
        # actually carry skinning, (b) ship the bone palette + bind
        # poses alongside, (c) apply the GL->Blender coordinate
        # swizzle to every transform consistently, (d) validate
        # round-trip through a real Blender + consumer (Max / Maya
        # / Unity / Unreal) before merging.
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
