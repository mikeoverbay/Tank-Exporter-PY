"""
File loaders: parse WoT .primitives_processed and .visual_processed files,
and load DDS/PNG/JPG textures into OpenGL.

Classes:
    MeshParser    : static methods to parse .primitives_processed binary mesh files.
    VisualLoader  : static methods to parse .visual_processed for material/texture data.
    TextureLoader : static methods to load images and create OpenGL textures.

Inputs accepted:
    MeshParser.parse_primitives_processed(filepath)
        filepath: absolute path to a .primitives_processed file
        returns:  list of dicts, one per primitive group:
                  {'name', 'vertices' (dict of np arrays), 'indices', 'prim_groups',
                   'format', 'vertex_count'}

    VisualLoader.parse_textures(visual_file, group_names)
        visual_file: absolute path to .visual_processed file
        group_names: list of group names from the matching primitives file
        returns:  dict { group_name -> {'diffuse','normal','ao','gmm',
                                        'alpha_reference','alpha_test_enable',
                                        'double_sided','identifier'} }

    VisualLoader.resolve_hd_path(rel_path, res_mods_root)
        rel_path:      "vehicles/.../X.dds"
        res_mods_root: absolute path to the res_mods/<version>/ folder
        returns:       (absolute_path or None, used_hd: bool)

    TextureLoader.load_texture(filepath, is_normal=False)
        filepath:  absolute path to image file
        is_normal: when True, force RGBA so alpha channel survives (for ANM maps)
        returns:   GLuint texture id

    TextureLoader.create_placeholder(value)
        value: float 0..1 used as solid grayscale color
        returns: GLuint texture id (1x1 RGB)
"""

import os
import re
import shutil
import struct
import tempfile
import xml.etree.ElementTree as ET
import zipfile

import numpy as np

from OpenGL.GL import *

from .common import unpack_normal, unpack_normal_bpvt, read_c_string, decode_bwxml, is_bwxml

try:
    from PIL import Image
except ImportError:
    Image = None


# ============================================================================
# MeshParser - .primitives_processed binary mesh parser
# ============================================================================

