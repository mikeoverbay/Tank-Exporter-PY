"""Scan every WoT gun mesh's MATERIAL fx-shader names to find the
cloth / rubber-specific shaders.

Per Coffee 2026-05-14 ("find mask for cloth?"): no bones in the
corpus are literally named cloth/fabric/rubber (confirmed by
`build_gun_palette_table.py` -- 0 cloth-bone matches across 1,185
tanks).  WoT expresses cloth at the MATERIAL level: a gun sub-mesh
has its own material whose `<fx>` element points to a shader like
`PBS_simple_alpha_cloth` / `PBS_simple_rubber`.  This script walks
the materials of every tank's gun visual file and reports:

  * Every distinct fx shader name seen on a gun material.
  * The set of tanks using each shader.
  * The sub-mesh index (= primitive group range) for any
    cloth / rubber match.

Output: `gun_material_fx_scan.txt` at repo root.

Usage:
    python cust_tools/scan_gun_materials.py
"""
from __future__ import annotations

import os
import sys
import json
from collections import defaultdict
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from tankExporterPy.loaders import (
    PkgExtractor, VisualLoader, VehicleXMLLoader)


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


# Keywords in fx shader names that indicate cloth / rubber.
CLOTH_FX_KEYWORDS = ('cloth', 'fabric', 'flex')
RUBBER_FX_KEYWORDS = ('rubber',)


