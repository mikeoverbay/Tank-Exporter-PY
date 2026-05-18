"""Round every float in an F3 manual-record JSON to N decimal
places, optionally re-shape a v1 record to the v2 schema.

Per Coffee 2026-05-16 ("truncate on read because that [write-
side fix] didn't change anything"): the v1.232.1 round-on-write
only helps recordings taken AFTER restart.  Pre-existing
recordings stay verbose -- this tool reads, rounds, writes the
truncated copy alongside the original.

`--strip-to-v2` additionally migrates a schema-v1 file (with
its per-frame chassis pose / integrator / per-wheel-classifier
fields and full `chassis_info_xml` meta) into the v2 layout:
just `{frame, t_s, dt_s, speed_mps, chain_focus}` per frame
with chain_focus pads carrying `angle_rad` (computed post-hoc
from each pad's `y`/`z` and its wheel's `hub_chassis_yz`).
`on_arc` is OMITTED in stripped output because the renderer
arc/hub stash isn't in the JSON -- only live recordings can
populate it.  Stripped meta keeps just the road-wheel ids,
their radii, the chain knobs (segmentLength / segmentsCount /
segmentsInnerThickness) and a `migrated_from_schema_v1` flag.

Usage:
    python cust_tools/truncate_f3_floats.py <path.json>
        [--places 4] [--out <path>] [--inplace] [--strip-to-v2]
"""
import argparse
import json
import math
import os
import sys


def round_floats(obj, places=4):
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, float):
        return round(obj, places)
    if isinstance(obj, dict):
        return {k: round_floats(v, places) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [round_floats(v, places) for v in obj]
    return obj


def _strip_meta_v1_to_v2(meta_v1):
    """Build a v2-shape meta from a v1 record's meta dict."""
    ci = meta_v1.get('chassis_info_xml') or {}
    roles_all = ci.get('wheel_roles') or {}
    radii_all = ci.get('wheel_radii') or {}
    road_names = set(roles_all.get('road_wheels_L') or [])
    road_names.update(roles_all.get('road_wheels_R') or [])
    road_radii = {nm: float(radii_all[nm])
                  for nm in road_names if nm in radii_all}
    return {
        'schema_version':            2,
        'kind':                      meta_v1.get('kind',
                                                 'tank_manual_record'),
        'tank':                      meta_v1.get('tank'),
        'timestamp':                 meta_v1.get('timestamp'),
        'tepy_version':              meta_v1.get('tepy_version'),
        'frame_count':               meta_v1.get('frame_count'),
        'duration_s':                meta_v1.get('duration_s'),
        'segmentsInnerThickness':    float(
            ci.get('segmentsInnerThickness') or 0.0),
        'segmentLength':             float(
            ci.get('segmentLength') or 0.0),
        'segmentsCount':             int(
            ci.get('segmentsCount') or 0),
        'road_wheels_L':             list(
            roles_all.get('road_wheels_L') or []),
        'road_wheels_R':             list(
            roles_all.get('road_wheels_R') or []),
        'wheel_radii':               road_radii,
        'migrated_from_schema_v1':   True,
    }


def _strip_chain_focus_v1_to_v2(cf_v1):
    """Convert v1 chain_focus (list per side with hub_chassis_yz
    + pads carrying y/z/d_hub) into the v2 shape (per-side
    {s_offset_m, wheels: [{wheel_name, wheel_angle_rad, pads:
    [{idx, angle_rad}]}]}).  Recomputes `angle_rad` from each
    pad's y/z and its wheel's stashed hub position.  Drops
    `on_arc` (can't be recovered from the v1 record)."""
    out = {}
    if not isinstance(cf_v1, dict):
        return out
    for side, wheel_list in cf_v1.items():
        if not isinstance(wheel_list, list) or not wheel_list:
            continue
        # All v1 wheel entries on a side share `s_offset_m`;
        # promote it to the per-side dict.
        s_offset = float(wheel_list[0].get('s_offset_m') or 0.0)
        wheels_v2 = []
        for w in wheel_list:
            hub = w.get('hub_chassis_yz') or [0.0, 0.0]
            hub_y, hub_z = float(hub[0]), float(hub[1])
            pads_v2 = []
            for pd in w.get('pads') or ():
                py = float(pd.get('y') or 0.0)
                pz = float(pd.get('z') or 0.0)
                pads_v2.append({
                    'idx':       int(pd.get('idx') or 0),
                    'angle_rad': math.atan2(py - hub_y,
                                             pz - hub_z),
                })
            wheels_v2.append({
                'wheel_name':      w.get('wheel_name'),
                'wheel_angle_rad': float(
                    w.get('wheel_angle_rad') or 0.0),
                'pads':            pads_v2,
            })
        out[side] = {
            's_offset_m': s_offset,
            'wheels':     wheels_v2,
        }
    return out


def _strip_frame_v1_to_v2(frame_v1):
    return {
        'frame':       int(frame_v1.get('frame') or 0),
        't_s':         float(frame_v1.get('t_s') or 0.0),
        'dt_s':        float(frame_v1.get('dt_s') or 0.0),
        'speed_mps':   float(frame_v1.get('speed_mps') or 0.0),
        'chain_focus': _strip_chain_focus_v1_to_v2(
            frame_v1.get('chain_focus')),
    }


def strip_to_v2(payload):
    """Top-level migrator.  Pass-through for already-v2 payloads."""
    if not isinstance(payload, dict):
        return payload
    meta = payload.get('meta') or {}
    if int(meta.get('schema_version') or 0) >= 2:
        return payload
    new_meta = _strip_meta_v1_to_v2(meta)
    new_frames = [_strip_frame_v1_to_v2(f)
                  for f in (payload.get('frames') or [])]
    return {'meta': new_meta, 'frames': new_frames}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('path', help='Input JSON path.')
    ap.add_argument('--places', type=int, default=4,
                    help='Decimal places to keep (default 4).')
    ap.add_argument('--out', default=None,
                    help='Output path (default: <name>_trunc.json '
                         'or <name>_v2.json with --strip-to-v2).')
    ap.add_argument('--inplace', action='store_true',
                    help='Overwrite the input file.')
    ap.add_argument('--strip-to-v2', action='store_true',
                    help='Migrate schema-v1 record to v2 layout '
                         '(drops unused chassis-pose / classifier '
                         '/ integrator fields).')
    args = ap.parse_args()

    inp = os.path.abspath(args.path)
    if not os.path.isfile(inp):
        print(f'not a file: {inp}', file=sys.stderr)
        return 2

    if args.inplace:
        out = inp
    elif args.out:
        out = os.path.abspath(args.out)
    else:
        base, ext = os.path.splitext(inp)
        suffix = '_v2' if args.strip_to_v2 else '_trunc'
        out = base + suffix + ext

    size_in = os.path.getsize(inp)
    with open(inp, encoding='utf-8') as fh:
        data = json.load(fh)
    if args.strip_to_v2:
        data = strip_to_v2(data)
    data = round_floats(data, args.places)
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=1)
    size_out = os.path.getsize(out)

    pct = (1.0 - size_out / max(size_in, 1)) * 100.0
    print(f'in  : {inp}')
    print(f'out : {out}')
    print(f'size: {size_in / 1024:.1f} KB -> {size_out / 1024:.1f} KB '
          f'({pct:+.1f}%)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
