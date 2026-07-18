"""Headless smoke-test + diagnostic harness for the tank-rig sidecar.

Per Coffee 2026-06-04 (TRACK_SPLINE_BLENDER_PLAN handoff): the
Blender export now drops a `<base>.tankrig.json` next to every
FBX/GLB/GLTF.  This tool exercises the sidecar build path WITHOUT
launching Blender or the full viewer, so a developer can verify the
schema lands correctly on any tank in 1-2 seconds.

It does the minimum a Viewer would do at load:
  * Extract the chassis XML from the WoT pkg hierarchy.
  * Run `VehicleXMLLoader.parse_info` to get the chassis info dict.
  * Extract `Chassis.visual_processed` and parse the bone palette
    via `track_spline.parse_chassis_bone_world_positions`.
  * Resolve the track-pad model paths via VehicleXMLLoader._entry.
  * Stuff the lot onto a Viewer-shaped object and call
    `_tankrig_sidecar.build_tankrig_payload`.

Then it asserts the payload shape + counts and dumps a short
report.  No GL window, no pygame -- runs anywhere with Python +
numpy + the WoT install.

Usage
-----
    python cust_tools/dump_tankrig_sidecar.py G102_Pz_III
    python cust_tools/dump_tankrig_sidecar.py A14_T30
    python cust_tools/dump_tankrig_sidecar.py A83_T110E4

By default writes nothing to disk -- only stdout.  Pass `--write
<path>` to drop the sidecar JSON for inspection.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# Nation prefix map matches dump_track_skinning.py.  Single letter
# covers every nation in the live WoT install today.
# WoT inherits TWO parallel "nation" labels: the script-side
# (`scripts/item_defs/vehicles/<n>/...`) and the asset-side
# (`vehicles/<n>/...`).  They DON'T agree -- script side says
# "germany / france / uk / usa", asset side says "german /
# french / british / american".  We track both per nation.
NATION_MAP = {
    # tag prefix -> (scripts_nation, vehicles_nation)
    'A':  ('usa',     'american'),
    'G':  ('germany', 'german'),
    'R':  ('ussr',    'russian'),
    'F':  ('france',  'french'),
    'B':  ('uk',      'british'),
    'C':  ('china',   'china'),
    'J':  ('japan',   'japan'),
    'CZ': ('czech',   'czech'),
    'S':  ('sweden',  'sweden'),
    'IT': ('italy',   'italian'),
    'PL': ('poland',  'poland'),
}


class _FakeMesh:
    """Just enough of `Mesh` for the sidecar to walk viewer.meshes
    and recover the nation from `primitives_zip`.  Real Mesh objects
    have ~30 more fields; this tool only needs the one path string."""

    def __init__(self, primitives_zip: str):
        self.primitives_zip = primitives_zip


class _FakeViewer:
    """Duck-typed Viewer-shaped object the sidecar consumer requires.

    The sidecar reads:
      * _pending_chassis_info       (dict)
      * _track_chassis_bones_bind   (dict {name: np.ndarray((3,))})
      * _track_pad_paths            (dict)
      * source_tank_name            (str)
      * meshes                      (iterable, for nation lookup)
      * _pkg_extractor              (for visual-processed re-resolve)
      * _chassis_visual_path        (local path; bypasses pkg lookup)
      * _resolve_pads_per_side()    (optional; falls back to internal
                                    halving rule)

    Everything else (model_matrix, GL VAOs, etc.) is irrelevant for
    the sidecar bake.
    """

    def __init__(self):
        self._pending_chassis_info = {}
        self._track_chassis_bones_bind = {}
        self._track_pad_paths = {}
        self.source_tank_name = None
        self.meshes = []
        self._pkg_extractor = None
        self._chassis_visual_path = None


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_chassis_info(pe, tank_tag: str, nation: str) -> tuple:
    """Find the vehicle XML in scripts.pkg, run parse_info, return
    `(info_dict, chassis_info_dict, vehicle_xml_local_path)`."""
    from tankExporterPy.loaders import VehicleXMLLoader

    # Vehicle XML lives at scripts/item_defs/vehicles/<n>/<tag>.xml
    xml_internal = f'scripts/item_defs/vehicles/{nation}/{tank_tag}.xml'
    local_xml = pe.extract(xml_internal)
    if not local_xml:
        raise SystemExit(
            f"could not extract {xml_internal}; check tag/nation map")

    info = VehicleXMLLoader.parse_info(local_xml, pkg_extractor=pe)
    chassis = info.get('chassis') or {}
    return info, chassis, local_xml


def _load_chassis_bones(pe, tank_tag: str, nation: str) -> tuple:
    """Extract Chassis.visual_processed and parse the bone positions.

    Returns `(bones_dict, local_visual_path, chassis_primitives_zip)`.
    The primitives zip path lets us populate viewer.meshes with one
    FakeMesh so the nation-detection branch of the sidecar's
    `_build_tank_block` lights up.
    """
    from tankExporterPy.track_spline import parse_chassis_bone_world_positions

    base = f'vehicles/{nation}/{tank_tag}/normal/lod0'
    chassis_visual_zip = f'{base}/Chassis.visual_processed'
    chassis_prim_zip   = f'{base}/Chassis.primitives_processed'

    local_visual = pe.extract(chassis_visual_zip)
    if not local_visual:
        raise SystemExit(f"could not extract {chassis_visual_zip}")
    bones = parse_chassis_bone_world_positions(local_visual)
    if not bones:
        raise SystemExit(f"no chassis bones parsed from {local_visual}")
    return bones, local_visual, chassis_prim_zip


def _resolve_pad_paths(pe, chassis_info: dict, res_mods: str) -> dict:
    """Walk track_segment_models and use VehicleXMLLoader._entry to
    resolve every .model ref to its local .primitives_processed +
    .visual_processed.  Mirrors viewer._resolve_track_pad_paths."""
    from tankExporterPy.loaders import VehicleXMLLoader as _VXL

    tsm = chassis_info.get('track_segment_models') or {}
    out: dict = {}
    for key, value in tsm.items():
        if not isinstance(value, str) or not value.endswith('.model'):
            continue
        try:
            entry = _VXL._entry(
                label=key, model_path=value,
                res_mods_root=res_mods, offset=(0.0, 0.0, 0.0),
                pkg_extractor=pe)
        except Exception as exc:
            print(f"[smoke] {key} _entry failed: {exc}")
            continue
        out[key] = {
            'primitives': entry.get('primitives'),
            'visual':     entry.get('visual'),
            'model_zip':  value,
        }
    return out


# ---------------------------------------------------------------------------
# Smoke-test harness
# ---------------------------------------------------------------------------

def _nations_for_tag(tag: str) -> tuple:
    """First-letter -> (scripts_nation, vehicles_nation)."""
    pref2 = tag[:2].upper()
    if pref2 in NATION_MAP:
        return NATION_MAP[pref2]
    pref1 = tag[:1].upper()
    return NATION_MAP.get(pref1, ('usa', 'american'))


def _read_config_pkg_dir() -> tuple:
    """Pull pkg_dir + res_mods + lookup_xml out of tankExporterPy.json.
    Returns (pkg_dir, res_mods, lookup_xml) -- last two may be empty."""
    cfg_path = os.path.join(ROOT, 'tankExporterPy.json')
    if not os.path.isfile(cfg_path):
        return None, '', os.path.join(ROOT, 'TheItemList.xml')
    with open(cfg_path, encoding='utf-8') as fh:
        cfg = json.load(fh)
    pkg_dir = (cfg.get('pkg_dir') or '').strip()
    if pkg_dir:
        # cfg pkg_dir is "<wot>/res/packages"; the PkgExtractor wants
        # the WoT install root.
        wot_root = os.path.normpath(os.path.join(pkg_dir, '..', '..'))
    else:
        wot_root = None
    res_mods   = (cfg.get('res_mods')  or '').strip()
    lookup_xml = (cfg.get('lookup_xml')
                   or os.path.join(ROOT, 'TheItemList.xml'))
    return wot_root, res_mods, lookup_xml


def run_smoke(tank_tag: str, write_path: str = '') -> int:
    """One-tank dry-run.  Returns process exit code (0 = success)."""
    from tankExporterPy.loaders               import PkgExtractor
    from tankExporterPy.exporters._tankrig_sidecar import (
        build_tankrig_payload, write_tankrig_sidecar)

    wot_root, res_mods, lookup_xml = _read_config_pkg_dir()
    if not wot_root or not os.path.isdir(wot_root):
        print(f"[smoke] no usable wot_root from tankExporterPy.json "
              f"(got {wot_root!r})")
        return 2

    scripts_nation, vehicles_nation = _nations_for_tag(tank_tag)
    print(f"[smoke] {tank_tag}: scripts_nation={scripts_nation!r}, "
          f"vehicles_nation={vehicles_nation!r}, wot_root={wot_root}")
    pe = PkgExtractor(wot_root, lookup_xml=lookup_xml)

    # ---- 1. Chassis XML + chassis_info ------------------------------
    info, chassis, _vxml = _load_chassis_info(pe, tank_tag, scripts_nation)

    # ---- 2. Bone palette --------------------------------------------
    bones, local_visual, chassis_prim_zip = _load_chassis_bones(
        pe, tank_tag, vehicles_nation)
    print(f"[smoke] {tank_tag}: parsed {len(bones)} chassis bones; "
          f"visual_processed at {local_visual}")

    # ---- 3. Pad path resolve ----------------------------------------
    pad_paths = _resolve_pad_paths(pe, chassis, res_mods)

    # ---- 4. Stuff a fake viewer + bake ------------------------------
    fv = _FakeViewer()
    fv._pending_chassis_info     = chassis
    fv._track_chassis_bones_bind = bones
    fv._track_pad_paths          = pad_paths
    fv.source_tank_name          = tank_tag
    fv.meshes                    = [_FakeMesh(chassis_prim_zip)]
    fv._pkg_extractor            = pe
    fv._chassis_visual_path      = local_visual

    payload = build_tankrig_payload(fv)
    if payload is None:
        print(f"[smoke] {tank_tag}: payload is None")
        return 3

    # ---- 5. Asserts -------------------------------------------------
    n_bones    = len(payload['bone_palette'])
    n_pads_L   = len(payload['pad_transforms_bind_pose']['L'])
    n_pads_R   = len(payload['pad_transforms_bind_pose']['R'])
    n_cline_L  = len(payload['centerline_v_loc']['L'])
    expected_n = _expected_pad_count(chassis)

    assert n_bones > 0, "bone_palette empty"
    if expected_n:
        # Two-piece tanks halve segmentsCount inside the resolver; the
        # baked count must match what _resolve_pads_per_side returns.
        assert n_pads_L == expected_n, (
            f"L pad count {n_pads_L} != expected {expected_n}")
        assert n_pads_R == expected_n, (
            f"R pad count {n_pads_R} != expected {expected_n}")
        assert n_cline_L == expected_n, (
            f"L centerline count {n_cline_L} != expected {expected_n}")
    for entry in payload['bone_palette']:
        assert len(entry['bind_world_mat4']) == 16, (
            f"bone {entry['name']!r} mat4 not 16 floats")
    for side in ('L', 'R'):
        for i, m in enumerate(payload['pad_transforms_bind_pose'][side]):
            assert len(m) == 16, f"{side}[{i}] mat4 not 16 floats"

    # JSON round-trip cleanliness check (= numpy didn't leak in).
    rt = json.loads(json.dumps(payload))
    assert rt['schema_version'] == 1

    # ---- 6. Optional write ------------------------------------------
    if write_path:
        ok, msg = write_tankrig_sidecar(fv, write_path)
        if not ok:
            print(f"[smoke] write failed: {msg}")
            return 4
        print(f"[smoke] wrote {msg}")

    # ---- 7. Report --------------------------------------------------
    print()
    print(f"=== sidecar report for {tank_tag} ===")
    print(f"  schema_version: {payload['schema_version']}")
    print(f"  tank:           {payload['tank']}")
    print(f"  bone_palette:   {n_bones} entries")
    roles_summary = ", ".join(
        f"{k}={len(v)}" for k, v in payload['wheel_roles'].items() if v)
    print(f"  wheel_roles:    {roles_summary or '(empty)'}")
    print(f"  wheel_radii:    {len(payload['wheel_radii'])} entries")
    ci_out = payload['chain_inputs']
    print(f"  chain_inputs:   segLen={ci_out['segmentLength']}  "
          f"segCount={ci_out['segmentsCount']}  "
          f"innerT={ci_out['segmentsInnerThickness']}  "
          f"groupRadius_road={ci_out['groupRadius_road']}")
    print(f"  segmentOffsets: {ci_out['segmentOffsets']}")
    print(f"  teethSyncs:     "
          f"{list(ci_out['teethSyncs'].keys()) or '(none)'}")
    print(f"  pad_mesh:       {payload['pad_mesh']}")
    print(f"  pad_xforms L/R: {n_pads_L} / {n_pads_R}")
    print(f"  centerline L/R: {n_cline_L} / "
          f"{len(payload['centerline_v_loc']['R'])}")
    if n_pads_L:
        first = payload['pad_transforms_bind_pose']['L'][0]
        print(f"  first L pad mat4 (row-major):")
        for r in range(4):
            row = first[r * 4:r * 4 + 4]
            print(f"      [{row[0]:+.4f} {row[1]:+.4f} "
                  f"{row[2]:+.4f} {row[3]:+.4f}]")
    print(f"=== {tank_tag} OK ===")
    print()
    return 0


def _expected_pad_count(chassis: dict) -> int:
    """Mirror of viewer._resolve_pads_per_side's halving rule, so the
    smoke test can assert against the same number the runtime uses."""
    sc = chassis.get('segmentsCount')
    try:
        n = int(sc or 0)
    except (TypeError, ValueError):
        return 0
    if n < 4 or n > 500:
        return 0
    tsm = chassis.get('track_segment_models') or {}
    if tsm.get('segment2ModelLeft'):
        n = n // 2
    return n


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('tank', help='Tank tag, e.g. G102_Pz_III')
    p.add_argument('--write', default='',
                   help='Optional path to write the sidecar JSON to '
                        '(e.g. ./G102_Pz_III.fbx -- the tool will '
                        'emit the sidecar with .tankrig.json suffix)')
    args = p.parse_args()
    return run_smoke(args.tank, args.write)


if __name__ == '__main__':
    sys.exit(main())
