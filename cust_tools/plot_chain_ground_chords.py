"""Plot the chord-based per-wheel ground-tangent algorithm.

For each frame in an F3 recording (v1.243+ with `ground_chords`
captured), draw:
  * Every wheel as a grey circle.
  * For each on-ground road wheel: the chord from first to last
    claimed pad (red), the tilted grab box (light red), and
    the per-pad up arrow (orange) for every claimed pad (blue
    filled).
  * Unclaimed pads = grey dots (handled by the non-road
    nearest-hub branch).

Usage:
    python cust_tools/plot_chain_ground_chords.py <path.json>
        [--frame N] [--side L|R|both] [--out <png>]
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


def _draw_side(ax, sd, side_label, frame_idx):
    arcs   = sd.get('chain_arcs') or []
    chords = sd.get('ground_chords') or []
    wheels_dict = {a['wheel_name']: a for a in arcs}

    # Hub locations + radii from chain_arcs.
    if not arcs:
        ax.text(0.5, 0.5, f'no chain_arcs for {side_label}',
                transform=ax.transAxes, ha='center')
        return

    # Reconstruct pad positions per wheel from chain_focus pads:
    # NOTE caller has to supply this via sd['wheels'][i]['pads']
    # plus the wheel's hub.  Use only on_arc pads (chain-touching).
    pad_positions = {}   # idx -> (z, y) in renderer frame
    for w in sd.get('wheels') or ():
        nm = w.get('wheel_name')
        hub = wheels_dict.get(nm)
        if hub is None: continue
        for pd in w.get('pads') or ():
            i = int(pd.get('idx') or 0)
            if i in pad_positions:
                continue
            ang = float(pd.get('angle_rad') or 0.0)
            pad_positions[i] = (
                hub['hub_z'] + hub['R'] * math.cos(ang),
                hub['hub_y'] + hub['R'] * math.sin(ang))

    all_z = [a['hub_z'] for a in arcs]
    all_y = [a['hub_y'] for a in arcs]
    all_r = [a['R']     for a in arcs]
    margin = max(all_r) + 0.4
    zmin, zmax = min(all_z) - margin, max(all_z) + margin
    ymin, ymax = min(all_y) - margin, max(all_y) + margin

    # Wheels.
    for a in arcs:
        ax.add_patch(Circle((a['hub_z'], a['hub_y']),
                              a['R'], fill=False,
                              color='#666', lw=1.0, alpha=0.5))
        ax.text(a['hub_z'], a['hub_y'] + a['R'] + 0.04,
                a['wheel_name'] or '?',
                ha='center', va='bottom', fontsize=7,
                color='#333')

    # Build the set of claimed-on-ground pad indices.
    claimed_idxs = set()
    for ch in chords:
        claimed_idxs.update(int(i) for i in (
            ch.get('pad_idxs') or ()))

    # Per Coffee 2026-05-17 ("i want the points of the terrain
    # drawn as captured in pad nodes"): plot the chain's view
    # of the ground.  Each on-ground pad's (Z, Y) IS a sample
    # of the terrain surface at that Z; connecting them in
    # chain index order gives the visible terrain polyline
    # under the tank.  Unclaimed pads (top run, sprocket /
    # idler wraps) plotted dim and small as a backdrop.
    ground_pts = sorted(
        (i, pad_positions[i]) for i in claimed_idxs
        if i in pad_positions)
    if ground_pts:
        gz = [p[1][0] for p in ground_pts]
        gy = [p[1][1] for p in ground_pts]
        # Terrain polyline -- bold black through the ground pads.
        ax.plot(gz, gy, color='#111111', lw=1.8, alpha=0.85,
                 zorder=4, label='terrain (pad polyline)')
        # Pad markers as terrain samples.
        ax.scatter(gz, gy, s=42, facecolor='#ffd000',
                    edgecolor='#222', linewidth=0.8,
                    zorder=6, label='ground pad nodes')
    # Unclaimed pads -- small grey backdrop.
    for i, (pz, py) in pad_positions.items():
        if i in claimed_idxs:
            continue
        ax.scatter([pz], [py], s=10, color='#aaaaaa',
                    alpha=0.5, zorder=3)

    # Chord + grab box + outward arrows.
    seg_L = None
    if arcs:
        # Per-arc segLen isn't in the diag; chain pitch unknown
        # here without meta -- skip grab-box rectangle width.
        pass

    for ch in chords:
        nm = ch['wheel_name']
        hub_z = ch['hub_z']
        hub_y = ch['hub_y']
        f_idx = ch['first_idx']; l_idx = ch['last_idx']
        if f_idx not in pad_positions or l_idx not in pad_positions:
            continue
        f_z, f_y = pad_positions[f_idx]
        l_z, l_y = pad_positions[l_idx]
        # Chord line (red).
        ax.plot([f_z, l_z], [f_y, l_y],
                color='#cc1122', lw=2.0, zorder=5)
        # Out arrows from each claimed pad.
        oz, oy = ch['out_z'], ch['out_y']
        scale = 0.10
        for i in (ch.get('pad_idxs') or ()):
            i = int(i)
            if i not in pad_positions:
                continue
            pz, py = pad_positions[i]
            ax.arrow(pz, py, oz * scale, oy * scale,
                      width=0.005, head_width=0.025,
                      color='#ee8800',
                      length_includes_head=True,
                      zorder=6)
        # Chord-angle annotation.
        ang_deg = math.degrees(ch.get('chord_angle_rad') or 0.0)
        ax.text(hub_z, hub_y, f"{ang_deg:+.1f}°\n{len(ch.get('pad_idxs') or ()):d}p",
                ha='center', va='center', fontsize=7,
                bbox=dict(facecolor='white', alpha=0.7,
                           edgecolor='none', pad=1),
                zorder=7)

    ax.set_xlim(zmin, zmax)
    ax.set_ylim(ymin, ymax)
    ax.set_aspect('equal')
    ax.set_xlabel('Z (m) renderer +Z forward')
    ax.set_ylabel('Y (m) world up')
    ax.grid(True, alpha=0.2)
    ax.set_title(
        f'{side_label}  frame {frame_idx}  '
        f'{len(chords)} on-ground road wheel(s)')


def _default_recording():
    import glob
    base = os.path.join(
        os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))),
        'test_runs')
    paths = glob.glob(os.path.join(
        base, 'manual_*_latest.json'))
    if not paths:
        return None
    return max(paths, key=os.path.getmtime)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('path', nargs='?', default=None,
                    help='F3 JSON path (defaults to most '
                          'recent manual_*_latest.json in '
                          'test_runs/).')
    ap.add_argument('--frame', type=int, default=0)
    ap.add_argument('--side', choices=('L', 'R', 'both'),
                    default='both')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()
    if args.path is None:
        guess = _default_recording()
        if guess is None:
            print('no recording found in test_runs/',
                  file=sys.stderr)
            return 2
        args.path = guess
        print(f'using {guess}')
    inp = os.path.abspath(args.path)
    d = json.load(open(inp, encoding='utf-8'))
    frames = d.get('frames') or []
    if not frames:
        print('no frames', file=sys.stderr); return 2
    idx = max(0, min(int(args.frame), len(frames) - 1))
    f = frames[idx]
    cf = f.get('chain_focus') or {}
    sides = ('L', 'R') if args.side == 'both' else (args.side,)
    fig, axes = plt.subplots(
        len(sides), 1, figsize=(13, 6 * len(sides)),
        squeeze=False)
    for i, st in enumerate(sides):
        _draw_side(axes[i, 0], cf.get(st) or {}, st, idx)
    meta = d.get('meta') or {}
    fig.suptitle(
        f"{meta.get('tank','?')}  tepy {meta.get('tepy_version','?')}  "
        f"rec {meta.get('timestamp','?')}",
        fontsize=11)
    plt.tight_layout()
    if args.out:
        out = os.path.abspath(args.out)
    else:
        plot_dir = os.path.join(os.path.dirname(inp), 'plots')
        os.makedirs(plot_dir, exist_ok=True)
        tank = meta.get('tank', '?')
        out = os.path.join(plot_dir,
                            f'{tank}_chord_algo_frame{idx}.png')
    plt.savefig(out, dpi=120)
    print(f'wrote {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
