"""Build a per-tank gun-bone conversion table.

Per Coffee 2026-05-14 ("make a conversion table.  we ask for gun
colors, table gives them.  we want rigid, same and for
cloth/rubber"): the recoil classifier we built into the shader
(`iii.x not in {0, 6}`) fails on tanks like GB147 whose palette
ships 17 bones with WoT-specific naming.  The right answer is a
LOOKUP TABLE keyed by tank tag -- the runtime asks "what palette
indices recoil for this tank?" and the table answers.

This script walks every tank XML the PkgExtractor knows about,
loads the gun mesh's renderSet bone palette + iii bytes present,
classifies each palette index by BONE NAME PATTERN (see
`classify_bone` below), and writes two files at repo root:

  * `gun_palette_table.json` -- machine-readable; the runtime
    consumer keys into it by `<tag>` (= XML basename without
    `.xml`) and reads back the per-category byte lists.
  * `gun_palette_table.txt`  -- human-readable; for each tank the
    palette + categorisation + actually-present iii bytes.

Categories:
  recoil      = bone moves with the recoiling barrel
  rigid       = bone is fixed to the gun mount / parent
  cloth       = bone is the anchor for a cloth / rubber overlay
  autoloader  = bone is an animated mechanism (loader, cover,
                ejector, etc.) -- NOT recoil, has its own anim
                curve in WoT's engine; for our viewer this means
                "don't apply recoil" (we'd need real bone anim
                support to drive these correctly).
  unknown     = name didn't match any rule -- we treat it as
                rigid by default but flag it so the user can
                update the rules if a new convention shows up.

Usage:
    python cust_tools/build_gun_palette_table.py
"""
from __future__ import annotations

import os
import sys
import json
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

import numpy as np

from tankExporterPy.loaders import (
    PkgExtractor, VisualLoader, MeshParser, VehicleXMLLoader)


# ---------------------------------------------------------------------------
# Bone-name classifier
# ---------------------------------------------------------------------------

# Exact matches that always mean "this bone recoils".  Per Coffee
# 2026-05-14 (spatial analysis on Tiger I gun mesh):
#
#   `G_BlendBone`   spans Z=[-5.44, -1.43] (= the BARREL).
#   `Gun_BlendBone` spans Z=[-1.43, -0.01] (= the MANTLE / mount).
#
# So WoT's naming convention is the OPPOSITE of intuition:
#   `G_*`   = the gun itself (the recoiling part)
#   `Gun_*` = the gun assembly (= mount / parent, rigid)
#
# Twin-gun tanks (GB147) name their two barrels `G_R_BlendBone`
# and `G_L_BlendBone` -- consistent with the `G_*` = barrel rule.
# Comparisons are case-insensitive (lowered).
RECOIL_EXACT = {
    'g_blendbone',     # single-barrel gun
    'g_r_blendbone',   # twin-gun: right barrel
    'g_l_blendbone',   # twin-gun: left barrel
}

# Exact matches that always mean "rigid mount / parent".  Per the
# convention above, `Gun_BlendBone` is the assembly mount, never
# the recoiling barrel.
RIGID_PARENT_EXACT = {
    'gun_blendbone',
}

# Substring keywords that mean "rigid / static mount".  Catches
# `static_BlendBone`, `Static_BlendBone`, `Static_T2_BlendBone`,
# `Static_Joint_BlendBone`, `joint*_BlendBone`, `Join_*` (typo of
# `joint`), and `statik_*` (German spelling on F131_Brennos)
# -- all corpus-walk discoveries.
RIGID_KEYWORDS = ('static', 'joint', 'statik', 'join_')

# Substring rules (case-insensitive).  Order matters: first match
# wins, with `cloth` checked before `autoloader` so a bone called
# `G_Cover_Cloth_BlendBone` (hypothetical) lands in cloth.
CLOTH_KEYWORDS = ('cloth', 'fabric', 'rubber')

# Autoloader / mechanism words.  These bones have their own
# WoT-authored animation curves; for our viewer we class them as
# "non-recoil" (the runtime currently has no bone-animation
# support so they just stay at bind pose).  `rotate` / `move` /
# `cap_` cover the R230 Object 432U sub-cap animations.
AUTOLOADER_KEYWORDS = (
    'cover', 'close', 'pusher', 'ejector',
    'breech', 'loader', 'mag', 'feed',
    'rotate', 'move', 'cap_',
    # spring + valve from A195_Gorilla (mechanism on a recoiling
    # assembly).
    'spring', 'valve',
)


