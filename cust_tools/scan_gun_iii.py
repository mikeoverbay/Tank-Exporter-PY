"""Scan the gun-mesh `iii` (bone-index) vertex stream for a small set
of named tanks and report every distinct byte value used + its
mapping into the renderSet palette.

Goal: figure out the magic byte-value rule that classifies a vert
as "recoils" vs "rigid" vs "cloth" -- per Coffee 2026-05-14
("you need scan the primitive data for the bone index values..
add all found to the list of colors.. we need to find what the
magic thing is that tells the shader what to move").

For each requested tank (matched by filename substring, case-
insensitive across all nations the PkgExtractor knows about), the
scanner dumps:

  * Nation + full tag (so the user can confirm the match).
  * Gun mesh path + group name.
  * RenderSet palette (= ordered list of bones whose indices the
    `iii` bytes address, byte = palette_idx * 3 per the
    SC_UBYTE4_REVERSE_PADDED convention).
  * Per-slot (iii.x, iii.y, iii.z, iii.w) byte frequency tables.
  * Distinct iii.x values, with each mapped to its palette bone.
  * (iii.x, iii.y) pair frequencies -- the actual classifier the
    shader sees, since iii.x alone doesn't disambiguate cases
    like (3, 3) "two slots on recoil bone" vs (3, 0) "primary
    recoil + rigid secondary".

Output saved to `cust_tools/gun_iii_scan.txt` at repo root.

Usage:
    python cust_tools/scan_gun_iii.py
"""
from __future__ import annotations

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

import numpy as np

from tankExporterPy.loaders import (
    PkgExtractor, VisualLoader, MeshParser, VehicleXMLLoader)


# Tank tag substrings to match (case-insensitive).  Multiple
# entries per "logical tank" cover the variations WoT uses --
# Tiger I is `G05_PzVI`, T110E4 might be tagged `T110E4` or
# `T011E4` (the user's spelling).  All matches are scanned and
# the actual filename appears in the report so we can confirm
# which specific variant landed in the dump.
TANK_TAG_PATTERNS = [
    'G183',
    'GB147',
    'T110E4',   # canonical
    'T011E4',   # user spelling, just in case
    'PzVI',     # Tiger I family
    'Tiger',
]


def _load_cfg():
    candidates = [
        os.path.expanduser('~/.tankExporterPy.json'),
        os.path.join(os.getcwd(), 'tankExporterPy.json'),
        os.path.join(os.path.dirname(__file__),
                     '..', 'tankExporterPy.json'),
    ]
    for p in candidates:
        if os.path.isfile(p):
            with open(p, 'r') as f:
                return json.load(f)
    return {}


def _scan_one_gun(pkg, res_mods, xml_internal_path):
    """Return a dict with palette + bone_indices array for the
    first skinned gun mesh group, or None on any failure."""
    local_xml = pkg.extract(xml_internal_path)
    if not local_xml or not os.path.isfile(local_xml):
        return None
    try:
        components = VehicleXMLLoader.parse(
            local_xml, res_mods_root=res_mods, pkg_extractor=pkg)
    except Exception as exc:
        return {'error': f'parse XML: {type(exc).__name__}: {exc}'}
    gun_comp = next(
        (c for c in components if c.get('label') == 'gun'), None)
    if gun_comp is None:
        return {'error': 'no gun component in XML'}
    vis_local  = gun_comp.get('visual')
    prim_local = gun_comp.get('primitives')
    if not (vis_local and prim_local
            and os.path.isfile(vis_local)
            and os.path.isfile(prim_local)):
        return {'error': f'gun visual/primitives missing: '
                          f'vis={bool(vis_local)} prim={bool(prim_local)}'}
    try:
        palette_by_group = VisualLoader.parse_renderset_bones(
            vis_local)
        parsed = MeshParser.parse_primitives_processed(prim_local)
    except Exception as exc:
        return {'error':
                f'parse visual/prim: {type(exc).__name__}: {exc}'}
    for g in parsed:
        verts = g.get('vertices') or {}
        bi = verts.get('bone_indices')
        if bi is None or getattr(bi, 'size', 0) == 0:
            continue
        gname = g.get('name')
        pal = palette_by_group.get(gname, [])
        bi_arr = np.asarray(bi).reshape(-1, 4).astype(np.int32)
        return {
            'visual':   os.path.basename(vis_local),
            'prim':     os.path.basename(prim_local),
            'group':    gname,
            'palette':  list(pal),
            'iii':      bi_arr,
        }
    return {'error': 'no skinned group with bone_indices found'}


