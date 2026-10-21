"""
Patch node TRANSLATIONS inside a `.visual_processed` file.

Purpose (marker-pin modding workflow): when an imported FBX carries
`WoTPin_*` wheel markers that the user MOVED in Blender, the game's
chassis visual must be rewritten so the wheel nodes sit where the pins
are.  Only the node `<transform>` row3 translations change.

Two source forms, handled transparently:

  * BWXML packed binary (every pkg-shipped visual).  We do a BINARY
    PATCH: walk the packed stream with the same traversal
    `common.decode_bwxml` uses, record the FILE OFFSET of every
    `<transform>` 12-float block, compute new row3 values, and write
    the 12 bytes in place.  Every other byte of the file is untouched,
    so there is zero re-encode risk -- the engine sees the exact
    original file with three floats changed per moved node.

  * Plain XML (already-unpacked res_mods visuals).  Standard
    ElementTree rewrite of the `<row3>` text.

Frame/convention notes
----------------------
Node transforms compose the way `track_spline.
parse_chassis_bone_world_positions` composes them:

    world = parent_world @ M        (4x4, rows; M[3,:3] = translation)

which gives the recurrence

    world_pos = parent_pos @ M[:3,:3] + M[3,:3]

so moving a node to `target` (in the visual's own frame -- the frame
that parse function reports) needs

    M[3,:3] = target - parent_pos @ M[:3,:3]

Rotation blocks are never touched.  Children of a moved node ride
along via the engine's own composition, exactly like they do here.
"""

import io
import struct
import re
import xml.etree.ElementTree as ET

import numpy as np

from .common import is_bwxml


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read_node_worlds(raw):
    """Return `{identifier: world_pos (np.float64 x3)}` for every named
    node in the visual bytes `raw` (BWXML or plain XML).  Same frame
    and composition as `parse_chassis_bone_world_positions`, but reads
    the floats losslessly from the packed stream instead of through a
    text round-trip."""
    root = _parse_tree(raw)
    worlds = {}
    for el in _iter_children(root, 'node'):
        _walk_nodes(el, np.eye(4), {}, [], {}, worlds)
    return worlds


def patch_visual_node_positions(raw, targets):
    """Move named nodes to new positions.

    Args:
        raw     : bytes of a .visual_processed (BWXML or plain XML).
        targets : {identifier: (x, y, z)} target WORLD positions in
                  the visual's own frame.

    Returns:
        (patched_bytes, applied, missing)
          applied : {identifier: (old_world_pos, new_world_pos)}
          missing : [identifier, ...] targets with no matching node
                    (or a node without a 12-float transform).
    """
    if is_bwxml(raw):
        root = _parse_tree(raw)
        patches = []            # [(file_offset, np3 new row3), ...]
        applied = {}
        for el in _iter_children(root, 'node'):
            _walk_nodes(el, np.eye(4), dict(targets), patches,
                        applied, {})
        out = bytearray(raw)
        for off, row3 in patches:
            out[off:off + 12] = struct.pack(
                '<3f', float(row3[0]), float(row3[1]), float(row3[2]))
        missing = [k for k in targets if k not in applied]
        return bytes(out), applied, missing
    return _patch_plain_xml(raw, targets)


# ---------------------------------------------------------------------------
# BWXML stream walk (mirrors common.decode_bwxml byte-for-byte, but
# builds a lightweight dict tree and records float-block file offsets)
# ---------------------------------------------------------------------------

def _desc(val):
    return val & 0x0FFFFFFF, val >> 28


def _read_cstr(f):
    buf = bytearray()
    while True:
        b = f.read(1)
        if not b or b == b'\x00':
            break
        buf.extend(b)
    return buf.decode('latin-1')


def _new_el(tag):
    return {'tag': tag, 'text': None, 'floats': None,
            'floats_off': None, 'children': []}


def _parse_tree(raw):
    """Parse BWXML bytes OR plain-XML bytes into the dict-tree form the
    walkers below consume."""
    if is_bwxml(raw):
        f = io.BytesIO(raw)
        f.read(4)       # magic
        f.read(1)       # padding byte
        dictionary = []
        while True:
            s = _read_cstr(f)
            if not s:
                break
            dictionary.append(s)
        root = _new_el('root')
        _bw_element(f, root, dictionary)
        return root
    # Plain XML -> same dict shape (no byte offsets; the plain-XML
    # patcher works on the ET tree directly, this path exists so
    # read_node_worlds works uniformly).
    text = raw.decode('utf-8', errors='replace')
    text = re.sub(r'<xmlns:[^>]*>[^<]*</xmlns:[^>]*>', '', text)
    et_root = ET.fromstring(text)
    return _et_to_dict(et_root, wrap_root=True)


def _bw_element(f, el, dictionary):
    num_children = struct.unpack('<H', f.read(2))[0]
    self_end, self_type = _desc(struct.unpack('<I', f.read(4))[0])
    kids = []
    for _ in range(num_children):
        name_idx = struct.unpack('<H', f.read(2))[0]
        child_end, ctype = _desc(struct.unpack('<I', f.read(4))[0])
        kids.append((name_idx, child_end, ctype))
    offset = _bw_data(f, el, dictionary, 0, self_end, self_type)
    for name_idx, child_end, ctype in kids:
        tag = (dictionary[name_idx] if name_idx < len(dictionary)
               else f'unk{name_idx}')
        child = _new_el(tag)
        el['children'].append(child)
        offset = _bw_data(f, child, dictionary, offset, child_end, ctype)


