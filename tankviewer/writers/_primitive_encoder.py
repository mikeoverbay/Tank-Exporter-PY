"""
Byte-level encoder for the .primitives_processed format.

Direct port of the four key writer routines from the legacy VB
TankExporter (`Modules/modPrimWriter.vb`):

    write_primitives(ID)        -> encode_file()        -- orchestrator
    write_list_data(id)         -> _encode_indices_*()  -- index sections
    write_vertex_data(id)       -> _encode_vertices_*() -- vertex sections
    write_UV2(id)               -> _encode_uv2_*()      -- UV2 sidecar

What's NOT carried over (per project decisions):
    * write_BSP2 -- collision data, "old format, not used today"
    * The visual-rewrite pass that splices new <PG_ID> blocks into
      the .visual_processed XML when the user added new objects.
      Will be revisited when we wire up "add new mesh to a tank".

================================================================
Section-naming layout -- TWO MODES
================================================================

WoT's .primitives_processed comes in two flavours and we have to
preserve whichever the source file used (the .visual_processed
references match one or the other -- they're not interchangeable).

MODE A: BARE-SHARED  (e.g. chassis on G102_Pz_III)
    * ONE `indices` section + ONE `vertices` section, no name prefix.
    * The indices section's group_count field reports N (the number
      of primitive groups), and N entries of (start_index, prim_count,
      start_vertex, vert_count) live after the index data.
    * Visual references by *index*, not by name.
    * Detected by: every mesh.name is empty.

MODE B: NAMED-PER-MESH  (e.g. some hull / turret variants)
    * One `<base>.indices` + `<base>.vertices` section per mesh, where
      <base> is mesh.name (preserved verbatim from the source file).
    * Each section's group_count field is 1.  The single primitive
      group entry covers the whole local buffer.
    * Visual references by *name*.
    * Detected by: every mesh.name is non-empty.

We never *invent* a name -- if mesh.name is empty we always use the
bare-shared layout, even if the mesh has a non-empty mesh.identifier.
The identifier is the WoT material name (an internal label on the
visual side); it is NOT the section name and substituting it would
break engine lookups.

Mixed input (some named, some empty) is rejected with ValueError --
the source files we've seen are uniform per-component, and a mixed
write would silently corrupt the visual<->primitives binding.

See VISUAL_PROCESSED_FORMAT.md for the on-disk byte spec.

Returned by encode_file: bytes object containing the ENTIRE file from
magic header through trailing section-table-offset.  Caller writes it
to disk (atomic-ish) so a half-written file never lands at the target
path.
"""

import io
import struct

from ..common import pack_normal_bpvt


# Magic header: little-endian uint32 = 0x42A14E65, written as bytes
# 65 4E A1 42  -- the BWXML magic that load_primitives_processed sees
# at the start of every file.  Fixed; do not change.
_MAGIC = 0x42A14E65


# ---------------------------------------------------------------------------
# Helpers

def _pad_str(s, total_bytes):
    """Right-pad an ASCII string to `total_bytes` with NUL bytes."""
    b = s.encode('ascii')
    if len(b) >= total_bytes:
        return b[:total_bytes]
    return b + b'\x00' * (total_bytes - len(b))


def _align4(stream):
    """Pad `stream` (a BytesIO) to a 4-byte boundary.  Returns the
    number of pad bytes written so the caller can subtract them when
    reporting the section's logical size."""
    pad = (-stream.tell()) & 3
    if pad:
        stream.write(b'\x00' * pad)
    return pad


def _classify_layout(meshes):
    """Decide which layout (bare-shared / named-per-mesh) to write.

    Returns one of:
        'bare'  : every mesh has empty mesh.name -> single shared
                  indices+vertices section pair, group_count = N.
        'named' : every mesh has non-empty mesh.name -> one named
                  section pair per mesh.

    Raises ValueError on mixed input -- writing some meshes named and
    some not would produce a file the visual can't fully resolve, and
    we'd rather fail loud than silently corrupt the binding.
    """
    has_named = []
    for m in meshes:
        nm = (getattr(m, 'name', '') or '').strip()
        has_named.append(bool(nm))

    if all(has_named):
        return 'named'
    if not any(has_named):
        return 'bare'
    # Mixed -- the source layout is ambiguous; bail rather than guess.
    bad = [(i, getattr(meshes[i], 'name', ''))
           for i, h in enumerate(has_named)]
    raise ValueError(
        f"encode_file: mesh names are mixed (some named, some empty); "
        f"cannot decide between bare-shared and named-per-mesh layouts. "
        f"Per-mesh names: {bad}")


