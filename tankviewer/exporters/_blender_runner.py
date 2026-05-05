"""
Blender-side runner: builds a scene from a JSON payload and exports it.

Invoked by tankviewer.exporters.blender_bridge as:

    blender --background --python <this file> -- \\
        --input  <payload.json> \\
        --output <out.fbx | out.glb | out.obj> \\
        --format <fbx | glb | obj>

The payload schema is what `common.collect_payload` produces.  See that
module for the field-by-field contract; in short, each entry under
'meshes' has positions/normals/uvs/indices, a model matrix, and a
material with texture filenames (relative to a sidecar 'tex_dir').

NOTE: This file is executed by Blender's Python interpreter, not by the
Tank Viewer's interpreter, so `bpy` is available here but the rest of
the tankviewer package is NOT importable.  Keep imports minimal.
"""

import os
import sys
import json
import argparse


def main():
    args = _parse_argv()

    # Bootstrap: must run inside Blender; bail with a clear error
    # if for some reason this script was launched standalone.
    try:
        import bpy
    except ImportError:
        print("[blender-runner] bpy not available -- run inside Blender.")
        sys.exit(2)

    with open(args.input, 'r', encoding='utf-8') as fh:
        payload = json.load(fh)

    # --- Wipe Blender's default scene (cube, camera, light) -----------------
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    # Also nuke leftover datablocks so the resulting file is minimal
    for col in (bpy.data.meshes, bpy.data.materials, bpy.data.textures,
                bpy.data.images, bpy.data.armatures, bpy.data.lights,
                bpy.data.cameras):
        for item in list(col):
            col.remove(item, do_unlink=True)

    tex_dir = payload.get('tex_dir', '')
    img_cache = {}    # texture filename -> bpy Image (dedupe)

    # --- Build a parent Empty so the user gets a clean root in Blender ----
    root = bpy.data.objects.new(payload['name'] or "Tank", None)
    bpy.context.collection.objects.link(root)
    # Source-tank tag: a child Empty whose name encodes the WoT tank
    # XML basename.  Visible in the outliner so the user knows it's
    # there, survives FBX round-trip without depending on Blender's
    # custom-property feature.  Magic prefix '_WOT_TANK_' lets the
    # importer find it without scanning every object's name.
    src_tank = payload.get('source_tank')
    if src_tank:
        # Strip any path / extension before stamping
        src_tank = os.path.splitext(os.path.basename(str(src_tank)))[0]
        tag = bpy.data.objects.new(f"_WOT_TANK_{src_tank}", None)
        bpy.context.collection.objects.link(tag)
        tag.parent = root

    # --- One mesh + one material per sub-mesh ------------------------------
    for mdef in payload.get('meshes', []):
        obj = _build_mesh_object(bpy, mdef, tex_dir, img_cache)
        if obj is None:
            continue
        # Parent to the root empty so move/rotate ops act on the whole tank
        obj.parent = root

    # NOTE: we deliberately do NOT save a .blend file alongside the
    # export.  An earlier version of this runner did (as a debugging
    # convenience) but that polluted the user's export folder with
    # a 10-20 MB working file every time.  The headless Blender
    # process exits cleanly without saving; only the requested
    # FBX / GLB / GLTF / OBJ lands on disk.

    # --- Export ------------------------------------------------------------
    fmt = args.format.lower()
    if fmt == 'fbx':
        bpy.ops.export_scene.fbx(
            filepath=args.output,
            use_selection=False,
            apply_unit_scale=True,
            apply_scale_options='FBX_SCALE_NONE',
            axis_forward='-Z',
            axis_up='Y',
            object_types={'EMPTY', 'MESH'},
            use_mesh_modifiers=True,
            mesh_smooth_type='EDGE',
            # path_mode='AUTO' writes texture paths into the FBX that
            # point at our existing <base>_textures/ sidecar folder
            # (the one _build_mesh_object already populated).  We used
            # to use 'COPY' here, but COPY makes Blender clone every
            # texture a SECOND time into a <basename>.fbm/ folder next
            # to the FBX -- duplicates of files we just wrote, in a
            # location no downstream tool expects.  AUTO + the sidecar
            # folder is what the rest of the pipeline (and our own
            # importer) is built around.
            path_mode='AUTO',
            embed_textures=False,
        )
    elif fmt == 'glb':
        # GLB is the binary glTF container -- single self-contained
        # file with geometry buffer + textures + materials all packed
        # in.  Best for "send me the model" workflows.
        bpy.ops.export_scene.gltf(
            filepath=args.output,
            export_format='GLB',
            export_apply=True,
            export_materials='EXPORT',
            export_image_format='AUTO',     # keep PNG as PNG, JPG as JPG
        )
    elif fmt == 'gltf':
        # GLTF_EMBEDDED writes ONE .gltf file with the geometry buffer
        # AND every referenced texture inlined as base64 data URIs --
        # no sidecar .bin, no sidecar .png/.jpg, just one self-contained
        # JSON.  Larger on disk than GLB but human-inspectable, which
        # is the whole point of asking for the .gltf form over .glb.
        # Materials carry the full Principled-BSDF node graph
        # (diffuse + normal + metallic/roughness from GMM) that
        # _build_material constructs upstream -- glTF exporter walks
        # that node graph and writes a real PBR shader stack.
        bpy.ops.export_scene.gltf(
            filepath=args.output,
            export_format='GLTF_EMBEDDED',
            export_apply=True,
            export_materials='EXPORT',
            export_image_format='AUTO',
        )
    elif fmt == 'obj':
        bpy.ops.wm.obj_export(
            filepath=args.output,
            apply_modifiers=True,
            export_materials=True,
            export_uv=True,
            export_normals=True,
        )
    else:
        print(f"[blender-runner] unknown format: {fmt!r}")
        sys.exit(3)

    print(f"[blender-runner] wrote {args.output}")


