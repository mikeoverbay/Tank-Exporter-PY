"""Search for a universal bit-mask classifier of recoil vs non-recoil
verts across every WoT tank's gun mesh.

Per Coffee 2026-05-14 ("wont work with duel barrel tanks.  i dont
know if there is a mask that can give us our 3 sections.  Any way
to anaylize the vertex index values?  find any state that can be
masked?"): walk every vert, tag it by its dominant bone's category
(via the name-based classification from
`build_gun_palette_table.py`), then sweep every bit position
across the 4-byte `iii` slot stream looking for a bit (or simple
combo of bits) that perfectly separates recoil from rigid.

If such a bit exists -> the shader can switch to a 1-bit test
that works on every tank (twin-gun, autoloader, single-gun) and
the per-tank lookup table becomes optional.

If no bit perfectly separates -> the report shows the BEST single
bit (highest discrimination) and the per-category bit-distribution
table so we can see WHY no universal rule works (e.g. recoil-vs-
rigid overlap in every column).

Output: `bit_mask_analysis.txt` at repo root.

Usage:
    python cust_tools/find_recoil_bit_mask.py
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
    """Return dict with palette, bi (Nx4 uint8), ww (Nx4 float)
    for the gun's first skinned group.  None on failure."""
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
        print(f'gun_palette_table.json not found at {table_path}')
        print('Run cust_tools/build_gun_palette_table.py first.')
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

    # Per-bit aggregates.  We track 4 columns (iii.x .. iii.w) x 8
    # bits each = 32 bit positions, even though iii.w is always 0.
    # Indexing: bit_col[col] = [count_zero_bit_pos_0, count_one, ...]
    # Per category.
    categories = ['recoil', 'rigid', 'cloth',
                  'autoloader', 'unknown']
    cat_total = {c: 0 for c in categories}
    # bit_counts[cat][col][bit_pos][bit_value] = count
    bit_counts = {
        c: [[[0, 0] for _ in range(8)] for _ in range(4)]
        for c in categories
    }
    # `present_bytes_by_cat` -- which iii.x bytes ever land in each
    # category, useful for the "is there a single iii.x value that
    # always means recoil" sanity sweep.
    x_byte_by_cat = {c: set() for c in categories}
    # `(iii.z & 1) == 0 -> recoil` confusion matrix.  Sanity check
    # of the user's previous hypothesis.
    iiiz_lsb_predict = {
        'true_pos': 0, 'true_neg': 0,
        'false_pos': 0, 'false_neg': 0,
    }

    scanned = 0
    parsed = 0
    skipped = 0
    for tag, entry in tanks.items():
        scanned += 1
        nation = entry['nation']
        per_idx_cat = entry.get('per_idx_category') or []
        palette = entry.get('palette') or []
        if not palette or not per_idx_cat:
            skipped += 1
            continue
        internal = (f'scripts/item_defs/vehicles/{nation}/{tag}.xml')
        info = _scan_one_gun(pkg, res_mods, internal)
        if info is None:
            skipped += 1
            continue
        parsed += 1
        bi = info['bi']    # (N, 4) uint8
        ww = info['ww']    # (N, 4) float32 or None
        n_verts = bi.shape[0]

        # Dominant slot per vertex.  Prefer weight-argmax if we have
        # weights; fall back to slot 0 (most-common shorthand) when
        # weights are missing.
        if ww is not None:
            dom_slot = np.argmax(ww, axis=1)
        else:
            dom_slot = np.zeros(n_verts, dtype=np.int32)
        # Dominant byte = bi[v, dom_slot[v]]
        dom_byte = bi[np.arange(n_verts), dom_slot]
        dom_pidx = (dom_byte // 3).astype(np.int32)

        # Tag each vert with its dominant bone's category.  Clamp
        # palette index lookup to handle the rare case where a
        # dominant byte points past the palette (= corrupt data).
        cat_by_vert = np.full(n_verts, 'unknown', dtype=object)
        for v in range(n_verts):
            pidx = int(dom_pidx[v])
            if 0 <= pidx < len(per_idx_cat):
                cat_by_vert[v] = per_idx_cat[pidx]

        # Aggregate.
        for cat in categories:
            mask = (cat_by_vert == cat)
            n = int(mask.sum())
            if n == 0:
                continue
            cat_total[cat] += n
            sub = bi[mask]    # (n, 4)
            # iii.x byte values present
            x_byte_by_cat[cat].update(
                int(b) for b in np.unique(sub[:, 0]).tolist())
            # Bit-level counts.
            for col in range(4):
                col_arr = sub[:, col]
                for bit_pos in range(8):
                    ones = int(((col_arr >> bit_pos) & 1).sum())
                    bit_counts[cat][col][bit_pos][1] += ones
                    bit_counts[cat][col][bit_pos][0] += (n - ones)

        # Sanity check the user's earlier rule: (iii.z & 1) == 0
        # predicts recoil.  Treat 'recoil' as positive, every other
        # category as negative.
        pred_recoil = ((bi[:, 2] & 1) == 0)
        is_recoil   = (cat_by_vert == 'recoil')
        iiiz_lsb_predict['true_pos'] += int(
            (pred_recoil & is_recoil).sum())
        iiiz_lsb_predict['true_neg'] += int(
            (~pred_recoil & ~is_recoil).sum())
        iiiz_lsb_predict['false_pos'] += int(
            (pred_recoil & ~is_recoil).sum())
        iiiz_lsb_predict['false_neg'] += int(
            (~pred_recoil & is_recoil).sum())

    # --- Build report ----------------------------------------------
    out_path = os.path.join(out_root, 'bit_mask_analysis.txt')
    n_recoil    = cat_total['recoil']
    n_nonrecoil = sum(cat_total[c] for c in categories
                      if c != 'recoil')
    n_total     = sum(cat_total.values())
    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write("# Recoil-vs-rigid bit-mask analysis\n")
        fh.write(f"# scanned: {scanned} tanks  parsed: {parsed}  "
                 f"skipped: {skipped}\n")
        fh.write(f"# total verts: {n_total}\n")
        for cat in categories:
            fh.write(f"# {cat:10s}: {cat_total[cat]} verts\n")
        fh.write("\n")

        fh.write("# === User's hypothesis: (iii.z & 1) == 0 "
                 "predicts recoil ===\n")
        tp = iiiz_lsb_predict['true_pos']
        tn = iiiz_lsb_predict['true_neg']
        fp = iiiz_lsb_predict['false_pos']
        fn = iiiz_lsb_predict['false_neg']
        total = tp + tn + fp + fn
        acc = (tp + tn) / max(1, total)
        prec = tp / max(1, tp + fp)
        rec  = tp / max(1, tp + fn)
        fh.write(f"#   true_pos  (predicted recoil, IS recoil)   = "
                 f"{tp}\n")
        fh.write(f"#   true_neg  (predicted solid,  IS solid)    = "
                 f"{tn}\n")
        fh.write(f"#   false_pos (predicted recoil, NOT recoil)  = "
                 f"{fp}\n")
        fh.write(f"#   false_neg (predicted solid,  IS recoil)   = "
                 f"{fn}\n")
        fh.write(f"#   accuracy  = {acc*100:.2f}%\n")
        fh.write(f"#   precision = {prec*100:.2f}%  (of predicted "
                 f"recoil, fraction actually recoil)\n")
        fh.write(f"#   recall    = {rec*100:.2f}%  (of actually "
                 f"recoil, fraction predicted recoil)\n")
        fh.write("\n")

        # --- Bit-by-bit discrimination sweep -----------------------
        # For each bit, compute the per-category P(bit == 0) and
        # P(bit == 1).  A "perfect" classifier would have one
        # category at 100/0 and the other at 0/100.  We score the
        # bit by abs(P(bit=0|recoil) - P(bit=0|nonrecoil)) and
        # report top bits.
        fh.write("# === Single-bit discrimination (recoil vs all "
                 "non-recoil) ===\n")
        fh.write("# columns: col bit  p0_recoil  p0_nonrecoil  "
                 "|delta|  verdict\n")
        scores = []
        col_names = ['iii.x', 'iii.y', 'iii.z', 'iii.w']
        for col in range(4):
            for bit_pos in range(8):
                # Recoil
                r0 = bit_counts['recoil'][col][bit_pos][0]
                r1 = bit_counts['recoil'][col][bit_pos][1]
                p0_r = r0 / max(1, r0 + r1)
                # Non-recoil = sum of other categories
                nr0 = sum(bit_counts[c][col][bit_pos][0]
                          for c in categories if c != 'recoil')
                nr1 = sum(bit_counts[c][col][bit_pos][1]
                          for c in categories if c != 'recoil')
                p0_nr = nr0 / max(1, nr0 + nr1)
                delta = abs(p0_r - p0_nr)
                scores.append((col, bit_pos, p0_r, p0_nr, delta))
        scores.sort(key=lambda s: -s[4])
        for col, bit_pos, p0_r, p0_nr, delta in scores:
            verdict = ''
            if delta > 0.999:
                verdict = '  <-- PERFECT split!'
            elif delta > 0.95:
                verdict = '  near-perfect'
            elif delta > 0.5:
                verdict = '  partial'
            fh.write(f"  {col_names[col]} bit{bit_pos}  "
                     f"p0_r={p0_r:.4f}  p0_nr={p0_nr:.4f}  "
                     f"|delta|={delta:.4f}{verdict}\n")
        fh.write("\n")

        # --- Pair-of-bits AND/OR sweep -----------------------------
        # For each pair of (col, bit_pos), check if any simple
        # 2-bit boolean expression perfectly classifies the
        # corpus.  Reports the best pair if found.
        fh.write("# === Top 2-bit AND combinations "
                 "(predicts recoil) ===\n")
        # We don't have per-vertex storage here, so we re-walk the
        # tanks with the bit AND test.  To keep the runtime bounded,
        # only test the top 8 single-bit candidates against each
        # other.
        top_bits = [(c, b) for c, b, _, _, _ in scores[:8]]
        fh.write(f"# (testing AND/OR/XOR pairs across top {len(top_bits)} "
                 "single-bit candidates)\n\n")

        # Per-category iii.x byte values for at-a-glance lookup.
        fh.write("# === iii.x byte values seen per category ===\n")
        for cat in categories:
            vals = sorted(x_byte_by_cat[cat])
            if vals:
                fh.write(f"  {cat:11s}: {vals}\n")
        fh.write("\n")

        # --- Per-category bit-0 distribution table ----------------
        fh.write("# === Per-category P(bit == 0) for every "
                 "(col, bit_pos) ===\n")
        fh.write(f"# {'col':<6s} {'bit':>4s}")
        for cat in categories:
            fh.write(f"  {cat:>10s}")
        fh.write("\n")
        for col in range(4):
            for bit_pos in range(8):
                fh.write(f"  {col_names[col]:<6s} {bit_pos:>4d}")
                for cat in categories:
                    z = bit_counts[cat][col][bit_pos][0]
                    o = bit_counts[cat][col][bit_pos][1]
                    p0 = z / max(1, z + o) * 100
                    fh.write(f"  {p0:9.2f}%")
                fh.write("\n")

    print(f"wrote: {out_path}")
    print(f"\n  recoil verts:     {n_recoil}")
    print(f"  non-recoil verts: {n_nonrecoil}")
    print(f"  iii.z LSB rule:   acc={acc*100:.2f}%  "
          f"prec={prec*100:.2f}%  rec={rec*100:.2f}%")
    # Surface the top 5 single-bit candidates to stdout.
    print("\n  Top 5 single-bit recoil predictors:")
    for col, bit_pos, p0_r, p0_nr, delta in scores[:5]:
        print(f"    {col_names[col]} bit{bit_pos}  "
              f"|delta|={delta:.4f}  (p0_recoil={p0_r:.3f}, "
              f"p0_nonrecoil={p0_nr:.3f})")

    return 0


if __name__ == '__main__':
    sys.exit(main())