# ---------------------------------------------------------------------------
# MODE A -- BARE-SHARED encoders

def _encode_indices_bare(meshes, list32):
    """Bare-shared `indices` section: one combined buffer covering
    every mesh, with N primitive-group metadata entries.

    Layout:
        offset 0..63    : 64-byte format string ('list' / 'list32')
        offset 64..67   : uint32 total_index_count
        offset 68..69   : uint16 primitive_group_count = N
        offset 70..71   : 2 bytes padding
        offset 72..    : indices (concatenated, re-based by running
                                  vertex offset)
        offset (after) : N * 16 bytes of (start_index, prim_count,
                                          start_vertex, vert_count)

    Re-flips winding back to source CW order (loader did CCW->source
    flip at parse time, so we re-reverse here in 3-element strides).

    Returns (bytes, padding_byte_count).
    """
    fmt_str = 'list32' if list32 else 'list'
    buf = io.BytesIO()
    buf.write(_pad_str(fmt_str, 64))

    total_indices = sum(len(m.indices) for m in meshes)
    buf.write(struct.pack('<IHH', total_indices, len(meshes), 0))

    pack_idx = '<I' if list32 else '<H'
    vert_off = 0
    for m in meshes:
        idx = list(m.indices)
        # Re-reverse winding (3-element strides) to undo the loader's
        # source->OpenGL flip, then re-base into the shared buffer by
        # adding the running vertex offset.
        for j in range(0, len(idx) - 2, 3):
            idx[j], idx[j + 2] = idx[j + 2], idx[j]
        for raw in idx:
            buf.write(struct.pack(pack_idx, int(raw) + vert_off))
        vert_off += len(m.positions)

    # Per-mesh primitiveGroup metadata.
    s_index = 0
    s_vertex = 0
    for m in meshes:
        n_inds  = len(m.indices)
        n_verts = len(m.positions)
        n_prims = n_inds // 3
        buf.write(struct.pack('<IIII', s_index, n_prims, s_vertex, n_verts))
        s_index  += n_inds
        s_vertex += n_verts

    pad = _align4(buf)
    return buf.getvalue(), pad


def _encode_vertices_bare(meshes, want_bones, want_tangents):
    """Bare-shared `vertices` section: one stream containing every
    mesh's vertices in the same order the indices were emitted.

    Layout:
        offset 0..63    : 64-byte format string (BPVT primary)
        offset 64..127  : 64-byte secondary format string (set3/...)
        offset 128..131 : uint32 vertex_count = sum(len(m.positions))
        offset 132..    : per-vertex stream

    Format-flag detection runs ACROSS all meshes here -- bare layout
    uses a single shared format string, so any flag (bones/tangents)
    must be set if even one mesh in the file carries that data.  See
    encode_file for the orchestrator-level decision.

    Returns (bytes, padding_byte_count).
    """
    primary, secondary = _format_strings(want_bones, want_tangents)
    buf = io.BytesIO()
    buf.write(_pad_str(primary,   68))
    buf.write(_pad_str(secondary, 64))

    total_verts = sum(len(m.positions) for m in meshes)
    buf.write(struct.pack('<I', total_verts))

    for m in meshes:
        _pack_mesh_vertex_stream(buf, m, want_bones, want_tangents)

    pad = _align4(buf)
    return buf.getvalue(), pad


def _encode_uv2_bare(meshes):
    """Bare-shared `uv2` sidecar covering every vertex in the file.

    Meshes without a uv1 array contribute zero-pairs so the section
    body always lines up with the shared vertex buffer in 1:1 order.

    Preamble layout MIRRORS the matching vertices section -- 136 bytes
    total, not 132 -- since the engine probes both at the same body
    offset:
        offset 0..67   (68 bytes) : primary format string 'BPVSuv2'
        offset 68..131 (64 bytes) : secondary format string 'set3/uv2pc'
        offset 132..135 (4 bytes) : uint32 vertex_count
    """
    buf = io.BytesIO()
    buf.write(_pad_str('BPVSuv2',     68))
    buf.write(_pad_str('set3/uv2pc',  64))
    total_verts = sum(len(m.positions) for m in meshes)
    buf.write(struct.pack('<I', total_verts))

    for m in meshes:
        n = len(m.positions)
        uv1 = getattr(m, 'uv1', None)
        if uv1 is None:
            buf.write(b'\x00' * (8 * n))
            continue
        for k in range(n):
            u = float(uv1[k][0])
            v = 1.0 - float(uv1[k][1])
            buf.write(struct.pack('<ff', u, v))

    pad = _align4(buf)
    return buf.getvalue(), pad