# ---------------------------------------------------------------------------

def _parse_argv():
    """Strip Blender's own args and parse our flags after `--`."""
    argv = sys.argv
    if '--' in argv:
        argv = argv[argv.index('--') + 1:]
    else:
        argv = []
    p = argparse.ArgumentParser()
    p.add_argument('--input',  required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--format', default='fbx',
                   choices=('fbx', 'glb', 'gltf', 'obj'))
    return p.parse_args(argv)


def _build_mesh_object(bpy, mdef, tex_dir, img_cache):
    """Create a bpy Mesh + Object from one entry of payload['meshes'].

    Coordinate-system handling
    --------------------------
    Tank Viewer holds vertex data in OpenGL space (right-handed,
    Y-up, -Z forward).  Blender's internal scene is right-handed
    Z-up -Y-forward.  Blender's FBX exporter is then configured to
    rotate Z-up -> Y-up at write time (axis_up='Y', axis_forward='-Z'
    below).  So the chain is:

        OpenGL Y-up  --(swizzle here)-->  Blender Z-up
                                                |
                                                |  FBX exporter rotates
                                                v
                                          FBX  Y-up   <-- final file

    The two rotations cancel out and the FBX file ends up matching the
    user's original Y-up data with no axis flips.

    Swizzle: (x, y, z)  ->  (x, -z, y)
    """
    name      = mdef['name']
    positions = mdef['positions']
    normals   = mdef.get('normals')
    uvs       = mdef.get('uvs')
    # Optional second UV channel (lightmap / detail-routing).  Present
    # only on parts where the WoT primitives_processed had a sidecar
    # '.uv2' section or an 'uvuv' interleaved format.  Stays None for
    # everything else so we don't waste a Blender uv_layer slot on
    # static hull/turret meshes that don't need one.
    uvs2      = mdef.get('uvs2')
    indices   = mdef['indices']

    # OpenGL Y-up -> Blender Z-up swizzle on positions and normals
    positions_bl = [(p[0], -p[2], p[1]) for p in positions]
    normals_bl   = ([(n[0], -n[2], n[1]) for n in normals]
                    if normals else None)

    # Build the mesh datablock
    me = bpy.data.meshes.new(name + "_mesh")
    # Triangle list -> faces of length 3
    faces = [tuple(indices[i:i + 3]) for i in range(0, len(indices), 3)]
    me.from_pydata(positions_bl, [], faces)
    me.update(calc_edges=True)

    # Per-vertex normals: Blender 4.1+ removed use_auto_smooth (it's
    # implicit now); older Blender required it to be True before the
    # custom normals call would stick.  Set it conditionally.
    if normals_bl:
        try:
            if hasattr(me, 'use_auto_smooth'):
                me.use_auto_smooth = True
            me.normals_split_custom_set_from_vertices(
                [tuple(n) for n in normals_bl])
        except Exception as exc:
            print(f"[blender-runner] custom normals failed for "
                  f"{name}: {exc}")

    # UV layer (primary -- 'UVMap', the Blender default)
    if uvs:
        uv_layer = me.uv_layers.new(name='UVMap')
        uv_data  = uv_layer.data
        for poly in me.polygons:
            for li in poly.loop_indices:
                vi = me.loops[li].vertex_index
                if 0 <= vi < len(uvs):
                    uv_data[li].uv = (uvs[vi][0], uvs[vi][1])

    # Second UV layer (only when the source actually carried one).
    # Naming: 'UVMap2' -- matches what FBX/glTF importers create when
    # they read a second UV set, so a downstream Blender artist sees
    # a familiar channel name.  The runner *importer* (round-trip
    # companion) walks every uv_layer regardless of name, so any
    # rename in Blender survives back to the WoT side.
    if uvs2:
        uv2_layer = me.uv_layers.new(name='UVMap2')
        uv2_data  = uv2_layer.data
        for poly in me.polygons:
            for li in poly.loop_indices:
                vi = me.loops[li].vertex_index
                if 0 <= vi < len(uvs2):
                    uv2_data[li].uv = (uvs2[vi][0], uvs2[vi][1])

    # Stash every remaining per-vertex value as a named FLOAT_COLOR
    # vertex attribute so FBX/glTF preserve them through round-trip.
    # Naming: 'WoT...' so an importer can find them and reconstruct
    # the original vertex stream.
    #
    #   WoTTangent     <- mesh.tangents   (xyz, A=1)
    #   WoTBinormal    <- mesh.binormals  (xyz, A=1)
    #   WoTBoneIdx     <- mesh.bone_indices  (4 byte-indices as floats)
    #   WoTBoneWeight  <- mesh.bone_weights  (4 floats in [0,1])
    #
    # FLOAT_COLOR (vs BYTE_COLOR) gives full float precision -- no
    # clamping to [0,1] and no quantisation, so unit-length tangents
    # and 0..255 bone indices both survive untouched.
    _add_float_color(bpy, me, 'WoTTangent',
                     _vec3_to_vec4(mdef.get('tangents'),  pad=1.0))
    _add_float_color(bpy, me, 'WoTBinormal',
                     _vec3_to_vec4(mdef.get('binormals'), pad=1.0))
    _add_float_color(bpy, me, 'WoTBoneIdx',
                     _ints_to_vec4(mdef.get('bone_indices')))
    _add_float_color(bpy, me, 'WoTBoneWeight',
                     _floats_to_vec4(mdef.get('bone_weights')))

    # Material with a Principled BSDF + AM connected to Base Color
    mat_def = mdef.get('material') or {}
    mat = _build_material(bpy, mat_def, tex_dir, img_cache)
    if mat:
        me.materials.append(mat)

    # Object + transform
    obj = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(obj)

    mm = mdef.get('model_matrix')
    if mm and len(mm) == 16:
        from mathutils import Matrix
        # Our model_matrix is numpy row-major with translation in
        # column 3 (M[0,3]=tx, M[1,3]=ty, M[2,3]=tz).  Apply the same
        # Y-up -> Z-up swizzle to the translation: (tx, ty, tz) ->
        # (tx, -tz, ty).  For tank components this matrix is identity
        # rotation + a translation, so swapping the column suffices.
        # If we ever ship rotated component matrices, replace this with
        # a full basis change M_bl = B @ M @ B^-1.
        tx, ty, tz = mm[3], mm[7], mm[11]
        rows = [
            [mm[0], mm[1], mm[2],  tx],
            [mm[4], mm[5], mm[6], -tz],
            [mm[8], mm[9], mm[10], ty],
            [mm[12], mm[13], mm[14], mm[15]],
        ]
        obj.matrix_world = Matrix(rows)

    return obj


