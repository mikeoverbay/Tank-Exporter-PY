"""Brute-force search for the best 2-bit AND combination that
predicts recoil-vs-rigid across the WoT corpus.  Continuation of
`find_recoil_bit_mask.py`: that tool showed iii.x bit1 is the best
SINGLE bit (89% accuracy).  This one explores whether two bits
ANDed together do better.

Loads gun_palette_table.json + walks every gun mesh once, builds
per-vertex (cat, iii.x, iii.y, iii.z) triples, then enumerates
all (col_a, bit_a, col_b, bit_b) pairs across 24 useful bit
positions (32 minus the always-zero iii.w slot).  For each pair,
computes the four rules:

    A == 0           AND  B == 0      -> recoil
    A == 0           AND  B == 1      -> recoil
    A == 1           AND  B == 0      -> recoil
    A == 1           AND  B == 1      -> recoil

Picks the rule with the highest balanced accuracy.

Output: `bit_combo_analysis.txt` at repo root.  Reports the top
10 rules with confusion matrices.

Usage:
    python cust_tools/find_recoil_bit_combo.py
"""
from __future__ import annotations

import os
import sys
import json
from itertools import combinations_with_replacement

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

    # Pass 1: gather all (cat_is_recoil, iii.x, iii.y, iii.z) into
    # numpy arrays.  iii.w is omitted (always 0).
    is_recoil_chunks = []
    iiix_chunks = []
    iiiy_chunks = []
    iiiz_chunks = []
    parsed = 0
    for tag, entry in tanks.items():
        nation = entry['nation']
        per_idx_cat = entry.get('per_idx_category') or []
        palette = entry.get('palette') or []
        if not palette or not per_idx_cat:
            continue
        internal = f'scripts/item_defs/vehicles/{nation}/{tag}.xml'
        info = _scan_one_gun(pkg, res_mods, internal)
        if info is None:
            continue
        parsed += 1
        bi = info['bi']
        ww = info['ww']
        n = bi.shape[0]
        if ww is not None:
            dom_slot = np.argmax(ww, axis=1)
        else:
            dom_slot = np.zeros(n, dtype=np.int32)
        dom_byte = bi[np.arange(n), dom_slot]
        dom_pidx = (dom_byte // 3).astype(np.int32)
        is_rec  = np.zeros(n, dtype=bool)
        for v in range(n):
            pidx = int(dom_pidx[v])
            if 0 <= pidx < len(per_idx_cat) and per_idx_cat[pidx] == 'recoil':
                is_rec[v] = True
        is_recoil_chunks.append(is_rec)
        iiix_chunks.append(bi[:, 0])
        iiiy_chunks.append(bi[:, 1])
        iiiz_chunks.append(bi[:, 2])

    is_recoil = np.concatenate(is_recoil_chunks)
    iiix      = np.concatenate(iiix_chunks)
    iiiy      = np.concatenate(iiiy_chunks)
    iiiz      = np.concatenate(iiiz_chunks)
    n_total = is_recoil.size
    n_rec   = int(is_recoil.sum())
    n_nrc   = n_total - n_rec
    print(f"parsed {parsed} tanks, {n_total} verts "
          f"(recoil={n_rec}, non-recoil={n_nrc})")

    # Pre-extract bit arrays so the inner loop is a few ANDs.
    cols = [iiix, iiiy, iiiz]
    col_names = ['iii.x', 'iii.y', 'iii.z']
    bit_arrays = []     # list of (col_name, bit_pos, bool_array)
    for ci, col in enumerate(cols):
        for bp in range(8):
            bits = ((col >> bp) & 1).astype(bool)
            bit_arrays.append((col_names[ci], bp, bits))

    # Single-bit baseline (matches the earlier tool, sanity check).
    print("\nSingle-bit baseline:")
    best_single = (-1.0, '', '')
    for name, bp, bits in bit_arrays:
        for predict in (bits, ~bits):     # b == 1 or b == 0
            tp = int((predict & is_recoil).sum())
            tn = int((~predict & ~is_recoil).sum())
            acc = (tp + tn) / n_total
            if acc > best_single[0]:
                rule_str = f'{name} bit{bp} == ' + (
                    '0' if predict is bits else '1')
                # ↑ predict==bits means rule "bit==1"; rule on the
                # other branch is "bit==0".  Build the string
                # correctly:
                if predict is bits:
                    rule_str = f'{name} bit{bp} == 1'
                else:
                    rule_str = f'{name} bit{bp} == 0'
                best_single = (acc, rule_str, '')

    print(f"  best: {best_single[1]}  acc={best_single[0]*100:.2f}%")

    # 2-bit search.  4 ops per pair: each bit can be inverted (==0 or
    # ==1), combined with AND.  That gives 4 polarity options.
    # Total tests: 24 * 24 * 4 / 2 (unordered) = 1152.  Cheap.
    print("\n2-bit AND search (24*24*4 = 2304 rules)...")
    results = []
    for i in range(len(bit_arrays)):
        n_i, b_i, bits_i = bit_arrays[i]
        for j in range(i, len(bit_arrays)):
            n_j, b_j, bits_j = bit_arrays[j]
            if i == j:
                continue
            for pol_i in (False, True):     # False = bit==0
                for pol_j in (False, True):
                    a = bits_i if pol_i else ~bits_i
                    b = bits_j if pol_j else ~bits_j
                    predict = a & b
                    tp = int((predict & is_recoil).sum())
                    tn = int((~predict & ~is_recoil).sum())
                    acc = (tp + tn) / n_total
                    rule = (
                        f'({n_i} bit{b_i} == {1 if pol_i else 0}) '
                        f'AND ({n_j} bit{b_j} == {1 if pol_j else 0})'
                    )
                    fp = int((predict & ~is_recoil).sum())
                    fn = int((~predict & is_recoil).sum())
                    results.append((acc, rule, tp, tn, fp, fn))

    results.sort(key=lambda r: -r[0])

    out_path = os.path.join(out_root, 'bit_combo_analysis.txt')
    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write("# 2-bit AND combination analysis (recoil predictor)\n")
        fh.write(f"# parsed {parsed} tanks, {n_total} verts "
                 f"(recoil={n_rec}, non-recoil={n_nrc})\n\n")
        fh.write(f"# Single-bit baseline: "
                 f"{best_single[1]}  acc={best_single[0]*100:.2f}%\n\n")
        fh.write("# Top 25 two-bit AND rules:\n")
        fh.write(f"# {'acc':>7} | {'rule':<60} | "
                 f"{'TP':>6} {'TN':>6} {'FP':>6} {'FN':>6}\n")
        for acc, rule, tp, tn, fp, fn in results[:25]:
            fh.write(f"  {acc*100:6.2f}% | {rule:<60} | "
                     f"{tp:6d} {tn:6d} {fp:6d} {fn:6d}\n")
        fh.write("\n")
        # XOR sweep too, for completeness -- different from AND.
        fh.write("# Top 10 XOR rules:\n")
        xor_results = []
        for i in range(len(bit_arrays)):
            n_i, b_i, bits_i = bit_arrays[i]
            for j in range(i+1, len(bit_arrays)):
                n_j, b_j, bits_j = bit_arrays[j]
                predict = bits_i ^ bits_j
                for pol in (False, True):
                    p = predict if pol else ~predict
                    tp = int((p & is_recoil).sum())
                    tn = int((~p & ~is_recoil).sum())
                    acc = (tp + tn) / n_total
                    rule = (
                        f'({n_i} bit{b_i} XOR {n_j} bit{b_j}) == '
                        f'{1 if pol else 0}'
                    )
                    xor_results.append((acc, rule))
        xor_results.sort(key=lambda r: -r[0])
        for acc, rule in xor_results[:10]:
            fh.write(f"  {acc*100:6.2f}% | {rule}\n")
    print(f"\nwrote: {out_path}")
    print("\nTop 5 two-bit AND rules:")
    for acc, rule, tp, tn, fp, fn in results[:5]:
        print(f"  {acc*100:6.2f}%  {rule}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