# ---------------------------------------------------------------------------
# MODE B -- NAMED-PER-MESH encoders

def _encode_indices_named(mesh, list32):
    """Named-per-mesh `<name>.indices`: one mesh, one section, one
    primitive group covering the whole local buffer.

    Layout (same byte spec as bare; only meaning differs):
        offset 0..63    : 64-byte format string ('list' / 'list32')
        offset 64..67   : uint32 index_count   (this mesh only)
        offset 68..69   : uint16 group_count = 1
        offset 70..71   : 2 bytes padding
        offset 72..    : the indices themselves
        offset (after) : single 16-byte primitive-group metadata
                          (start_index=0, prim_count, start_vertex=0,
                           vert_count)

    Returns (bytes, padding_byte_count).
    """
    fmt_str = 'list32' if list32 else 'list'
    buf = io.BytesIO()
    buf.write(_pad_str(fmt_str, 64))

    idx = list(mesh.indices)
    n_inds = len(idx)
    n_verts = len(mesh.positions)
    n_prims = n_inds // 3

    buf.write(struct.pack('<IHH', n_inds, 1, 0))

    for j in range(0, n_inds - 2, 3):
        idx[j], idx[j + 2] = idx[j + 2], idx[j]
    pack_idx = '<I' if list32 else '<H'
    for raw in idx:
        buf.write(struct.pack(pack_idx, int(raw)))

    buf.write(struct.pack('<IIII', 0, n_prims, 0, n_verts))

    pad = _align4(buf)
    return buf.getvalue(), pad


def _encode_vertices_named(mesh, want_bones, want_tangents):
    """Named-per-mesh `<name>.vertices`: one mesh, one section.

    Format flags are derived from THIS MESH, not from the file as a
    whole -- mixed bone/non-bone meshes in the same file is fine here
    because each gets its own format string.
    """
    primary, secondary = _format_strings(want_bones, want_tangents)
    buf = io.BytesIO()
    buf.write(_pad_str(primary,   68))
    buf.write(_pad_str(secondary, 64))

    n_verts = len(mesh.positions)
    buf.write(struct.pack('<I', n_verts))

    _pack_mesh_vertex_stream(buf, mesh, want_bones, want_tangents)

    pad = _align4(buf)
    return buf.getvalue(), pad


def _encode_uv2_named(mesh):
    """Named-per-mesh `<name>.uv2`: one mesh, one sidecar.

    Same 136-byte preamble layout as `_encode_uv2_bare` (mirrors the
    vertices preamble), just sized for one mesh.
    """
    buf = io.BytesIO()
    buf.write(_pad_str('BPVSuv2',     68))
    buf.write(_pad_str('set3/uv2pc',  64))
    n = len(mesh.positions)
    buf.write(struct.pack('<I', n))
    uv1 = mesh.uv1
    for k in range(n):
        u = float(uv1[k][0])
        v = 1.0 - float(uv1[k][1])
        buf.write(struct.pack('<ff', u, v))
    pad = _align4(buf)
    return buf.getvalue(), pad


# ---------------------------------------------------------------------------
# Shared helpers used by both layouts

def _format_strings(want_bones, want_tangents):
    """Pick the (primary, secondary) BPVT format strings based on what
    the vertex stream carries.  Centralised so the bare and named
    encoders agree on the four valid combinations."""
    if want_bones and want_tangents:
        return 'BPVTxyznuviiiwwtb',  'set3/xyznuviiiwwtbpc'
    if want_tangents:
        return 'BPVTxyznuvtb',       'set3/xyznuvtbpc'
    if want_bones:
        return 'BPVTxyznuviiiww',    'set3/xyznuviiiwwpc'
    return 'BPVTxyznuv', 'set3/xyznuvpc'


