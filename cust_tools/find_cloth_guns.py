"""Scan every tank's gun mesh and flag the ones with cloth/rubber covers.

Detection signal:

  PALETTE LEN >= 3
        ([G_BlendBone, Gun_BlendBone, <flex_anchor>] -- the third
         bone is what cloth / rubber overlays bind their stretching
         end to.  Guns with no overlay typically have just the two
         root/recoil bones.)

  III byte 6 PRESENT in any slot
        (palette idx 2 -> byte 6 in the SC_UBYTE4_REVERSE_PADDED
         convention; presence of byte 6 in the vertex stream
         confirms verts are actually bound to that third bone, not
         just declaring it in the palette).

Cloth vs rubber breakdown is harder -- WoT's authoring uses the
material's `fx` shader name (e.g. `PBS_simple_*` for cloth,
`PBS_*_rubber` for rubber) but the .visual_processed material data
is parsed lazily; for tonight we just flag overlay PRESENCE.  Add a
per-material fx-name probe later if you want the cloth/rubber split.

Output: one line per tank with overlay.  Sorted by nation then tag.
Run from project root:

    python cust_tools/find_cloth_guns.py
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
    """Return (palette_len, palette, byte_set) for this tank's gun
    primary mesh group, or None on any failure."""
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
    # First skinned group is the one we care about (guns are single-
    # group meshes in practice).
    for g in parsed:
        verts = g.get('vertices') or {}
        bi = verts.get('bone_indices')
        if bi is None or getattr(bi, 'size', 0) == 0:
            continue
        gname = g.get('name')
        pal = palette_by_group.get(gname, [])
        bi_arr = np.asarray(bi).reshape(-1, 4)
        byte_set = set(int(b) for b in np.unique(bi_arr).tolist())
        return (len(pal), pal, byte_set)
    return None


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
    print(f'scanning {sum(len(v) for v in by_nation.values())} tanks...')
    print()

    flagged = []
    no_overlay = 0
    failed = 0
    for nation in sorted(by_nation):
        for fname in sorted(by_nation[nation]):
            tag = fname[:-4]  # strip .xml
            internal = (f'scripts/item_defs/vehicles/{nation}/{fname}')
            info = _scan_one_gun(pkg, res_mods, internal)
            if info is None:
                failed += 1
                continue
            pal_len, palette, byte_set = info
            # Flag when the gun palette has a 3rd bone AND the
            # vertex stream actually references it via byte 6.
            has_third_bone = pal_len >= 3
            references_third = 6 in byte_set
            if has_third_bone and references_third:
                flagged.append((nation, tag, palette, byte_set))
            else:
                no_overlay += 1

    print(f'flagged {len(flagged)} tanks with gun overlay '
          f'(cloth/rubber):')
    print(f'  ({no_overlay} guns without overlay, '
          f'{failed} failed to parse)')
    print()
    print(f'{"NATION":<10} {"TAG":<30}  PALETTE                                '
          f'III BYTES')
    print('-' * 100)
    for nation, tag, palette, byte_set in flagged:
        bytes_s = ','.join(str(b) for b in sorted(byte_set))
        print(f'{nation:<10} {tag:<30}  '
              f'{str(palette):<40}  {bytes_s}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