def _palette_lookup(pal, byte_value):
    """Map a raw byte value to the palette bone name via the
    SC_UBYTE4_REVERSE_PADDED `byte / 3 = palette_idx` rule.
    Returns (palette_idx, bone_name) or (idx, '<out of range>').
    """
    idx = byte_value // 3
    if 0 <= idx < len(pal):
        return idx, pal[idx]
    return idx, '<out of range>'


def _format_byte_freq_table(fh, label, slot_array, pal):
    """Write one slot's (byte -> count) frequency table to `fh`."""
    fh.write(f"  {label}\n")
    uniq, counts = np.unique(slot_array, return_counts=True)
    for b, c in sorted(zip(uniq.tolist(), counts.tolist()),
                       key=lambda x: -x[1]):
        pidx, name = _palette_lookup(pal, int(b))
        fh.write(f"    byte={int(b):3d}  count={int(c):6d}  "
                 f"palette_idx={pidx}  bone={name}\n")


def _format_pair_freq_table(fh, label, a, b, pal, top_k=20):
    """Write the top-K (slot_a_byte, slot_b_byte) pair frequencies."""
    fh.write(f"  {label}\n")
    pair = a.astype(np.int64) * 256 + b.astype(np.int64)
    uniq, counts = np.unique(pair, return_counts=True)
    items = sorted(zip(uniq.tolist(), counts.tolist()),
                   key=lambda x: -x[1])
    for code, count in items[:top_k]:
        ba = (code >> 8) & 0xFF
        bb = code & 0xFF
        ia, na = _palette_lookup(pal, ba)
        ib, nb = _palette_lookup(pal, bb)
        fh.write(f"    ({ba:3d}, {bb:3d})  count={int(count):6d}  "
                 f"[{na} | {nb}]\n")
    if len(items) > top_k:
        fh.write(f"    ... ({len(items) - top_k} more pairs)\n")


