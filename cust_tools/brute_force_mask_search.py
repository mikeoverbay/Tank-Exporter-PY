"""Brute-force every mask value 0..1023 against the 221 unique
(iii.x, iii.y, iii.z) tuples seen in the WoT gun corpus.

Per Coffee 2026-05-14 ("run a mask check.  start with 0.  search
all combos in the list.  Mask it and if there is a hit add it to
hit count for that pattern and show it.  inc 1 and run all combos
again.  loop all 1,000 possible mask"): we want to see whether
any single mask value M produces a clean per-category vert
classifier across the corpus.

Four match rules are tested for every mask M:

  Rule A: `iii.x == M`                     (mask = slot-0 byte)
  Rule B: `M ∈ {iii.x, iii.y, iii.z}`      (mask present in any slot)
  Rule C: `(iii.x & M) == M`               (mask is bit-subset of slot 0)
  Rule D: `((iii.x | iii.y | iii.z) & M) == M`
                                           (mask is bit-subset of OR of slots)

For each mask + rule, count the total vert hits AND break down
by category (recoil_only / rigid_only / stretch / autoloader_only
/ cross_mix).  We then sort by "discrimination" (= |recoil-rigid|
delta normalised) so the most promising masks float to the top.

Output: `mask_sweep_results.txt` at repo root.  Big file
(4096 lines table-driven), but trimmed to the top 100 most
discriminating masks per rule.

Usage:
    python cust_tools/brute_force_mask_search.py
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


WEIGHT_THRESH = 0.1


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
    vis_local  = gun_comp.get('visual')
    prim_local = gun_comp.get('primitives')
    if not (vis_local and prim_local
            and os.path.isfile(vis_local)
            and os.path.isfile(prim_local)):
        return None
    try:
        palette_by_group = VisualLoader.parse_renderset_bones(
            vis_local)
        parsed = MeshParser.parse_primitives_processed(prim_local)
    except Exception:
        return None
    for g in parsed:
        verts = g.get('vertices') or {}
        bi = verts.get('bone_indices')
        ww = verts.get('bone_weights')
        if bi is None or getattr(bi, 'size', 0) == 0:
            continue
        gname = g.get('name')
        pal = palette_by_group.get(gname, [])
        bi_arr = np.asarray(bi).reshape(-1, 4).astype(np.uint8)
        if ww is not None:
            ww_arr = np.asarray(ww).reshape(-1, 4).astype(np.float32)
        else:
            ww_arr = None
        return {
            'palette': list(pal),
            'bi':      bi_arr,
            'ww':      ww_arr,
        }
    return None


def main():
    out_root = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..'))
    with open(os.path.join(out_root, 'gun_palette_table.json'),
              'r', encoding='utf-8') as f:
        table = json.load(f)
    tanks = table.get('tanks') or {}

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

    # Pass 1: aggregate vert counts per (iii.x, iii.y, iii.z, category).
    # tuple_cat_count[(x, y, z)][cat] = total verts across all tanks
    tuple_cat_count = defaultdict(lambda: defaultdict(int))
    parsed = 0
    for tag, entry in tanks.items():
        nation = entry['nation']
        per_idx_cat = entry.get('per_idx_category') or []
        if not per_idx_cat:
            continue
        info = _scan_one_gun(
            pkg, res_mods,
            f'scripts/item_defs/vehicles/{nation}/{tag}.xml')
        if info is None or info['ww'] is None:
            continue
        parsed += 1
        bi = info['bi']
        ww = info['ww']
        n = bi.shape[0]

        cat_code_table = np.zeros(64, dtype=np.int32)
        for i, cat in enumerate(per_idx_cat):
            if i >= 64:
                break
            cat_code_table[i] = {
                'recoil':     1,
                'rigid':      2,
                'autoloader': 3,
                'cloth':      4,
            }.get(cat, 5)
        active = ww > WEIGHT_THRESH
        pidx   = (bi // 3).astype(np.int32)
        pidx_clip = np.clip(pidx, 0, 63)
        cat_per_slot = cat_code_table[pidx_clip]
        cat_per_slot = np.where(active, cat_per_slot, 0)

        for v in range(n):
            cs = cat_per_slot[v]
            has_rec  = bool((cs == 1).any())
            has_rig  = bool((cs == 2).any())
            has_auto = bool((cs == 3).any())
            if has_rec and has_rig:
                cat = 'stretch'
            elif has_rec and not has_rig and not has_auto:
                cat = 'recoil_only'
            elif has_rig and not has_rec and not has_auto:
                cat = 'rigid_only'
            elif has_auto and not has_rec and not has_rig:
                cat = 'autoloader_only'
            else:
                cat = 'cross_mix'
            triple = (int(bi[v, 0]), int(bi[v, 1]), int(bi[v, 2]))
            tuple_cat_count[triple][cat] += 1

    print(f"parsed {parsed} tanks, {len(tuple_cat_count)} unique "
          f"triples")

    triples = sorted(tuple_cat_count.keys())
    cat_names = ['recoil_only', 'rigid_only', 'stretch',
                 'autoloader_only', 'cross_mix']

    # Vectorise: stack triples into (N, 3) array + (N, 5) counts.
    arr = np.array(triples, dtype=np.uint16)
    counts = np.zeros((len(triples), 5), dtype=np.int64)
    for i, t in enumerate(triples):
        for j, c in enumerate(cat_names):
            counts[i, j] = tuple_cat_count[t].get(c, 0)
    total_verts = counts.sum()

    # For each mask M in 0..1023, evaluate all four rules.
    # We store, per (M, rule_idx), an array of 5 vert counts.
    results = {
        'A_x_eq_M':         np.zeros((1024, 5), dtype=np.int64),
        'B_M_in_xyz':       np.zeros((1024, 5), dtype=np.int64),
        'C_M_subset_x':     np.zeros((1024, 5), dtype=np.int64),
        'D_M_subset_orxyz': np.zeros((1024, 5), dtype=np.int64),
    }
    x = arr[:, 0]
    y = arr[:, 1]
    z = arr[:, 2]
    orxyz = x | y | z
    for M in range(1024):
        mask_a = (x == M)
        # M ∈ {x, y, z}
        mask_b = (x == M) | (y == M) | (z == M)
        # M is bit-subset of x:  (x & M) == M
        if M == 0:
            # Every value AND 0 == 0, so the rule matches EVERY
            # triple.  Tag explicitly so the dump shows it.
            mask_c = np.ones(len(triples), dtype=bool)
            mask_d = np.ones(len(triples), dtype=bool)
        else:
            mask_c = (np.bitwise_and(x.astype(np.int64), M)
                      == M)
            mask_d = (np.bitwise_and(orxyz.astype(np.int64), M)
                      == M)
        for name, m in (('A_x_eq_M',         mask_a),
                         ('B_M_in_xyz',       mask_b),
                         ('C_M_subset_x',     mask_c),
                         ('D_M_subset_orxyz', mask_d)):
            sel = counts[m]
            results[name][M] = sel.sum(axis=0)

    # Save the dense table + a discrimination-sorted top list.
    out_path = os.path.join(out_root, 'mask_sweep_results.txt')
    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write('# Brute-force mask sweep over 0..1023\n')
        fh.write(f'# corpus: {parsed} tanks, '
                 f'{len(triples)} unique (iii.x, iii.y, iii.z) '
                 f'triples\n')
        fh.write(f'# total verts: {total_verts}\n')
        fh.write('# Categories: recoil / rigid / stretch / '
                 'autoloader / cross_mix\n')
        fh.write('=' * 78 + '\n\n')

        rule_blurbs = {
            'A_x_eq_M':         'Rule A: iii.x == M',
            'B_M_in_xyz':       'Rule B: M ∈ {iii.x, iii.y, iii.z}',
            'C_M_subset_x':     'Rule C: (iii.x & M) == M',
            'D_M_subset_orxyz':
                'Rule D: ((iii.x | iii.y | iii.z) & M) == M',
        }

        for rule_name, table_arr in results.items():
            fh.write(f'\n# {rule_blurbs[rule_name]}\n')
            fh.write(f'# {"mask":>4}  {"total":>10}  {"recoil":>10}  '
                     f'{"rigid":>10}  {"stretch":>8}  '
                     f'{"autoload":>8}  {"cross":>6}\n')
            # Full dump: every M with at least one hit.
            for M in range(1024):
                row = table_arr[M]
                total = int(row.sum())
                if total == 0:
                    continue
                fh.write(
                    f'  {M:4d}  {total:>10d}  {int(row[0]):>10d}  '
                    f'{int(row[1]):>10d}  {int(row[2]):>8d}  '
                    f'{int(row[3]):>8d}  {int(row[4]):>6d}\n')

            # Discrimination summary: top 20 masks by purity.
            # "Purity" = max(any one category) / total, restricted
            # to masks with >= 1000 hits (avoid 1-vert outliers).
            fh.write(f'\n  # Top 20 masks for {rule_name} by '
                     f'category purity (>=1000 hits):\n')
            scored = []
            for M in range(1024):
                row = table_arr[M]
                total = int(row.sum())
                if total < 1000:
                    continue
                purity = float(row.max()) / total
                dom_cat = cat_names[int(row.argmax())]
                scored.append((M, total, purity, dom_cat, row))
            scored.sort(key=lambda s: (-s[2], -s[1]))
            for M, tot, pur, cat, row in scored[:20]:
                fh.write(f'    mask={M:4d}  total={tot:>9d}  '
                         f'purity={pur*100:5.1f}%  dominant='
                         f'{cat:15s}  '
                         f'breakdown=[r={row[0]} g={row[1]} '
                         f's={row[2]} a={row[3]} x={row[4]}]\n')

    print(f'wrote: {out_path}')
    # Sample to stdout.
    print('\nSample: Rule B (M ∈ {x, y, z}), masks 0..15:')
    for M in range(16):
        row = results['B_M_in_xyz'][M]
        tot = int(row.sum())
        if tot == 0:
            continue
        print(f'  mask={M:3d}  total={tot:>9d}  recoil={int(row[0]):>9d}  '
              f'rigid={int(row[1]):>9d}  stretch={int(row[2]):>6d}  '
              f'auto={int(row[3]):>6d}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