def _add_float_color(bpy, me, name, vec4_per_vertex):
    """Create a FLOAT_COLOR domain=POINT attribute on `me` and fill
    it with `vec4_per_vertex`.  No-op when the data is None / empty.

    FLOAT_COLOR is a 4xfloat32 attribute -- no clamping, no quantisation,
    so values like (-0.7, +1.2, 0, 1) round-trip through FBX cleanly.
    """
    if not vec4_per_vertex:
        return
    try:
        attr = me.color_attributes.new(name=name, type='FLOAT_COLOR',
                                       domain='POINT')
    except Exception as exc:
        print(f"[blender-runner] couldn't add color attr {name}: {exc}")
        return
    n = min(len(attr.data), len(vec4_per_vertex))
    for i in range(n):
        attr.data[i].color = vec4_per_vertex[i]


# ---- Encoders ---------------------------------------------------------------
#
# Blender's FBX *exporter* writes our FLOAT_COLOR attributes as float
# RGBA in the FBX file.  Blender's FBX *importer*, however, reads them
# back as BYTE_COLOR (8-bit, clamped to [0, 1]).  To survive that
# round-trip we encode every value into [0, 1] before storage and
# document how an importer should reverse it.
#
# Channel layouts and decoders:
#
#   WoTTangent     pack: (v + 1) * 0.5         decode: v = c*2 - 1
#   WoTBinormal    pack: (v + 1) * 0.5         decode: v = c*2 - 1
#   WoTBoneIdx     pack: i / 255.0             decode: i = round(c * 255)
#   WoTBoneWeight  pack: w (already in [0,1])  decode: w = c
#
# Quantisation: byte-color storage gives 1/255 precision per channel.
# For tangents/binormals (unit vectors), that's about 0.5 degrees of
# angular error -- visually invisible.  Bone indices are integer 0..255
# in WoT, so /255 + round() recovers them exactly.  Bone weights are
# already byte-quantised in the source (uint8 / 255 at parse time),
# so byte-color storage is lossless for them.