class MeshParser:
    """Parse .primitives_processed binary mesh files."""

    @staticmethod
    def parse_primitives_processed(filepath):
        """Parse the binary file and return a list of primitive groups.

        Each group dict contains: 'name', 'vertices', 'indices', 'prim_groups',
        'format', 'vertex_count'."""
        with open(filepath, 'rb') as f:
            data = f.read()

        if len(data) < 4:
            raise ValueError("File too small")

        file_len = len(data)
        section_table_offset = struct.unpack('<i', data[file_len-4:file_len])[0]
        section_table_pos = file_len - 4 - section_table_offset
        if section_table_pos < 0 or section_table_pos >= file_len:
            raise ValueError(f"Invalid section table offset: {section_table_pos}")

        sections = MeshParser._parse_section_table(data, section_table_pos)

        # Group sections by primitive group name.  WoT's section table
        # carries up to four sections per primitive group:
        #     <base>.vertices      -- per-vertex stream (always present)
        #     <base>.indices       -- triangle list    (always present)
        #     <base>.uv2           -- second UV channel (sidecar; some
        #                             tanks/parts ship this in addition
        #                             to the interleaved 'uvuv' format)
        #     <base>.colour        -- per-vertex colour set (rare)
        # Single-group files (Hull) use bare "vertices"/"indices" so
        # the base name is the empty string.  Anything still
        # unrecognised after these four suffixes is logged so a future
        # exotic layout (e.g. 'colour2') becomes visible without code
        # changes.
        groups = {}
        unknown_sections = []   # list[(name, size)] -- diagnostic only

        # Suffix list, ordered so the longer / more specific names win
        # before shorter ambiguous ones (otherwise '.uvs2' would also
        # match the '.uvs' rule).  Each entry: (suffix, kind).
        # Multiple suffixes can map to the same kind so we don't have
        # to know in advance which spelling WoT uses.
        # WoT's shaders/formats/uv2.xml defines this stream officially
        # as a single TEXCOORD with semanticIndex=1, FLOAT2 (8 bytes
        # per vertex) on stream=10.  In the .primitives_processed file
        # it appears as a section suffixed '.uv2' -- the other
        # spellings here are belt-and-braces for older / typo'd files.
        _UV2_SUFFIXES = ('.uv2', '.uvs2', '.uv1', '.uvs1', '.uvb', '.uvsb',
                         '.uv', '.uvs')
        # WoT's shaders/formats/colour.xml + colour2.xml define two
        # separate vertex-colour channels at semanticIndex=0 and 1, both
        # on stream=11.  The latter shows up as a section suffixed
        # '.colour2' -- claim that variant too so tanks that ship both
        # colour streams don't drop the second one.
        _COLOUR_SUFFIXES = ('.colour',  '.colours',
                            '.colour2', '.colours2',
                            '.color',   '.colors',
                            '.color2',  '.colors2')
        _SUFFIX_KIND = (
            [('.vertices', 'vertices'), ('.indices',  'indices')]
            + [(s, 'uv2')    for s in _UV2_SUFFIXES]
            + [(s, 'colour') for s in _COLOUR_SUFFIXES]
        )

        def _strip_known_suffix(nm):
            """Return (base, kind) for a recognised section name, or
            (None, None) when the suffix isn't one we handle.

            'kind' is the dict key we'll store the section under:
            'vertices' / 'indices' / 'uv2' / 'colour'.  The matcher is
            generous about UV2 spellings -- WoT has shipped at least
            '.uv2' and (anecdotally) '.uvs2' / '.uv' over the years
            and we'd rather claim them all than lose data to a typo
            mismatch.
            """
            for suffix, kind in _SUFFIX_KIND:
                if nm == suffix.lstrip('.'):
                    return ('', kind)
                if nm.endswith(suffix):
                    return (nm[:-len(suffix)], kind)
            return (None, None)

        for sec in sections:
            name = sec['name'].strip()
            base, kind = _strip_known_suffix(name)
            if kind is None:
                unknown_sections.append((name, sec.get('size', 0)))
                continue
            if base not in groups:
                groups[base] = {}
            groups[base][kind] = sec

        print(f"\nFound {len(groups)} primitive group(s):")
        for nm in groups:
            has_v   = 'vertices' in groups[nm]
            has_i   = 'indices'  in groups[nm]
            has_uv2 = 'uv2'      in groups[nm]
            has_col = 'colour'   in groups[nm]
            print(f"  [{'+' if has_v else '-'}v "
                  f"{'+' if has_i else '-'}i "
                  f"{'+' if has_uv2 else '-'}u2 "
                  f"{'+' if has_col else '-'}c] {nm}")
        if unknown_sections:
            print(f"  Other sections (still unrecognised): "
                  f"{len(unknown_sections)}")
            for nm, sz in unknown_sections:
                print(f"    section '{nm}'  size={sz}")

        primitive_groups = []
        for base_name, secs in groups.items():
            if 'vertices' not in secs or 'indices' not in secs:
                print(f"  Skipping {base_name} (missing vertices or indices)")
                continue

            v_sec = secs['vertices']
            i_sec = secs['indices']
            v_data = data[v_sec['offset']:v_sec['offset'] + v_sec['size']]
            i_data = data[i_sec['offset']:i_sec['offset'] + i_sec['size']]

            vertex_format, vertex_list = MeshParser._parse_vertices(v_data)
            index_list, prim_group_meta = MeshParser._parse_indices(i_data)

            # Sidecar UV2 section (present on parts that need a 2nd UV
            # channel without bumping up to a 'uvuv' interleaved
            # vertex format -- see _parse_uv2_section for the layout).
            # Overrides any uv1 already parsed from the interleaved
            # stream; in practice a primitive group only ever supplies
            # ONE of the two paths.
            uv2_sec = secs.get('uv2')
            if uv2_sec is not None:
                uv2_data = data[uv2_sec['offset']:
                                uv2_sec['offset'] + uv2_sec['size']]
                expected = len(vertex_list['positions'])
                uv1_arr = MeshParser._parse_uv2_section(
                    uv2_data, expected, base_name)
                if uv1_arr is not None:
                    vertex_list['uv1'] = uv1_arr

            # Sidecar vertex-colour section.  Same parallel-section
            # idea as UV2 but each entry is 4 bytes (BGRA uint8) so
            # the body is half the size of a UV2 section.  Stays
            # absent on most parts; surfaces on equipment / decals
            # that bake colour variation into the mesh.
            col_sec = secs.get('colour')
            if col_sec is not None:
                col_data = data[col_sec['offset']:
                                col_sec['offset'] + col_sec['size']]
                expected = len(vertex_list['positions'])
                col_arr = MeshParser._parse_colour_section(
                    col_data, expected, base_name)
                if col_arr is not None:
                    vertex_list['colour'] = col_arr

            primitive_groups.append({
                'name': base_name,
                'vertices': vertex_list,
                'indices': index_list,
                'prim_groups': prim_group_meta,
                'format': vertex_format,
                'vertex_count': len(vertex_list['positions']),
            })

        if not primitive_groups:
            raise ValueError("No valid primitive groups found")

        # Per-file UV2 summary -- collapses the per-group spam into a
        # single roll-up the user can grep for after a load.  Counts
        # both interleaved (format string contains 'uvuv') and sidecar
        # (separate <group>.uv2 section) sources, since either yields
        # the same downstream uv1 array.
        uv2_groups = [g['name'] for g in primitive_groups
                      if g['vertices'].get('uv1') is not None]
        if uv2_groups:
            print(f"  UV2 SUMMARY: {len(uv2_groups)} of "
                  f"{len(primitive_groups)} group(s) carry a second UV set "
                  f"(via interleaved format OR sidecar .uv2 section):")
            for nm in uv2_groups:
                print(f"    + {nm}")
        else:
            print(f"  UV2 SUMMARY: none of the {len(primitive_groups)} "
                  f"group(s) in this file carry a second UV set "
                  f"(checked both 'uvuv' interleaved and .uv2 sidecar)")

        # Same idea for vertex-colour sections.  Quiet line when the
        # whole file has no colour data so the load log doesn't grow
        # per-file noise on the common case.
        col_groups = [g['name'] for g in primitive_groups
                      if g['vertices'].get('colour') is not None]
        if col_groups:
            print(f"  COLOUR SUMMARY: {len(col_groups)} of "
                  f"{len(primitive_groups)} group(s) carry per-vertex colours:")
            for nm in col_groups:
                print(f"    + {nm}")

        # Also list every distinct format string encountered, so a
        # quick console scan reveals UV2 variants we haven't seen
        # before -- e.g. if WoT ever ships a layout where the tag is
        # 'uv2' rather than 'uvuv'.
        fmts = sorted({g['format'] for g in primitive_groups})
        print(f"  Distinct vertex formats in this file: "
              + ", ".join(repr(f) for f in fmts))

        return primitive_groups

    # ---- internals ---------------------------------------------------------

    @staticmethod
    def _parse_section_table(data, start_pos):
        """Parse the section table at the end of the file.
        Returns list of {'name', 'size', 'offset'} dicts."""
        sections = []
        pos = start_pos
        i = 0
        while pos < len(data) - 4 and i < 100:
            if pos + 4 > len(data) - 4:
                break
            size = struct.unpack('<I', data[pos:pos+4])[0]
            pos += 4
            pos += 16  # 16 bytes of unused junk
            if pos + 4 > len(data):
                break
            name_len = struct.unpack('<I', data[pos:pos+4])[0]
            pos += 4
            if pos + name_len > len(data):
                break
            name = read_c_string(data, pos, name_len)
            pos += name_len
            pos += (4 - (name_len % 4)) % 4  # align name to 4 bytes
            sections.append({'name': name, 'size': size, 'offset': None})
            i += 1

        # Compute section data offsets (Tank Exporter alignment: location += location % 4)
        current = 4
        for sec in sections:
            sec['offset'] = current
            current += sec['size']
            current += current % 4
        return sections

    @staticmethod
    def _parse_vertices(data):
        """Parse a 'vertices' section blob.
        Returns (format_str, vertex_data_dict)."""
        format_str = read_c_string(data, 0, 64).strip()
        pos = 64
        bpvt_mode = 'BPVT' in format_str
        if bpvt_mode:
            pos = 132

        vertex_count = struct.unpack('<I', data[pos:pos+4])[0]
        pos += 4

        has_real_normals = (format_str == 'xyznuv')
        has_bones = ('iii' in format_str)
        has_tangents = format_str.endswith('tb') or 'tbtb' in format_str
        # Some WoT parts (notably equipment / decorative pieces and
        # specific skinned tracks) carry a SECOND UV set in addition to
        # the diffuse UV0 -- typically used for lightmap or detail
        # routing.  Detect by counting 'uv' substrings in the format
        # string ('xyznuvuv', 'xyznuvuviiiww', 'xyznuvuvtb', etc.).
        # When present, we read 8 extra bytes per vertex right after
        # uv0 and stash them as 'uv1' so downstream consumers (Compare
        # dialog, future export) can see them.
        has_uv2 = format_str.count('uv') >= 2

        # Skinned meshes (has_bones) are authored with their forward axis pointing
        # in -Z local space; a 180-deg Y-rotation node transform in the visual file
        # would flip them to +Z in the game engine.  Since we never apply node
        # rotations, we must NOT flip Z for skinned meshes -- the raw -Z data is
        # already correct for GL convention.  Non-skinned meshes follow the usual
        # BigWorld +Z-forward convention and do need the DX->GL Z-flip.
        flip_z = not has_bones

        # Stride table for known formats (matching Tank Exporter).
        # All UV2 variants assume packed-normals (4 B) -- WoT does not
        # ship a real-normal+UV2 combination in any tank we've seen.
        stride_map = {
            # ---- single UV ----------------------------------------------
            'xyznuv':              32,  # pos(12)+nxnynz(12)+uv(8)
            'BPVTxyznuv':          24,  # pos(12)+n(4)+uv(8)
            'BPVTxyznuviiiww':     32,  # +iii(4)+ww(4)
            'BPVTxyznuviiiwwtb':   40,  # +t(4)+b(4)
            'xyznuvtb':            40,  # real normals + tangents
            'BPVTxyznuvtb':        32,
            'xyznuviiiwwtb':       48,  # full real-normal+bones+tangents
            # ---- dual UV (uv0 + uv1) ------------------------------------
            'xyznuvuv':            32,  # 12+4+8+8     (packed normals)
            'BPVTxyznuvuv':        32,
            'xyznuvuvtb':          40,  # +tb(8)
            'BPVTxyznuvuvtb':      40,
            'xyznuvuviiiww':       40,  # +iii+ww
            'BPVTxyznuvuviiiww':   40,
            'xyznuvuviiiwwtb':     48,  # full skinned + UV2 + tb
            'BPVTxyznuvuviiiwwtb': 48,
        }
        stride = stride_map.get(format_str)
        if stride is None:
            # Fallback: compute stride dynamically by walking the format
            # string left-to-right (xyz=12, n=4 packed / 12 real,
            # uv=8 each, iii=4, ww=4, tb=8 each).  Lets us handle
            # variants we haven't catalogued without falling back to
            # a hardcoded 32 that nearly always misparses.
            stride = MeshParser._compute_stride(format_str, has_real_normals)
            if stride > 0:
                print(f"  Vertex format '{format_str}' not in static map; "
                      f"computed stride={stride} from components")
            else:
                print(f"  WARNING: Unknown vertex format '{format_str}', "
                      f"defaulting to stride 32")
                stride = 32

        print(f"  Vertex format: '{format_str}' stride={stride} count={vertex_count} "
              f"BPVT={bpvt_mode} real_normals={has_real_normals} "
              f"bones={has_bones} tangents={has_tangents} uv2={has_uv2} "
              f"flip_z={flip_z}")
        if has_uv2:
            print(f"  *** UV2 DETECTED *** format='{format_str}' -- "
                  f"will parse {vertex_count} second UV pairs interleaved "
                  f"after uv0")

        vertices = []
        normals = []
        tangents = []
        binormals = []
        uv0_list = []
        # Optional second UV channel.  Populated only when has_uv2;
        # stays an empty list otherwise so np.array() of the empty list
        # yields nothing and we ship None instead.
        uv1_list = []
        # Per-vertex bone data captured for skinned meshes (chassis tracks /
        # turret-mounted equipment, etc.).  Each vertex carries up to 4
        # influences: bone indices are uint8s into the renderSet's bone
        # table; weights are uint8 normalised to [0, 1] floats.  Stays
        # None for non-skinned formats (the static hull / turret / gun).
        # Used by FBX / glTF export to author Skin / skin clusters.
        bone_indices_list = []   # list[tuple[int,int,int,int]]
        bone_weights_list = []   # list[list[float]]  (4 floats each)

        for i in range(vertex_count):
            v_start = pos
            x, y, z = struct.unpack('<fff', data[pos:pos+12]); pos += 12
            vertices.append([x, y, z])

            if has_real_normals:
                nx, ny, nz = struct.unpack('<fff', data[pos:pos+12]); pos += 12
                normals.append([nx, ny, nz])
            else:
                n_packed = struct.unpack('<I', data[pos:pos+4])[0]; pos += 4
                normals.append(unpack_normal_bpvt(n_packed) if bpvt_mode
                               else unpack_normal(n_packed))

            u, v = struct.unpack('<ff', data[pos:pos+8]); pos += 8
            uv0_list.append([u, v])

            # UV2 lives immediately after UV1 in every WoT dual-UV
            # format we've encountered (xyznuvuv*, BPVTxyznuvuv*).
            # If we ever hit a layout that interleaves UV2 elsewhere
            # this assumption will need to be format-driven, but for
            # now this matches every catalogued case.
            if has_uv2:
                u2, v2 = struct.unpack('<ff', data[pos:pos+8]); pos += 8
                uv1_list.append([u2, v2])

            if has_bones:
                # 4 byte-indices into renderSet's bone table
                bi = struct.unpack('<4B', data[pos:pos+4]); pos += 4
                # 4 byte-weights normalised to [0, 1]
                bw = struct.unpack('<4B', data[pos:pos+4]); pos += 4
                bone_indices_list.append(bi)
                bone_weights_list.append([w / 255.0 for w in bw])

            if has_tangents:
                t_packed = struct.unpack('<I', data[pos:pos+4])[0]; pos += 4
                tangents.append(unpack_normal_bpvt(t_packed) if bpvt_mode
                                else unpack_normal(t_packed))
                bn_packed = struct.unpack('<I', data[pos:pos+4])[0]; pos += 4
                binormals.append(unpack_normal_bpvt(bn_packed) if bpvt_mode
                                 else unpack_normal(bn_packed))

            consumed = pos - v_start
            if consumed != stride:
                if i == 0:
                    print(f"  WARNING: vertex consumed {consumed} bytes but stride is {stride}")
                pos = v_start + stride

        # DirectX -> OpenGL conversions
        positions = np.array(vertices, dtype=np.float32)
        if flip_z:
            positions[:, 2] *= -1.0  # flip Z (non-skinned only)

        normals_arr = np.array(normals, dtype=np.float32)
        if flip_z:
            normals_arr[:, 2] *= -1.0

        tangents_arr = None
        if tangents:
            tangents_arr = np.array(tangents, dtype=np.float32)
            if flip_z:
                tangents_arr[:, 2] *= -1.0

        binormals_arr = None
        if binormals:
            binormals_arr = np.array(binormals, dtype=np.float32)
            if flip_z:
                binormals_arr[:, 2] *= -1.0

        uv0_arr = np.array(uv0_list, dtype=np.float32)
        uv0_arr[:, 1] = 1.0 - uv0_arr[:, 1]  # flip V

        # Optional UV1 -- same V-flip as UV0; None when format had no
        # second UV channel so downstream `is None` checks still work.
        uv1_arr = None
        if has_uv2 and uv1_list:
            uv1_arr = np.array(uv1_list, dtype=np.float32)
            uv1_arr[:, 1] = 1.0 - uv1_arr[:, 1]

        # Pack bone arrays only when this format actually carried them.
        # Stored as None (rather than empty arrays) so consumers can
        # cheaply detect "non-skinned mesh" via `is None`.
        bone_indices_arr = None
        bone_weights_arr = None
        if has_bones and bone_indices_list:
            bone_indices_arr = np.array(bone_indices_list, dtype=np.uint8)
            bone_weights_arr = np.array(bone_weights_list, dtype=np.float32)

        return format_str, {
            'positions':    positions,
            'normals':      normals_arr,
            'tangents':     tangents_arr,
            'binormals':    binormals_arr,
            'uv0':          uv0_arr,
            'uv1':          uv1_arr,
            'bone_indices': bone_indices_arr,
            'bone_weights': bone_weights_arr,
        }

    @staticmethod
    def _compute_stride(format_str, has_real_normals):
        """Compute per-vertex byte size by walking the format string.

        Used as a fallback when the format isn't in the static stride
        map (e.g. UV2 / extended-skin variants we haven't catalogued).
        Returns 0 when the string contains no recognisable component
        tokens -- caller treats that as "unknown, default to 32".

        Component sizes (bytes):
            xyz      -> 12  (3 floats)
            n        -> 12 if real-normal mode else 4  (packed)
            uv       ->  8 each (per occurrence)
            iii      ->  4
            ww       ->  4
            tb       ->  8 each (4 packed tangent + 4 packed binormal)
        """
        s = format_str
        # Strip the BPVT header marker -- it's a flag, not a component.
        s = s.replace('BPVT', '')
        # 'set3/...' prefix used by some legacy skinned formats: keep
        # only the component half.
        if '/' in s:
            s = s.split('/', 1)[1]

        stride = 0
        if 'xyz' in s:
            stride += 12
        if 'n' in s:
            stride += 12 if has_real_normals else 4
        stride += 8 * s.count('uv')
        if 'iii' in s:
            stride += 4
        if 'ww' in s:
            stride += 4
        stride += 8 * s.count('tb')
        return stride

    @staticmethod
    def _parse_indices(data):
        """Parse an 'indices' section blob.
        Returns (indices_array, list_of_primitive_group_meta)."""
        format_str = read_c_string(data, 0, 64).strip()
        pos = 64
        is_32bit = 'list32' in format_str
        index_size = 4 if is_32bit else 2

        index_count = struct.unpack('<I', data[pos:pos+4])[0]; pos += 4
        group_count = struct.unpack('<H', data[pos:pos+2])[0]; pos += 2
        pos += 2  # padding

        indices = []
        for i in range(index_count):
            if is_32bit:
                idx = struct.unpack('<I', data[pos:pos+4])[0]; pos += 4
            else:
                idx = struct.unpack('<H', data[pos:pos+2])[0]; pos += 2
            indices.append(idx)

        # Primitive group metadata starts 72 bytes after section header
        groups_pos = 72 + (index_count * index_size)
        pos = groups_pos
        prim_groups = []
        for i in range(group_count):
            si, np_, sv, nv = struct.unpack('<IIII', data[pos:pos+16]); pos += 16
            prim_groups.append({
                'start_index': si, 'prim_count': np_,
                'start_vertex': sv, 'vertex_count': nv,
            })

        return np.array(indices, dtype=np.uint32), prim_groups

    # Candidate body offsets for a sidecar UV2 section.  WoT writes
    # the same preamble as the matching vertex section: a 64-byte
    # primary format string + a 64-byte secondary format string +
    # uint32 count.  Two real layouts:
    #
    #   136 bytes  -- BPVT-mirroring (live tanks, e.g. G102_Pz_III
    #                 chassis track_*.uv2).  Layout:
    #                     0..67   primary format string ('BPVSuv2')
    #                     68..131 secondary format string ('set3/uv2pc')
    #                     132..135 uint32 vertex_count
    #                     136..    body
    #   68 bytes   -- non-BPVT (older / simpler):
    #                     0..63   primary format string
    #                     64..67  uint32 vertex_count
    #                     68..    body
    #
    # 132 is intentionally NOT in the list: an off-by-4 read of the
    # 136-byte layout coincidentally passes the (size-offset)//8 ==
    # expected probe via integer-division truncation, which silently
    # shifts the entire UV stream forward by 4 bytes (the count field
    # gets read as the first u-value, ~2.3e-42 garbage).  The exact
    # divisibility guard in _parse_uv2_section is the second line of
    # defence; keeping the probe list clean is the first.
    _UV2_PROBE_OFFSETS = (136, 68)

    @staticmethod
    def _parse_uv2_section(data, expected_count, group_label=''):
        """Parse a sidecar '<group>.uv2' section blob.

        WoT stores a SECOND UV set as a parallel section to the main
        vertex stream when a part needs UV2 (typically lightmap or
        detail routing) but the primary vertex format stays single-UV.
        The section preamble matches the matching .vertices section:

            non-BPVT layout (68-byte preamble)
                offset 0   : 64-byte format string (right-padded NULs)
                offset 64  : uint32 vertex_count
                offset 68  : N pairs of (float u, float v)

            BPVT layout (132-byte preamble) -- used when the matching
            vertex stream is itself BPVT (i.e. its format string starts
            with 'BPVT')
                offset 0   : 64-byte format string
                offset 64..127 : 64 bytes of additional header (mirrors
                                 the BPVT vertices preamble)
                offset 128 : uint32 vertex_count
                offset 132 : N pairs of (float u, float v)

        We don't trust the in-section count (one observed live tank
        has it set to 0 while the body still carries the correct
        number of pairs).  Instead we probe each candidate body
        offset and pick the one where (size - offset) / 8 ==
        expected_count.

        Args:
            data           (bytes): the slice for this section
            expected_count (int)  : vertex count from the matching
                                    .vertices section
            group_label    (str)  : just for log messages

        Returns:
            np.ndarray (N, 2) float32 with V flipped (1 - v) to match
            uv0's orientation, or None on failure.
        """
        if len(data) < 68:
            print(f"  [uv2] '{group_label}': section too small "
                  f"({len(data)} bytes) -- skipped")
            return None

        format_str = read_c_string(data, 0, 64).strip()

        chosen_offset = None
        for body_offset in MeshParser._UV2_PROBE_OFFSETS:
            if body_offset > len(data):
                continue
            body_len = len(data) - body_offset
            # Require exact divisibility -- without this, a 136-byte
            # layout's (size - 132) yields 1638.5 pairs which rounds
            # down to 1638 and false-matches expected_count.  Found
            # the hard way; see _UV2_PROBE_OFFSETS comment.
            if body_len % 8 != 0:
                continue
            if body_len // 8 == expected_count:
                chosen_offset = body_offset
                break

        if chosen_offset is None:
            # Diagnose why we bailed -- list every probe result so the
            # user can see whether we're off-by-one (body_pairs is
            # close but not equal, suggesting yet another preamble
            # variant) or whether the data is the wrong size entirely.
            probe_msg = ", ".join(
                f"offset={o}->{(len(data)-o)//8} pairs"
                for o in MeshParser._UV2_PROBE_OFFSETS
                if o <= len(data))
            print(f"  [uv2] '{group_label}': format={format_str!r} "
                  f"size={len(data)} expected={expected_count} pairs "
                  f"({probe_msg}) -- no probe matched, skipping")
            return None

        count = expected_count
        # Bulk-decode the float pairs in one numpy call -- much faster
        # than a Python loop for 20k+ vertex tanks.
        uv = np.frombuffer(data, dtype='<f4',
                           count=count * 2,
                           offset=chosen_offset).reshape(count, 2).copy()
        uv[:, 1] = 1.0 - uv[:, 1]   # match uv0's V-flip

        print(f"  [uv2] '{group_label}': format={format_str!r}  "
              f"count={count}  body_offset={chosen_offset}  "
              f"-- parsed sidecar UV2 section")
        return uv

    # Body-offset probe list for .colour sections -- same preamble
    # rules as .uv2.  Entries are 4 bytes each (BGRA uint8) so the
    # math is body_size = N * 4 instead of N * 8.
    _COLOUR_PROBE_OFFSETS = (132, 68)

    @staticmethod
    def _parse_colour_section(data, expected_count, group_label=''):
        """Parse a sidecar '<group>.colour' section blob.

        Layout mirrors the .vertices / .uv2 section preambles:
            offset 0..63       : 64-byte format string
            offset 64+ / 132+  : per-vertex colour entries

        Each entry is 4 bytes -- treated as BGRA uint8 to match the
        BigWorld convention -- and we return them re-ordered to RGBA
        in [0,1] floats so downstream code can multiply against
        sampled textures without an extra normalisation step.

        Args:
            data           (bytes): the slice for this section
            expected_count (int)  : vertex count from the matching
                                    .vertices section
            group_label    (str)  : log only

        Returns:
            np.ndarray (N, 4) float32 RGBA in [0, 1], or None on failure.
        """
        if len(data) < 68:
            print(f"  [colour] '{group_label}': section too small "
                  f"({len(data)} bytes) -- skipped")
            return None

        format_str = read_c_string(data, 0, 64).strip()

        chosen_offset = None
        for body_offset in MeshParser._COLOUR_PROBE_OFFSETS:
            if body_offset > len(data):
                continue
            body_entries = (len(data) - body_offset) // 4
            if body_entries == expected_count:
                chosen_offset = body_offset
                break

        if chosen_offset is None:
            probe_msg = ", ".join(
                f"offset={o}->{(len(data)-o)//4} entries"
                for o in MeshParser._COLOUR_PROBE_OFFSETS
                if o <= len(data))
            print(f"  [colour] '{group_label}': format={format_str!r} "
                  f"size={len(data)} expected={expected_count} entries "
                  f"({probe_msg}) -- no probe matched, skipping")
            return None

        # Read as BGRA uint8 then swizzle to RGBA float [0, 1].
        bgra = np.frombuffer(data, dtype=np.uint8,
                             count=expected_count * 4,
                             offset=chosen_offset
                             ).reshape(expected_count, 4)
        rgba = np.empty_like(bgra, dtype=np.float32)
        rgba[:, 0] = bgra[:, 2] / 255.0   # R <- B-position
        rgba[:, 1] = bgra[:, 1] / 255.0   # G <- G-position
        rgba[:, 2] = bgra[:, 0] / 255.0   # B <- R-position
        rgba[:, 3] = bgra[:, 3] / 255.0   # A <- A-position

        print(f"  [colour] '{group_label}': format={format_str!r}  "
              f"count={expected_count}  body_offset={chosen_offset}  "
              f"-- parsed sidecar colour section (BGRA->RGBA float)")
        return rgba


# ============================================================================
# VisualLoader - .visual_processed material/texture extractor
# ============================================================================

