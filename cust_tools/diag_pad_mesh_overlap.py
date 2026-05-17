"""Pad-mesh rectangle overlap diagnostic.

Per Coffee 2026-05-16 ("2 at pads at center.. see it?",
"spline looks better.. pads not so much").  The pad CENTERS
look correctly spaced (seg_len apart) but the rendered pad
meshes visually OVERLAP near the wheels.  Cause hypothesis:
on the wheel arc, adjacent pads rotate around the hub.  Each
pad is a RECTANGLE of length ~ seg_len oriented along the
chain tangent.  When two pads' rectangles are oriented at
sufficiently different angles (= the chain wraps tightly),
their rectangles INTERSECT visually even though their
CENTRES are seg_len apart.

This script:
  1. Loads a real tank (T110E4 by default).
  2. Runs the production homie + PBD pipeline.
  3. Steps PBD over multiple frames with chain flow + a
     suspension perturbation (one wheel bumped up).
  4. Builds each pad as a rectangle (length = seg_len,
     width = a fixed fraction).
  5. Detects rectangle-rectangle intersections (2D polygon
     SAT test in YZ plane).
  6. Plots the chain with rectangles + highlights overlapping
     pairs.

Output: <repo>/math_images/diag_pad_mesh_overlap_*.png
"""
import argparse
import json
import math
import os
import sys

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, Polygon

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tankExporterPy.loaders import PkgExtractor, VehicleXMLLoader
from tankExporterPy.track_spline import (
    parse_chassis_bone_world_positions,
)
from tankExporterPy import track_homie
from tankExporterPy import track_chain_pbd as _pbd


OUT_DIR = os.path.join(ROOT, 'math_images')