def _pack_mesh_vertex_stream(buf, mesh, want_bones, want_tangents):
    """Append one mesh's worth of vertices to `buf` using BPVT byte
    layout.  Used by both bare-shared and named-per-mesh paths so the
    per-vertex byte format stays identical across layouts.

    Z-flip back to WoT convention (+Z forward) for non-skinned meshes
    -- the loader applied `z *= -1` at parse time, so we undo it
    here.  Skinned meshes were loaded WITHOUT Z-flip and pass through
    unchanged.
    """
    positions = mesh.positions
    normals   = mesh.normals
    tangents  = (mesh.tangents  if want_tangents
                 and mesh.tangents  is not None else None)
    binormals = (mesh.binormals if want_tangents
                 and mesh.binormals is not None else None)
    uv0       = mesh.uv0
    bone_idx  = (mesh.bone_indices if want_bones
                 and mesh.bone_indices is not None else None)
    bone_wt   = (mesh.bone_weights if want_bones
                 and mesh.bone_weights is not None else None)

    flip_z = not want_bones
    n_verts = len(positions)
    for k in range(n_verts):
        x = float(positions[k][0])
        y = float(positions[k][1])
        z = float(positions[k][2])
        if flip_z:
            z = -z
        buf.write(struct.pack('<fff', x, y, z))

        nx = float(normals[k][0])
        ny = float(normals[k][1])
        nz = float(normals[k][2])
        if flip_z:
            nz = -nz
        buf.write(struct.pack('<I', pack_normal_bpvt(nx, ny, nz)))

        u  = float(uv0[k][0])
        v  = 1.0 - float(uv0[k][1])
        buf.write(struct.pack('<ff', u, v))

        if want_bones:
            if bone_idx is not None:
                bi = bone_idx[k]
                buf.write(struct.pack('<4B',
                    int(bi[0]) & 0xFF, int(bi[1]) & 0xFF,
                    int(bi[2]) & 0xFF, int(bi[3]) & 0xFF))
            else:
                buf.write(b'\x00\x00\x00\x00')
            if bone_wt is not None:
                bw = bone_wt[k]
                buf.write(struct.pack('<4B',
                    max(0, min(255, int(round(float(bw[0]) * 255)))),
                    max(0, min(255, int(round(float(bw[1]) * 255)))),
                    max(0, min(255, int(round(float(bw[2]) * 255)))),
                    max(0, min(255, int(round(float(bw[3]) * 255))))))
            else:
                buf.write(b'\x00\x00\x00\x00')

        if want_tangents:
            if tangents is not None:
                tx = float(tangents[k][0])
                ty = float(tangents[k][1])
                tz = float(tangents[k][2])
            else:
                tx = ty = tz = 0.0
            if binormals is not None:
                bx = float(binormals[k][0])
                by = float(binormals[k][1])
                bz = float(binormals[k][2])
            else:
                bx = by = bz = 0.0
            if flip_z:
                tz = -tz
                bz = -bz
            buf.write(struct.pack('<I', pack_normal_bpvt(tx, ty, tz)))
            buf.write(struct.pack('<I', pack_normal_bpvt(bx, by, bz)))


# ---------------------------------------------------------------------------
# Section table

def _section_table_entry(size, name):
    """Build one section-table entry as raw bytes:
        uint32 size + 16 zero bytes + uint32 name_length + ASCII name
        + pad to 4-byte alignment.
    Returns (entry_bytes, pad_byte_count) so the caller can keep a
    running offset for the trailing offset write.
    """
    name_b = name.encode('ascii')
    out = io.BytesIO()
    out.write(struct.pack('<I', size))
    out.write(b'\x00' * 16)
    out.write(struct.pack('<I', len(name_b)))
    out.write(name_b)
    pad = (-out.tell()) & 3
    if pad:
        out.write(b'\x00' * pad)
    return out.getvalue(), pad


# ---------------------------------------------------------------------------
# Public entry point

def encode_file(meshes, want_uv2=True):
    """Encode `meshes` as a single .primitives_processed file.

    Picks bare-shared vs named-per-mesh layout from the per-mesh
    `mesh.name` field (see `_classify_layout` for the rules).  Never
    invents section names from `mesh.identifier` -- that's the WoT
    material identifier on the visual side, not a primitives section
    name, and substituting it would break engine lookups.

    Args:
        meshes  (list[Mesh]): every mesh that belongs in the output
                              file (caller already grouped by component).
        want_uv2 (bool): when True AND a mesh has uv1 data, emit a
                              UV2 sidecar section.  Bare layout writes
                              one shared `uv2` section; named layout
                              writes `<name>.uv2` per mesh.

    Returns:
        bytes -- the full file contents, ready to write to disk.
    """
    if not meshes:
        raise ValueError("encode_file: empty mesh list")

    layout = _classify_layout(meshes)

    if layout == 'bare':
        return _encode_file_bare(meshes, want_uv2)
    return _encode_file_named(meshes, want_uv2)


