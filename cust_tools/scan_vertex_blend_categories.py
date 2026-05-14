"""Search the vertex stream for a 3-way recoil / rigid / stretch
classifier using BOTH the iii palette bytes AND the ww weights.

Per Coffee 2026-05-14 ("search the actual primitive for masks we
have 3 max colors sets.  one is recoil, one is ridged and one is
stretchable"): each vertex has up to 3 effective bone bindings
(iii.x/y/z + matched weights; iii.w is always padding).  This
script classifies each vertex by the CATEGORIES of bones in its
weighted slots:

    recoil_only       -- every slot with weight > THRESH binds to
                          a `recoil` palette bone.
    rigid_only        -- every slot with weight > THRESH binds to
                          a `rigid` (or autoloader) palette bone.
    stretch           -- slots mix recoil AND rigid -- the vert
                          interpolates between a moving and a
                          stationary bone, so it STRETCHES as the
                          barrel recoils.  Classic cloth / rubber
                          drape signature.
    autoloader_only   -- all slots on autoloader bones (rare; tanks
                          like R230 Object 432U).
    unclassified      -- anything else (cross-category mixes
                          besides recoil+rigid).

Uses the gun_palette_table.json bone classifications as ground
truth (so 'recoil' / 'rigid' are name-based, not byte-position
heuristics).

Output: `gun_vertex_blend_scan.txt` at repo root.

Usage:
    python cust_tools/scan_vertex_blend_categories.py
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


# Weight threshold below which a slot doesn't count.  WoT weights
# are stored as uint8 normalised to [0, 1]; 0.1 = 25.5 raw counts.
# Slots with weight under this contribute < 10% of the skin matrix,
# so we treat them as inactive for category-blend purposes.
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
    table_path = os.path.join(out_root, 'gun_palette_table.json')
    if not os.path.isfile(table_path):
        print(f'gun_palette_table.json missing at {table_path}')
        return 1
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

    # Corpus-wide totals.
    corpus = {
        'recoil_only':     0,
        'rigid_only':      0,
        'stretch':         0,    # recoil + rigid mix
        'autoloader_only': 0,
        'cross_mix':       0,    # any other multi-category mix
        'no_active':       0,    # all weights below threshold
    }
    # Per-tank counts for tanks with stretch verts -- so we can
    # see WHICH tanks have a cloth drape and how many verts.
    stretch_tanks = []   # list of (tag, n_stretch, n_total)
    parsed = 0
    for tag, entry in tanks.items():
        nation = entry['nation']
        per_idx_cat = entry.get('per_idx_category') or []
        if not per_idx_cat:
            continue
        internal = f'scripts/item_defs/vehicles/{nation}/{tag}.xml'
        info = _scan_one_gun(pkg, res_mods, internal)
        if info is None:
            continue
        parsed += 1
        bi = info['bi']
        ww = info['ww']
        n = bi.shape[0]
        if ww is None:
            # No weights -> can't measure blend.  Skip.
            continue

        # Per-vertex: for each of the 4 slots, if weight > THRESH,
        # look up the slot's palette bone category.  Build a SET of
        # categories per vert and bucket the vert by its set
        # composition.
        # Vectorised by broadcasting:
        active = ww > WEIGHT_THRESH                  # (n, 4) bool
        pidx   = (bi // 3).astype(np.int32)          # (n, 4) palette idx
        # Map per-slot palette idx to category code.  Codes:
        # 0 = none / unknown, 1 = recoil, 2 = rigid,
        # 3 = autoloader, 4 = cloth, 5 = unknown.
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
        # Look up per slot, mask by active.
        pidx_clip = np.clip(pidx, 0, 63)
        cat_per_slot = cat_code_table[pidx_clip]     # (n, 4)
        cat_per_slot = np.where(active, cat_per_slot, 0)

        # For each vert, build a 5-bit signature of present
        # categories.  Bit 1 = recoil, bit 2 = rigid, bit 3 =
        # autoloader, bit 4 = cloth, bit 5 = unknown.  Bit 0 is
        # the "no active slot" tag.
        sig = np.zeros(n, dtype=np.int32)
        for code, bit in [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)]:
            present = np.any(cat_per_slot == code, axis=1)
            sig |= (present.astype(np.int32) << bit)
        no_active = np.all(cat_per_slot == 0, axis=1)
        sig[no_active] = 1   # bit 0 only

        # Bucket by signature.
        n_stretch = 0
        n_recoil = 0
        n_rigid = 0
        n_auto  = 0
        n_cross = 0
        n_none  = 0
        for v in range(n):
            s = int(sig[v])
            has_recoil = bool(s & (1 << 1))
            has_rigid  = bool(s & (1 << 2))
            has_auto   = bool(s & (1 << 3))
            if s == 1:
                n_none += 1
            elif has_recoil and has_rigid:
                n_stretch += 1
            elif has_recoil and not has_rigid and not has_auto:
                n_recoil += 1
            elif has_rigid and not has_recoil and not has_auto:
                n_rigid += 1
            elif has_auto and not has_recoil and not has_rigid:
                n_auto += 1
            else:
                n_cross += 1

        corpus['recoil_only']     += n_recoil
        corpus['rigid_only']      += n_rigid
        corpus['stretch']         += n_stretch
        corpus['autoloader_only'] += n_auto
        corpus['cross_mix']       += n_cross
        corpus['no_active']       += n_none
        if n_stretch > 0:
            stretch_tanks.append((tag, n_stretch, n, n_recoil,
                                   n_rigid))

    n_total = sum(corpus.values())
    print(f"parsed {parsed} tanks, {n_total} verts total")
    print(f"  recoil_only:     {corpus['recoil_only']:>9d}  "
          f"({corpus['recoil_only']/max(1,n_total)*100:.2f}%)")
    print(f"  rigid_only:      {corpus['rigid_only']:>9d}  "
          f"({corpus['rigid_only']/max(1,n_total)*100:.2f}%)")
    print(f"  stretch:         {corpus['stretch']:>9d}  "
          f"({corpus['stretch']/max(1,n_total)*100:.2f}%)")
    print(f"  autoloader_only: {corpus['autoloader_only']:>9d}  "
          f"({corpus['autoloader_only']/max(1,n_total)*100:.2f}%)")
    print(f"  cross_mix:       {corpus['cross_mix']:>9d}  "
          f"({corpus['cross_mix']/max(1,n_total)*100:.2f}%)")
    print(f"  no_active:       {corpus['no_active']:>9d}  "
          f"({corpus['no_active']/max(1,n_total)*100:.2f}%)")
    print(f"\n  tanks with stretch verts: {len(stretch_tanks)}")

    out_path = os.path.join(out_root, 'gun_vertex_blend_scan.txt')
    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write('# Per-vertex blend-category scan\n')
        fh.write(f'# parsed {parsed} tanks, {n_total} verts\n')
        fh.write(f'# weight threshold: {WEIGHT_THRESH}\n')
        fh.write('=' * 78 + '\n\n')
        fh.write('# === Corpus-wide vertex category totals ===\n')
        for cat in ('recoil_only', 'rigid_only', 'stretch',
                    'autoloader_only', 'cross_mix', 'no_active'):
            v = corpus[cat]
            fh.write(f'  {cat:18s}: {v:>9d}  '
                     f'({v/max(1,n_total)*100:.2f}%)\n')
        fh.write('\n')
        fh.write(f'# === Tanks with stretch verts '
                 f'({len(stretch_tanks)}) ===\n')
        stretch_tanks.sort(key=lambda x: -x[1])
        for tag, n_s, n_t, n_r, n_rg in stretch_tanks[:200]:
            fh.write(f'  {tag:40s}  stretch={n_s:>5d} / total='
                     f'{n_t:>5d}  '
                     f'({n_s/max(1,n_t)*100:5.1f}%)  '
                     f'recoil_only={n_r}  rigid_only={n_rg}\n')
        if len(stretch_tanks) > 200:
            fh.write(f'  ... ({len(stretch_tanks) - 200} more)\n')

    print(f'\nwrote: {out_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