def _bw_data(f, el, dictionary, offset, end, dtype):
    length = end - offset
    if dtype == 0x0:          # nested element block
        _bw_element(f, el, dictionary)
    elif dtype == 0x1:        # string
        if length > 0:
            el['text'] = f.read(length).decode('utf-8', errors='replace')
    elif dtype == 0x3:        # float array -- record its file offset
        n = length // 4
        if n > 0:
            el['floats_off'] = f.tell()
            el['floats'] = list(
                struct.unpack(f'<{n}f', f.read(n * 4)))
        # trailing bytes that aren't a multiple of 4 (shouldn't
        # happen, but the decoder tolerates it by not reading them
        # as floats) -- consume to stay in sync
        rem = length - n * 4
        if rem > 0:
            f.read(rem)
    else:                     # int / bool / base64 / unknown: skip
        if length > 0:
            f.read(length)
    return end


def _et_to_dict(et_el, wrap_root=False):
    d = _new_el('root' if wrap_root else et_el.tag)
    if wrap_root:
        inner = _et_to_dict(et_el)
        # keep the real root as the single child so node walks that
        # start at root['children'] see it
        d['children'].append(inner)
        return d
    d['text'] = et_el.text
    rows = {c.tag: c for c in et_el}
    if (et_el.tag == 'transform'
            and all(f'row{i}' in rows for i in range(4))):
        fl = []
        ok = True
        for i in range(4):
            try:
                vals = [float(x) for x in (rows[f'row{i}'].text
                                           or '').split()]
            except ValueError:
                ok = False
                break
            if len(vals) < 3:
                ok = False
                break
            fl.extend(vals[:3])
        if ok:
            d['floats'] = fl
    for c in et_el:
        d['children'].append(_et_to_dict(c))
    return d


def _iter_children(el, tag):
    return [c for c in el['children'] if c['tag'] == tag]


def _node_transform_matrix(el):
    """4x4 (rows) from a node dict's <transform> child; identity when
    absent / malformed.  Returns (M, transform_el_or_None)."""
    tr = next((c for c in el['children'] if c['tag'] == 'transform'),
              None)
    M = np.eye(4, dtype=np.float64)
    if tr is not None and tr['floats'] and len(tr['floats']) >= 12:
        fl = tr['floats']
        for i in range(4):
            M[i, :3] = fl[i * 3:i * 3 + 3]
    return M, tr


def _walk_nodes(el, parent_world, targets, patches, applied, worlds):
    ident = next((c['text'] for c in el['children']
                  if c['tag'] == 'identifier'), None)
    ident = (ident or '').strip()
    M, tr = _node_transform_matrix(el)

    if ident and ident in targets and tr is not None \
            and tr['floats'] and len(tr['floats']) >= 12:
        target = np.asarray(targets[ident], dtype=np.float64)
        old_world = parent_world[3, :3] @ M[:3, :3] + M[3, :3]
        new_row3 = target - parent_world[3, :3] @ M[:3, :3]
        M[3, :3] = new_row3
        if tr['floats_off'] is not None:
            patches.append((tr['floats_off'] + 36, new_row3))
        else:
            # plain-XML path: stash on the dict; _patch_plain_xml
            # reads it back out
            tr['_new_row3'] = new_row3
        applied[ident] = (old_world, target)

    world = parent_world @ M
    if ident:
        worlds[ident] = np.asarray(world[3, :3], dtype=np.float64)
    for c in _iter_children(el, 'node'):
        _walk_nodes(c, world, targets, patches, applied, worlds)


# ---------------------------------------------------------------------------
# Plain-XML patch
# ---------------------------------------------------------------------------

def _patch_plain_xml(raw, targets):
    text = raw.decode('utf-8', errors='replace')
    text = re.sub(r'<xmlns:[^>]*>[^<]*</xmlns:[^>]*>', '', text)
    et_root = ET.fromstring(text)
    applied = {}

    def _walk(et_node, parent_world):
        ident_el = et_node.find('identifier')
        ident = (ident_el.text.strip()
                 if ident_el is not None and ident_el.text else '')
        tr = et_node.find('transform')
        M = np.eye(4, dtype=np.float64)
        rows_ok = False
        if tr is not None:
            rows_ok = True
            for i in range(4):
                e = tr.find(f'row{i}')
                if e is None or not e.text:
                    continue
                vals = [float(x) for x in e.text.split()]
                if len(vals) >= 3:
                    M[i, :3] = vals[:3]
        if ident and ident in targets and tr is not None and rows_ok:
            target = np.asarray(targets[ident], dtype=np.float64)
            old_world = parent_world[3, :3] @ M[:3, :3] + M[3, :3]
            new_row3 = target - parent_world[3, :3] @ M[:3, :3]
            M[3, :3] = new_row3
            r3 = tr.find('row3')
            if r3 is None:
                r3 = ET.SubElement(tr, 'row3')
            r3.text = (f'{new_row3[0]:.6f} {new_row3[1]:.6f} '
                       f'{new_row3[2]:.6f}')
            applied[ident] = (old_world, target)
        world = parent_world @ M
        for c in et_node.findall('node'):
            _walk(c, world)

    for n in et_root.findall('node'):
        _walk(n, np.eye(4, dtype=np.float64))

    out = ET.tostring(et_root, encoding='unicode').encode('utf-8')
    missing = [k for k in targets if k not in applied]
    return out, applied, missing