class VisualLoader:
    """Extract material and texture path data from .visual_processed files.

    Preferred path: BWXML binary -> decode to XML string -> ElementTree parse.
    Fallback path:  raw latin-1 text scan (legacy / plain-XML files).

    The decoded visual_processed XML structure (confirmed from live decode):
        <root>
          <renderSet>
            <geometry>
              <vertices>name.vertices</vertices>
              <primitive>name.indices</primitive>
              <primitiveGroup>
                <material>
                  <identifier>tank_hull_01</identifier>
                  <property>normalMap</property>
                  <Texture>vehicles/.../ANM.dds</Texture>
                  <property>diffuseMap</property>
                  <Texture>vehicles/.../AM.dds</Texture>
                  <property>excludeMaskAndAOMap</property>
                  <Texture>vehicles/.../AO.dds</Texture>
                  <property>metallicGlossMap</property>
                  <Texture>vehicles/.../GMM.dds</Texture>
                  <property>alphaTestEnable</property>
                  <Bool>false</Bool>
                </material>
              </primitiveGroup>
            </geometry>
          </renderSet>
        </root>
    Material properties are alternating sibling pairs:
        <property>propName</property>  (text = property name)
        <Texture|Bool|Float|...>value</...>  (typed value element)
    """

    @staticmethod
    def parse_textures(visual_file, group_names=None):
        """Return a dict: group_name -> dict of texture paths and material flags.

        Args:
            visual_file  : absolute path to .visual_processed file
            group_names  : list of group base-names from the matching .primitives_processed
                           (e.g. ['chassis_RShape21_split_1', ...] or [''] for single-group)
        Returns:
            { group_name: {'diffuse','normal','ao','gmm',
                           'alpha_reference','alpha_test_enable','double_sided',
                           'identifier'} }
        """
        result = {}
        if not os.path.exists(visual_file):
            print(f"[-] Visual file not found: {visual_file}")
            return result

        with open(visual_file, 'rb') as f:
            raw = f.read()

        # Try structured XML parsing (BWXML or plain XML)
        xml_root = None
        if is_bwxml(raw):
            try:
                xml_str  = decode_bwxml(raw)
                xml_root = ET.fromstring(xml_str)
                print(f"[+] Parsing visual (BWXML): {os.path.basename(visual_file)} "
                      f"({len(raw)} bytes -> {len(xml_str)} chars)")
            except Exception as exc:
                print(f"  BWXML parse failed ({exc}), falling back to text scan")
        else:
            try:
                xml_root = ET.fromstring(raw.decode('utf-8', errors='replace'))
                print(f"[+] Parsing visual (XML): {os.path.basename(visual_file)}")
            except Exception:
                print(f"[+] Parsing visual (text scan): {os.path.basename(visual_file)}")

        if xml_root is not None:
            return VisualLoader._parse_textures_xml(xml_root, group_names)

        # Text-scan fallback
        return VisualLoader._parse_textures_text(
            raw.decode('latin-1', errors='ignore'), group_names)

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_textures_xml(xml_root, group_names):
        """Parse material data from a decoded visual file's ElementTree root.

        A renderSet may carry multiple <primitiveGroup> children, each with
        its own <material>.  These map 1:1 to the sub-mesh ranges
        (prim_groups) inside the matching primitives_processed group:
        sub-mesh i uses primitiveGroup[i]'s material.

        Skinned tanks / 3D-style meshes routinely split their hulls into
        7+ sub-meshes (body + decorations + side-skirts + ...) so we
        return a LIST of materials per group, in source order.

        Returns:
            dict[group_name, list[material_dict]]
        """
        result    = {}
        group_set = set(group_names) if group_names is not None else None
        has_empty = group_set is not None and '' in group_set

        for rs in xml_root.findall('.//renderSet'):
            geom = rs.find('geometry')
            if geom is None:
                continue

            # Group name from <primitive>name.indices</primitive>
            prim_el    = geom.find('primitive')
            group_name = None
            if prim_el is not None and prim_el.text:
                pt = prim_el.text.strip()
                group_name = pt[:-len('.indices')] if pt.endswith('.indices') else pt

            # Match against requested group names
            if group_set is not None:
                if group_name not in group_set:
                    if has_empty:
                        group_name = ''   # single-group: everything collapses to ''
                    else:
                        continue          # not a group we need
            if group_name is None:
                group_name = ''

            # Already have materials for this group -> skip duplicate renderSet
            if group_name in result:
                continue

            # Collect every primitiveGroup's material, in source order.
            # Sub-meshes without textures (which can happen with helper
            # geometry) still get an entry so positional indexing into
            # prim_groups keeps working -- empty dicts fall back to the
            # placeholder texture in load_vehicle.
            mat_list = []
            for pg in geom.findall('primitiveGroup'):
                mat = pg.find('material')
                if mat is None:
                    mat_list.append({})
                    continue
                mat_list.append(VisualLoader._extract_material_xml(mat) or {})
            if mat_list:
                result[group_name] = mat_list

        print(f"  Parsed {len(result)} group material list(s):")
        for gn, mats in result.items():
            label = gn or '(single)'
            for i, t in enumerate(mats):
                d = t.get('diffuse', '')
                print(f"    [{label} #{i}]: id={t.get('identifier', '?')}  "
                      f"diffuse={os.path.basename(d) if d else '-'}")
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_material_xml(mat_el):
        """Build a texture/flags dict from a <material> ElementTree element.

        Each <property> child has the property name as element.text and the
        typed value as its first child element.  For example:

            <property>diffuseMap<Texture>vehicles/.../AM.dds</Texture></property>
            <property>normalMap<Texture>vehicles/.../ANM.dds</Texture></property>
            <property>alphaTestEnable<Bool>false</Bool></property>

        element.text holds the property name because BWXML writes self-data
        (type=string) before the sub-element children.

        Damage-layer properties (only present on `/crash/` visuals using
        `PBS_tank_crash.fx`):

            crashTileMap        Texture path -- single shared tile,
                                always 'vehicles/russian/Tank_detail/
                                crash_tile.dds' across every crashed
                                tank in every nation.  RGB = damage
                                colour (scorched metal / dirt /
                                exposed steel); A = blend mask.
            g_crashUVTiling     Vector4 (uv.x_mul, uv.y_mul, uv.x_off,
                                uv.y_off) for tiling the damage layer.
            crash_coefficient   Float, 0..1, "how damaged" -- shader
                                uses it to scale the blend mask.
            colorIdMap          Per-material ID lookup (already on
                                non-crash PBS materials too; we don't
                                consume it yet).

        Captured into `tex['crash_tile']`, `tex['crash_uv_tiling']`,
        `tex['crash_coefficient']` so the renderer can pick them up.
        Missing values fall through; we don't fabricate defaults --
        the GLSL side treats `crash_tile==0` as "no damage layer".
        """
        tex = {}

        # Identifier (plain string child)
        id_el = mat_el.find('identifier')
        if id_el is not None and id_el.text:
            tex['identifier'] = id_el.text.strip()

        # Shader filename ('shaders/std_effects/PBS_tank.fx' /
        # 'PBS_tank_crash.fx' / etc.).  Lets the renderer route
        # crash visuals through the damage-blend pass even when the
        # crash_tile texture path itself is missing -- keeping the
        # shader-vs-look question as one piece of state rather than
        # two correlated ones.
        fx_el = mat_el.find('fx')
        if fx_el is not None and fx_el.text:
            tex['fx'] = fx_el.text.strip()

        # Every <property> element: text = name, child[0] = typed value
        for prop in mat_el.findall('property'):
            prop_name = (prop.text or '').strip()
            if not prop_name or len(prop) == 0:
                continue
            val_el = prop[0]          # e.g. <Texture>, <Bool>, <Float>, <Vector4>
            val    = (val_el.text or '').strip()

            if prop_name == 'diffuseMap' and 'diffuse' not in tex:
                if val:
                    tex['diffuse'] = val
            elif prop_name == 'normalMap' and 'normal' not in tex:
                if val:
                    tex['normal'] = val
            elif prop_name == 'excludeMaskAndAOMap' and 'ao' not in tex:
                if val:
                    tex['ao'] = val
            elif prop_name == 'metallicGlossMap' and 'gmm' not in tex:
                if val:
                    tex['gmm'] = val
            elif prop_name == 'metallicDetailMap' and 'detail' not in tex:
                # Shared scratch / surface-variation noise texture.  Almost
                # always 'vehicles/russian/Tank_detail/Details_map.dds' --
                # the same map is wired into every nation's tanks.
                if val:
                    tex['detail'] = val
            elif prop_name == 'g_detailUVTiling':
                # Vector4: tiling.xy, offset.zw.  Typical value is
                # '7.0137 7.0137 0 0' -- the 7x repeat is what gives the
                # surface its scratchy micro-detail.
                parts = val.split()
                if len(parts) >= 2:
                    try:
                        tex['detail_tiling'] = (
                            float(parts[0]), float(parts[1]))
                    except ValueError:
                        pass
            elif prop_name == 'crashTileMap' and 'crash_tile' not in tex:
                # Damage-layer tile (PBS_tank_crash.fx only).  See
                # docstring above for the texture's contents and
                # provenance (single shared file under particles.pkg).
                if val:
                    tex['crash_tile'] = val
            elif prop_name == 'g_crashUVTiling':
                # Vector4: tiling.xy + offset.zw.  Typical value
                # '2 2 0 0' -- 2x repeat scales the damage-tile
                # detail to roughly hull-panel-sized chunks.
                parts = val.split()
                if len(parts) >= 4:
                    try:
                        tex['crash_uv_tiling'] = (
                            float(parts[0]), float(parts[1]),
                            float(parts[2]), float(parts[3]))
                    except ValueError:
                        pass
                elif len(parts) >= 2:
                    # Some materials only carry the xy component.
                    try:
                        tex['crash_uv_tiling'] = (
                            float(parts[0]), float(parts[1]),
                            0.0, 0.0)
                    except ValueError:
                        pass
            elif prop_name == 'crash_coefficient':
                # Float in [0, 1], "how damaged" -- the shader
                # multiplies the tile alpha by this before mixing,
                # so 0 = no damage visible, 1 = full coverage.
                # Materials that don't carry this property still
                # get the damage layer (the shader supplies its
                # own default, typically 1.0).
                try:
                    tex['crash_coefficient'] = float(val)
                except ValueError:
                    pass
            elif prop_name == 'alphaReference':
                try:
                    tex['alpha_reference'] = int(val)
                except Exception:
                    pass
            elif prop_name == 'alphaTestEnable':
                tex['alpha_test_enable'] = val.lower() not in ('false', '0', '')
            elif prop_name == 'doubleSided':
                tex['double_sided'] = val.lower() not in ('false', '0', '')

        return tex

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_textures_text(visual_text, group_names):
        """Text-scan fallback used when XML parsing is unavailable (legacy files)."""
        result = {}

        # Find renderSet markers: <group>.indices tokens.
        group_positions = []
        non_empty_names = [g for g in (group_names or []) if g]
        for gname in non_empty_names:
            search_token = gname + '.indices'
            idx = 0
            while True:
                idx = visual_text.find(search_token, idx)
                if idx < 0:
                    break
                group_positions.append((idx, gname))
                idx += len(search_token)

        if not group_positions and group_names is not None:
            if '' in group_names or len(group_names) == 1:
                empty_name = '' if '' in group_names else group_names[0]
                group_positions.append((0, empty_name))
                print(f"  Single-group mesh: assigning whole visual to '{empty_name}'")

        group_positions.sort(key=lambda x: x[0])
        print(f"  Found {len(group_positions)} renderSet reference(s) (text scan)")

        for i, (group_pos, group_name) in enumerate(group_positions):
            block_end = (group_positions[i + 1][0]
                         if i + 1 < len(group_positions) else len(visual_text))
            block = visual_text[group_pos:block_end]
            tex = {}

            for key in ('diffuseMap', 'normalMap', 'excludeMaskAndAOMap'):
                idx = block.find(key)
                if idx >= 0:
                    p = VisualLoader._extract_vehicle_path(block, idx + len(key))
                    if p:
                        tex[{
                            'diffuseMap': 'diffuse',
                            'normalMap': 'normal',
                            'excludeMaskAndAOMap': 'ao',
                        }[key]] = p

            # GMM: find by suffix
            g_idx = block.find('_GMM.dds')
            if g_idx >= 0:
                s = g_idx
                while s > 0 and (block[s-1].isalnum() or block[s-1] in '/_-.'):
                    s -= 1
                cand = block[s:g_idx + len('_GMM.dds')]
                if cand.startswith('vehicles/'):
                    tex['gmm'] = cand

            # Material properties (raw bytes; best-effort)
            ar_idx = block.find('alphaReference')
            if ar_idx >= 0:
                vp = ar_idx + len('alphaReference')
                if vp < len(block):
                    tex['alpha_reference'] = ord(block[vp])

            if 'alphaTestEnable' in block:
                tex['alpha_test_enable'] = True

            ds_idx = block.find('doubleSided')
            if ds_idx >= 0:
                vp = ds_idx + len('doubleSided')
                if vp < len(block):
                    tex['double_sided'] = (ord(block[vp]) != 0)

            # Material identifier
            identifier = None
            for prefix in ('tank_', 'track_mat'):
                pidx = block.find(prefix)
                while pidx >= 0:
                    end = pidx
                    while end < len(block) and (block[end].isalnum() or block[end] == '_'):
                        end += 1
                    cand = block[pidx:end]
                    if 5 < len(cand) < 80:
                        identifier = cand
                        break
                    pidx = block.find(prefix, pidx + 1)
                if identifier:
                    break
            if identifier:
                tex['identifier'] = identifier

            # Text fallback returns at most one material per group; wrap it
            # in a list so callers can use the same positional indexing as
            # _parse_textures_xml's multi-material output.
            if tex:
                result[group_name] = [tex]

        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_vehicle_path(text, after_key_pos):
        """Find the next 'vehicles/...' path starting near after_key_pos.

        Used only by the text-scan fallback.  Stops at XML tag boundaries
        (<>), whitespace, and null bytes.
        """
        max_skip = min(after_key_pos + 200, len(text))
        for s in range(after_key_pos, max_skip):
            if text[s:].startswith('vehicles/'):
                end = s
                while (end < len(text)
                       and text[end] >= ' '
                       and text[end] not in '\x00\n\r<>'):
                    end += 1
                return text[s:end]
        return None

    @staticmethod
    def get_node_world_translations(xml_root):
        """Walk the visual file's <node> hierarchy and return world-space
        row3 translations for every named node.

        BigWorld visual files store a skeleton tree.  Each <node> has:
            <identifier> -- the node name (string)
            <transform>  -- the LOCAL 4x3 matrix:
                                <row0>  right-vector
                                <row1>  up-vector
                                <row2>  forward-vector
                                <row3>  translation (local, relative to parent)

        World position = accumulated row3 from root down to the node.
        Y is 'up' in both BigWorld and OpenGL so no sign conversion needed here.

        Returns:
            dict {identifier_lower (str): world_pos (np.float32[3])}
        """
        result = {}

        def _parse_row3(transform_el):
            if transform_el is None:
                return np.zeros(3, dtype=np.float32)
            r3 = transform_el.find('row3')
            if r3 is None or not r3.text:
                return np.zeros(3, dtype=np.float32)
            vals = r3.text.strip().split()
            if len(vals) >= 3:
                return np.array([float(v) for v in vals[:3]], dtype=np.float32)
            return np.zeros(3, dtype=np.float32)

        def _walk(el, parent_world_pos):
            ident     = (el.findtext('identifier') or '').strip()
            local_pos = _parse_row3(el.find('transform'))
            world_pos = parent_world_pos + local_pos
            if ident:
                result[ident.lower()] = world_pos.copy()
            for child in el.findall('node'):
                _walk(child, world_pos)

        for top_node in xml_root.findall('node'):
            _walk(top_node, np.zeros(3, dtype=np.float32))

        return result

    @staticmethod
    def get_node_world_transforms(xml_root):
        """Walk the visual file's <node> tree and return per-node world-space
        position + forward direction (rotation-aware, unlike
        get_node_world_translations which only sums row3).

        BigWorld stores each node's transform as four rows of 3 floats:
            row0  = right-vector (local +X axis)
            row1  = up-vector    (local +Y axis)
            row2  = forward-vector (local +Z axis)   <-- emitter direction
            row3  = translation (in PARENT space)

        The matrix-form (row-vector convention) is:
            M = [[row0..0],
                 [row1..0],
                 [row2..0],
                 [row3..1]]

        World transform composes as:  M_world = M_local @ M_parent_world
        Position = M_world[3, :3];   Forward = M_world[2, :3]   (normalised).

        Used to discover engine-exhaust / fire / smoke hardpoints.

        Returns:
            dict {identifier_lower: (position np.float32[3],
                                     forward  np.float32[3])}
        """
        result = {}

        def _parse_transform_4x4(transform_el):
            M = np.eye(4, dtype=np.float32)
            if transform_el is None:
                return M
            for i, key in enumerate(('row0', 'row1', 'row2', 'row3')):
                r = transform_el.find(key)
                if r is None or not r.text:
                    continue
                vals = r.text.strip().split()
                if len(vals) >= 3:
                    M[i, 0] = float(vals[0])
                    M[i, 1] = float(vals[1])
                    M[i, 2] = float(vals[2])
            return M

        def _walk(el, parent_world_M):
            ident   = (el.findtext('identifier') or '').strip()
            local_M = _parse_transform_4x4(el.find('transform'))
            # Row-vector convention: child world = local @ parent world
            world_M = local_M @ parent_world_M
            if ident:
                pos = world_M[3, :3].copy()
                fwd = world_M[2, :3].copy()
                n = float(np.linalg.norm(fwd))
                if n > 1e-6:
                    fwd /= n
                else:
                    fwd = np.array([0.0, 0.0, 1.0], dtype=np.float32)
                result[ident.lower()] = (pos, fwd)
            for child in el.findall('node'):
                _walk(child, world_M)

        identity = np.eye(4, dtype=np.float32)
        for top_node in xml_root.findall('node'):
            _walk(top_node, identity)

        return result

    # Common BigWorld hardpoint-name fragments that mark an ENGINE EXHAUST
    # emission point.  Matched case-insensitively as a substring on the
    # node identifier.  HP_Fire_* is intentionally excluded -- those are
    # damage / burning-tank spawn points, NOT engine exhaust.
    #
    # WoT visual files actually use "Exhaus" (no trailing 't') -- e.g.
    # HP_Track_Exhaus_1 -- so we match the truncated form too.
    _EXHAUST_KEYWORDS = (
        'track_exhaus',   # HP_Track_Exhaus_##  (most common WoT form)
        'engineexhaust',  # HP_engineExhaust_##
        'engine_exhaust', # HP_engine_exhaust_##
        'exhaust',        # HP_exhaust_##, exhaust_##
        'exhaus',         # bare "Exhaus" typo catchall
    )

    @staticmethod
    def find_exhaust_nodes(visual_path):
        """Open a .visual_processed file and return its exhaust / fire /
        smoke hardpoints.

        Args:
            visual_path (str): absolute path to a .visual_processed file
                (BWXML binary or plain XML, both supported).

        Returns:
            list of (name_lower, position np.float32[3],
                     forward np.float32[3]),
            sorted by name.  Empty list when the file is missing or has
            no matching nodes.
        """
        # _read_visual_nodes already lives on VehicleXMLLoader; reuse it.
        xml_root = VehicleXMLLoader._read_visual_nodes(visual_path)
        if xml_root is None:
            return []
        transforms = VisualLoader.get_node_world_transforms(xml_root)
        out = []
        for name_lower, (pos, fwd) in transforms.items():
            if any(k in name_lower for k in VisualLoader._EXHAUST_KEYWORDS):
                out.append((name_lower, pos, fwd))
        out.sort(key=lambda item: item[0])
        return out

    # Hardpoint-name fragments that mark a FIRE / damage spawn point.
    # Independent of the engine-exhaust set so we can route them to a
    # separate particle system (smoke vs fire).  WoT mostly ships
    # `HP_Fire_*` -- substring match so we catch the rare `HP_fire_pt_*`
    # / `firepoint_*` variants too.
    _FIRE_KEYWORDS = (
        'fire',
    )

    @staticmethod
    def find_fire_nodes(visual_path):
        """Open a .visual_processed file and return its fire / damage
        hardpoints (the spawn points for burning-tank flames).

        Sibling of `find_exhaust_nodes` -- same return shape, different
        keyword filter.  Forward vector is read from the visual but
        callers should generally OVERRIDE it to (0, 1, 0) on the GL
        side; fire goes up regardless of how the artist oriented the
        node in 3DS Max.
        """
        xml_root = VehicleXMLLoader._read_visual_nodes(visual_path)
        if xml_root is None:
            return []
        transforms = VisualLoader.get_node_world_transforms(xml_root)
        out = []
        for name_lower, (pos, fwd) in transforms.items():
            # Skip exhaust matches that happen to contain "fire" in
            # some weird mod -- if the node also matches an exhaust
            # keyword, exhaust wins.  In practice no live tank does
            # this; the guard is defensive.
            if any(k in name_lower for k in VisualLoader._EXHAUST_KEYWORDS):
                continue
            if any(k in name_lower for k in VisualLoader._FIRE_KEYWORDS):
                out.append((name_lower, pos, fwd))
        out.sort(key=lambda item: item[0])
        return out

    @staticmethod
    def resolve_hd_path(rel_path, res_mods_root, pkg_extractor=None):
        """Try HD version first, fall back to SD.  Searches res_mods then res/.
        Falls back to PKG extraction when pkg_extractor is supplied.

        When `res_mods_root` is falsy (None / empty string), the disk
        walk is skipped entirely and the resolver goes straight to
        the pkg fallback.  Used by the "Load from res_mods" toggle
        and FBX import to force pkg-only lookups regardless of what
        is mounted in the user's res_mods/ folder.

        Returns (absolute_path or None, used_hd: bool)."""
        base, dot, ext = rel_path.rpartition('.')
        if not dot:
            base, ext = rel_path, ''
        hd_rel = base + '_hd' + (('.' + ext) if ext else '')
        sd_rel = rel_path

        # Disk walk: only when caller actually gave us a root to look
        # under.  Blank root = "skip res_mods + res/, go straight to
        # the pkg fallback below."
        search_roots = []
        if res_mods_root:
            search_roots.append(res_mods_root)
            # Walk up to find res/ alongside res_mods/
            parent = os.path.dirname(os.path.dirname(res_mods_root))
            if parent and os.path.isdir(parent):
                res_dir = os.path.join(parent, 'res')
                if os.path.isdir(res_dir):
                    search_roots.append(res_dir)

        for root in search_roots:
            full_hd = os.path.join(root, hd_rel.replace('/', os.sep))
            if os.path.exists(full_hd):
                return full_hd, True
        for root in search_roots:
            full_sd = os.path.join(root, sd_rel.replace('/', os.sep))
            if os.path.exists(full_sd):
                return full_sd, False

        # PKG fallback
        if pkg_extractor:
            path = pkg_extractor.extract(hd_rel)
            if path:
                return path, True
            path = pkg_extractor.extract(sd_rel)
            if path:
                return path, False

        return None, False