def classify_bone(name, has_gun_blendbone):
    """Return the category for a single palette bone.

    `has_gun_blendbone` is True iff the palette contains
    `Gun_BlendBone`.  When it's True, a plain `G_BlendBone` is
    the PARENT gun root (rigid -- the barrel lives at
    `Gun_BlendBone`).  When False, `G_BlendBone` is itself the
    barrel (single-bone gun palette).  This is the one
    context-dependent rule in the classifier; everything else is
    keyed off the name alone.
    """
    lname = name.lower()
    if lname in RECOIL_EXACT:
        return 'recoil'
    if lname in RIGID_PARENT_EXACT:
        return 'rigid'
    # `G_L_S1_BlendBone` / `G_R_S1_BlendBone` / `G_S2_BlendBone`
    # -- sub-barrel "stage" bones (multi-section recoiling barrel).
    # Caught on GB142 Canopener; likely shows up on other British
    # designs.  These ride the barrel so they recoil.
    if re.fullmatch(r'g(_[rl])?_s\d+_blendbone', lname):
        return 'recoil'
    if any(k in lname for k in CLOTH_KEYWORDS):
        return 'cloth'
    if any(k in lname for k in AUTOLOADER_KEYWORDS):
        return 'autoloader'
    # Generic patterns that hint at a directional mechanism --
    # `G_Up_*`, `G_Down_*`, `G_Left_*`, `G_Right_*` on GB147.
    # We treat these as autoloader as well.
    if re.search(
            r'_(up|down|left|right|in|out)(_|$)',
            lname):
        return 'autoloader'
    # `static_*` / `joint*` -- rigid mounts.  Checked AFTER cloth
    # and autoloader so a hypothetical `static_cloth_BlendBone`
    # would still land in cloth.
    if any(k in lname for k in RIGID_KEYWORDS):
        return 'rigid'
    return 'unknown'


def classify_palette(palette_names):
    """Run `classify_bone` over the palette in order and return:
        - per_idx:    list of category strings, parallel to palette
        - by_category: dict category -> [byte_values]
    """
    has_gun = any(n == 'Gun_BlendBone' for n in palette_names)
    per_idx = [
        classify_bone(n, has_gun) for n in palette_names
    ]
    by_category = {
        'recoil': [], 'rigid': [], 'cloth': [],
        'autoloader': [], 'unknown': [],
    }
    for i, cat in enumerate(per_idx):
        by_category[cat].append(i * 3)   # byte = palette_idx * 3
    return per_idx, by_category