def _vec3_to_vec4(arr, pad=1.0):
    """Tangent / binormal encoder.  Unit vectors [-1, +1] -> [0, 1].
    Returns [(R, G, B, pad), ...] suitable for a FLOAT_COLOR attribute.
    """
    if not arr:
        return None
    return [((v[0] + 1.0) * 0.5,
             (v[1] + 1.0) * 0.5,
             (v[2] + 1.0) * 0.5,
             pad) for v in arr]


def _ints_to_vec4(arr):
    """Bone-index encoder.  uint8 [0, 255] -> [0, 1].  Integer values
    survive byte-color quantisation exactly (the storage IS 8-bit).
    """
    if not arr:
        return None
    return [(r[0] / 255.0,
             r[1] / 255.0,
             r[2] / 255.0,
             r[3] / 255.0) for r in arr]


def _floats_to_vec4(arr):
    """Bone-weight encoder.  Already in [0, 1] from MeshParser, so this
    is just a tuple-cast for the bpy color slot."""
    if not arr:
        return None
    return [(float(r[0]), float(r[1]), float(r[2]), float(r[3]))
            for r in arr]


def _build_material(bpy, mat_def, tex_dir, img_cache):
    """Return a new bpy Material populated with a Principled BSDF and
    image-texture nodes for any maps the payload supplied."""
    name = mat_def.get('name') or 'TankMat'
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    mat.use_backface_culling = not mat_def.get('double_sided', False)

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    bsdf  = nodes.get('Principled BSDF')
    if bsdf is None:
        bsdf = nodes.new('ShaderNodeBsdfPrincipled')

    def _load_image(filename):
        if not filename:
            return None
        if filename in img_cache:
            return img_cache[filename]
        path = os.path.join(tex_dir, filename)
        if not os.path.isfile(path):
            return None
        try:
            img = bpy.data.images.load(path, check_existing=True)
            img_cache[filename] = img
            return img
        except Exception as exc:
            print(f"[blender-runner] image load failed {path}: {exc}")
            return None

    # Base Color from diffuse (AM)
    am = _load_image(mat_def.get('diffuse'))
    if am:
        node = nodes.new('ShaderNodeTexImage')
        node.image = am
        node.location = (-400, 200)
        links.new(node.outputs['Color'], bsdf.inputs['Base Color'])
        if mat_def.get('alpha_test'):
            mat.blend_method = 'CLIP'
            links.new(node.outputs['Alpha'], bsdf.inputs['Alpha'])

    # Normal map (NM) via a Normal Map node
    nm = _load_image(mat_def.get('normal'))
    if nm:
        nm.colorspace_settings.name = 'Non-Color'
        tex = nodes.new('ShaderNodeTexImage')
        tex.image = nm
        tex.location = (-400, -100)
        nmap = nodes.new('ShaderNodeNormalMap')
        nmap.location = (-200, -100)
        links.new(tex.outputs['Color'], nmap.inputs['Color'])
        links.new(nmap.outputs['Normal'], bsdf.inputs['Normal'])

    # GMM: B = metallic, G = gloss (-> roughness = 1-G).  Split via
    # Separate RGB.  Skipped if missing.
    gmm = _load_image(mat_def.get('gmm'))
    if gmm:
        gmm.colorspace_settings.name = 'Non-Color'
        tex = nodes.new('ShaderNodeTexImage')
        tex.image = gmm
        tex.location = (-700, -350)
        sep = nodes.new('ShaderNodeSeparateColor')
        sep.location = (-450, -350)
        sep.mode = 'RGB'
        links.new(tex.outputs['Color'], sep.inputs['Color'])
        # Metallic <- B
        links.new(sep.outputs['Blue'], bsdf.inputs['Metallic'])
        # Roughness <- 1 - G  (Math node)
        invert = nodes.new('ShaderNodeMath')
        invert.location = (-200, -350)
        invert.operation = 'SUBTRACT'
        invert.inputs[0].default_value = 1.0
        links.new(sep.outputs['Green'], invert.inputs[1])
        links.new(invert.outputs[0], bsdf.inputs['Roughness'])

    return mat


# ---------------------------------------------------------------------------

if __name__ == '__main__':
    main()