# ============================================================================
# TextureLoader - load image files and create OpenGL textures
# ============================================================================

class TextureLoader:
    """Load DDS / PNG / JPG textures via PIL and upload to GPU."""

    @staticmethod
    def load_texture(filepath, is_normal=False):
        """Load `filepath` and return an OpenGL texture id.
        Falls back to placeholder if PIL can't open the file."""
        if not Image:
            return TextureLoader.create_placeholder(1.0 if is_normal else 0.5)

        # Try a few extensions if the exact path is missing
        base = str(filepath).rsplit('.', 1)[0]
        candidates = [base + '.png', base + '.jpg', base + '.dds', filepath]
        for cand in candidates:
            if os.path.exists(cand):
                try:
                    return TextureLoader._load_image_file(cand, is_normal)
                except Exception as e:
                    print(f"Warning: Failed to load {cand}: {e}")

        print(f"Warning: Texture not found: {filepath}")
        return TextureLoader.create_placeholder(1.0 if is_normal else 0.5)

    @staticmethod
    def _load_image_file(filepath, is_normal=False):
        img = Image.open(filepath)
        if is_normal:
            img = img.convert('RGBA')  # need alpha channel for ANM (Y in alpha)
        elif img.mode not in ('RGB', 'RGBA'):
            img = img.convert('RGBA')

        # DDS textures use top-left origin; OpenGL uses bottom-left
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
        img_data = img.tobytes('raw', img.mode)

        tex_id = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, tex_id)
        fmt = GL_RGBA if img.mode == 'RGBA' else GL_RGB
        glTexImage2D(GL_TEXTURE_2D, 0, fmt, img.width, img.height, 0,
                     fmt, GL_UNSIGNED_BYTE, img_data)
        glGenerateMipmap(GL_TEXTURE_2D)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR_MIPMAP_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_REPEAT)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_REPEAT)
        print(f"    Loaded {os.path.basename(filepath)}: {img.width}x{img.height} {img.mode}")
        glBindTexture(GL_TEXTURE_2D, 0)
        return tex_id

    @staticmethod
    def create_placeholder(value):
        """Create a 1x1 grayscale RGB texture - used when source file missing."""
        color = np.uint8([int(value * 255)] * 3)
        img_data = color.tobytes()
        tex_id = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, tex_id)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB, 1, 1, 0, GL_RGB, GL_UNSIGNED_BYTE, img_data)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
        glBindTexture(GL_TEXTURE_2D, 0)
        return tex_id


# ============================================================================
# PkgExtractor - extract files from WoT .pkg ZIP archives
# ============================================================================