# ---------------------------------------------------------------------------
# Per-tank scan (same recipe as scan_gun_iii.py, condensed)
# ---------------------------------------------------------------------------

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
    """Returns dict {palette, present_bytes} or None on failure."""
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
        if bi is None or getattr(bi, 'size', 0) == 0:
            continue
        gname = g.get('name')
        pal = palette_by_group.get(gname, [])
        bi_arr = np.asarray(bi).reshape(-1, 4).astype(np.int32)
        present_bytes = sorted(set(
            int(b) for b in np.unique(bi_arr).tolist()))
        return {
            'palette':       list(pal),
            'present_bytes': present_bytes,
        }
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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
    total = sum(len(v) for v in by_nation.values())
    print(f'scanning {total} tanks for gun palettes...')

    # tank_tag -> classification dict
    table = {}
    # Aggregates for the summary report
    unknown_bones = {}     # bone_name -> set of tank_tags using it
    cat_counts = {
        'recoil': 0, 'rigid': 0, 'cloth': 0,
        'autoloader': 0, 'unknown': 0,
    }
    scanned = 0
    parsed_ok = 0
    for nation in sorted(by_nation):
        for fname in sorted(by_nation[nation]):
            scanned += 1
            tag = fname[:-4]
            internal = (f'scripts/item_defs/vehicles/{nation}/'
                        f'{fname}')
            info = _scan_one_gun(pkg, res_mods, internal)
            if info is None:
                continue
            parsed_ok += 1
            pal      = info['palette']
            present  = info['present_bytes']
            per_idx, by_cat = classify_palette(pal)
            for i, n in enumerate(pal):
                cat_counts[per_idx[i]] += 1
                if per_idx[i] == 'unknown':
                    unknown_bones.setdefault(
                        n, set()).add(tag)
            # Trim each category's byte list to ONLY those values
            # actually present in the vertex stream -- a tank might
            # declare 17 bones in the palette but only reference 8
            # of them; reporting non-present bytes inflates the
            # mask uselessly.
            present_set = set(present)
            entry = {
                'nation': nation,
                'palette': pal,
                'present_bytes': present,
                'recoil':     [b for b in by_cat['recoil']
                               if b in present_set],
                'rigid':      [b for b in by_cat['rigid']
                               if b in present_set],
                'cloth':      [b for b in by_cat['cloth']
                               if b in present_set],
                'autoloader': [b for b in by_cat['autoloader']
                               if b in present_set],
                'unknown':    [b for b in by_cat['unknown']
                               if b in present_set],
            }
            # Keep the FULL per-index categorisation alongside for
            # callers that want palette-idx granularity (e.g.
            # building a bitmask).
            entry['per_idx_category'] = per_idx
            table[tag] = entry

    print(f'parsed {parsed_ok} / {scanned} tank XMLs')
    print(f'palette-bone category totals: {cat_counts}')
    if unknown_bones:
        print(f'  unknown bone names ({len(unknown_bones)} '
              f'distinct):')
        for nm in sorted(unknown_bones)[:40]:
            tanks = sorted(unknown_bones[nm])
            print(f'    {nm:40s}  ({len(tanks)} tank(s), e.g. '
                  f'{tanks[0]})')
        if len(unknown_bones) > 40:
            print(f'    ... ({len(unknown_bones) - 40} more)')

    # --- Save JSON --------------------------------------------------
    out_root = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..'))
    json_path = os.path.join(out_root, 'gun_palette_table.json')
    out = {
        '_meta': {
            'description': (
                'Per-tank gun-bone classification table.  Keyed by '
                'tank XML basename (without .xml).  Each entry maps '
                'category -> list of iii.x byte values (= palette_idx '
                '* 3) for that category, FILTERED to bytes actually '
                'present in the gun mesh.  Categories: recoil, '
                'rigid, cloth, autoloader, unknown.  See '
                'cust_tools/build_gun_palette_table.py for the '
                'classification rules.'),
            'category_default_action': {
                'recoil':     'apply u_gun_recoil_translation',
                'rigid':      'leave at bind pose (skin via palette)',
                'cloth':      'leave at bind pose',
                'autoloader': 'leave at bind pose (no anim support)',
                'unknown':    'leave at bind pose; report to console',
            },
            'tank_count': len(table),
        },
        'default': {
            'recoil':     [3],
            'rigid':      [0, 6],
            'cloth':      [],
            'autoloader': [],
            'unknown':    [],
        },
        'tanks': table,
    }
    with open(json_path, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, indent=2, sort_keys=False)
    print(f'wrote: {json_path}')

    # --- Save human-readable report ---------------------------------
    txt_path = os.path.join(out_root, 'gun_palette_table.txt')
    with open(txt_path, 'w', encoding='utf-8') as fh:
        fh.write('# Gun-palette classification table\n')
        fh.write(f'# {len(table)} tanks (parsed) of {scanned} '
                 f'scanned\n')
        fh.write('# Generated by '
                 'cust_tools/build_gun_palette_table.py\n')
        fh.write('# Categories: recoil, rigid, cloth, autoloader, '
                 'unknown\n')
        fh.write('=' * 78 + '\n\n')
        for tag in sorted(table):
            e = table[tag]
            fh.write(f'=== [{e["nation"]}] {tag} ===\n')
            fh.write(f'  palette ({len(e["palette"])}):\n')
            for i, n in enumerate(e['palette']):
                fh.write(f'    [{i}] byte={i*3:3d}  '
                         f'cat={e["per_idx_category"][i]:11s}  '
                         f'{n}\n')
            fh.write(f'  present bytes: {e["present_bytes"]}\n')
            for cat in ('recoil', 'rigid', 'cloth',
                        'autoloader', 'unknown'):
                if e[cat]:
                    fh.write(f'  {cat:11s}: {e[cat]}\n')
            fh.write('\n')
        if unknown_bones:
            fh.write('# UNKNOWN bone names found across the corpus '
                     '(add to RECOIL_EXACT / RIGID_EXACT / '
                     'AUTOLOADER_KEYWORDS as needed):\n')
            for nm in sorted(unknown_bones):
                tanks = sorted(unknown_bones[nm])
                fh.write(f'#   {nm:40s}  ({len(tanks)} tank(s)) '
                         f'e.g. {tanks[0]}\n')
    print(f'wrote: {txt_path}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
