"""Headless side-by-side dump of gun data for two tanks.

Loads the gun primitives + visual file for each tank straight out of
the pkgs without spinning up the GUI, then prints the eight ingredients
the recoil pipeline needs to compare what's actually different between
a working tank and a non-working one.

Run from project root:
    python cust_tools/diff_gun_data.py G78 A38
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
    """Read pkg_dir + lookup_xml from the user's TEPY config."""
    candidates = [
        os.path.expanduser('~/.tankExporterPy.json'),
        os.path.join(os.getcwd(), 'tankExporterPy.json'),
        os.path.join(os.path.dirname(__file__),
                      '..', 'tankExporterPy.json'),
    ]
    for p in candidates:
        if os.path.isfile(p):
            with open(p, 'r') as f:
                return json.load(f), p
    return {}, None


def _find_tank_xml(pkg, tag):
    """Use PkgExtractor.list_vehicle_xmls() to find the tank whose
    filename starts with `tag_`.
    """
    tag_low = tag.lower()
    by_nation = pkg.list_vehicle_xmls()
    for nation, xmls in by_nation.items():
        for x in xmls:
            xl = x.lower()
            if xl.startswith(tag_low + '_') or xl == tag_low + '.xml':
                path = (f'scripts/item_defs/vehicles/{nation}/{x}')
                return path, nation
    return None, None


def _dump_one(tag, pkg, res_mods):
    print()
    print('=' * 70)
    print(f'  {tag}')
    print('=' * 70)

    xml_path, nation = _find_tank_xml(pkg, tag)
    if not xml_path:
        print(f'  -- XML not found for {tag}')
        return
    print(f'  vehicle XML: {xml_path}  nation={nation}')

    # Parse top-level info for shells + pitch limits.  pkg.extract
    # returns a LOCAL path to the extracted file -- pass that path
    # directly.
    local_xml = pkg.extract(xml_path)
    if not local_xml or not os.path.isfile(local_xml):
        print(f'  -- could not extract {xml_path}')
        return
    info = VehicleXMLLoader.parse_info(local_xml, pkg)

    gun = info.get('gun') or {}
    print(f'  gun XML scalars:')
    for k in ('userString', 'minPitch', 'maxPitch',
              'rotationSpeed', 'reloadTime'):
        v = gun.get(k)
        if v is not None:
            print(f'    {k!r:>18} = {v!r}')

    turret = info.get('turret') or {}
    print(f'  turret XML scalars:')
    for k in ('userString', 'rotationSpeed'):
        v = turret.get(k)
        if v is not None:
            print(f'    {k!r:>18} = {v!r}')

    # Find the gun's component path.
    components = VehicleXMLLoader.parse(
        local_xml, res_mods_root=res_mods, pkg_extractor=pkg)

    gun_comp = None
    for c in components:
        if c.get('label') == 'gun':
            gun_comp = c
            break
    if gun_comp is None:
        print('  -- no gun component found in components list')
        return

    prim_local = gun_comp.get('primitives')
    vis_local  = gun_comp.get('visual')
    print(f'  gun primitives: {prim_local!r}')
    print(f'  gun visual:     {vis_local!r}')

    palette_by_group = {}
    if vis_local and os.path.isfile(vis_local):
        palette_by_group = (
            VisualLoader.parse_renderset_bones(vis_local))

    print(f'  renderSet bone palette by group:')
    if not palette_by_group:
        print(f'    (empty -- no renderSet/node entries)')
    for g, bones in palette_by_group.items():
        print(f'    {g!r}: {bones}')

    if prim_local and os.path.isfile(prim_local):
        parsed = MeshParser.parse_primitives_processed(prim_local)
    else:
        parsed = []

    for g in parsed:
        gname = g.get('name')
        pos = g.get('positions')
        bi  = g.get('bone_indices')
        bw  = g.get('bone_weights')
        fmt = g.get('format', '')
        n_verts = 0 if pos is None else len(pos)
        has_iii = bi is not None and (
            getattr(bi, 'size', len(bi) if bi is not None else 0)
            > 0)
        has_ww  = bw is not None and (
            getattr(bw, 'size', len(bw) if bw is not None else 0)
            > 0)
        pal = palette_by_group.get(gname, [])

        print(f'  group {gname!r}:')
        print(f'    format    = {fmt!r}')
        print(f'    verts     = {n_verts}')
        print(f'    palette   = {pal}  (len={len(pal)})')
        print(f'    iii/ww    = '
              f'{"YES" if has_iii and has_ww else "NO"}')

        if has_iii and has_ww and n_verts > 0:
            bi_arr = np.asarray(bi).reshape(-1, 4)
            bw_arr = np.asarray(bw).reshape(-1, 4)
            for slot in range(4):
                u, c = np.unique(bi_arr[:, slot],
                                  return_counts=True)
                if len(u) <= 8:
                    counts = ', '.join(
                        f'{int(b)}x{int(n)}'
                        for b, n in zip(u, c))
                else:
                    counts = f'{len(u)} unique values'
                print(f'    iii[{slot}]  = {{{counts}}}')
            rg_key = (bi_arr[:, 0].astype(int) * 1000
                      + bi_arr[:, 1].astype(int))
            u, c = np.unique(rg_key, return_counts=True)
            pairs = ', '.join(
                f'(R={int(v)//1000},G={int(v)%1000})x{int(n)}'
                for v, n in zip(u, c))
            print(f'    (R,G)     = {pairs}')
            for sample_i in (0, n_verts // 2, n_verts - 1):
                ii = bi_arr[sample_i].tolist()
                ww = bw_arr[sample_i].tolist()
                print(f'    v[{sample_i}] iii={ii} ww={ww}')


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    cfg, cfg_path = _load_cfg()
    pkg_dir = cfg.get('pkg_dir')
    if not pkg_dir:
        pkg_dir = r'C:\Games\World_of_Tanks_NA\res\packages'
    # PkgExtractor expects the WoT ROOT (parent of `res\packages`).
    wot_root = pkg_dir
    if wot_root.replace('\\', '/').rstrip('/').lower().endswith(
            'res/packages'):
        wot_root = os.path.dirname(os.path.dirname(wot_root))
    res_mods = cfg.get('res_mods', '')
    print(f'config: {cfg_path}')
    print(f'wot_root: {wot_root}')
    pkg = PkgExtractor(wot_root)
    for tag in sys.argv[1:]:
        _dump_one(tag, pkg, res_mods)
    return 0


if __name__ == '__main__':
    sys.exit(main())
