"""Plot the homie chain's per-wheel arc construction with
direction sanity-check annotations.

For each frame in an F3 recording (schema v2 with `chain_arcs`
captured per side), plots:
  * Each wheel as a circle at its hub (chassis-renderer Z, Y).
  * The short-arc wrap between a_in and a_out.
  * An arrow at a_in showing the chain's entering line direction.
  * An arrow at a_in showing the arc's tangent in the current
    walk direction.
  * Color of the arc: green if `dot(line_in, arc_tan) > 0`
    (chain flow consistent with walk direction), red if `< 0`
    (would be flipped under the "wrap > 180" check).
  * Per-wheel text label: name, wrap_deg, direction, dot.

Per Coffee 2026-05-17 ("plot it"): the v1.240.0 dot-check was
shipped without verifying signs against a concrete case.  This
plot lets us SEE which wheels would (correctly or wrongly)
flip under that check before re-wiring it.

Usage:
    python cust_tools/plot_chain_arc_directions.py <path.json>
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
from matplotlib.patches import Arc, Circle


def _draw_side(ax, arcs, side_label, frame_idx, pads=None):
    if not arcs:
        ax.text(0.5, 0.5, f'no chain_arcs for {side_label}',
                transform=ax.transAxes, ha='center',
                color='#cc3333')
        return
    # World bounds.
    all_hubs_z = [a['hub_z'] for a in arcs]
    all_hubs_y = [a['hub_y'] for a in arcs]
    all_R      = [a['R']     for a in arcs]
    margin     = max(all_R) + 0.4
    zmin, zmax = min(all_hubs_z) - margin, max(all_hubs_z) + margin
    ymin, ymax = min(all_hubs_y) - margin, max(all_hubs_y) + margin

    # Wheel circles.
    for a in arcs:
        circ = Circle((a['hub_z'], a['hub_y']),
                      a['R'], fill=False, color='#888',
                      lw=1.0, alpha=0.6)
        ax.add_patch(circ)
        ax.text(a['hub_z'], a['hub_y'] + a['R'] + 0.04,
                a['wheel_name'] or '?',
                ha='center', va='bottom', fontsize=7,
                color='#222')

    # Pads if available -- small dots so arcs read on top.
    if pads is not None and len(pads) > 0:
        pads = np.asarray(pads, dtype=float)
        ax.scatter(pads[:, 2], pads[:, 1], s=3,
                    color='#1a44aa', alpha=0.45, zorder=2)

    # Arc wrap + line/arc-tan arrows + annotation.
    for a in arcs:
        cz, cy = a['hub_z'], a['hub_y']
        R      = a['R']
        a_in   = a['a_in']
        a_out  = a['a_out']
        direc  = a['direction']
        dot    = a['dot_in']
        wrap   = a['wrap_short_deg']
        # Arcs in native polar -> renderer-Z requires a_in/a_out
        # to be flipped about the Y axis: (cos a, sin a) was in
        # native (Z, Y); renderer-Z = -native-Z so
        # renderer angle = pi - a.
        a_in_r  = math.pi - a_in
        a_out_r = math.pi - a_out
        # matplotlib Arc draws CCW from theta1 to theta2.
        if direc > 0:
            theta1, theta2 = math.degrees(a_in_r), math.degrees(a_out_r)
        else:
            theta1, theta2 = math.degrees(a_out_r), math.degrees(a_in_r)
        color = '#1c9c1c' if dot >= 0 else '#cc2222'
        arc = Arc((cz, cy), 2*R, 2*R, angle=0,
                  theta1=theta1 % 360.0,
                  theta2=theta2 % 360.0,
                  color=color, lw=2.5, zorder=4)
        ax.add_patch(arc)
        # Contact point at a_in (renderer frame).
        cin_z = cz + R * math.cos(a_in_r)
        cin_y = cy + R * math.sin(a_in_r)
        # Line-in arrow (chain entering at a_in).
        liz, liy = a['line_in_z'], a['line_in_y']
        scale = 0.18
        ax.arrow(cin_z - liz * scale, cin_y - liy * scale,
                  liz * scale * 0.95, liy * scale * 0.95,
                  width=0.005, head_width=0.025,
                  color='#0066cc', length_includes_head=True,
                  zorder=5)
        # Arc-tan arrow at a_in (in current walk direction).
        atz, aty = a['arc_tan_z'], a['arc_tan_y']
        ax.arrow(cin_z, cin_y,
                  atz * scale, aty * scale,
                  width=0.005, head_width=0.025,
                  color='#ff8800', length_includes_head=True,
                  zorder=5)
        # Annotation: name + dot.
        ax.text(cz, cy - 0.02,
                f"{wrap:.1f}°\nd={direc:+d}\ndot={dot:+.2f}",
                ha='center', va='center', fontsize=6.5,
                color='#000', alpha=0.85, zorder=6,
                bbox=dict(facecolor='white', alpha=0.6,
                           edgecolor='none', pad=1))

    ax.set_xlim(zmin, zmax)
    ax.set_ylim(ymin, ymax)
    ax.set_aspect('equal')
    ax.set_xlabel('Z (m) -- renderer +Z forward')
    ax.set_ylabel('Y (m) -- world up')
    ax.grid(True, alpha=0.2)
    n_red = sum(1 for a in arcs if a['dot_in'] < 0)
    ax.set_title(
        f'{side_label} side  frame {frame_idx}  '
        f'{len(arcs)} arcs, {n_red} would flip (red)')


def _default_recording():
    """Return the most-recently-modified manual_*_latest.json
    under C:/experiment/test_runs, or None if no match.
    """
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
    ap.add_argument('--frame', type=int, default=0,
                    help='Frame index to plot (default 0).')
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
        print('no frames', file=sys.stderr)
        return 2
    idx = max(0, min(int(args.frame), len(frames) - 1))
    f = frames[idx]
    cf = f.get('chain_focus') or {}

    sides = ('L', 'R') if args.side == 'both' else (args.side,)
    fig, axes = plt.subplots(
        len(sides), 1, figsize=(13, 6 * len(sides)),
        squeeze=False)
    for i, st in enumerate(sides):
        sd = cf.get(st) or {}
        arcs = sd.get('chain_arcs') or []
        # Optional: gather pad positions for this side.
        # v2 schema doesn't carry pad Y/Z directly -- pads have
        # only angle_rad around their hub.  Reconstruct pad YZ
        # in chassis-renderer frame from the per-wheel hub +
        # angle_rad.  Sample only arc pads; line pads have no
        # parent hub.
        pads_yz = []
        for w in sd.get('wheels') or ():
            nm = w.get('wheel_name')
            R_w = None
            hub_y = hub_z = None
            for a in arcs:
                if a.get('wheel_name') == nm:
                    R_w = a['R']
                    hub_y = a['hub_y']
                    hub_z = a['hub_z']
                    break
            if R_w is None:
                continue
            for pd in w.get('pads') or ():
                if not pd.get('on_arc'):
                    continue
                ang = float(pd.get('angle_rad') or 0.0)
                py  = hub_y + R_w * math.sin(ang)
                pz  = hub_z + R_w * math.cos(ang)
                pads_yz.append((0.0, py, pz))
        _draw_side(axes[i, 0], arcs, st, idx,
                    pads=pads_yz if pads_yz else None)

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
                            f'{tank}_chain_arcs_frame{idx}.png')
    plt.savefig(out, dpi=120)
    print(f'wrote {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