def main():
    cfg = _load_cfg()
    pkg_dir = cfg.get('pkg_dir')
    if not pkg_dir:
        pkg_dir = r'C:\Games\World_of_Tanks_NA\res\packages'
    wot_root = pkg_dir
    if wot_root.replace('\\', '/').rstrip('/').lower().endswith(
            'res/packages'):
        wot_root = os.path.dirname(os.path.dirname(wot_root))
    res_mods = cfg.get('res_mods', '')
    print(f'wot_root: {wot_root}')
    pkg = PkgExtractor(wot_root)

    by_nation = pkg.list_vehicle_xmls()

    # Find every XML whose basename matches one of the requested
    # patterns.  We DON'T dedupe matches across patterns -- if a
    # file matches "PzVI" AND "Tiger" it still only appears once
    # because we key on the path.
    patterns_lower = [p.lower() for p in TANK_TAG_PATTERNS]
    matches = []     # list of (nation, fname, matched_pattern)
    for nation, fnames in by_nation.items():
        for fname in fnames:
            tag_lower = fname.lower()
            for pat in patterns_lower:
                if pat in tag_lower:
                    matches.append((nation, fname, pat))
                    break

    matches.sort(key=lambda x: (x[0], x[1]))
    print(f'matched {len(matches)} tanks:')
    for nation, fname, pat in matches:
        print(f'  [{nation}]  {fname}  (matched "{pat}")')
    print()
    if not matches:
        print('No tanks matched.  Edit TANK_TAG_PATTERNS at top of script.')
        return 1

    # Write the report.
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', 'gun_iii_scan.txt')
    out_path = os.path.normpath(out_path)
    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write("# Gun-mesh iii byte-value scan\n")
        fh.write(f"# generated by cust_tools/scan_gun_iii.py "
                 f"({len(matches)} tank(s))\n")
        fh.write("#\n")
        fh.write("# Convention: each vertex has 4 bone-index bytes "
                 "(iii.x .. iii.w) and 4 weights.\n")
        fh.write("# byte / 3 = palette index per "
                 "SC_UBYTE4_REVERSE_PADDED.  byte == 0 is the\n"
                 "# first palette bone (typically Gun_BlendBone / "
                 "the rigid root); other values\n"
                 "# address deeper palette slots.\n")
        fh.write("#\n")
        fh.write("# What we're looking for: the rule that decides\n"
                 "# whether a vert RECOILS (translates with the\n"
                 "# barrel) vs stays RIGID (mantlet, breech, hull-\n"
                 "# bolted parts).  Current shader rule:\n"
                 "#   iii.x != 0 AND iii.x != 6 -> recoil.\n"
                 "# This dump shows the (iii.x, iii.y) pair\n"
                 "# distribution per tank so we can sanity-check\n"
                 "# the rule against tanks like G183 / GB147 /\n"
                 "# T110E4 / Tiger I.\n")
        fh.write("=" * 78 + "\n\n")

        for nation, fname, pat in matches:
            tag = fname[:-4]    # strip .xml
            internal = (f'scripts/item_defs/vehicles/{nation}/'
                        f'{fname}')
            fh.write(f"=== [{nation}] {tag}  "
                     f"(matched pattern: {pat}) ===\n")
            info = _scan_one_gun(pkg, res_mods, internal)
            if info is None:
                fh.write("  scan failed (no info returned)\n\n")
                continue
            if 'error' in info:
                fh.write(f"  scan failed: {info['error']}\n\n")
                continue
            pal = info['palette']
            iii = info['iii']
            n_verts = int(iii.shape[0])
            fh.write(f"  visual    : {info['visual']}\n")
            fh.write(f"  prim      : {info['prim']}\n")
            fh.write(f"  group     : {info['group']}\n")
            fh.write(f"  verts     : {n_verts}\n")
            fh.write(f"  palette   : {len(pal)} bones\n")
            for i, b in enumerate(pal):
                fh.write(f"    [{i}] expected_byte={i*3:3d}  "
                         f"name={b}\n")

            # Distinct values across the entire stream.
            uniq_all = sorted(set(iii.flatten().tolist()))
            fh.write(f"\n  distinct bytes (any slot): "
                     f"{uniq_all}\n")
            for axis, name in enumerate(['iii.x', 'iii.y',
                                          'iii.z', 'iii.w']):
                uniq_axis = sorted(set(iii[:, axis].tolist()))
                fh.write(f"  distinct {name}: {uniq_axis}\n")

            # Per-slot frequency tables.
            fh.write("\n  PER-SLOT BYTE FREQUENCIES:\n")
            _format_byte_freq_table(fh, "iii.x", iii[:, 0], pal)
            _format_byte_freq_table(fh, "iii.y", iii[:, 1], pal)
            _format_byte_freq_table(fh, "iii.z", iii[:, 2], pal)
            _format_byte_freq_table(fh, "iii.w", iii[:, 3], pal)

            # Pair frequencies (where the classifier really lives).
            fh.write("\n  (iii.x, iii.y) PAIR FREQUENCIES "
                     "(top 20):\n")
            _format_pair_freq_table(fh, "", iii[:, 0], iii[:, 1],
                                     pal, top_k=20)

            fh.write("\n")

        fh.write("# end of dump\n")

    print(f'wrote report to: {out_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
