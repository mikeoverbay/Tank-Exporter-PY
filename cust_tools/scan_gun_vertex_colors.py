"""Enumerate every distinct vertex-color triple on WoT gun meshes.

Per Coffee 2026-06-04 ("we need to classify the guns verts... vertex
colors of 3.3.? are the recoiled... sort all vertex color types.
Number of different colors and values. only guns off course."):

Walks every vehicle def in the user's WoT install, resolves the gun
model reference(s) from `<guns><...><models><undamaged>`, pulls each
gun's `.primitives_processed`, and for every primitive group that
carries a sidecar `.colour` section decodes the per-vertex RGBA bytes.

Two output flavours:

  (a) Global triple frequency table -- distinct (R, G, B) byte triples
      (alpha ignored) with total vertex counts and how many gun meshes
      each triple appears in.  Prints top 40 by vert count to stdout,
      writes the full table to `hand_off/gun_vertex_colors_global.tsv`.

  (b) Per-mesh detail -- one row per gun primitive group with the
      distinct-triple count + top-5 triples for that mesh.  Written to
      `hand_off/gun_vertex_colors_per_mesh.tsv`.

Read-only against the WoT install.  Skips guns whose file is missing
or whose colour section fails to probe.  Prints progress every 100
tanks so the terminal doesn't look frozen.
"""

import json
import os
import re
import sys
import time
from collections import Counter, defaultdict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tankExporterPy.loaders import PkgExtractor, MeshParser
from tankExporterPy.common import is_bwxml, decode_bwxml


# ---------------------------------------------------------------------------
# Gun-path discovery per tank
# ---------------------------------------------------------------------------

_GUN_MODEL_RE = re.compile(
    r'<undamaged>([^<]*?/Gun[^<]*?\.model)</undamaged>',
    re.IGNORECASE)


def extract_gun_model_paths(pe, nation, xml_name):
    """Read `scripts/item_defs/vehicles/<nation>/<xml_name>`, return a
    de-duped set of every Gun*.model path referenced inside its <guns>
    block (including any per-skin <sets> variants).

    Empty set on missing / malformed tank defs.
    """
    internal = f'scripts/item_defs/vehicles/{nation}/{xml_name}'
    local = pe.extract(internal)
    if not local or not os.path.isfile(local):
        return set()
    try:
        with open(local, 'rb') as fh:
            raw = fh.read()
        text = decode_bwxml(raw) if is_bwxml(raw) else raw.decode(
            'utf-8', errors='replace')
    except Exception:
        return set()
    return {m.group(1).strip() for m in _GUN_MODEL_RE.finditer(text)}


_VISUAL_TAG_RE = re.compile(
    r'<node(?:full|less)?Visual>\s*([^<]+?)\s*</node(?:full|less)?Visual>',
    re.IGNORECASE)


def model_to_primitives_path(pe, model_zip):
    """Read a .model file, resolve to the matching .primitives_processed
    path (via its <nodefullVisual> tag).  Returns the pkg-internal
    string suitable for `pe.extract(...)`.  None on read failure.
    """
    local = pe.extract(model_zip)
    if not local or not os.path.isfile(local):
        return None
    try:
        with open(local, 'rb') as fh:
            raw = fh.read()
        text = decode_bwxml(raw) if is_bwxml(raw) else raw.decode(
            'utf-8', errors='replace')
    except Exception:
        return None
    m = _VISUAL_TAG_RE.search(text)
    if not m:
        return None
    stem = m.group(1).strip()
    return stem + '.primitives_processed'


# ---------------------------------------------------------------------------
# Colour extraction per mesh
# ---------------------------------------------------------------------------

def _lenient_colour_decode(raw_section_bytes, expected_verts):
    """Decode a `.colour` section blob to (N, 3) uint8 (R, G, B),
    tolerating +/- a few verts of padding overshoot.

    MeshParser._parse_colour_section requires an EXACT match between
    (section_size - probe_offset) // 4 and vertex_count; several WoT
    gun meshes (T49 in particular) ship an extra byte or two of
    padding, which trips the exact match and drops us into the "no
    probe matched, skipping" path.  For classification purposes we
    only need to see the byte distribution, so we accept up to +8
    entries of overshoot at each probe offset and truncate to
    expected_verts.

    Args:
        raw_section_bytes (bytes): full section blob (preamble + body).
        expected_verts (int):     matching vertex count for the group.

    Returns:
        np.ndarray shape (expected_verts, 3), dtype uint8 in R, G, B
        order (swizzled from BGRA), OR None if no probe worked.
    """
    import numpy as np
    for offset in (132, 68, 136, 72):
        if offset >= len(raw_section_bytes):
            continue
        body = raw_section_bytes[offset:]
        n_entries = len(body) // 4
        # Accept exact match OR small padding overshoot (up to +8).
        if not (expected_verts <= n_entries <= expected_verts + 8):
            continue
        # Read expected_verts * 4 bytes as BGRA uint8.
        bgra = np.frombuffer(body,
                             dtype=np.uint8,
                             count=expected_verts * 4).reshape(
                                 expected_verts, 4)
        rgb = np.empty((expected_verts, 3), dtype=np.uint8)
        rgb[:, 0] = bgra[:, 2]   # R <- byte 2
        rgb[:, 1] = bgra[:, 1]   # G <- byte 1
        rgb[:, 2] = bgra[:, 0]   # B <- byte 0
        return rgb
    return None


