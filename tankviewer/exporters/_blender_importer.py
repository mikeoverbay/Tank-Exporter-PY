"""
Blender-side import runner.  Reverse of _blender_runner.

Invoked by tankviewer.exporters.blender_bridge as:

    blender --background --python <this file> -- \\
        --input  <tank.fbx | tank.glb | tank.gltf | tank.obj> \\
        --output <payload.json>

Walks every mesh in the imported scene, extracts:
    - positions / normals (in Blender Z-up; the viewer-side import code
      swizzles back to its native Y-up)
    - per-loop -> per-vertex UVs (active UV layer)
    - triangulated indices
    - the four WoT* color attributes (decoded back to source units)
    - a single diffuse texture path per material (if findable)

Coordinate system: Blender is Z-up.  Our exporter wrote a Y-up FBX
which Blender then re-imports as Z-up internally (and re-exports as
Y-up via axis_forward/axis_up settings).  The viewer side is Y-up, so
this importer leaves data in Blender Z-up and lets the consumer
swizzle.

NOTE: This runs inside Blender's Python -- bpy is available, the
tankviewer package is NOT.  Keep imports minimal.
"""

import os
import sys
import json
import argparse


def main():
    args = _parse_argv()

    try:
        import bpy
    except ImportError:
        print("[blender-importer] bpy not available", file=sys.stderr)
        sys.exit(2)

    # Wipe the default scene before importing
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    for col in (bpy.data.meshes, bpy.data.materials, bpy.data.images,
                bpy.data.armatures, bpy.data.lights, bpy.data.cameras):
        for item in list(col):
            col.remove(item, do_unlink=True)

    # Dispatch by extension
    ext = os.path.splitext(args.input)[1].lower()
    if ext == '.fbx':
        bpy.ops.import_scene.fbx(filepath=args.input)
    elif ext in ('.glb', '.gltf'):
        bpy.ops.import_scene.gltf(filepath=args.input)
    elif ext == '.obj':
        try:
            bpy.ops.wm.obj_import(filepath=args.input)
        except AttributeError:
            bpy.ops.import_scene.obj(filepath=args.input)
    else:
        print(f"[blender-importer] unsupported format: {ext}", file=sys.stderr)
        sys.exit(3)

    payload = {
        'name':        os.path.splitext(os.path.basename(args.input))[0],
        'source_tank': None,
        'meshes':      [],
    }

    # Recover the source WoT tank XML basename via the magic-named
    # child Empty the exporter left in the scene -- name prefix
    # '_WOT_TANK_' followed by the tank XML basename.  Visible in the
    # outliner, survives FBX round-trip without depending on custom
    # property handling.
    _WOT_TAG_PREFIX = '_WOT_TANK_'
    for obj in bpy.data.objects:
        # FBX may suffix names with .001 etc. on collisions; .split('.')[0]
        # peels that back so 'It21_Lion.001' still resolves.
        nm = obj.name
        if nm.startswith(_WOT_TAG_PREFIX):
            tank = nm[len(_WOT_TAG_PREFIX):].split('.')[0]
            if tank:
                payload['source_tank'] = tank
                break

    for obj in bpy.data.objects:
        me = obj.data
        if not isinstance(me, bpy.types.Mesh):
            continue
        if len(me.vertices) == 0:
            continue
        mesh_dict = _extract_mesh(bpy, obj, me)
        if mesh_dict is not None:
            payload['meshes'].append(mesh_dict)

    with open(args.output, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh)
    print(f"[blender-importer] wrote {args.output} "
          f"with {len(payload['meshes'])} meshes")


# ---------------------------------------------------------------------------

def _parse_argv():
    argv = sys.argv
    if '--' in argv:
        argv = argv[argv.index('--') + 1:]
    else:
        argv = []
    p = argparse.ArgumentParser()
    p.add_argument('--input',  required=True)
    p.add_argument('--output', required=True)
    return p.parse_args(argv)


