"""Build a global table of every distinct (iii.x, iii.y, iii.z)
vertex-byte triple seen across the WoT gun corpus, classified by
the category of the vert it belongs to.

Per Coffee 2026-05-14 ("we are not finding what is common about
the 3 sections.. recoil, ridged and flexible.  we have all the
combos of index values there is right?  did we scan all tanks?"):
this is the exhaustive search.  For every vert in every gun
mesh:

  * Extract the triple (iii.x, iii.y, iii.z); iii.w is always 0.
  * Determine the vert's category via the weighted-blend rule
    from `scan_vertex_blend_categories.py`:
       recoil_only  -- every active slot binds to a recoil bone.
       rigid_only   -- every active slot binds to a rigid bone.
       stretch      -- mix of recoil + rigid (= flexible drape).
       autoloader   -- active slots on autoloader bones.
       cross_mix    -- multi-category mix that isn't recoil+rigid.

Aggregate the counts per (triple, category) and dump:

  1. Total distinct triples seen.
  2. Per-category, the TOP triples by frequency.
  3. Triples that are EXCLUSIVE to one category across the
     entire corpus -- those are the "signature" patterns we
     could use as a classifier without per-tank state.
  4. Triples that overlap categories -- those break any
     universal mask.

Also flags the 3 tanks that failed to parse so we can hunt them
down separately.

Output: `gun_iii_triple_scan.txt` at repo root.

Usage:
    python cust_tools/scan_iii_triples.py
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
        return None, 'no XML extract'
    try:
        components = VehicleXMLLoader.parse(
            local_xml, res_mods_root=res_mods, pkg_extractor=pkg)
    except Exception as exc:
        return None, f'parse fail: {type(exc).__name__}: {exc}'
    gun_comp = next(
        (c for c in components if c.get('label') == 'gun'), None)
    if gun_comp is None:
        return None, 'no gun component'
    vis_local  = gun_comp.get('visual')
    prim_local = gun_comp.get('primitives')
    if not (vis_local and prim_local
            and os.path.isfile(vis_local)
            and os.path.isfile(prim_local)):
        return None, 'gun visual/prim missing on disk'
    try:
        palette_by_group = VisualLoader.parse_renderset_bones(
            vis_local)
        parsed = MeshParser.parse_primitives_processed(prim_local)
    except Exception as exc:
        return None, f'mesh parse fail: {type(exc).__name__}: {exc}'
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
        return ({
            'palette': list(pal),
            'bi':      bi_arr,
            'ww':      ww_arr,
        }, None)
    return None, 'no skinned group in prim'


def main():
    out_root = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..'))
    table_path = os.path.join(out_root, 'gun_palette_table.json')
    with open(table_path, 'r', encoding='utf-8') as f:
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

    # ALL tanks the PkgExtractor knows about (not just the ones
    # the palette table covered -- some failed to parse there too,
    # and we want to surface those names).
    by_nation = pkg.list_vehicle_xmls()
    all_tags = []
    for nation in sorted(by_nation):
        for fname in sorted(by_nation[nation]):
            all_tags.append((nation, fname[:-4]))
    print(f'corpus: {len(all_tags)} tank XMLs')

    # (iii.x, iii.y, iii.z) -> {category: count}
    triple_counts = defaultdict(lambda: defaultdict(int))
    # Per-category totals
    cat_totals = defaultdict(int)
    parsed = 0
    failed = []   # list of (tag, reason)
    no_palette_entry = []
    no_weights = 0

    for nation, tag in all_tags:
        entry = tanks.get(tag)
        if entry is None:
            no_palette_entry.append(tag)
            continue
        per_idx_cat = entry.get('per_idx_category') or []
        if not per_idx_cat:
            no_palette_entry.append(tag)
            continue
        internal = (f'scripts/item_defs/vehicles/{nation}/'
                    f'{tag}.xml')
        info, err = _scan_one_gun(pkg, res_mods, internal)
        if info is None:
            failed.append((tag, err))
            continue
        bi = info['bi']
        ww = info['ww']
        if ww is None:
            no_weights += 1
            continue
        parsed += 1

        # Per-slot category code lookup (0=none, 1=recoil,
        # 2=rigid, 3=autoloader, 4=cloth, 5=unknown).
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

        n = bi.shape[0]
        for v in range(n):
            triple = (int(bi[v, 0]), int(bi[v, 1]), int(bi[v, 2]))
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
            triple_counts[triple][cat] += 1
            cat_totals[cat] += 1

    print(f'parsed: {parsed} tanks')
    print(f'failed: {len(failed)} tanks')
    if failed:
        for tag, err in failed[:10]:
            print(f'  {tag:40s}  {err}')
        if len(failed) > 10:
            print(f'  ... ({len(failed) - 10} more)')
    print(f'no palette entry: {len(no_palette_entry)} tanks')
    print(f'no weights: {no_weights} tanks')

    total_triples = len(triple_counts)
    total_verts = sum(cat_totals.values())
    print(f'\ntotal distinct (iii.x, iii.y, iii.z) triples: '
          f'{total_triples}')
    for cat in ('recoil_only', 'rigid_only', 'stretch',
                'autoloader_only', 'cross_mix'):
        v = cat_totals[cat]
        print(f'  {cat:18s}: {v:>9d}  '
              f'({v/max(1, total_verts)*100:.2f}%)')

    # Exclusive vs shared analysis: for each triple, check whether
    # it appears in only ONE category or multiple.
    exclusive = defaultdict(list)    # cat -> [(triple, count)]
    shared = []                      # list of (triple, {cat: count})
    for triple, cats in triple_counts.items():
        non_zero = {c: n for c, n in cats.items() if n > 0}
        if len(non_zero) == 1:
            cat = next(iter(non_zero))
            exclusive[cat].append((triple, non_zero[cat]))
        else:
            shared.append((triple, non_zero))

    print(f'\nexclusive triples by category:')
    for cat in ('recoil_only', 'rigid_only', 'stretch',
                'autoloader_only', 'cross_mix'):
        cnt = len(exclusive[cat])
        verts = sum(c for _, c in exclusive[cat])
        print(f'  {cat:18s}: {cnt:>6d} triples  '
              f'{verts:>9d} verts')
    print(f'shared triples: {len(shared)}  '
          f'(verts: {sum(sum(d.values()) for _, d in shared)})')

    # Write the report.
    out_path = os.path.join(out_root, 'gun_iii_triple_scan.txt')
    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write('# (iii.x, iii.y, iii.z) triple scan\n')
        fh.write(f'# corpus: {len(all_tags)} tanks ({parsed} '
                 f'parsed, {len(failed)} failed, '
                 f'{len(no_palette_entry)} no palette entry, '
                 f'{no_weights} no weights)\n')
        fh.write(f'# total verts: {total_verts}\n')
        fh.write(f'# distinct triples: {total_triples}\n')
        fh.write('\n')
        fh.write('# Per-category vert totals:\n')
        for cat in ('recoil_only', 'rigid_only', 'stretch',
                    'autoloader_only', 'cross_mix'):
            v = cat_totals[cat]
            fh.write(f'#   {cat:18s}: {v:>9d}  '
                     f'({v/max(1, total_verts)*100:5.2f}%)\n')
        fh.write('\n')

        if failed:
            fh.write(f'# === Tanks that FAILED to parse '
                     f'({len(failed)}) ===\n')
            for tag, err in failed:
                fh.write(f'  {tag:50s}  {err}\n')
            fh.write('\n')

        fh.write('# === Exclusive triples (only one category) ===\n')
        for cat in ('recoil_only', 'rigid_only', 'stretch',
                    'autoloader_only', 'cross_mix'):
            ex = sorted(exclusive[cat], key=lambda x: -x[1])
            cnt = len(ex)
            verts = sum(c for _, c in ex)
            fh.write(f'\n## {cat}: {cnt} exclusive triples '
                     f'({verts} verts total)\n')
            for triple, c in ex[:50]:
                fh.write(f'  {triple} -> {c} verts\n')
            if len(ex) > 50:
                fh.write(f'  ... ({len(ex) - 50} more)\n')

        fh.write('\n# === Shared triples (multiple categories) ===\n')
        fh.write(f'# Top 50 by total vert count.\n')
        shared.sort(
            key=lambda x: -sum(x[1].values()))
        for triple, cats in shared[:50]:
            total = sum(cats.values())
            breakdown = ', '.join(
                f'{c}={n}' for c, n in
                sorted(cats.items(), key=lambda kv: -kv[1]))
            fh.write(f'  {triple} -> {total} verts  '
                     f'[{breakdown}]\n')
        if len(shared) > 50:
            fh.write(f'  ... ({len(shared) - 50} more)\n')

    print(f'\nwrote: {out_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