def _gun_materials(pkg, res_mods, xml_internal_path):
    """Return list of (group_name, sub_mesh_idx, fx_name) tuples
    for the gun's renderSet, or [] on failure.  Walks the
    .visual_processed XML directly for each material's <fx> text
    -- VisualLoader.parse_textures already has the recipe.
    """
    local_xml = pkg.extract(xml_internal_path)
    if not local_xml or not os.path.isfile(local_xml):
        return []
    try:
        components = VehicleXMLLoader.parse(
            local_xml, res_mods_root=res_mods, pkg_extractor=pkg)
    except Exception:
        return []
    gun_comp = next(
        (c for c in components if c.get('label') == 'gun'), None)
    if gun_comp is None:
        return []
    vis_local = gun_comp.get('visual')
    if not (vis_local and os.path.isfile(vis_local)):
        return []
    # Use VisualLoader.parse_textures for the structured material
    # walk -- it returns {group_name: [material_dict, ...]} with
    # each material dict carrying its parsed fields including
    # `fx` when present.
    try:
        # The parse_textures call wants a list of group names; we
        # don't know them in advance, so first peek at the XML root
        # for the renderSet groups.  Cheap: VisualLoader exposes
        # _read_visual_nodes which parses the visual file just once.
        from tankExporterPy.loaders import MeshParser
        # Quickest path: parse the visual XML and pull every material
        # element ourselves.  fx is consistently under
        # `<material>/<fx>` per the BigWorld convention.
        xml_root = VehicleXMLLoader._read_visual_nodes(vis_local)
        if xml_root is None:
            return []
    except Exception:
        return []

    out = []
    # Each `<renderSet>` element wraps a primitive group + its
    # material list.  Within renderSet, the geometry side is
    # `<geometry><primitiveGroup>` and the material side is
    # `<geometry><primitiveGroup><material>` (one per submesh).
    for rs in xml_root.iter('renderSet'):
        # Group name lives at `<treatAsWorldSpaceObject>` adjacent
        # tag, but the geometry's `<vertices>` element carries it.
        geom = rs.find('geometry')
        if geom is None:
            continue
        prim_groups = geom.findall('primitiveGroup')
        for sub_idx, pg in enumerate(prim_groups):
            # Each <primitiveGroup> has a `<material>` block.
            mat = pg.find('material')
            if mat is None:
                continue
            # `<identifier>` is the material name; `<fx>` is the
            # shader path (relative to res/, e.g.
            # `shaders/std_effects/PBS_simple_alpha.fx`).
            ident = (mat.findtext('identifier') or '').strip()
            fx    = (mat.findtext('fx')         or '').strip()
            out.append((str(sub_idx), ident, fx))
    return out


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
    pkg = PkgExtractor(wot_root)

    by_nation = pkg.list_vehicle_xmls()
    total = sum(len(v) for v in by_nation.values())
    print(f'scanning {total} tanks for gun materials...')

    # fx -> set of tags
    fx_to_tanks = defaultdict(set)
    # Per-tank list of (sub_idx, mat_ident, fx)
    tank_mats   = {}
    scanned = 0
    with_mat = 0
    for nation in sorted(by_nation):
        for fname in sorted(by_nation[nation]):
            scanned += 1
            tag = fname[:-4]
            internal = (f'scripts/item_defs/vehicles/{nation}/'
                        f'{fname}')
            mats = _gun_materials(pkg, res_mods, internal)
            if not mats:
                continue
            with_mat += 1
            tank_mats[tag] = (nation, mats)
            for sub_idx, ident, fx in mats:
                if fx:
                    fx_to_tanks[fx].add(tag)

    # Classify each fx by keyword
    cloth_fx  = []
    rubber_fx = []
    other_fx  = []
    for fx in sorted(fx_to_tanks):
        lower = fx.lower()
        if any(k in lower for k in CLOTH_FX_KEYWORDS):
            cloth_fx.append(fx)
        elif any(k in lower for k in RUBBER_FX_KEYWORDS):
            rubber_fx.append(fx)
        else:
            other_fx.append(fx)

    # Build per-tank cloth/rubber submesh list
    cloth_tanks = {}    # tag -> [(sub_idx, mat_ident, fx)]
    for tag, (nation, mats) in tank_mats.items():
        hits = []
        for sub_idx, ident, fx in mats:
            lower = fx.lower()
            if any(k in lower for k in CLOTH_FX_KEYWORDS):
                hits.append(('cloth', sub_idx, ident, fx))
            elif any(k in lower for k in RUBBER_FX_KEYWORDS):
                hits.append(('rubber', sub_idx, ident, fx))
        if hits:
            cloth_tanks[tag] = (nation, hits)

    # --- Save report ----------------------------------------------
    out_root = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..'))
    out_path = os.path.join(out_root, 'gun_material_fx_scan.txt')
    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write('# Gun-material fx-shader scan\n')
        fh.write(f'# scanned {scanned} tanks, {with_mat} had '
                 f'parseable gun materials\n')
        fh.write(f'# distinct fx shaders: {len(fx_to_tanks)}\n')
        fh.write('=' * 78 + '\n\n')

        fh.write(f'# CLOTH-keyword fx shaders ({len(cloth_fx)}):\n')
        for fx in cloth_fx:
            fh.write(f'  {fx}\n')
            fh.write(f'    used by {len(fx_to_tanks[fx])} tank(s)\n')
        fh.write('\n')

        fh.write(f'# RUBBER-keyword fx shaders '
                 f'({len(rubber_fx)}):\n')
        for fx in rubber_fx:
            fh.write(f'  {fx}\n')
            fh.write(f'    used by {len(fx_to_tanks[fx])} tank(s)\n')
        fh.write('\n')

        fh.write(f'# Other fx shaders ({len(other_fx)}):\n')
        for fx in other_fx:
            fh.write(f'  {fx}  ({len(fx_to_tanks[fx])} tanks)\n')
        fh.write('\n')

        fh.write('# === Per-tank cloth / rubber sub-meshes ===\n')
        fh.write(f'# {len(cloth_tanks)} tanks have a cloth or '
                 f'rubber material on their gun\n')
        for tag in sorted(cloth_tanks):
            nation, hits = cloth_tanks[tag]
            fh.write(f'[{nation}] {tag}:\n')
            for kind, sub_idx, ident, fx in hits:
                fh.write(f'  sub#{sub_idx}  {kind:6s}  '
                         f'{ident}  ({fx})\n')

    print(f'wrote: {out_path}')
    print(f'  cloth fx shaders: {len(cloth_fx)}')
    print(f'  rubber fx shaders: {len(rubber_fx)}')
    print(f'  tanks with cloth/rubber gun material: '
          f'{len(cloth_tanks)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
