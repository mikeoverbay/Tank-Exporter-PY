"""Dump every distinct (iii.x, iii.y, iii.z, iii.w) tuple seen in
any WoT gun vertex stream -- one row per unique tuple, no
truncation.

Per Coffee 2026-05-14 ("do we have every index value set for all
tank guns?  excluding repeats?"): this is the authoritative
"every byte pattern ever used in a WoT gun" list.  Includes
iii.w in the tuple so we can confirm it's universally 0 (and
flag any exception).  Sorted by total vert count descending.

Output: `gun_iii_unique_tuples.txt` at repo root.

Usage:
    python cust_tools/dump_all_iii_triples.py
"""
from __future__ import annotations

import os
import sys
import json
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

import numpy as np

from tankExporterPy.loaders import (
    PkgExtractor, VisualLoader, MeshParser, VehicleXMLLoader)


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
    local_xml = pkg.extract(xml_internal_path)
    if not local_xml or not os.path.isfile(local_xml):
        return None
    try:
        components = VehicleXMLLoader.parse(
            local_xml, res_mods_root=res_mods, pkg_extractor=pkg)
    except Exception:
        return None
    gun_comp = next(
        (c for c in components if c.get('label') == 'gun'), None)
    if gun_comp is None:
        return None
    prim_local = gun_comp.get('primitives')
    if not (prim_local and os.path.isfile(prim_local)):
        return None
    try:
        parsed = MeshParser.parse_primitives_processed(prim_local)
    except Exception:
        return None
    for g in parsed:
        verts = g.get('vertices') or {}
        bi = verts.get('bone_indices')
        if bi is None or getattr(bi, 'size', 0) == 0:
            continue
        return np.asarray(bi).reshape(-1, 4).astype(np.uint8)
    return None


def main():
    out_root = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..'))

    cfg = _load_cfg()
    pkg_dir = cfg.get('pkg_dir')
    if not pkg_dir:
        pkg_dir = r'C:\Games\World_of_Tanks_NA\res\packages'
    wot_root = pkg_dir
    if wot_root.replace('\\', '/').rstrip('/').lower().endswith(
            'res/packages'):
        wot_root = os.path.dirname(os.path.dirname(wot_root))
    res_mods = cfg.get('res_mods', '')
    pkg = PkgExtractor(wot_root)

    by_nation = pkg.list_vehicle_xmls()
    all_tags = []
    for nation in sorted(by_nation):
        for fname in sorted(by_nation[nation]):
            all_tags.append((nation, fname[:-4]))
    print(f'corpus: {len(all_tags)} tank XMLs')

    # (x, y, z, w) -> count of verts across all tanks
    tuple_counts = defaultdict(int)
    # (x, y, z, w) -> set of tank tags using it
    tuple_tanks = defaultdict(set)
    # Also track just the 3-tuple (x, y, z) for comparison.
    triple_counts = defaultdict(int)
    parsed = 0
    skipped = 0
    for nation, tag in all_tags:
        internal = (f'scripts/item_defs/vehicles/{nation}/'
                    f'{tag}.xml')
        bi = _scan_one_gun(pkg, res_mods, internal)
        if bi is None:
            skipped += 1
            continue
        parsed += 1
        # Count distinct tuples per tank, then aggregate.
        unique_rows = np.unique(bi, axis=0)
        for row in unique_rows:
            t = (int(row[0]), int(row[1]),
                 int(row[2]), int(row[3]))
            tuple_tanks[t].add(tag)
        # Count VERTS per tuple (sum across tanks).
        for row in bi:
            t = (int(row[0]), int(row[1]),
                 int(row[2]), int(row[3]))
            tuple_counts[t] += 1
            triple_counts[(t[0], t[1], t[2])] += 1

    print(f'parsed: {parsed} tanks ({skipped} skipped)')
    print(f'unique 4-tuples (iii.x, iii.y, iii.z, iii.w): '
          f'{len(tuple_counts)}')
    print(f'unique 3-tuples (iii.x, iii.y, iii.z):        '
          f'{len(triple_counts)}')

    # Confirm iii.w is always zero by listing any 4-tuple where
    # iii.w != 0.
    nonzero_w = [t for t in tuple_counts if t[3] != 0]
    print(f'4-tuples with iii.w != 0: {len(nonzero_w)}')
    if nonzero_w:
        for t in sorted(nonzero_w):
            print(f'  {t}  ({tuple_counts[t]} verts, '
                  f'{len(tuple_tanks[t])} tanks)')

    # Write the report.
    out_path = os.path.join(out_root, 'gun_iii_unique_tuples.txt')
    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write('# Every distinct (iii.x, iii.y, iii.z, iii.w) '
                 'tuple seen in any WoT gun mesh\n')
        fh.write(f'# corpus: {len(all_tags)} tanks scanned, '
                 f'{parsed} with parseable gun primitives\n')
        fh.write(f'# unique 4-tuples: {len(tuple_counts)}\n')
        fh.write(f'# unique 3-tuples (ignoring iii.w): '
                 f'{len(triple_counts)}\n')
        fh.write(f'# 4-tuples with iii.w != 0: {len(nonzero_w)}\n')
        if nonzero_w:
            fh.write('#   (those tuples are listed below; '
                     'iii.w is otherwise universally 0)\n')
        fh.write('\n')
        fh.write('# ALL UNIQUE 4-TUPLES (sorted by total vert '
                 'count, descending):\n')
        fh.write(f'# {"x":>3} {"y":>3} {"z":>3} {"w":>3}  '
                 f'{"verts":>8}  {"tanks":>5}\n')
        ordered = sorted(tuple_counts.items(),
                         key=lambda kv: (-kv[1], kv[0]))
        for t, count in ordered:
            fh.write(f'  {t[0]:3d} {t[1]:3d} {t[2]:3d} {t[3]:3d}  '
                     f'{count:8d}  {len(tuple_tanks[t]):5d}\n')

    print(f'\nwrote: {out_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