def _extract_mesh(bpy, obj, me):
    """Pull positions / normals / uvs / indices / WoT* attribs out of
    a single Blender mesh into a JSON-serialisable dict.

    Vertex data stays in MESH-LOCAL space; the per-component object
    transform is reported separately as 'model_matrix' so the viewer
    can re-apply it at render time and -- crucially -- write the
    original mesh-local layout back out as a WoT .primitives_processed
    later without having to un-bake the world transform.
    """

    # Make sure we have triangulated face data + custom split normals.
    me.calc_loop_triangles()

    # ---- Positions / normals (Blender Z-up, MESH-LOCAL space) ----------
    positions = [(v.co.x, v.co.y, v.co.z) for v in me.vertices]
    normals   = [(v.normal.x, v.normal.y, v.normal.z) for v in me.vertices]

    # ---- Object transform (Blender Z-up, world placement) --------------
    # Stored as 16 floats in row-major order so the viewer can
    # reconstruct a 4x4 numpy matrix and apply its own Z-up -> Y-up
    # axis swizzle before writing it to mesh.model_matrix.
    mw = obj.matrix_world
    model_matrix = []
    for r in range(4):
        for c in range(4):
            model_matrix.append(float(mw[r][c]))

    # ---- UVs: active UV layer + optional 2nd, mapped per-vertex ------
    # FBX stores UV per-loop (face-corner).  For our viewer pipeline we
    # need per-vertex.  Take the first loop's UV at each vertex; if the
    # source mesh has true UV seams (different UVs at the same vert),
    # downstream gets the first one we see.
    #
    # We read TWO layers when present so the WoT-side UV2 (which
    # _blender_runner.py wrote as 'UVMap2') survives the round-trip.
    # Layer order: index 0 -> uvs (the diffuse channel), index 1 ->
    # uvs2 (the lightmap / detail channel).  Anything past the second
    # layer is ignored -- WoT's primitives_processed format only has
    # slots for two.
    def _layer_to_per_vertex(layer):
        out = [None] * len(me.vertices)
        layer_data = layer.data
        for poly in me.polygons:
            for li in poly.loop_indices:
                vi = me.loops[li].vertex_index
                if out[vi] is None:
                    u, v = layer_data[li].uv
                    out[vi] = (float(u), float(v))
        return [(0.0, 0.0) if uv is None else uv for uv in out]

    uvs  = [(0.0, 0.0)] * len(me.vertices)
    uvs2 = None
    if me.uv_layers and len(me.uv_layers) >= 1:
        uvs = _layer_to_per_vertex(me.uv_layers[0])
    if me.uv_layers and len(me.uv_layers) >= 2:
        uvs2 = _layer_to_per_vertex(me.uv_layers[1])

    # ---- Triangulated indices -----------------------------------------
    indices = []
    for tri in me.loop_triangles:
        indices.extend(int(i) for i in tri.vertices)

    # ---- WoT* color attributes (with documented decoders) -------------
    by_name = {a.name: a for a in me.color_attributes}

    def _read_color(name):
        """Return [(r,g,b,a), ...] one per VERTEX, or None if absent.

        We exported with domain=POINT (per-vertex) but Blender's FBX
        *importer* restores them as domain=CORNER (per-loop, one slot
        per face-corner), so we have to handle both cases.  For
        CORNER we take the first loop touching each vertex.
        """
        a = by_name.get(name)
        if not a:
            return None
        if a.domain == 'POINT':
            return [tuple(c.color) for c in a.data]
        if a.domain == 'CORNER':
            per_vert = [None] * len(me.vertices)
            for li, loop in enumerate(me.loops):
                vi = loop.vertex_index
                if per_vert[vi] is None:
                    per_vert[vi] = tuple(a.data[li].color)
            return [c if c is not None else (0.0, 0.0, 0.0, 0.0)
                    for c in per_vert]
        return None

    raw_tan = _read_color('WoTTangent')
    raw_bin = _read_color('WoTBinormal')
    raw_bid = _read_color('WoTBoneIdx')
    raw_bwt = _read_color('WoTBoneWeight')

    # Decoder formulas mirror _blender_runner._vec3_to_vec4 etc.
    #   tangent / binormal :  v = c * 2 - 1   (only XYZ, alpha is pad)
    #   bone index         :  i = round(c * 255)
    #   bone weight        :  w = c           (no transform)
    tangents  = ([((c[0] * 2.0 - 1.0),
                   (c[1] * 2.0 - 1.0),
                   (c[2] * 2.0 - 1.0)) for c in raw_tan]
                 if raw_tan else None)
    binormals = ([((c[0] * 2.0 - 1.0),
                   (c[1] * 2.0 - 1.0),
                   (c[2] * 2.0 - 1.0)) for c in raw_bin]
                 if raw_bin else None)
    bone_indices = ([(int(round(c[0] * 255.0)),
                      int(round(c[1] * 255.0)),
                      int(round(c[2] * 255.0)),
                      int(round(c[3] * 255.0))) for c in raw_bid]
                    if raw_bid else None)
    bone_weights = ([(float(c[0]), float(c[1]),
                      float(c[2]), float(c[3])) for c in raw_bwt]
                    if raw_bwt else None)

    # ---- Diffuse texture path (best-effort, first image-tex node) -----
    diffuse_path = None
    for mat in me.materials:
        if not mat or not mat.node_tree:
            continue
        for node in mat.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and node.image and node.image.filepath:
                p = bpy.path.abspath(node.image.filepath)
                if os.path.isfile(p):
                    diffuse_path = p
                    break
        if diffuse_path:
            break

    return {
        'name':         obj.name,
        'positions':    positions,
        'normals':      normals,
        'uvs':          uvs,
        'uvs2':         uvs2,
        'indices':      indices,
        'tangents':     tangents,
        'binormals':    binormals,
        'bone_indices': bone_indices,
        'bone_weights': bone_weights,
        'model_matrix': model_matrix,
        'diffuse_path': diffuse_path,
    }


if __name__ == '__main__':
    main()
