"""Diagnostic for the 'two pads pile up at the wheel center' bug.

Per Coffee 2026-05-16 ("2 at pads at center.. see it?" with
screenshots showing chain pads physically overlapping near
where the chain meets a road wheel; "spline looks better..
pads not so much").

The screenshot evidence: the spline polyline (= pad-center
positions) reads as a smooth chain, but the rendered pad
MESHES visibly overlap.  Two adjacent pads land at almost
the same XYZ near the wheel contact.

Hypothesis: the wheel-push step in PBD pushes each pad inside
the wheel by its OWN penetration depth.  Adjacent pads at
different dZ have different `pen` -> different Y deltas ->
the chain bends more tightly than seg_len permits.  The
distance constraint then tries to restore seg_len by sliding
pads along the curve, but the wheel push keeps pulling them
back.  Result: 2 (or more) pads collapse into one another at
the wheel center where penetration is deepest.

This script runs the actual production code path (homie -> PBD)
on a real T30 chassis, perturbs one road wheel UP by 5 cm
(simulated suspension compression), and:

  1. Plots the chain at each PBD relaxation iteration.
  2. Measures min(|pad[i+1] - pad[i]|) per iteration -- if it
     drops below half seg_len, that's a pile-up signal.
  3. Identifies WHICH wheel push iteration causes the collapse.

Output: PNGs in <repo>/math_images/diag_pbd_pileup_*.png plus
console summary.
"""
import json
import math
import os
import sys

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Circle

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tankExporterPy.loaders import PkgExtractor, VehicleXMLLoader
from tankExporterPy.track_spline import (
    parse_chassis_bone_world_positions,
)
from tankExporterPy import track_homie
from tankExporterPy import track_chain_pbd as _pbd


TANK_XML = ('scripts/item_defs/vehicles/usa/A83_T110E4.xml')
TANK_VIS = ('vehicles/american/A83_T110E4/normal/lod0/'
            'Chassis.visual_processed')
OUT_DIR = os.path.join(ROOT, 'math_images')