class PkgExtractor:
    """Extract files from WoT .pkg archives (standard ZIP format).

    Uses TheItemList.xml (next to tankExporterPy.py) for an O(1) lookup of
    which archive contains each file.  Falls back to scanning all archives
    in pkg_dir when the lookup table is absent or returns no match.

    Args:
        wot_root   (str)       : WoT installation root (parent of 'res'/'res_mods')
        pkg_dir    (str|None)  : override path to the packages folder;
                                 defaults to <wot_root>/res/packages/
        lookup_xml (str|None)  : override path to the item-list XML;
                                 auto-discovered next to tankExporterPy.py when None
    """

    # Path to the experiment root (where tankExporterPy.py and TheItemList.xml live)
    _SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Pkg-basename filters for the scan-fallback list.  We never need to
    # search MAP archives (numeric prefix like '01_karelia.pkg' /
    # '203_battle_royale.pkg'), MAP HD variants, NOR the handful of
    # non-map game-mode bundles that don't carry tank assets.  Filtering
    # these out:
    #   * cuts scan-fallback time by roughly half on a fresh session
    #   * avoids opening 100+ map-archive central directories for files
    #     that are never going to be in any of them
    #
    # Add new entries here as we identify them.  The regex catches every
    # numeric-prefix pkg automatically; the literal set is for the
    # non-numeric oddballs the user has flagged.
    _MAP_PKG_RE = re.compile(r'^\d+_')
    _EXCLUDE_PKG_BASENAMES = {
        # Battle modes / event bundles -- no tank assets
        'fun_random.pkg',
        'battle_royale.pkg',
        'battle_modifiers.pkg',
        'comp7.pkg',
        'comp7_core.pkg',
        'comp7_light.pkg',
        'frontline.pkg',
        'last_stand.pkg',
        'last_stand_hd.pkg',
        'event_platform.pkg',
        'story_mode.pkg',
        'in_battle_achievements.pkg',
        'la_pinger.pkg',
        'open_bundle.pkg',
        'prime_gaming_content.pkg',
        'resource_well.pkg',
        'server_side_replay.pkg',
        # Hangars are environments, not vehicles
        'hangar_v4.pkg',
        'hangar_v4_hd.pkg',
        'hangar_v4_last_stand.pkg',
        'hangar_v4_last_stand_hd.pkg',
        # h33_* pkgs are map variants (numeric prefix masked by 'h' tag)
        'h33_battle_royale_2021.pkg',
        'h33_battle_royale_2021_hd.pkg',
        'h33_comp7.pkg',
        'h33_comp7_hd.pkg',
        # Audio -- never indexes a 3D asset
        'audioww-part1.pkg',
        'audioww-part2.pkg',
        'audioww-part3.pkg',
    }

    @classmethod
    def _is_excluded_pkg(cls, basename):
        """True when the given basename is a map archive or one of
        the explicit non-vehicle bundles we know never carry tank assets.
        """
        return (cls._MAP_PKG_RE.match(basename) is not None
                or basename in cls._EXCLUDE_PKG_BASENAMES)

    def __init__(self, wot_root, pkg_dir=None, lookup_xml=None,
                  progress_callback=None):
        # progress_callback (callable(str) | None) -- fired with short
        # status messages during the slow pre-warm step so the splash
        # screen / startup log can show pkg-by-pkg progress instead
        # of a silent multi-second pause.  None = no progress reports.
        self._progress_cb = progress_callback
        # Pending TheItemList.xml additions accumulated during the
        # session.  Pushed by _persist_entry on every scan-fallback
        # discovery; flushed in one batched read+write by
        # flush_persisted_entries() at end-of-load (and again on
        # cleanup) so we never rewrite the 15 MB XML 20+ times in
        # a row during a single tank load.
        # List of (zip_path, pkg_basename) tuples.
        self._pending_persists = []
        self.wot_root  = wot_root
        self.pkg_dir   = pkg_dir or os.path.join(wot_root, 'res', 'packages')
        self._temp_dir = tempfile.mkdtemp(prefix='tankExporterPy_pkg_')
        self._extracted = {}   # zip_path -> local path (already extracted)
        self._index     = {}   # pkg_path -> frozenset of names (lazy, scan-fallback only)
        # Open ZipFile handles per pkg.  Reading a single .primitives_processed
        # via a fresh `with zipfile.ZipFile(pkg, 'r') as zf: zf.open(...)`
        # costs ~50-100 ms BECAUSE the ZipFile constructor reads the
        # entire central directory (which can be hundreds of KB on a
        # vehicles_level_*.pkg).  Holding the handle across calls means
        # we pay that tax once per pkg per session instead of once per
        # extract.  Cleaned up in cleanup() at viewer-shutdown time.
        self._open_zips = {}   # pkg_path -> open zipfile.ZipFile

        # ------ Timing instrumentation -----------------------------------
        # Records every extract() call with how long it took, which path
        # served it (lookup hit / case-fallback hit / scan-fallback hit /
        # missing), and how many pkg namelists had to be opened for the
        # scan-fallback case.  reset_timing() clears the buffer at the
        # start of each load_vehicle so the summary at the end shows
        # JUST that load.
        #
        # Each entry:
        #   {'op': 'extract' | 'index_pkg',
        #    'path': str,        # zip_path for extract; pkg basename for index_pkg
        #    'route': str,       # 'lookup' / 'case' / 'scan' / 'missing' / 'cached'
        #    'pkg_scans': int,   # how many pkg namelists were opened (scan only)
        #    'elapsed_ms': float}
        self._timing = []
        # Threshold in seconds above which a single op is logged in real
        # time (not just at summary).  Most lookups are <1 ms; anything
        # above 100 ms is interesting enough to surface immediately.
        self.timing_log_threshold_s = 0.100
        # Lazy case-insensitive view of self._file_to_pkg used as a
        # fallback when an exact-case lookup misses.  Built on first use
        # and invalidated on _persist_entry / new entries.  Some WoT
        # visuals reference textures with the wrong case (e.g.
        # 'Lion_KL_3Dst_turret_01_AM.dds' for the actual file
        # 'Lion_KL_3DSt_turret_01_AM.dds') and the linux-style
        # case-sensitive ZIP namelist won't find them otherwise.
        self._lower_index_dirty = True
        self._lower_to_actual   = {}   # lowercase zip_path -> actual zip_path
        self._missing   = set()  # zip_paths confirmed absent this session
                                 # (negative cache to skip repeat scan-fallback)

        # --- Lookup table: filename (forward-slash) -> pkg basename -------------
        self._file_to_pkg = {}
        # Path is retained so scan-fallback discoveries can be persisted
        # back to the same file via _persist_entry().
        self._lookup_xml_path = (
            lookup_xml or os.path.join(self._SCRIPT_DIR, 'TheItemList.xml'))
        # Root element name -- captured from the actual file in _load_lookup.
        # Tank Exporter writes <FileList>; older copies (or freshly-created
        # files) use <root>.  _persist_entry uses this to find the closing tag.
        self._lookup_root_tag = 'FileList'
        if os.path.isfile(self._lookup_xml_path):
            if self._progress_cb:
                # Surface the parse on the splash log -- the file is
                # ~15 MB / ~99k entries and takes 1-2 s to walk on
                # first launch (Windows file cache will speed up
                # subsequent runs).  User sees the wait isn't a hang.
                size_mb = os.path.getsize(self._lookup_xml_path) / (1024.0 * 1024.0)
                self._progress_cb(
                    f"Loading TheItemList.xml "
                    f"({size_mb:.1f} MB)...")
            self._load_lookup(self._lookup_xml_path)
            if self._progress_cb:
                self._progress_cb(
                    f"  -> {len(self._file_to_pkg):,} lookup entries")
        else:
            print(f"[PkgExtractor] Lookup XML not found: {self._lookup_xml_path}")
            if self._progress_cb:
                self._progress_cb(
                    f"WARNING: TheItemList.xml not found at "
                    f"{os.path.basename(self._lookup_xml_path)}")

        # --- Fallback pkg list (used only when lookup misses) ------------------
        # We deliberately drop:
        #   * MAP pkgs (numeric prefix) -- never carry tank assets
        #   * Game-mode / event bundles (battle_royale, fun_random,
        #     comp7*, frontline, hangars, ...) -- same reason
        #   * Audio bundles -- pure sound, no 3D
        # See _is_excluded_pkg above for the full filter.  Without this
        # a lookup miss walks ~200 archives, ~150 of which are pointless.
        self._pkg_files = []
        if os.path.isdir(self.pkg_dir):
            all_pkgs = sorted(
                os.path.join(self.pkg_dir, fn)
                for fn in os.listdir(self.pkg_dir)
                if fn.lower().endswith('.pkg')
            )
            kept = [p for p in all_pkgs
                    if not self._is_excluded_pkg(os.path.basename(p))]
            dropped = len(all_pkgs) - len(kept)
            # Vehicle pkgs go first so scan-fallback hits them earliest.
            veh  = [p for p in kept if 'vehicle' in os.path.basename(p).lower()]
            rest = [p for p in kept if 'vehicle' not in os.path.basename(p).lower()]
            self._pkg_files = veh + rest
            print(f"[PkgExtractor] pkg_dir: {self.pkg_dir}  "
                  f"({len(self._pkg_files)} archives "
                  f"after dropping {dropped} maps + non-vehicle bundles, "
                  f"{len(self._file_to_pkg)} lookup entries)")

            # ---- Pre-warm: open every kept pkg once at startup so the
            # ~50-150 ms central-directory read is paid up-front in a
            # tight loop instead of trickling out across the first few
            # tank loads.  Each ZipFile stays open for the life of the
            # session (cleanup() closes them).  Costs ~3-6 s of startup
            # time on a typical install in exchange for instant lookups
            # and fast scan-fallback once it's done.
            #
            # Progress is reported via self._progress_cb every 10 pkgs
            # so the splash screen shows real progress instead of
            # appearing frozen for several seconds.  Without a callback
            # this is a silent stdout-only operation.
            import time as _time
            t0 = _time.perf_counter()
            opened = 0
            n_total = len(self._pkg_files)
            if self._progress_cb:
                self._progress_cb(
                    f"Pre-warming {n_total} pkg archives...")
            # Log EVERY pkg (not every 10th) so the user can see each
            # archive open in real time -- including gui-part1..4 and
            # other named pkgs they care about confirming.  Each
            # callback also pumps the OS event queue (via the
            # callback's render+flip+pump chain in
            # viewer._splash_status) so Windows doesn't mark the
            # window unresponsive during the multi-second warmup.
            for i, p in enumerate(self._pkg_files):
                if self._get_zip(p) is not None:
                    opened += 1
                if self._progress_cb:
                    self._progress_cb(
                        f"  [{i + 1:2d}/{n_total}] {os.path.basename(p)}")
            warm_ms = (_time.perf_counter() - t0) * 1000.0
            print(f"[PkgExtractor] Pre-opened {opened}/{n_total} "
                  f"archives in {warm_ms:.0f} ms")
            if self._progress_cb:
                self._progress_cb(
                    f"Pre-warmed {opened}/{n_total} pkg archives "
                    f"in {warm_ms:.0f} ms")
        else:
            print(f"[PkgExtractor] pkg_dir not found: {self.pkg_dir}")

    # ------------------------------------------------------------------
    def reload_lookup(self, path=None):
        """Drop the current `_file_to_pkg` dict and re-parse the lookup
        XML in place.  Used by the in-app "Rebuild ItemList" action so
        the running session picks up entries the rebuild just added,
        without a restart.

        Args:
            path (str | None): override path; defaults to whatever the
                extractor was constructed with (`_lookup_xml_path`).

        Side effects:
            * Empties `_file_to_pkg`, `_lower_to_actual`, and `_missing`
              (the negative cache that was based on the old table).
            * Re-runs `_load_lookup` so `_lookup_root_tag` stays in sync
              with whatever the rebuilt file used.
            * Drops queued `_pending_persists` -- they're already in the
              freshly written file.
        """
        target = path or self._lookup_xml_path
        if not target or not os.path.isfile(target):
            print(f"[PkgExtractor] reload_lookup: no file at {target}")
            return
        self._file_to_pkg.clear()
        self._lower_to_actual.clear()
        self._lower_index_dirty = True
        self._missing.clear()
        self._pending_persists = []
        self._lookup_xml_path  = target
        self._load_lookup(target)

    # ------------------------------------------------------------------
    def _load_lookup(self, xml_path):
        """Parse TheItemList.xml into self._file_to_pkg.

        Also captures self._lookup_root_tag (e.g. 'FileList' for the Tank
        Exporter format) so _persist_entry can splice new entries before
        the matching closing tag.
        """
        import xml.etree.ElementTree as ET
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
            self._lookup_root_tag = root.tag    # 'FileList' on real files
            for item in root.findall('items'):
                fn  = (item.findtext('filename') or '').strip()
                pkg = (item.findtext('package')  or '').strip()
                if fn and pkg:
                    self._file_to_pkg[fn] = pkg
            print(f"[PkgExtractor] Lookup: {len(self._file_to_pkg):,} entries "
                  f"<- {os.path.basename(xml_path)} (root=<{root.tag}>)")
        except Exception as exc:
            print(f"[PkgExtractor] Failed to parse {xml_path}: {exc}")

    # ------------------------------------------------------------------
    def _get_zip(self, pkg_path):
        """Return a cached, open zipfile.ZipFile for `pkg_path`.

        First call per pkg pays the ~50-150 ms central-directory read.
        Every subsequent call returns the same handle in microseconds,
        which is the entire reason _extract_from went from ~80 ms per
        call to ~3 ms per call.

        Returns None when the file isn't a readable archive (broken
        download, locked file, etc.) -- callers fall back to None
        from _extract_from in that case.
        """
        zf = self._open_zips.get(pkg_path)
        if zf is not None:
            return zf
        if not os.path.isfile(pkg_path):
            return None
        try:
            zf = zipfile.ZipFile(pkg_path, 'r')
        except Exception as exc:
            print(f"[PkgExtractor] Cannot open {os.path.basename(pkg_path)}: {exc}")
            return None
        self._open_zips[pkg_path] = zf
        return zf

    # ------------------------------------------------------------------
    def _extract_from(self, pkg_path, zip_path):
        """Open pkg_path and extract zip_path to temp dir.  Returns local path or None."""
        zf = self._get_zip(pkg_path)
        if zf is None:
            return None
        try:
            out_path = os.path.join(self._temp_dir, zip_path.replace('/', os.sep))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with zf.open(zip_path) as src, open(out_path, 'wb') as dst:
                dst.write(src.read())
            self._extracted[zip_path] = out_path
            print(f"[PkgExtractor] {os.path.basename(zip_path)} "
                  f"<- {os.path.basename(pkg_path)}")
            return out_path
        except Exception as exc:
            print(f"[PkgExtractor] Error extracting {zip_path}: {exc}")
            return None

    # ------------------------------------------------------------------
    def _index_pkg(self, pkg_path):
        """Lazily build the member-name set for one archive (scan fallback).

        Times the FIRST build per pkg (cache miss) -- subsequent calls
        hit the in-memory dict and are essentially free.  The first
        index of a big pkg can be 50-300 ms because zipfile reads the
        whole central directory; that's the cost we're trying to make
        visible with timing.
        """
        if pkg_path not in self._index:
            import time
            t0 = time.perf_counter()
            zf = self._get_zip(pkg_path)
            if zf is None:
                self._index[pkg_path] = frozenset()
            else:
                try:
                    self._index[pkg_path] = frozenset(zf.namelist())
                except Exception as exc:
                    print(f"[PkgExtractor] Cannot index "
                          f"{os.path.basename(pkg_path)}: {exc}")
                    self._index[pkg_path] = frozenset()
            elapsed_s = time.perf_counter() - t0
            self._timing.append({
                'op':         'index_pkg',
                'path':       os.path.basename(pkg_path),
                'route':      'first-open',
                'pkg_scans':  1,
                'elapsed_ms': elapsed_s * 1000.0,
            })
            if elapsed_s >= self.timing_log_threshold_s:
                print(f"[PkgExtractor:timing] index_pkg "
                      f"{os.path.basename(pkg_path)} took "
                      f"{elapsed_s * 1000.0:.1f} ms "
                      f"({len(self._index[pkg_path])} entries)")
        return self._index[pkg_path]

    # ------------------------------------------------------------------
    # Timing helpers
    # ------------------------------------------------------------------

    def reset_timing(self):
        """Clear the timing buffer.  Called by Viewer at the start of
        each load_vehicle so the summary at the end shows JUST that
        load, not the cumulative session.
        """
        self._timing = []

    def summarize_timing(self, top_n=10):
        """Return (summary_lines, totals) describing the timing buffer.

        Returns:
            (list[str], dict) where:
                summary_lines is a human-readable digest
                  (totals + top-N slowest individual ops) suitable for
                  feeding into Viewer.log() one line at a time.
                totals = {
                    'count_extract': int,
                    'count_index_pkg': int,
                    'total_ms': float,
                    'by_route': {route: (count, total_ms), ...},
                    'pkg_scans_total': int,
                }
        """
        ext_calls = [e for e in self._timing if e['op'] == 'extract']
        idx_calls = [e for e in self._timing if e['op'] == 'index_pkg']

        total_ms = sum(e['elapsed_ms'] for e in self._timing)

        # Per-route aggregation for extract calls
        by_route = {}
        for e in ext_calls:
            r = e['route']
            ct, ms = by_route.get(r, (0, 0.0))
            by_route[r] = (ct + 1, ms + e['elapsed_ms'])

        scans_total = sum(e['pkg_scans'] for e in ext_calls)

        # Top slowest single ops (mix of extract + index_pkg)
        slowest = sorted(self._timing,
                         key=lambda e: e['elapsed_ms'],
                         reverse=True)[:top_n]

        lines = []
        lines.append(f"PkgExtractor timing: {total_ms:.0f} ms total  "
                     f"(extracts={len(ext_calls)}, "
                     f"pkg-namelists-opened={len(idx_calls)}, "
                     f"scan-iters={scans_total})")
        for r in ('lookup', 'case', 'scan', 'cached', 'missing'):
            if r in by_route:
                ct, ms = by_route[r]
                lines.append(f"  route {r:<8s}  "
                             f"{ct:>4d} call(s)  {ms:>7.1f} ms")
        if slowest:
            lines.append(f"Top {min(top_n, len(slowest))} slowest:")
            for e in slowest:
                if e['op'] == 'extract':
                    lines.append(f"  {e['elapsed_ms']:>6.1f} ms  "
                                 f"[{e['route']:<6s} scans={e['pkg_scans']:>2d}]  "
                                 f"{e['path'][-60:]}")
                else:
                    lines.append(f"  {e['elapsed_ms']:>6.1f} ms  "
                                 f"[index_pkg]  {e['path']}")

        totals = {
            'count_extract':   len(ext_calls),
            'count_index_pkg': len(idx_calls),
            'total_ms':        total_ms,
            'by_route':        by_route,
            'pkg_scans_total': scans_total,
        }
        return lines, totals

    # ------------------------------------------------------------------
    def extract(self, internal_path):
        """Extract *internal_path* and return its local absolute path, or None.

        Lookup-table path (fast, O(1)):
            Consults self._file_to_pkg to find the exact archive, then extracts.

        Scan fallback (slow, used when no lookup or lookup misses):
            Checks every .pkg in pkg_dir until the file is found.

        Args:
            internal_path (str): forward-slash archive path,
                e.g. 'vehicles/american/A14_T30/normal/lod0/Hull.primitives_processed'
        """
        import time
        t0 = time.perf_counter()
        zip_path = internal_path.replace(os.sep, '/')
        scans = 0   # how many pkg namelists we touched in scan-fallback

        def _record(route, result):
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self._timing.append({
                'op':         'extract',
                'path':       zip_path,
                'route':      route,
                'pkg_scans':  scans,
                'elapsed_ms': elapsed_ms,
            })
            if elapsed_ms / 1000.0 >= self.timing_log_threshold_s:
                print(f"[PkgExtractor:timing] extract "
                      f"{zip_path[-60:]:>60s}  "
                      f"route={route:<10s}  scans={scans:>2d}  "
                      f"{elapsed_ms:7.1f} ms")
            return result

        # Already extracted this session
        if zip_path in self._extracted:
            return _record('cached', self._extracted[zip_path])

        # Negative cache: same missing file was already scanned and not
        # found in any pkg this session.  Avoids re-walking ~30 archives
        # for files that genuinely don't exist (HD-skin variants, etc.).
        if zip_path in self._missing:
            return _record('missing', None)

        # Fast path: lookup table (exact case)
        if self._file_to_pkg:
            pkg_basename = self._file_to_pkg.get(zip_path)
            if pkg_basename:
                return _record('lookup', self._extract_from(
                    os.path.join(self.pkg_dir, pkg_basename), zip_path))
            # Case-insensitive fallback.  WoT visuals occasionally
            # reference textures with the wrong case (e.g.
            # 'Lion_KL_3Dst_turret_01_AM.dds' vs the actual file
            # 'Lion_KL_3DSt_turret_01_AM.dds').  Tank Exporter on
            # Windows didn't notice because NTFS is case-insensitive,
            # but ZIP namelists ARE case-sensitive.
            actual = self._lookup_case_insensitive(zip_path)
            if actual is not None:
                pkg_basename = self._file_to_pkg.get(actual)
                if pkg_basename:
                    result = self._extract_from(
                        os.path.join(self.pkg_dir, pkg_basename), actual)
                    if result:
                        # Cache under BOTH the requested (wrong-case) and
                        # actual paths so subsequent calls hit the fast path
                        self._extracted[zip_path] = result
                        return _record('case', result)
            # Lookup miss -- fall through to scan-fallback.  TheItemList.xml
            # is often older than the WoT install (newly-added tanks like
            # F143_Fauteur won't be indexed), so we need to keep looking
            # rather than giving up.

        # Slow fallback: scan every archive (vehicle pkgs first --
        # see PkgExtractor.__init__ ordering).  Successful hits are
        # cached in self._file_to_pkg AND eagerly persisted to
        # TheItemList.xml via _persist_entry, so the next session
        # hits the O(1) lookup path on first try.
        zip_path_lower = zip_path.lower()
        for pkg_path in self._pkg_files:
            scans += 1
            members = self._index_pkg(pkg_path)
            if zip_path in members:
                actual = zip_path
            else:
                # Case-insensitive scan-side match too
                lower_map = self._index_pkg_lower(pkg_path)
                actual = lower_map.get(zip_path_lower)
                if actual is None:
                    continue
            result = self._extract_from(pkg_path, actual)
            if result:
                pkg_basename = os.path.basename(pkg_path)
                self._file_to_pkg[actual] = pkg_basename
                self._lower_index_dirty = True
                self._persist_entry(actual, pkg_basename)
                if actual != zip_path:
                    # Cache the wrong-case request too so we don't repeat
                    # the case-insensitive scan
                    self._extracted[zip_path] = result
                return _record('scan', result)

        # File doesn't exist anywhere -- remember so we don't rescan
        self._missing.add(zip_path)
        return _record('missing', None)

    # ------------------------------------------------------------------
    def _lookup_case_insensitive(self, zip_path):
        """Lazy case-folded view of the lookup table.  Returns the
        actual-case key that matches `zip_path` ignoring case, or None."""
        if self._lower_index_dirty:
            self._lower_to_actual = {
                k.lower(): k for k in self._file_to_pkg.keys()}
            self._lower_index_dirty = False
        return self._lower_to_actual.get(zip_path.lower())

    # ------------------------------------------------------------------
    def _index_pkg_lower(self, pkg_path):
        """Case-folded view of one pkg's namelist for case-insensitive
        scan-fallback.  Built once per pkg, alongside _index_pkg."""
        cache_key = (pkg_path, '__lower__')
        if cache_key not in self._index:
            members = self._index_pkg(pkg_path)
            self._index[cache_key] = {m.lower(): m for m in members}
        return self._index[cache_key]

    # ------------------------------------------------------------------
    def extract_from_pkg(self, pkg_basename, internal_path):
        """Extract *internal_path* from a specific archive by basename.

        Used when the caller already knows which archive owns the file
        (e.g. vehicle XMLs are always in scripts.pkg).  Bypasses the
        TheItemList.xml lookup table, which only indexes res/ assets.

        Args:
            pkg_basename  (str): archive filename, e.g. 'scripts.pkg'
            internal_path (str): forward-slash path inside the archive
        """
        zip_path = internal_path.replace(os.sep, '/')
        if zip_path in self._extracted:
            return self._extracted[zip_path]
        pkg_path = os.path.join(self.pkg_dir, pkg_basename)
        return self._extract_from(pkg_path, zip_path)

    # ------------------------------------------------------------------
    def extract_from_pkg_group(self, pkg_basenames, internal_path):
        """Try each archive in *pkg_basenames* until one contains
        *internal_path*; return the local extracted path or None.

        Used for sharded asset sets like gui-part1..N.pkg where the
        same logical resource (e.g. an icon) is split across multiple
        archives and the caller doesn't know which one owns any given
        entry up front.

        Membership is checked via the cached zip namelist (`_index_pkg`)
        so each archive is opened at most once for indexing.

        Args:
            pkg_basenames (iterable[str]): archives to try, in priority order
            internal_path (str)          : forward-slash path inside the archive
        """
        zip_path = internal_path.replace(os.sep, '/')
        if zip_path in self._extracted:
            return self._extracted[zip_path]
        for basename in pkg_basenames:
            pkg_path = os.path.join(self.pkg_dir, basename)
            if not os.path.isfile(pkg_path):
                continue
            if zip_path not in self._index_pkg(pkg_path):
                continue
            local = self._extract_from(pkg_path, zip_path)
            if local:
                return local
        return None

    # Recognised vehicle-class tags (first token of <tags> in list.xml).
    # Anything else is treated as 'other'.
    _VCLASSES = {'lightTank', 'mediumTank', 'heavyTank', 'AT-SPG', 'SPG'}

    # ------------------------------------------------------------------
    def list_vehicle_xmls(self, with_tier=False):
        """Enumerate the canonical tank list per nation.

        Source of truth: each nation's scripts/item_defs/vehicles/<nation>/list.xml
        (BigWorld packed XML).  The per-nation list.xml maps tank tag ->
        metadata (level, tags, userString, ...) and is the authoritative
        index of playable vehicles.  Reading it directly avoids needing
        any directory-scan blocklists (customization.xml, dev/test files,
        non-playable Pillbox / Observer entries are all simply absent).

        Args:
            with_tier (bool):
                * False -> returns {nation: [xml_name, ...]} sorted by name.
                * True  -> returns
                    {nation: [{'xml': str,
                               'tier':   int   | None,
                               'vclass': str   | None}, ...]}
                  sorted by (tier, name).  vclass is one of
                  'lightTank' / 'mediumTank' / 'heavyTank' / 'AT-SPG' /
                  'SPG' / 'other' -- carried for use as tree-node icons.

        Returns:
            dict   (see Args)
        """
        scripts_pkg = os.path.join(self.pkg_dir, 'scripts.pkg')
        if not os.path.isfile(scripts_pkg):
            print(f"[PkgExtractor] scripts.pkg not found at {scripts_pkg}")
            return {}

        # Discover which nations have a list.xml (no directory walking,
        # just look for entries matching <nation>/list.xml).
        prefix      = 'scripts/item_defs/vehicles/'
        list_paths  = {}    # nation -> internal scripts.pkg path
        try:
            with zipfile.ZipFile(scripts_pkg, 'r') as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename
                    if not name.startswith(prefix):
                        continue
                    rel = name[len(prefix):]
                    parts = rel.split('/')
                    if len(parts) != 2 or parts[1] != 'list.xml':
                        continue
                    nation = parts[0]
                    if nation == 'common':
                        continue
                    list_paths[nation] = name
        except Exception as exc:
            print(f"[PkgExtractor] list_vehicle_xmls scan failed: {exc}")
            return {}

        # Parse every list.xml into {tag: {tier, vclass}}
        per_nation = {}
        for nation, list_path in list_paths.items():
            per_nation[nation] = self._read_tank_table(
                scripts_pkg, list_path, nation)

        # Filter out non-tank tags that share the vehicle-list
        # plumbing but aren't standalone playable vehicles for our
        # purposes.  Two token families are dropped:
        #
        #   'StoryMode' -- campaign variants (suffixes seen on a
        #     current install: `_StoryMode`, `_StoryModeStealth`,
        #     `_StoryModeHard`, `_StoryMode_3_4`).  An
        #     `endswith('StoryMode')` match misses three quarters
        #     of them, so substring is the right call.
        #
        #   'G00_' -- the prefix WoT uses for German "vehicle"
        #     entries that are actually props / scenery: bunkers,
        #     pillboxes, coastal-gun emplacements, the Fireball
        #     bomber, etc.  They have a tier and a class but no
        #     real tank assets behind them; loading them ends in
        #     a broken visual.  All known entries on a current
        #     install: G00_Bomber_SH, G00_Pillbox_Gun_*_SM24,
        #     G00_Pillbox_Tank_Turret_SM24.
        #
        # Both substrings are safe -- no production tank tag
        # carries either token for any other reason.  Done at the
        # single source-of-truth so every downstream consumer
        # (tier tree, load dialog, FBX auto-twin, etc.) sees a
        # clean list.
        DROP_TOKENS = ('StoryMode', 'G00_')
        n_dropped = 0
        for nation, tbl in per_nation.items():
            drop_tags = [t for t in tbl
                         if any(tok in t for tok in DROP_TOKENS)]
            for tag in drop_tags:
                del tbl[tag]
                n_dropped += 1
        if n_dropped:
            print(f"[PkgExtractor] vehicle xmls: dropped {n_dropped} "
                  f"non-tank variant(s) "
                  f"(StoryMode / G00_ props)")

        if not with_tier:
            result = {
                n: sorted(t + '.xml' for t in tbl)
                for n, tbl in per_nation.items()
            }
            total = sum(len(v) for v in result.values())
            print(f"[PkgExtractor] vehicle xmls: {len(result)} nations, "
                  f"{total} tanks")
            return result

        result = {}
        for nation, tbl in per_nation.items():
            entries = []
            for tag, meta in tbl.items():
                entries.append({
                    'xml':         tag + '.xml',
                    'tier':        meta.get('tier'),
                    'vclass':      meta.get('vclass'),
                    'user_string': meta.get('user_string'),
                })
            entries.sort(key=lambda e: (
                (e['tier'] is None, e['tier'] or 0),
                e['xml']))
            result[nation] = entries

        total = sum(len(v) for v in result.values())
        print(f"[PkgExtractor] vehicle xmls (with tier): "
              f"{len(result)} nations, {total} tanks")
        return result

    # ------------------------------------------------------------------
    def _read_tank_table(self, scripts_pkg, list_xml_internal, nation):
        """Decode <nation>/list.xml inside scripts.pkg and return
        {tank_tag: {'tier': int|None, 'vclass': str|None}}.
        Missing or unparseable file -> {}.

        list.xml format (BWXML):
            <root>
              <A21_T14>
                <level>5</level>
                <tags>heavyTank HD ...</tags>
                ...
              </A21_T14>
              ...
            </root>
        """
        if not list_xml_internal:
            return {}
        try:
            with zipfile.ZipFile(scripts_pkg, 'r') as zf:
                raw = zf.read(list_xml_internal)
        except Exception as exc:
            print(f"[PkgExtractor] read {list_xml_internal} failed: {exc}")
            return {}

        if is_bwxml(raw):
            try:
                xml_str = decode_bwxml(raw)
                xml_str = re.sub(
                    r'<xmlns:[^>]*>[^<]*</xmlns:[^>]*>', '', xml_str)
                root = ET.fromstring(xml_str)
            except Exception as exc:
                print(f"[PkgExtractor] decode {nation}/list.xml: {exc}")
                return {}
        else:
            try:
                root = ET.fromstring(raw.decode('utf-8', errors='replace'))
            except Exception as exc:
                print(f"[PkgExtractor] parse {nation}/list.xml: {exc}")
                return {}

        out = {}
        for tank_el in root:
            tag = tank_el.tag
            tier = None
            lvl_el = tank_el.find('level')
            if lvl_el is not None and lvl_el.text:
                try:
                    tier = int(lvl_el.text.strip())
                except ValueError:
                    pass

            vclass = None
            tags_el = tank_el.find('tags')
            if tags_el is not None and tags_el.text:
                first = tags_el.text.strip().split(None, 1)
                if first:
                    cand = first[0]
                    vclass = cand if cand in self._VCLASSES else 'other'

            # userString is a localization reference, format
            # `#<catalog>:<key>` (e.g. `#usa_vehicles:A37_M40M43`).
            # Resolved to a friendly localized name by
            # tankExporterPy.localization.WoTLocalizer at consumption
            # time.  We just carry the raw ref through here.
            user_str = None
            us_el = tank_el.find('userString')
            if us_el is not None and us_el.text:
                user_str = us_el.text.strip() or None

            out[tag] = {'tier': tier, 'vclass': vclass,
                        'user_string': user_str}
        return out

    # ------------------------------------------------------------------
    def cleanup(self):
        """Delete every file in the temp dir + close every cached
        ZipFile handle.  Also flushes any queued TheItemList.xml
        discoveries that haven't been written out yet (one final
        batched read+write pass).
        """
        # Drain any pending lookup-table writes BEFORE wiping anything
        # else -- we want every scan-fallback discovery from this
        # session preserved across runs.
        try:
            self.flush_persisted_entries()
        except Exception as exc:
            print(f"[PkgExtractor] cleanup: flush failed: {exc}")
        # Close cached zip handles BEFORE wiping the temp dir.  Open
        # zipfile objects don't lock the underlying .pkg on POSIX but
        # do on Windows -- closing first avoids spurious "file in use"
        # errors during shutdown.
        for zf in list(self._open_zips.values()):
            try:
                zf.close()
            except Exception:
                pass
        self._open_zips = {}
        if self._temp_dir and os.path.isdir(self._temp_dir):
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        self._temp_dir = None

    # ------------------------------------------------------------------
    def _persist_entry(self, zip_path, pkg_basename):
        """Queue one (zip_path, pkg_basename) entry for later batch
        write to TheItemList.xml.  Returns True (always queues; the
        actual disk write happens in flush_persisted_entries()).

        OLD behaviour was to read+rewrite the entire 15 MB lookup
        XML on every scan-fallback discovery -- that turned a fresh
        tank load into 20-30 file rewrites and accounted for most of
        the multi-second first-load slowness.  Now we just append
        to an in-memory list; the in-memory `self._file_to_pkg`
        dict was already updated by the caller, so subsequent
        lookups in this session hit the fast path immediately.

        Caller (Viewer.load_vehicle / cleanup) is responsible for
        invoking flush_persisted_entries() when convenient.
        """
        if not self._lookup_xml_path:
            return False
        self._pending_persists.append((zip_path, pkg_basename))
        return True

    # ------------------------------------------------------------------
    def flush_persisted_entries(self):
        """Write every queued discovery to TheItemList.xml in ONE
        read + write pass, then clear the queue.  No-op when the
        queue is empty.

        Called by Viewer.load_vehicle after each successful load
        (so a session that loads 5 tanks does 5 batched writes
        instead of dozens), and again by cleanup() at shutdown.
        """
        if not self._pending_persists:
            return
        xml_path = self._lookup_xml_path
        if not xml_path:
            self._pending_persists = []
            return

        from xml.sax.saxutils import escape as _xml_escape
        # Build one big snippet covering every queued entry.
        snippet_parts = []
        for zip_path, pkg_basename in self._pending_persists:
            snippet_parts.append(
                '  <items>\n'
                f'    <filename>{_xml_escape(zip_path)}</filename>\n'
                f'    <package>{_xml_escape(pkg_basename)}</package>\n'
                '  </items>\n'
            )
        snippet = ''.join(snippet_parts)

        root_tag = getattr(self, '_lookup_root_tag', 'FileList')
        close_tag_bytes = f'</{root_tag}>'.encode('utf-8')
        n = len(self._pending_persists)

        try:
            # No file yet -- create with the full snippet
            if not os.path.isfile(xml_path):
                with open(xml_path, 'w', encoding='utf-8') as fh:
                    fh.write('<?xml version="1.0" standalone="yes"?>\n'
                             f'<{root_tag}>\n' + snippet + f'</{root_tag}>\n')
                print(f"[PkgExtractor] persisted +{n} entries  "
                      f"(created {os.path.basename(xml_path)})")
                self._pending_persists = []
                return

            # Existing file -- splice once before </root>
            with open(xml_path, 'rb') as fh:
                data = fh.read()
            idx = data.rfind(close_tag_bytes)
            if idx < 0:
                print(f"[PkgExtractor] flush_persisted_entries: </{root_tag}> "
                      f"not found in {os.path.basename(xml_path)}")
                return
            new_data = data[:idx] + snippet.encode('utf-8') + data[idx:]
            with open(xml_path, 'wb') as fh:
                fh.write(new_data)
            print(f"[PkgExtractor] persisted +{n} entries to "
                  f"{os.path.basename(xml_path)}")
            self._pending_persists = []
        except Exception as exc:
            print(f"[PkgExtractor] flush_persisted_entries failed: {exc}")