def _encode_file_bare(meshes, want_uv2):
    """Assemble a MODE A (bare-shared) file."""
    # Index width is driven by the SUM of vertices across all meshes
    # because they share one buffer in this layout.
    total_verts = sum(len(m.positions) for m in meshes)
    list32 = total_verts > 0xFFFF

    # File-wide format flags: any bone-carrying mesh forces 'iiiww';
    # any tangent-carrying mesh forces 'tb'.  Bare layout shares ONE
    # vertex format string across the whole stream, so we can't have
    # mixed presence within a single file -- the loader re-checks.
    want_bones = any(m.bone_indices is not None and m.bone_weights is not None
                     for m in meshes)
    want_tangents = any(m.tangents is not None and m.binormals is not None
                        for m in meshes)

    indices_data,  ipad = _encode_indices_bare(meshes, list32)
    indices_size  = len(indices_data)  - ipad
    vertices_data, vpad = _encode_vertices_bare(meshes, want_bones, want_tangents)
    vertices_size = len(vertices_data) - vpad

    has_uv2 = want_uv2 and any(getattr(m, 'uv1', None) is not None
                                for m in meshes)
    if has_uv2:
        uv2_data, upad = _encode_uv2_bare(meshes)
        uv2_size = len(uv2_data) - upad
    else:
        uv2_data = b''
        uv2_size = 0

    out = io.BytesIO()
    out.write(struct.pack('<I', _MAGIC))
    out.write(indices_data)
    out.write(vertices_data)
    if has_uv2:
        out.write(uv2_data)

    return _finalize(out, [
        (indices_size,  'indices'),
        (vertices_size, 'vertices'),
    ] + ([(uv2_size, 'uv2')] if has_uv2 else []))


def _encode_file_named(meshes, want_uv2):
    """Assemble a MODE B (named-per-mesh) file."""
    # Each mesh's vertices live in its own local buffer, so index
    # width is driven by the LARGEST single mesh -- not the sum.
    max_local = max(len(m.positions) for m in meshes)
    list32 = max_local > 0xFFFF

    out_payloads = io.BytesIO()
    section_specs = []   # [(logical_size, section_name), ...]

    for mesh in meshes:
        base = mesh.name.strip()
        # Per-mesh format flags so a non-skinned mesh in a skinned
        # file doesn't get 'iiiww' tokens it doesn't actually carry.
        want_bones = (mesh.bone_indices is not None
                      and mesh.bone_weights is not None)
        want_tangents = (mesh.tangents is not None
                         and mesh.binormals is not None)

        idx_data, idx_pad = _encode_indices_named(mesh, list32)
        out_payloads.write(idx_data)
        section_specs.append((len(idx_data) - idx_pad, base + '.indices'))

        vtx_data, vtx_pad = _encode_vertices_named(
            mesh, want_bones, want_tangents)
        out_payloads.write(vtx_data)
        section_specs.append((len(vtx_data) - vtx_pad, base + '.vertices'))

        if want_uv2 and getattr(mesh, 'uv1', None) is not None:
            uv2_data, uv2_pad = _encode_uv2_named(mesh)
            out_payloads.write(uv2_data)
            section_specs.append((len(uv2_data) - uv2_pad, base + '.uv2'))

    out = io.BytesIO()
    out.write(struct.pack('<I', _MAGIC))
    out.write(out_payloads.getvalue())
    return _finalize(out, section_specs)


def _finalize(out, section_specs):
    """Append the section table + trailing offset to `out` and return
    the assembled bytes.  Common to both layout modes."""
    table_offset_running = 0
    table = io.BytesIO()
    for size, name in section_specs:
        entry, _pad = _section_table_entry(size, name)
        table.write(entry)
        table_offset_running += len(entry)
    out.write(table.getvalue())
    out.write(struct.pack('<I', table_offset_running))
    return out.getvalue()