def load_tank(basename, nation='usa'):
    with open(os.path.join(ROOT, 'tankExporterPy.json')) as f:
        cfg = json.load(f)
    pd = cfg.get('pkg_dir', '').strip()
    pkg_dir = os.path.normpath(os.path.join(pd, '..', '..'))
    pe = PkgExtractor(pkg_dir,
                       lookup_xml=os.path.join(ROOT, 'TheItemList.xml'))
    xml_path = f'scripts/item_defs/vehicles/{nation}/{basename}.xml'
    nation_pkg = {'usa': 'american', 'germany': 'german',
                  'ussr': 'russian', 'uk': 'british',
                  'france': 'french', 'poland': 'poland',
                  'sweden': 'sweden', 'italy': 'italian',
                  'japan': 'japan', 'china': 'china',
                  'czech': 'czech'}.get(nation, nation)
    info = VehicleXMLLoader.parse_info(pe.extract(xml_path), pe)
    ch = info.get('chassis', {})
    roles = ch.get('wheel_roles', {})
    radii = ch.get('wheel_radii', {})
    seg_len = float(ch.get('segmentLength'))
    seg_count = int(ch.get('segmentsCount'))
    has_seg2 = bool((ch.get('track_segment_models') or {}
                    ).get('segment2ModelLeft'))
    n_pads = (seg_count // 2) if has_seg2 else seg_count
    inner_t = float(ch.get('segmentsInnerThickness') or 0.0)
    vis_path = (f'vehicles/{nation_pkg}/{basename}/normal/lod0/'
                'Chassis.visual_processed')
    vis_local = pe.extract(vis_path)
    bones = parse_chassis_bone_world_positions(vis_local)
    return bones, radii, roles, seg_len, n_pads, inner_t


def gauge_for_side_L(bones, radii):
    xs = []
    for nm in radii:
        if 'L' not in nm:
            continue
        b = bones.get(nm)
        if b is None:
            b = bones.get(nm + '_BlendBone')
        if b is None:
            continue
        xs.append(float(b[0]))
    xs.sort()
    return abs(xs[len(xs)//2]) * 2.0 if xs else 2.75


def build_pbd_inputs(bones, radii, roles, side='L'):
    all_names = (roles.get(f'drive_sprockets_{side}', [])
                 + roles.get(f'idlers_{side}', [])
                 + roles.get(f'road_wheels_{side}', [])
                 + roles.get(f'return_rollers_{side}', []))
    kind_for = {}
    for nm in roles.get(f'drive_sprockets_{side}', []): kind_for[nm] = 'sprocket'
    for nm in roles.get(f'idlers_{side}', []):          kind_for[nm] = 'idler'
    for nm in roles.get(f'road_wheels_{side}', []):     kind_for[nm] = 'road'
    for nm in roles.get(f'return_rollers_{side}', []):  kind_for[nm] = 'roller'

    hubs_yz, rs, names, kinds = [], [], [], []
    for nm in all_names:
        b = bones.get(nm)
        if b is None:
            b = bones.get(nm + '_BlendBone')
        if b is None:
            continue
        R = radii.get(nm)
        if R is None:
            continue
        hubs_yz.append((float(b[1]), -float(b[2])))
        rs.append(float(R))
        names.append(nm)
        kinds.append(kind_for.get(nm, '?'))
    return hubs_yz, rs, names, kinds


def pad_rectangle(pos_yz, tan_yz, length, width):
    """Return 4 corners of an oriented rectangle centred at pos_yz,
    with the long side along tan_yz of length `length`, and the
    short side perpendicular of length `width`.
    """
    t = np.asarray(tan_yz, dtype=np.float64)
    tn = np.linalg.norm(t)
    if tn < 1e-9:
        t = np.array([1.0, 0.0])
    else:
        t = t / tn
    n = np.array([-t[1], t[0]])
    L2 = length * 0.5
    W2 = width * 0.5
    p = np.asarray(pos_yz, dtype=np.float64)
    return np.array([
        p + L2 * t + W2 * n,
        p - L2 * t + W2 * n,
        p - L2 * t - W2 * n,
        p + L2 * t - W2 * n,
    ])


def rect_overlap_sat(r1, r2):
    """2D SAT test for axis-aligned-or-rotated rectangles.
    Returns True if rectangles overlap (with margin 0 = touching
    counts as no overlap).
    """
    def axes(r):
        e1 = r[1] - r[0]
        e2 = r[3] - r[0]
        n1 = np.array([-e1[1], e1[0]])
        n2 = np.array([-e2[1], e2[0]])
        return [n1 / np.linalg.norm(n1), n2 / np.linalg.norm(n2)]

    for a in axes(r1) + axes(r2):
        p1 = r1 @ a
        p2 = r2 @ a
        if p1.max() <= p2.min() or p2.max() <= p1.min():
            return False
    return True


def step_pbd_n_frames(inst, n_frames, dt=1.0/60.0,
                      chain_flow_per_frame=0.0):
    """Run inst.step(dt) for n_frames frames, optionally drifting
    chain pads by chain_flow_per_frame each frame (along the
    seeded tangent) to simulate forward motion.
    """
    snapshots = []
    for f in range(n_frames):
        # We can't easily inject "chain flow" into PBD since it's
        # closed-loop position-based.  Skip for now -- PBD itself
        # doesn't animate chain flow, the production code re-seeds
        # from homie each frame.  Just run PBD step.
        inst.step(dt)
        snapshots.append((f, inst.pos.copy()))
    return snapshots


def main():
    pa = argparse.ArgumentParser()
    pa.add_argument('--tank', default='A83_T110E4',
                    help='tank basename')
    pa.add_argument('--nation', default='usa')
    pa.add_argument('--bump', type=float, default=-0.15,
                    help='hub Y bump (m); negative = extend down')
    pa.add_argument('--frames', type=int, default=10,
                    help='PBD steps to run after bump')
    pa.add_argument('--pad-len', type=float, default=None,
                    help='override pad rectangle length (m)')
    pa.add_argument('--pad-width', type=float, default=0.03,
                    help='pad rectangle width (perpendicular)')
    pa.add_argument('--use-override', action='store_true',
                    help='use production pos-hub orientation override')
    args = pa.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    print(f'Loading {args.tank}...')
    bones, radii, roles, seg_len, n_pads, inner_t = load_tank(
        args.tank, args.nation)
    print(f'  seg_len={seg_len:.4f}m  n_pads={n_pads}  '
          f'inner_t={inner_t:.4f}m')

    gauge_x = gauge_for_side_L(bones, radii)
    side_x  = -0.5 * gauge_x

    # Homie chain (inflated by inner_t -- matches production).
    arcs_3 = track_homie.build_chain_segments(
        bones, radii, roles, side='left',
        n_pads=n_pads, seg_len=seg_len,
        inner_thickness=inner_t)
    pos3, tan3, hubs3, on_arc = track_homie.assemble_chain_arrays(
        arcs_3, gauge_x, 'left', n_pads, s_offset=0.0)
    pos3 = pos3.copy(); pos3[:, 2] *= -1.0
    tan3 = tan3.copy(); tan3[:, 2] *= -1.0

    hubs_yz, rs, names, kinds = build_pbd_inputs(
        bones, radii, roles, side='L')
    road_idxs = [i for i, k in enumerate(kinds) if k == 'road']
    if not road_idxs:
        print('No road wheels on L'); return
    idx_road = road_idxs[len(road_idxs)//2]
    print(f'Perturbing road wheel: {names[idx_road]} '
          f'({args.bump:+.2f} m)')

    # FIX: inflate PBD wheel_radii by inner_t (matches v1.221.4
    # production fix).
    rs_pbd = [r + inner_t for r in rs]

    inst = _pbd.TrackChainPBD(
        side_x=side_x, seg_len=seg_len, n_pads=n_pads,
        hubs_yz=hubs_yz, wheel_radii=rs_pbd,
        wheel_names=names, wheel_kinds=kinds)
    inst.seed_from_homie(pos3)
    print(f'seeded; bound pads: {(inst._bound_wheel >= 0).sum()}/'
          f'{inst.n_pads}')

    # Apply bump.
    hubs_yz_up = [list(h) for h in hubs_yz]
    hubs_yz_up[idx_road][0] += args.bump
    inst.update_hubs([tuple(h) for h in hubs_yz_up], rs_pbd)

    # Step n frames.
    snapshots = step_pbd_n_frames(inst, args.frames)
    print(f'\nRan {len(snapshots)} PBD frames.')

    # Final positions.
    final_pos = snapshots[-1][1]
    # Two alternatives to compare:
    # A) PRODUCTION (orientation override): tangent from chord;
    #    arc pads override "up" with radial-out from nearest hub.
    # B) PLAIN CHORD: tangent from chord; up = perpendicular.
    nxt = np.roll(final_pos, -1, axis=0)
    prv = np.roll(final_pos, +1, axis=0)
    chord = nxt - prv
    cn = np.linalg.norm(chord, axis=1, keepdims=True)
    tan_pbd_chord = chord / np.maximum(cn, 1e-9)
    # Production override path:
    hubs_arr_inf = np.array(hubs_yz_up, dtype=np.float32)
    radii_arr = np.array(rs_pbd, dtype=np.float32)
    diffp = final_pos[:, None, :] - hubs_arr_inf[None, :, :]
    distp = np.linalg.norm(diffp, axis=2)
    slack = distp - radii_arr[None, :]
    w_idx_per = np.argmin(np.abs(slack), axis=1)
    pad_slack = np.abs(slack[np.arange(n_pads), w_idx_per])
    on_arc_now = pad_slack < 0.01
    print(f'pads near wheel rim (arc pads): {on_arc_now.sum()}/'
          f'{n_pads}')
    up_arc = final_pos - hubs_arr_inf[w_idx_per]
    up_arc_n = np.linalg.norm(up_arc, axis=1, keepdims=True)
    up_arc /= np.maximum(up_arc_n, 1e-9)
    up_line = np.column_stack(
        [-tan_pbd_chord[:, 1], tan_pbd_chord[:, 0]])
    ups_override = np.where(on_arc_now[:, None], up_arc, up_line)
    tan_override = np.column_stack(
        [ups_override[:, 1], -ups_override[:, 0]])
    flip = (tan_override * tan_pbd_chord).sum(axis=1) < 0
    tan_override[flip] *= -1
    # Pick which orientation to use.
    use_override = args.use_override
    tan_pbd = (tan_override.astype(np.float32)
               if use_override else tan_pbd_chord.astype(np.float32))
    print(f'orientation: {"override (pos-hub)" if use_override else "plain chord"}')

    # Pad rectangle length: default to seg_len if not given.
    pad_len = args.pad_len if args.pad_len else seg_len * 0.95

    # Build rectangles + detect overlaps.
    rects = [pad_rectangle(final_pos[i], tan_pbd[i],
                            pad_len, args.pad_width)
             for i in range(n_pads)]
    overlapping_pairs = []
    for i in range(n_pads):
        for di in [1, 2]:   # adjacent + 2-apart only (close pairs)
            j = (i + di) % n_pads
            if i >= j and di == 1: continue
            if rect_overlap_sat(rects[i], rects[j]):
                overlapping_pairs.append((i, j))
    print(f'pad rectangles ({pad_len:.3f}m x {args.pad_width:.3f}m): '
          f'{len(overlapping_pairs)} overlapping pairs')
    if overlapping_pairs:
        print('  examples:')
        for pi, pj in overlapping_pairs[:15]:
            d_centre = np.linalg.norm(final_pos[pi] - final_pos[pj])
            print(f'    pad[{pi:3d}] - pad[{pj:3d}]  '
                  f'centre-d={d_centre:.4f}m')

    # Plot final state with rectangles.
    Z_c = hubs_yz_up[idx_road][1]
    Y_c = hubs_yz_up[idx_road][0]
    SPAN = 0.8
    fig, ax = plt.subplots(figsize=(11, 7))
    ax.set_aspect('equal')
    ax.set_xlim(Z_c - SPAN, Z_c + SPAN)
    ax.set_ylim(Y_c - SPAN, Y_c + SPAN)
    ax.set_title(
        f'{args.tank} -- L side -- PBD frame {args.frames}, '
        f'{names[idx_road]} hub {args.bump:+.2f} m.  '
        f'red rects = overlapping pad mesh pairs',
        fontsize=10)
    ax.set_xlabel('Z (m)'); ax.set_ylabel('Y (m)')
    for i, (h, R, nm, k) in enumerate(zip(
            hubs_yz_up, rs_pbd, names, kinds)):
        if abs(h[1] - Z_c) > SPAN * 1.5: continue
        edge = 'red' if i == idx_road else 'grey'
        c = Circle((h[1], h[0]), R, fill=False,
                   edgecolor=edge, linewidth=1.2)
        ax.add_patch(c)
        ax.text(h[1], h[0], nm[-3:], fontsize=7,
                ha='center', va='center')
    # Pads as rectangles (Z, Y).
    overlapping_set = {p for pair in overlapping_pairs for p in pair}
    for i, r in enumerate(rects):
        # rectangle corners are in (Y, Z); plot expects (Z, Y).
        corners_zy = np.column_stack([r[:, 1], r[:, 0]])
        face = 'red' if i in overlapping_set else 'lightgrey'
        edge = 'darkred' if i in overlapping_set else 'black'
        alpha = 0.55 if i in overlapping_set else 0.40
        ax.add_patch(Polygon(corners_zy, closed=True,
                              facecolor=face, edgecolor=edge,
                              linewidth=0.5, alpha=alpha))
    # Spline polyline.
    ax.plot(final_pos[:, 1], final_pos[:, 0], '-',
            color='orange', linewidth=0.8, alpha=0.6)
    ax.scatter(final_pos[:, 1], final_pos[:, 0],
               s=8, c='blue', alpha=0.7, zorder=10)
    ax.grid(alpha=0.3)
    suffix = '_override' if args.use_override else '_chord'
    out_path = os.path.join(
        OUT_DIR,
        f'diag_pad_mesh_overlap_{args.tank}_bump{int(args.bump*100):+03d}{suffix}.png')
    plt.tight_layout()
    plt.savefig(out_path, dpi=120, facecolor='white')
    plt.close()
    print(f'  wrote {out_path}')


if __name__ == '__main__':
    main()