# ============================================================================
# VehicleXMLLoader - parse WoT vehicle XML and resolve component paths
# ============================================================================

class VehicleXMLLoader:
    """Parse a WoT scripts/item_defs vehicle XML and return the best-equipped
    component set with their model paths and positional offsets.

    Usage:
        info = VehicleXMLLoader.parse(xml_path, res_mods_root)
        # info is a list of dicts, one per component:
        # [{'primitives': abs_path, 'visual': abs_path, 'offset': np.array([x,y,z])}, ...]
    """

    @staticmethod
    def find_engine_exhaust(def_xml_path):
        """Read a tank's def XML and return its engine-exhaust spec.

        WoT vehicle XMLs ship a single <exhaust> block per tank, e.g.:

            <exhaust>
              <pixie>diesel_large</pixie>
              <nodes>HP_Track_Exhaus_1 HP_Track_Exhaus_2</nodes>
            </exhaust>

        The pixie field names a particle preset (gas_medium /
        diesel_medium / diesel_large / ...).  The nodes field is a
        space-separated list of hardpoint names whose positions are
        looked up in the visual files.

        Args:
            def_xml_path (str): absolute path to the extracted XML
                (BWXML binary or plain XML, both supported).

        Returns:
            dict {'pixie': str|None, 'nodes': list[str]} on success,
            or None when the file is missing / has no <exhaust> block.
        """
        if not def_xml_path or not os.path.isfile(def_xml_path):
            return None
        try:
            with open(def_xml_path, 'rb') as fh:
                raw = fh.read()
        except Exception:
            return None
        text = decode_bwxml(raw) if is_bwxml(raw) else \
               raw.decode('utf-8', errors='replace')
        m = re.search(r'<exhaust\b[^>]*>(.*?)</exhaust>',
                      text, re.IGNORECASE | re.DOTALL)
        if not m:
            return None
        body = m.group(1)
        pixie_m = re.search(r'<pixie\b[^>]*>(.*?)</pixie>',
                            body, re.IGNORECASE | re.DOTALL)
        nodes_m = re.search(r'<nodes\b[^>]*>(.*?)</nodes>',
                            body, re.IGNORECASE | re.DOTALL)
        return {
            'pixie': pixie_m.group(1).strip() if pixie_m else None,
            'nodes': (nodes_m.group(1).strip().split() if nodes_m else []),
        }

    @staticmethod
    def parse(xml_path, res_mods_root, pkg_extractor=None, damaged=False,
              chassis_tag=None, turret_tag=None, gun_tag=None, skin=None):
        """Parse the vehicle XML and return component list for hull, chassis,
        the chosen turret, and the chosen gun.

        Args:
            xml_path      (str): absolute path to the vehicle XML file
            res_mods_root (str): absolute path to res_mods/<version>/ folder
            pkg_extractor (PkgExtractor or None): used when files are missing from
                          res_mods and must be pulled from a .pkg archive.
            damaged       (bool): if True, load the destroyed/crashed variant of
                          each component (uses <models/destroyed> instead of
                          <models/undamaged>).
            chassis_tag   (str | None): pick this chassis tag instead of the
                          last <chassis> child.  None falls back to last child.
            turret_tag    (str | None): pick this turret tag instead of the
                          highest-<price> turret.  None uses the price rule.
            gun_tag       (str | None): pick this gun tag (under the chosen
                          turret) instead of the last <gun> child.
            skin          (str | None): name of a skin under <models>/<sets>;
                          when given, model paths come from
                          <models>/<sets>/<skin>/<variant> instead of
                          <models>/<variant>.  Per-component fallback to the
                          default path applies if the skin doesn't list that
                          component.

        Returns:
            list of dicts: [{'label', 'primitives', 'visual', 'offset'}, ...]
        """
        # 'destroyed' is WoT's tag for the crashed model variant
        model_tag = 'destroyed' if damaged else 'undamaged'

        # Helper: read a component's model path, honouring the skin override.
        # Falls back to <models>/<variant> when the skin doesn't list it.
        def _resolve_model(component_el, variant):
            if component_el is None:
                return ''
            if skin:
                skin_el = component_el.find(f'models/sets/{skin}/{variant}')
                if skin_el is not None and skin_el.text:
                    return skin_el.text.strip()
            el = component_el.find(f'models/{variant}')
            return el.text.strip() if (el is not None and el.text) else ''

        # Vehicle XMLs in scripts.pkg are BigWorld packed-XML (BWXML) binary;
        # res_mods copies are usually plain text.  Sniff and decode.
        with open(xml_path, 'rb') as fh:
            raw = fh.read()
        if is_bwxml(raw):
            xml_str = decode_bwxml(raw)
            # Strip BigWorld's <xmlns:xmlref>...</xmlns:xmlref> schema-ref
            # element -- ET.fromstring treats the colon as an undeclared
            # namespace prefix and refuses to parse otherwise.
            xml_str = re.sub(r'<xmlns:[^>]*>[^<]*</xmlns:[^>]*>', '', xml_str)
            root = ET.fromstring(xml_str)
        else:
            root = ET.fromstring(raw.decode('utf-8', errors='replace'))

        components = []

        # --- BW-space pivot positions from the vehicle XML --------------------
        #
        # turret_pos_bw : hull-local BigWorld XYZ of the turret joint
        #                 (<hull/turretPositions/turret>)
        # gun_pos_bw    : turret-local BigWorld XYZ of the gun joint
        #                 (<best_turret/gunPosition>)
        #
        # BigWorld uses +Z=forward.  OpenGL uses -Z=forward.
        # Y is "up" in both systems so no sign change is needed for Y.
        # Conversion applied below: gl_z = -bw_z
        turret_pos_bw = VehicleXMLLoader._vec3(
            root.find('hull/turretPositions/turret'))

        # --- Chassis (override tag, else last listed) -------------------------
        chassis_el = root.find('chassis')
        best_chassis = None
        if chassis_el is not None:
            if chassis_tag:
                best_chassis = chassis_el.find(chassis_tag)
            if best_chassis is None and len(list(chassis_el)) > 0:
                best_chassis = list(chassis_el)[-1]
        chassis_model = _resolve_model(best_chassis, model_tag)
        chassis_entry = VehicleXMLLoader._entry(
            'chassis', chassis_model, res_mods_root, [0, 0, 0], pkg_extractor)
        components.append(chassis_entry)

        # --- Chassis V-bone world Y (hull attachment height) ------------------
        #
        # The chassis visual contains a node named "V" whose world Y (summed
        # row3.y down from Scene Root) gives the height above ground at which
        # the hull body sits.  All other components are offset from this Y.
        chassis_attach_y = 0.0
        if chassis_entry['visual']:
            chassis_attach_y = VehicleXMLLoader._chassis_attach_y(
                chassis_entry['visual'])

        # --- Hull offset (GL space) -------------------------------------------
        # Hull vertices are in hull-local space (Y=0 at hull geometric centre).
        # Raise the hull so its local origin sits at the chassis V-bone height.
        hull_offset_gl = np.array([0.0, chassis_attach_y, 0.0], dtype=np.float32)
        hull_model = _resolve_model(root.find('hull'), model_tag)
        components.append(VehicleXMLLoader._entry(
            'hull', hull_model, res_mods_root, hull_offset_gl, pkg_extractor))

        # --- Turret offset (GL space) -----------------------------------------
        # turret_pos_bw is the joint position in hull-local BW space.
        # In GL space: add chassis_attach_y to Y, negate Z.
        turret_offset_gl = np.array([
            turret_pos_bw[0],
            turret_pos_bw[1] + chassis_attach_y,
            -turret_pos_bw[2],
        ], dtype=np.float32)

        turrets0    = root.find('turrets0')
        best_turret = None
        if turrets0 is not None:
            if turret_tag:
                best_turret = turrets0.find(turret_tag)
            if best_turret is None:
                best_turret = VehicleXMLLoader._pick_best(turrets0, 'price')
        turret_model = _resolve_model(best_turret, model_tag)
        components.append(VehicleXMLLoader._entry(
            'turret', turret_model, res_mods_root, turret_offset_gl, pkg_extractor))

        # --- Gun offset (GL space) --------------------------------------------
        # gun_pos_bw is the joint position in turret-local BW space.
        # Accumulate turret + gun pivots; convert to GL.
        gun_pos_bw = VehicleXMLLoader._vec3(best_turret.find('gunPosition'))
        gun_offset_gl = np.array([
            turret_pos_bw[0] + gun_pos_bw[0],
            turret_pos_bw[1] + gun_pos_bw[1] + chassis_attach_y,
            -(turret_pos_bw[2] + gun_pos_bw[2]),
        ], dtype=np.float32)

        guns_el  = best_turret.find('guns') if best_turret is not None else None
        best_gun = None
        if guns_el is not None:
            if gun_tag:
                best_gun = guns_el.find(gun_tag)
            if best_gun is None and len(list(guns_el)) > 0:
                best_gun = list(guns_el)[-1]
        gun_model = _resolve_model(best_gun, model_tag)
        components.append(VehicleXMLLoader._entry(
            'gun', gun_model, res_mods_root, gun_offset_gl, pkg_extractor))

        return components

    # ------------------------------------------------------------------
    @staticmethod
    def list_options(xml_path):
        """Enumerate the swappable parts + available skins for a tank.

        Drives the load dialog: every available chassis / turret / gun
        appears as a radio choice, the per-tank "best" picks are flagged
        as defaults, and any 3D-style skins under <hull>/<models>/<sets>
        become per-skin dialog blocks.

        Returns:
            dict   with keys:
                'chassis'             : list[str] of chassis tags
                'best_chassis'        : str (last child -- matches parse())
                'turrets'             : list[str] of turret tags
                'best_turret'         : str (highest <price> -- matches parse())
                'guns_per_turret'     : dict[turret_tag, list[str]] gun tags
                'best_gun_per_turret' : dict[turret_tag, str] last gun
                'skins'               : list[str] of skin names from <sets>
                                        (Default is implicit, NOT included)
        """
        with open(xml_path, 'rb') as fh:
            raw = fh.read()
        if is_bwxml(raw):
            xml_str = decode_bwxml(raw)
            xml_str = re.sub(
                r'<xmlns:[^>]*>[^<]*</xmlns:[^>]*>', '', xml_str)
            root = ET.fromstring(xml_str)
        else:
            root = ET.fromstring(raw.decode('utf-8', errors='replace'))

        chassis_el = root.find('chassis')
        chassis_children = list(chassis_el) if chassis_el is not None else []
        chassis_tags = [c.tag for c in chassis_children]
        best_chassis = chassis_children[-1].tag if chassis_children else ''

        turrets_el = root.find('turrets0')
        turret_children = list(turrets_el) if turrets_el is not None else []
        turret_tags = [t.tag for t in turret_children]
        best_turret_el = (VehicleXMLLoader._pick_best(turrets_el, 'price')
                          if turret_children else None)
        best_turret = best_turret_el.tag if best_turret_el is not None else ''

        guns_per_turret = {}
        best_gun_per_turret = {}
        for t in turret_children:
            guns_el = t.find('guns')
            gun_children = list(guns_el) if guns_el is not None else []
            guns_per_turret[t.tag] = [g.tag for g in gun_children]
            best_gun_per_turret[t.tag] = (gun_children[-1].tag
                                          if gun_children else '')

        # Skins live under hull/models/sets/<skin_name>/...  Each child of
        # <sets> is one skin.  We use the hull as the canonical list of
        # skins (sub-component <sets> mirror these names).
        skins = []
        sets_el = root.find('hull/models/sets')
        if sets_el is not None:
            skins = [c.tag for c in sets_el]

        return {
            'chassis':             chassis_tags,
            'best_chassis':        best_chassis,
            'turrets':             turret_tags,
            'best_turret':         best_turret,
            'guns_per_turret':     guns_per_turret,
            'best_gun_per_turret': best_gun_per_turret,
            'skins':               skins,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def parse_info(xml_path, pkg_extractor=None):
        """Extract human-readable stats from a vehicle XML.

        Returns a dict with sections that the viewer's info panel can
        render directly.  All numeric fields are returned as strings so
        the panel doesn't have to format / round / fall back -- missing
        values come back as ''.

        Engine and radio specs live in <nation>/components/{engines,radios}.xml
        and are looked up by the stub element name in the vehicle XML.
        Looking those up requires a PkgExtractor (since the components
        XMLs are inside scripts.pkg) -- when it's None, those sections
        are filled with whatever text the stub element carries (usually
        nothing).

        Args:
            xml_path      (str): absolute path to the vehicle XML
            pkg_extractor (PkgExtractor or None): used to fetch the
                          per-nation components/{engines,radios}.xml

        Returns:
            dict   with keys:
                'header'  -- {'xml', 'nation'}
                'hull'    -- scalar fields from <hull>
                'chassis' -- scalar fields from best chassis
                'turret'  -- scalar fields from best turret
                'gun'     -- scalar fields from top gun + pitch/yaw limits
                'shells'  -- list of {kind, damage, penetration, ...}
                'engine'  -- shared engine fields (or {}'s if not resolved)
                'radio'   -- shared radio fields
                'fueltank'-- shared fueltank fields
        """
        # Decode the vehicle XML (BWXML-aware, same as parse())
        with open(xml_path, 'rb') as fh:
            raw = fh.read()
        if is_bwxml(raw):
            xml_str = decode_bwxml(raw)
            xml_str = re.sub(
                r'<xmlns:[^>]*>[^<]*</xmlns:[^>]*>', '', xml_str)
            root = ET.fromstring(xml_str)
        else:
            root = ET.fromstring(raw.decode('utf-8', errors='replace'))

        # Try to detect the nation from the vehicle XML path (e.g. .../usa/A14...)
        nation = ''
        parts = os.path.normpath(xml_path).replace('\\', '/').split('/')
        try:
            i = parts.index('vehicles')
            nation = parts[i + 1].lower()
        except (ValueError, IndexError):
            pass

        info = {
            'header':   {'xml': os.path.basename(xml_path), 'nation': nation},
            'hull':     VehicleXMLLoader._scalars(root.find('hull'),
                            keep={'weight', 'maxHealth', 'primaryArmor'}),
            'chassis':  {},
            'turret':   {},
            'gun':      {},
            'shells':   [],
            'engine':   {},
            'radio':    {},
            'fueltank': {},
        }

        # ---- Best chassis (last child) ---------------------------------
        chassis_el = root.find('chassis')
        if chassis_el is not None and len(list(chassis_el)) > 0:
            best_chassis = list(chassis_el)[-1]
            info['chassis'] = VehicleXMLLoader._scalars(best_chassis, keep={
                'userString', 'level', 'price', 'weight',
                'maxHealth', 'maxRegenHealth', 'repairCost',
                'rotationSpeed', 'brakeForce', 'maxClimbAngle',
                'terrainResistance', 'repairTime',
            })

        # ---- Best turret + top gun -------------------------------------
        turrets_el = root.find('turrets0')
        best_turret = None
        if turrets_el is not None and len(list(turrets_el)) > 0:
            best_turret = VehicleXMLLoader._pick_best(turrets_el, 'price')
            info['turret'] = VehicleXMLLoader._scalars(best_turret, keep={
                'userString', 'level', 'price', 'weight',
                'maxHealth', 'rotationSpeed',
                'circularVisionRadius', 'invisibilityFactor',
            })

        if best_turret is not None:
            guns_el = best_turret.find('guns')
            if guns_el is not None and len(list(guns_el)) > 0:
                top_gun = list(guns_el)[-1]
                info['gun'] = VehicleXMLLoader._scalars(top_gun, keep={
                    'userString', 'level', 'price', 'weight',
                    'maxHealth', 'rotationSpeed',
                    'maxAmmo', 'reloadTime', 'aimingTime',
                    'shotDispersionRadius', 'invisibilityFactorAtShot',
                })
                # Pitch / yaw limits ("gun deflection")
                pl = top_gun.find('pitchLimits')
                if pl is not None:
                    info['gun']['minPitch'] = (pl.findtext('minPitch')
                                                or '').strip()
                    info['gun']['maxPitch'] = (pl.findtext('maxPitch')
                                                or '').strip()
                yaw = top_gun.findtext('turretYawLimits')
                if yaw:
                    info['gun']['turretYawLimits'] = yaw.strip()

                # Shells fired by this gun
                info['shells'] = VehicleXMLLoader._read_shells(
                    top_gun, nation, pkg_extractor)

        # ---- Engine / radio / fueltank (resolve through components/) ---
        info['engine']   = VehicleXMLLoader._resolve_component(
            root, 'engines', 'engines.xml', nation, pkg_extractor,
            keep={'userString', 'tags', 'level', 'price', 'weight',
                  'power', 'maxHealth', 'fireStartingChance',
                  'rpm_min', 'rpm_max', 'repairCost'})
        info['radio']    = VehicleXMLLoader._resolve_component(
            root, 'radios', 'radios.xml', nation, pkg_extractor,
            keep={'userString', 'level', 'price', 'weight',
                  'distance', 'maxHealth', 'repairCost'})
        info['fueltank'] = VehicleXMLLoader._resolve_component(
            root, 'fuelTanks', 'fuelTanks.xml', nation, pkg_extractor,
            keep={'userString', 'level', 'price', 'weight',
                  'maxHealth', 'repairCost'})

        return info

    # ------------------------------------------------------------------
    @staticmethod
    def _scalars(parent_el, keep=None):
        """Return {tag: stripped_text} for every direct text child of
        parent_el.  When *keep* is given, restrict to that whitelist."""
        out = {}
        if parent_el is None:
            return out
        for c in parent_el:
            if keep is not None and c.tag not in keep:
                continue
            if len(list(c)) > 0:
                continue   # has children -- not a scalar
            txt = (c.text or '').strip()
            if txt:
                out[c.tag] = txt
        return out

    @staticmethod
    def _read_shells(top_gun_el, nation, pkg_extractor):
        """Walk the gun's <shots> children + look up each shell's stats
        in <nation>/components/shells.xml.

        The per-tank gun stub typically does NOT carry <shots> -- those
        live on the shared gun definition in components/guns.xml,
        keyed by the same tag (e.g. '_105mm_M4_M4A1').  We try the stub
        first, fall back to the shared gun when empty.

        Each result entry combines per-shot fields (speed, gravity,
        max distance, piercing) with per-shell fields (kind, caliber,
        damage, ricochet, price)."""
        shots_el = top_gun_el.find('shots')

        # Fall back to the shared gun definition when the stub has no shots
        if (shots_el is None or len(list(shots_el)) == 0) \
                and nation and pkg_extractor is not None:
            guns_table = VehicleXMLLoader._read_shared_xml(
                f'scripts/item_defs/vehicles/{nation}/components/guns.xml',
                pkg_extractor)
            shared_gun = guns_table.get(top_gun_el.tag)
            if shared_gun is not None:
                shots_el = shared_gun.find('shots')

        if shots_el is None or len(list(shots_el)) == 0:
            return []

        # Lazily decode shells.xml for this nation
        shell_table = {}
        if nation and pkg_extractor is not None:
            shell_table = VehicleXMLLoader._read_shared_xml(
                f'scripts/item_defs/vehicles/{nation}/components/shells.xml',
                pkg_extractor)

        result = []
        for shot in shots_el:
            entry = {'tag': shot.tag}
            entry.update(VehicleXMLLoader._scalars(shot, keep={
                'defaultPortion', 'speed', 'gravity', 'maxDistance',
                'piercingPower',
            }))
            shared = shell_table.get(shot.tag)
            if shared is not None:
                # Merge shared shell fields
                entry.update(VehicleXMLLoader._scalars(shared, keep={
                    'userString', 'icon', 'price',
                    'kind', 'caliber',
                    'normalizationAngle', 'ricochetAngle',
                    'isTracer',
                }))
                # damage is a sub-element with armor/devices children
                dmg = shared.find('damage')
                if dmg is not None:
                    entry['damageArmor']   = (dmg.findtext('armor')
                                               or '').strip()
                    entry['damageDevices'] = (dmg.findtext('devices')
                                               or '').strip()
            result.append(entry)
        return result

    @staticmethod
    def _resolve_component(root, list_tag, components_filename,
                           nation, pkg_extractor, keep):
        """Find the last <list_tag>/<EntryName> in the vehicle XML, then
        look up <EntryName> in <nation>/components/<components_filename>'s
        <shared> block.  Returns merged scalar fields."""
        list_el = root.find(list_tag)
        if list_el is None or len(list(list_el)) == 0:
            return {}
        # 'best' = last child, matches what parse() does for engines etc.
        stub = list(list_el)[-1]
        out  = VehicleXMLLoader._scalars(stub, keep=keep)
        # Merge shared fields if available
        if nation and pkg_extractor is not None:
            shared = VehicleXMLLoader._read_shared_xml(
                f'scripts/item_defs/vehicles/{nation}/components/'
                f'{components_filename}', pkg_extractor)
            shared_el = shared.get(stub.tag)
            if shared_el is not None:
                merged = VehicleXMLLoader._scalars(shared_el, keep=keep)
                # stub fields override shared (rare, but possible)
                merged.update(out)
                out = merged
        return out

    # Internal cache for parsed components/*.xml so we don't decode
    # the same BWXML on every tank load.
    _shared_xml_cache = {}

    @staticmethod
    def _read_shared_xml(internal_path, pkg_extractor):
        """Extract <internal_path> from scripts.pkg, BWXML-decode it,
        return {tag: ET element} of the <shared> block (or top level if
        no <shared> wrapper).  Cached by internal_path."""
        cache = VehicleXMLLoader._shared_xml_cache
        if internal_path in cache:
            return cache[internal_path]

        local = pkg_extractor.extract_from_pkg('scripts.pkg', internal_path)
        if not local:
            cache[internal_path] = {}
            return {}
        try:
            with open(local, 'rb') as fh:
                raw = fh.read()
            if is_bwxml(raw):
                s = decode_bwxml(raw)
                s = re.sub(r'<xmlns:[^>]*>[^<]*</xmlns:[^>]*>', '', s)
                root = ET.fromstring(s)
            else:
                root = ET.fromstring(raw.decode('utf-8', errors='replace'))
        except Exception as exc:
            print(f"[VehicleXMLLoader] parse {internal_path} failed: {exc}")
            cache[internal_path] = {}
            return {}

        shared = root.find('shared')
        scope  = shared if shared is not None else root
        out    = {child.tag: child for child in scope}
        cache[internal_path] = out
        return out

    # ------------------------------------------------------------------
    @staticmethod
    def _vec3(el):
        """Parse space-separated XYZ from an element's text."""
        if el is None:
            return np.zeros(3, dtype=np.float32)
        vals = el.text.strip().split()
        return np.array([float(v) for v in vals[:3]], dtype=np.float32)

    @staticmethod
    def _pick_best(parent_el, price_tag):
        """Return the child element with the highest integer <price_tag>
        value.  Treats missing / empty / non-numeric text as 0 so that
        starter tanks (tier 1, sold for free) don't crash on
        `<price/>` -- whose `.text` is None even though the element exists.
        """
        best, best_price = None, -1
        for child in parent_el:
            el   = child.find(price_tag)
            text = (el.text or '').strip() if el is not None else ''
            try:
                price = int(text)
            except ValueError:
                price = 0
            if price >= best_price:
                best_price = price
                best = child
        return best

    @staticmethod
    def _read_model_base(model_bytes):
        """Return the visual base path from a BigWorld packed-XML .model file.

        Decodes the BWXML binary, then looks for the element named
        'nodefullVisual' or 'nodelessVisual'.  Returns the element text
        (a forward-slash path WITHOUT extension, e.g.
        'vehicles/american/A14_T30/normal/lod0/Hull') or None.

        Falls back to a raw byte scan only if the magic header is missing
        (i.e. the file is already plain text or from an unusual source).
        """
        import xml.etree.ElementTree as ET

        if is_bwxml(model_bytes):
            try:
                xml_str = decode_bwxml(model_bytes)
                root    = ET.fromstring(xml_str)
                for key in ('nodefullVisual', 'nodelessVisual'):
                    el = root.find(key)
                    if el is not None and el.text:
                        return el.text.strip()
            except Exception as exc:
                print(f'[model] BWXML decode error: {exc}')
        else:
            # Plain-text XML  (rare / already extracted)
            try:
                root = ET.fromstring(model_bytes.decode('utf-8', errors='replace'))
                for key in ('nodefullVisual', 'nodelessVisual'):
                    el = root.find(key)
                    if el is not None and el.text:
                        return el.text.strip()
            except Exception:
                pass

        return None

    @staticmethod
    def _read_visual_nodes(visual_path):
        """Decode a .visual_processed file and return its ElementTree root, or None.

        Handles both BWXML binary (the normal case) and plain-XML files.
        """
        if not os.path.isfile(visual_path):
            return None
        with open(visual_path, 'rb') as fh:
            raw = fh.read()
        if is_bwxml(raw):
            try:
                xml_str = decode_bwxml(raw)
                return ET.fromstring(xml_str)
            except Exception as exc:
                print(f"  [nodes] BWXML parse error: {exc}")
                return None
        else:
            try:
                return ET.fromstring(raw.decode('utf-8', errors='replace'))
            except Exception:
                return None

    @staticmethod
    def _chassis_attach_y(chassis_visual_path):
        """Read the chassis .visual_processed and return the world-space Y of
        the 'V' node -- the height at which the hull body attaches.

        The chassis node tree (typical WoT layout):
            Scene Root
              nodes_01  (local Y ~ 0.5)
                V        (local Y ~ 0.625)    <- hull attachment bone

        V world Y = sum of row3.y values from root down to V.

        Y is 'up' in both BigWorld and OpenGL, so no sign flip is needed.

        Returns:
            float  world Y of the V-bone, or 0.0 if it cannot be found.
        """
        xml_root = VehicleXMLLoader._read_visual_nodes(chassis_visual_path)
        if xml_root is None:
            return 0.0

        nodes = VisualLoader.get_node_world_translations(xml_root)

        attach_y = nodes.get('v')
        if attach_y is not None:
            y = float(attach_y[1])
            print(f"  [chassis] V-bone world Y (hull attach): {y:.6f}")
            return y

        print("  [chassis] WARNING: 'V' node not found -- using Y=0 hull offset")
        return 0.0

    @staticmethod
    def _entry(label, model_path, res_mods_root, offset, pkg_extractor=None):
        """Build a component dict: resolve .model -> .primitives_processed/.visual_processed.

        Resolution order:
          1. Read the .model file (from res_mods or PKG) to get the canonical
             base path embedded inside it.
          2. Look for .primitives_processed / .visual_processed at that canonical
             path in res_mods.
          3. Fall back to PKG extraction using the canonical path.
          4. If the .model file could not be read, derive the path from the XML
             model_path directly (old behaviour, last resort).
        """
        model_zip = model_path.replace('\\', '/')          # forward-slash for ZIP / lookup
        model_os  = os.path.join(res_mods_root,
                                  model_path.replace('/', os.sep))

        # --- Step 1: read .model file -----------------------------------------
        model_bytes = None
        if os.path.isfile(model_os):
            with open(model_os, 'rb') as fh:
                model_bytes = fh.read()
        elif pkg_extractor:
            extracted = pkg_extractor.extract(model_zip)
            if extracted:
                with open(extracted, 'rb') as fh:
                    model_bytes = fh.read()

        # --- Step 2: determine canonical base path ----------------------------
        if model_bytes:
            canon_zip = VehicleXMLLoader._read_model_base(model_bytes)
            if canon_zip:
                print(f"    .model -> {canon_zip}")
            else:
                print(f"    .model read but no vehicles/ path found -- using XML path")
                canon_zip = model_zip.replace('.model', '')
        else:
            print(f"    .model not found -- deriving path from XML")
            canon_zip = model_zip.replace('.model', '')

        prim_zip  = canon_zip + '.primitives_processed'
        vis_zip   = canon_zip + '.visual_processed'
        canon_os  = canon_zip.replace('/', os.sep)
        prim_local = os.path.join(res_mods_root, canon_os + '.primitives_processed')
        vis_local  = os.path.join(res_mods_root, canon_os + '.visual_processed')

        # --- Step 3: PKG fallback ---------------------------------------------
        if not os.path.isfile(prim_local) and pkg_extractor:
            extracted = pkg_extractor.extract(prim_zip)
            if extracted:
                prim_local = extracted

        if not os.path.isfile(vis_local) and pkg_extractor:
            extracted = pkg_extractor.extract(vis_zip)
            if extracted:
                vis_local = extracted

        return {
            'label':          label,
            'primitives':     prim_local if os.path.isfile(prim_local) else None,
            'visual':         vis_local  if os.path.isfile(vis_local)  else None,
            'offset':         np.array(offset, dtype=np.float32),
            # Canonical pkg-relative paths (forward slashes, no leading
            # 'res/').  Used by the .primitives_processed writer to
            # mirror the original layout under res_mods/<version>/.
            'primitives_zip': prim_zip,
            'visual_zip':     vis_zip,
        }