def load_tank():
    with open(os.path.join(ROOT, 'tankExporterPy.json')) as f:
        cfg = json.load(f)
    pd = cfg.get('pkg_dir', '').strip()
    pkg_dir = os.path.normpath(os.path.join(pd, '..', '..'))
    pe = PkgExtractor(pkg_dir,
                       lookup_xml=os.path.join(ROOT, 'TheItemList.xml'))
    info = VehicleXMLLoader.parse_info(pe.extract(TANK_XML), pe)
    ch = info.get('chassis', {})
    roles = ch.get('wheel_roles', {})
    radii = ch.get('wheel_radii', {})
    seg_len = float(ch.get('segmentLength'))
    seg_count = int(ch.get('segmentsCount'))
    has_seg2 = bool((ch.get('track_segment_models') or {}
                    ).get('segment2ModelLeft'))
    n_pads = (seg_count // 2) if has_seg2 else seg_count
    inner_t = float(ch.get('segmentsInnerThickness') or 0.0)

    vis_local = pe.extract(TANK_VIS)
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
        # Match viewer._bone_yz: Y = bone Y, Z = -bone Z (renderer
        # frame).  PBD sees +Z forward.
        hubs_yz.append((float(b[1]), -float(b[2])))
        rs.append(float(R))
        names.append(nm)
        kinds.append(kind_for.get(nm, '?'))
    return hubs_yz, rs, names, kinds


def perturb_road_wheel_up(hubs_yz, names, kinds, idx_road, dy):
    """Push the road wheel at index `idx_road` UP by dy.  Returns a
    new hubs_yz with the modified Y.
    """
    out = [list(h) for h in hubs_yz]
    out[idx_road][0] += dy
    return [tuple(h) for h in out]


def step_pbd(inst, dt, capture_iters=True):
    """Run inst.step(dt) and capture each iteration's pos snapshot.

    Patches inst's relaxation loop via monkey-patch since the
    real `step()` doesn't expose per-iteration state.  Easier
    path: call the inner numpy operations directly here, in
    order, mimicking the production `step()`.
    """
    snapshots = []

    # Predict (Verlet)
    dt = min(dt, inst.MAX_DT)
    pos_save = inst.pos.copy()
    vel = (inst.pos - inst.prev_pos) / max(inst._prev_dt, 1e-6)
    inst.pos = (inst.pos + vel * dt
                 + 0.5 * inst._gravity_yz * inst.GRAVITY_SCALE
                 * dt * dt).astype(np.float32)
    inst.prev_pos = pos_save
    snapshots.append(('predict', inst.pos.copy()))

    free_arr = (inst._bound_wheel < 0)
    DEGEN_ARC_RAD = 0.005

    for it in range(inst.N_RELAX_ITERS):
        # 2a Bilateral wheel bind: snap bound pads to rim
        if inst._bound_wheel is not None and (inst._bound_wheel >= 0).any():
            for w_idx_unique in np.unique(inst._bound_wheel):
                if w_idx_unique < 0:
                    continue
                mask = inst._bound_wheel == w_idx_unique
                hub = inst.hubs[w_idx_unique]
                R   = inst.radii[w_idx_unique]
                diff = inst.pos[mask] - hub
                r    = np.linalg.norm(diff, axis=1, keepdims=True)
                r    = np.maximum(r, 1e-6)
                inst.pos[mask] = hub + R * (diff / r)

        # 2a-ii Distance constraint
        if inst.DISTANCE_STIFF > 0.0:
            nxt = np.roll(inst.pos, -1, axis=0)
            d   = nxt - inst.pos
            L   = np.linalg.norm(d, axis=1, keepdims=True)
            L   = np.maximum(L, 1e-6)
            err = (L - inst.seg_len) * (0.5 * inst.DISTANCE_STIFF)
            corr = err * (d / L)
            inst.pos += corr
            inst.pos -= np.roll(corr, +1, axis=0)

        # 2b Bend smoothing (free pads)
        if inst.BEND_STIFF > 0.0:
            prev = np.roll(inst.pos, +1, axis=0)
            nxt  = np.roll(inst.pos, -1, axis=0)
            mid  = 0.5 * (prev + nxt)
            target = mid - inst.pos
            corr_b = target * inst.BEND_STIFF
            corr_b[~free_arr] = 0.0
            inst.pos += corr_b

        # 2b-bis SKIP-K tensioner
        if inst.SKIP_K > 0 and inst.SKIP_STIFF > 0.0:
            nxt_k = np.roll(inst.pos, -inst.SKIP_K, axis=0)
            dk    = nxt_k - inst.pos
            Lk    = np.linalg.norm(dk, axis=1, keepdims=True)
            Lk    = np.maximum(Lk, 1e-6)
            rest  = inst._skip_rest_len[:, None]
            err_k = (Lk - rest) * (0.5 * inst.SKIP_STIFF)
            corr_k = err_k * (dk / Lk)
            inst.pos += corr_k
            inst.pos -= np.roll(corr_k, +inst.SKIP_K, axis=0)

        # 2c Unilateral wheel push (the suspect block)
        diff = inst.pos[:, None, :] - inst.hubs[None, :, :]
        r    = np.linalg.norm(diff, axis=2)
        r    = np.maximum(r, 1e-6)
        penetration = inst.radii[None, :] - r
        inside = penetration > 0.0
        if inside.any():
            worst_w = np.argmax(penetration * inside, axis=1)
            pad_idx = np.arange(inst.n_pads)
            w_idx   = worst_w
            pen     = penetration[pad_idx, w_idx]
            mask    = (pen > 0.0) & free_arr
            if mask.any():
                push_dir = (diff[pad_idx, w_idx]
                            / r[pad_idx, w_idx, None])
                radial_delta = (push_dir
                                 * pen[:, None]
                                 * inst.WHEEL_PUSH_MULT)
                is_road   = np.array(
                    [inst.wheel_kinds[int(wi)] == 'road'
                     for wi in w_idx], dtype=bool)
                is_roller = np.array(
                    [inst.wheel_kinds[int(wi)] == 'roller'
                     for wi in w_idx], dtype=bool)
                sign_y = np.where(is_road, -1.0,
                          np.where(is_roller, +1.0, 0.0))
                delta = radial_delta.copy()
                flat_mask = is_road | is_roller
                if flat_mask.any():
                    delta[flat_mask, 0] = (
                        sign_y[flat_mask]
                        * pen[flat_mask]
                        * inst.WHEEL_PUSH_MULT)
                    delta[flat_mask, 1] = 0.0
                inst.pos[mask] += delta[mask]

        if capture_iters and (it % 2 == 0 or it == inst.N_RELAX_ITERS - 1):
            snapshots.append((f'iter{it:02d}', inst.pos.copy()))

    inst._prev_dt = dt
    snapshots.append(('final', inst.pos.copy()))
    return snapshots


def pair_distances(pos):
    """Return adjacent pad-to-pad distances (closed loop)."""
    nxt = np.roll(pos, -1, axis=0)
    return np.linalg.norm(nxt - pos, axis=1)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print('Loading T30...')
    bones, radii, roles, seg_len, n_pads, inner_t = load_tank()
    print(f'  seg_len={seg_len:.4f}m  n_pads={n_pads}  '
          f'inner_t={inner_t:.4f}m')
    print(f'  road wheels L: {roles.get("road_wheels_L", [])}')

    gauge_x = gauge_for_side_L(bones, radii)
    side_x  = -0.5 * gauge_x

    # Build homie chain (also Z-flipped to renderer frame).
    pos3, tan3, hubs3, on_arc = track_homie.compute_homie_chain(
        bones, radii, roles, side='left',
        n_pads=n_pads, seg_len=seg_len, gauge_x=gauge_x)
    if pos3 is None:
        print('FAIL: homie returned None'); return
    pos3 = pos3.copy(); pos3[:, 2] *= -1.0
    print(f'homie pos3 shape: {pos3.shape}')

    # Build PBD inputs (hubs in PBD frame).
    hubs_yz, rs, names, kinds = build_pbd_inputs(
        bones, radii, roles, side='L')
    print(f'wheels: {len(names)}')
    for h, R, nm, k in zip(hubs_yz, rs, names, kinds):
        print(f'  {nm:20s} kind={k:8s} '
              f'Y={h[0]:+.3f}  Z={h[1]:+.3f}  R={R:.3f}')

    # Find the middle road wheel (most common compression target).
    road_idxs = [i for i, k in enumerate(kinds) if k == 'road']
    if not road_idxs:
        print('No road wheels on L'); return
    idx_road = road_idxs[len(road_idxs)//2]
    print(f'\nPerturbing road wheel: {names[idx_road]} '
          f'(idx {idx_road})')

    # v1.221.4 FIX: PBD wheel_radii = R + inner_t to match the
    # homie chain wrap radius.  Without this, tanks with
    # inner_t > 0 (T110E4 = 0.0615) have their homie chain sit
    # OUTSIDE the PBD wheel circles -- no binds, no push.
    rs_pbd = [r + inner_t for r in rs]
    rs_adj = rs_pbd     # alias

    # Re-run homie with inner_thickness so the seed lines up
    # with what production uses.
    pos3i, _, _, _ = track_homie.compute_homie_chain(
        bones, radii, roles, side='left',
        n_pads=n_pads, seg_len=seg_len, gauge_x=gauge_x)
    # Actually compute_homie_chain doesn't take inner_thickness.
    # The production path uses build_chain_segments +
    # assemble_chain_arrays with inner_thickness=inner_t.  Mirror:
    arcs_3i = track_homie.build_chain_segments(
        bones, radii, roles, side='left',
        n_pads=n_pads, seg_len=seg_len, inner_thickness=inner_t)
    if arcs_3i is None:
        print('FAIL: build_chain_segments returned None'); return
    pos3_inf, _, _, _ = track_homie.assemble_chain_arrays(
        arcs_3i, gauge_x, 'left', n_pads, s_offset=0.0)
    pos3_inf = pos3_inf.copy(); pos3_inf[:, 2] *= -1.0
    print(f'  homie pos3 (inner_t={inner_t:.4f}) shape: {pos3_inf.shape}')

    # Build PBD with BARE radii (matches production).
    inst = _pbd.TrackChainPBD(
        side_x=side_x, seg_len=seg_len, n_pads=n_pads,
        hubs_yz=hubs_yz, wheel_radii=rs_pbd,
        wheel_names=names, wheel_kinds=kinds)
    inst.seed_from_homie(pos3_inf)
    print(f'seeded; bound pads: {(inst._bound_wheel >= 0).sum()}/'
          f'{inst.n_pads}')

    # Perturb: push road wheel UP by a large amount so the chain
    # actually gets pushed inside the wheel (= the wheel-push
    # block fires).  Test multiple magnitudes.
    import argparse
    pa = argparse.ArgumentParser()
    pa.add_argument('--bump', type=float, default=0.20,
                    help='Road-wheel hub Y bump (m)')
    args, _unk = pa.parse_known_args()
    bump_dy = float(args.bump)
    print(f'\n=== Bumping {names[idx_road]} by +{bump_dy*100:.1f} cm ===')
    hubs_yz_up = perturb_road_wheel_up(hubs_yz, names, kinds,
                                         idx_road, bump_dy)
    inst.update_hubs(hubs_yz_up, rs_adj)

    # Record pre-step snapshot.
    pre = inst.pos.copy()
    pre_dists = pair_distances(pre)
    print(f'pre-step adjacent pad distances: '
          f'min={pre_dists.min():.4f}  '
          f'max={pre_dists.max():.4f}  '
          f'mean={pre_dists.mean():.4f}  (seg_len={seg_len:.4f})')

    # Step PBD, capturing per-iteration snapshots.
    snapshots = step_pbd(inst, 1.0 / 60.0, capture_iters=True)
    print(f'\ncaptured {len(snapshots)} snapshots through PBD step')

    # Analyse: per snapshot, find min adjacent distance and which
    # pair, and whether any pads are physically piled (< seg_len/3).
    print('\n=== per-snapshot adjacent-pad-distance analysis ===')
    print(f'{"step":<10s}  {"min_d":>8s}  {"max_d":>8s}  '
          f'{"mean":>8s}  {"piled":>7s}  {"worst pair":>20s}')
    perturbed_hub = np.array(hubs_yz_up[idx_road], dtype=np.float32)
    perturbed_R = rs_adj[idx_road]
    for label, p in snapshots:
        d = pair_distances(p)
        i_worst = int(np.argmin(d))
        piled = (d < seg_len * 0.5).sum()
        # Also: how many pads inside the perturbed wheel?
        diff_w = p - perturbed_hub
        dist_w = np.linalg.norm(diff_w, axis=1)
        inside_n = int((dist_w < perturbed_R).sum())
        worst_pair = f'{i_worst}-{i_worst+1} d={d[i_worst]:.4f}'
        print(f'{label:<10s}  {d.min():>8.4f}  {d.max():>8.4f}  '
              f'{d.mean():>8.4f}  {piled:>3d}/{n_pads:>3d}  '
              f'{worst_pair:>20s}  '
              f'inside_perturbed_wheel: {inside_n}')

    # Plot the chain in YZ at: pre, mid-iter, final.  PBD pos
    # is (N, 2) = (Y, Z); p[:, 0]=Y, p[:, 1]=Z.
    print('\nPlotting...')
    fig, axes = plt.subplots(3, 1, figsize=(11, 14),
                              sharex=True, sharey=True)
    sample = [('pre', pre)] + snapshots[1::3]
    sample = sample[:3] if len(sample) > 3 else sample
    for ax, (lbl, p) in zip(axes, sample):
        ax.set_aspect('equal')
        ax.set_title(f'PBD step -- {lbl}', fontsize=10)
        ax.set_xlabel('Z (m)')
        ax.set_ylabel('Y (m)')
        for i, (h, R, nm, k) in enumerate(zip(
                hubs_yz_up, rs_adj, names, kinds)):
            edge = 'red' if i == idx_road else 'black'
            c = Circle((h[1], h[0]), R, fill=False,
                       edgecolor=edge, linewidth=1.5)
            ax.add_patch(c)
            ax.text(h[1], h[0], nm[-3:], fontsize=6,
                    ha='center', va='center')
        ax.plot(p[:, 1], p[:, 0], '-', color='orange',
                linewidth=0.8, alpha=0.7)
        ax.scatter(p[:, 1], p[:, 0], s=8, c='blue', alpha=0.6)
        ax.grid(alpha=0.25)
    plt.tight_layout()
    out_path = os.path.join(OUT_DIR, 'diag_pbd_pileup_overview.png')
    plt.savefig(out_path, dpi=120, facecolor='white')
    plt.close()
    print(f'  wrote {out_path}')

    # Zoom plot at the perturbed wheel for each snapshot.
    fig, axes = plt.subplots(1, len(snapshots), figsize=(
        3 * len(snapshots), 5), sharey=True)
    if len(snapshots) == 1:
        axes = [axes]
    Z_c, Y_c = perturbed_hub[1], perturbed_hub[0]
    SPAN = 0.6
    for ax, (lbl, p) in zip(axes, snapshots):
        ax.set_aspect('equal')
        ax.set_title(lbl, fontsize=9)
        ax.set_xlim(Z_c - SPAN, Z_c + SPAN)
        ax.set_ylim(Y_c - SPAN, Y_c + SPAN)
        for i, (h, R, nm, k) in enumerate(zip(
                hubs_yz_up, rs_adj, names, kinds)):
            if abs(h[1] - Z_c) > SPAN * 1.5:
                continue
            edge = 'red' if i == idx_road else 'grey'
            c = Circle((h[1], h[0]), R, fill=False,
                       edgecolor=edge, linewidth=1.0)
            ax.add_patch(c)
        mask_z = np.abs(p[:, 1] - Z_c) < SPAN * 1.2
        ax.plot(p[mask_z, 1], p[mask_z, 0], '-',
                color='orange', linewidth=0.8, alpha=0.7)
        ax.scatter(p[mask_z, 1], p[mask_z, 0],
                   s=14, c='blue', alpha=0.7)
        d = pair_distances(p)
        piled_pairs = np.where(d < seg_len * 0.5)[0]
        for i_p in piled_pairs:
            j = (i_p + 1) % n_pads
            if abs(p[i_p, 1] - Z_c) > SPAN * 1.5: continue
            ax.plot([p[i_p, 1], p[j, 1]],
                    [p[i_p, 0], p[j, 0]],
                    'r-', linewidth=3, alpha=0.7)
        ax.tick_params(labelsize=7)
        ax.set_xlabel('Z (m)', fontsize=8)
    axes[0].set_ylabel('Y (m)', fontsize=8)
    plt.suptitle(
        f'PBD step zoom at perturbed road wheel '
        f'({names[idx_road]}, hub +5 cm).  Red segments = pads '
        f'< seg_len/2 apart (pile-up signal).')
    plt.tight_layout()
    out_path = os.path.join(OUT_DIR, 'diag_pbd_pileup_zoom.png')
    plt.savefig(out_path, dpi=120, facecolor='white')
    plt.close()
    print(f'  wrote {out_path}')


if __name__ == '__main__':
    main()