def _walk_section_table(data):
    """Return list of (name, offset, size) for every section in a
    .primitives_processed file.  Reuses MeshParser's own section-table
    parser so we can't drift from the loader's canonical layout.
    """
    import struct
    if len(data) < 4:
        return []
    file_len = len(data)
    st_off = struct.unpack('<i', data[file_len - 4:file_len])[0]
    st_pos = file_len - 4 - st_off
    if st_pos < 0 or st_pos >= file_len:
        return []
    secs = MeshParser._parse_section_table(data, st_pos)
    return [(s['name'], s['offset'], s['size']) for s in secs]


def scan_prim_file(pe, prim_zip):
    """Return list of dicts, one per primitive group with a decodable
    `.colour` sidecar section.

    First tries MeshParser (strict match); for any group that MeshParser
    dropped due to size mismatch, retries with a lenient +/- 8-vert
    probe against the raw section bytes so slightly-padded WoT files
    still contribute to the classification.

    Each dict:  {'group': str, 'n_verts': int, 'skinned': bool,
                 'triples': Counter[(r, g, b)]}
    """
    local = pe.extract(prim_zip)
    if not local or not os.path.isfile(local):
        return None
    # MeshParser prints one diagnostic line per group + several per file
    # -- across 1000+ tanks that's 10k+ lines of noise.  Silence stdout
    # around the parse call so only our sweep progress reaches the
    # terminal.
    import io as _io
    import contextlib as _cx
    with _cx.redirect_stdout(_io.StringIO()):
        try:
            groups = MeshParser.parse_primitives_processed(local)
        except Exception:
            return None

    # Raw section blob cache -- reused by the lenient-probe fallback.
    raw_by_name = None

    out = []
    for g in groups:
        v = g.get('vertices') or {}
        colour = v.get('colour')
        n_verts = int(g.get('vertex_count') or len(
            v.get('positions') or []))
        rgb = None
        if colour is not None:
            # Strict path succeeded.  Convert float RGBA back to uint8
            # RGB.
            try:
                import numpy as np
                rgb = (colour[:, :3] * 255.0 + 0.5).astype(np.uint8)
            except Exception:
                rgb = None
        if rgb is None:
            # Lenient fallback: read the `.colour` sidecar blob directly
            # and probe with +/- 8-vert tolerance.
            if raw_by_name is None:
                try:
                    with open(local, 'rb') as fh:
                        data = fh.read()
                    sec_list = _walk_section_table(data)
                    raw_by_name = {name: data[off:off + sz]
                                    for (name, off, sz) in sec_list}
                except Exception:
                    raw_by_name = {}
            base = g.get('name', '')
            # Look for `<base>.colour` or bare `.colour` for single-
            # group files.
            candidates = (f'{base}.colour', '.colour', 'colour')
            for cand in candidates:
                blob = raw_by_name.get(cand)
                if blob is not None:
                    rgb = _lenient_colour_decode(blob, n_verts)
                    if rgb is not None:
                        break
        if rgb is None:
            continue
        triples = Counter()
        for row in rgb:
            triples[(int(row[0]), int(row[1]), int(row[2]))] += 1
        out.append({
            'group':    g.get('name', ''),
            'n_verts':  n_verts,
            'skinned':  v.get('bone_indices') is not None,
            'triples':  triples,
        })
    return out


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def main():
    cfg = {}
    cfg_path = os.path.join(ROOT, 'tankExporterPy.json')
    if os.path.isfile(cfg_path):
        with open(cfg_path, encoding='utf-8') as f:
            cfg = json.load(f)
    pkg_dir    = cfg.get('pkg_dir') or 'C:/Games/World_of_Tanks_NA/res/packages'
    wot_root   = os.path.normpath(os.path.join(pkg_dir, '..', '..'))
    lookup_xml = cfg.get('lookup_xml') or 'C:/experiment/TheItemList.xml'

    print(f'wot_root  : {wot_root}')
    print(f'pkg_dir   : {pkg_dir}')
    print(f'lookup_xml: {lookup_xml}')
    print()

    pe = PkgExtractor(wot_root, pkg_dir=pkg_dir, lookup_xml=lookup_xml)

    # Enumerate every tank def.
    all_xmls = pe.list_vehicle_xmls(with_tier=True)
    total_tanks   = sum(len(v) for v in all_xmls.values())
    print(f'Vehicles: {total_tanks} across {len(all_xmls)} nations')

    # Global tallies + per-mesh detail rows.
    global_triples = Counter()            # (r,g,b) -> total verts
    global_triple_mesh_count = Counter()  # (r,g,b) -> n meshes containing it
    per_mesh_rows = []
    prim_seen = set()

    # Stats
    n_scanned_prims  = 0
    n_prims_missing  = 0
    n_prims_no_col   = 0
    n_meshes_col     = 0    # meshes (primitive groups) that had colour data

    t0 = time.time()
    tanks_seen = 0
    for nation in sorted(all_xmls.keys()):
        for ent in all_xmls[nation]:
            tanks_seen += 1
            xml_name = ent['xml']
            basename = xml_name[:-4] if xml_name.endswith('.xml') else xml_name
            gun_model_paths = extract_gun_model_paths(pe, nation, xml_name)

            for model_zip in gun_model_paths:
                prim_zip = model_to_primitives_path(pe, model_zip)
                if prim_zip is None:
                    continue
                if prim_zip in prim_seen:
                    # Skip duplicates (base gun shared across skins).
                    continue
                prim_seen.add(prim_zip)
                n_scanned_prims += 1
                mesh_details = scan_prim_file(pe, prim_zip)
                if mesh_details is None:
                    n_prims_missing += 1
                    continue
                if not mesh_details:
                    n_prims_no_col += 1
                    continue
                for md in mesh_details:
                    n_meshes_col += 1
                    # Top-5 triples per mesh
                    top5 = md['triples'].most_common(5)
                    top5_str = '; '.join(
                        f'({r},{g},{b}):{c}' for (r, g, b), c in top5)
                    per_mesh_rows.append({
                        'nation':          nation,
                        'tank':            basename,
                        'prim_path':       prim_zip,
                        'group':           md['group'],
                        'n_verts':         md['n_verts'],
                        'skinned':         'Y' if md['skinned'] else 'N',
                        'distinct_triples': len(md['triples']),
                        'top5':            top5_str,
                    })
                    # Global tally
                    for triple, count in md['triples'].items():
                        global_triples[triple] += count
                        global_triple_mesh_count[triple] += 1

            if tanks_seen % 100 == 0:
                print(f'  ... {tanks_seen}/{total_tanks} tanks scanned '
                      f'({n_scanned_prims} unique gun prims, '
                      f'{n_meshes_col} meshes with colour) '
                      f'[{time.time()-t0:.1f}s]')

    elapsed = time.time() - t0
    print()
    print(f'=== SUMMARY ===')
    print(f'tanks scanned            : {tanks_seen}')
    print(f'unique gun prim files    : {n_scanned_prims}')
    print(f'  extraction failed      : {n_prims_missing}')
    print(f'  no colour sidecar      : {n_prims_no_col}')
    print(f'  had colour sidecar     : '
          f'{n_scanned_prims - n_prims_missing - n_prims_no_col}')
    print(f'meshes with colour data  : {n_meshes_col}')
    print(f'total coloured verts     : {sum(global_triples.values()):,}')
    print(f'distinct (R,G,B) triples : {len(global_triples)}')
    print(f'elapsed                  : {elapsed:.1f} s')
    print()
    print(f'--- top 40 (R,G,B) triples by total vert count ---')
    print(f'{"(R,G,B)":>15}  {"vert_count":>12}  {"n_meshes":>10}  {"pct_verts":>10}')
    total_v = sum(global_triples.values()) or 1
    for triple, cnt in global_triples.most_common(40):
        r, g, b = triple
        pct = 100.0 * cnt / total_v
        n_m = global_triple_mesh_count[triple]
        print(f'   ({r:>3},{g:>3},{b:>3})  {cnt:>12,}  {n_m:>10}  {pct:>9.2f}%')

    # --------- Write TSVs ---------
    out_dir = os.path.join(ROOT, 'hand_off')
    os.makedirs(out_dir, exist_ok=True)

    global_tsv = os.path.join(out_dir, 'gun_vertex_colors_global.tsv')
    with open(global_tsv, 'w', encoding='utf-8') as fh:
        fh.write('r\tg\tb\ttotal_vert_count\tn_meshes\tpct_of_all_verts\n')
        for triple, cnt in global_triples.most_common():
            r, g, b = triple
            pct = 100.0 * cnt / total_v
            n_m = global_triple_mesh_count[triple]
            fh.write(f'{r}\t{g}\t{b}\t{cnt}\t{n_m}\t{pct:.4f}\n')

    per_tsv = os.path.join(out_dir, 'gun_vertex_colors_per_mesh.tsv')
    with open(per_tsv, 'w', encoding='utf-8') as fh:
        fh.write('nation\ttank\tprim_path\tgroup\tskinned\t'
                 'n_verts\tdistinct_triples\ttop5\n')
        for row in per_mesh_rows:
            fh.write('\t'.join(str(row[k]) for k in (
                'nation', 'tank', 'prim_path', 'group', 'skinned',
                'n_verts', 'distinct_triples', 'top5')) + '\n')

    print()
    print(f'wrote {global_tsv}')
    print(f'wrote {per_tsv}')


if __name__ == '__main__':
    main()
