"""Plot one frame from an F3 recording.

Per Coffee 2026-05-16 ("F3 is bound to record... maybe other
info?" + chain_focus capture in v1.223.0).  Reads an F3 JSON
and renders one frame's chain_focus per side: focus wheel as
a circle, all pads in the band as labelled points, with the
sequence drawn so reversed segments are obvious.

Usage:
    python cust_tools/plot_f3_frame.py <json_path> [--frame N]

Output: math_images/f3_frame_<tank>_<N>.png
"""
import argparse
import json
import math
import os
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Circle

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, 'math_images')


def main():
    pa = argparse.ArgumentParser()
    pa.add_argument('json_path')
    pa.add_argument('--frame', type=int, default=0)
    args = pa.parse_args()

    with open(args.json_path) as f:
        d = json.load(f)
    meta = d['meta']
    frames = d['frames']
    if args.frame < 0:
        args.frame = len(frames) + args.frame
    if args.frame >= len(frames):
        print(f'frame {args.frame} out of range (have {len(frames)})')
        return
    fr = frames[args.frame]

    os.makedirs(OUT_DIR, exist_ok=True)
    tank = meta['tank']

    # State color map
    STATE_COL = {
        'CONTACT':   'green',
        'HANGING':   'cyan',
        'OVER_COMP': 'red',
        'NONE':      'gray',
    }

    fig, axes = plt.subplots(1, 2, figsize=(14, 7))

    for ax, side in zip(axes, ('L', 'R')):
        side_wheels = [w for w in fr['wheels'] if w['side'] == side]
        L = fr.get('chain_focus', {}).get(side, {})
        if not L:
            ax.set_title(f'{side}-side: no chain_focus data')
            continue
        hub_y, hub_z = L['hub_chassis_yz']
        R = L['hub_R_m']
        focus_name = L['wheel_name']
        ax.set_aspect('equal')
        # Camera-on-the-side view: X = Z (chassis), Y = Y (chassis).
        SPAN = 0.85
        ax.set_xlim(hub_z - SPAN, hub_z + SPAN)
        ax.set_ylim(hub_y - SPAN, hub_y + SPAN)
        ax.set_xlabel('Z (chassis-local, renderer +Z forward)')
        ax.set_ylabel('Y (chassis-local)')
        ax.set_title(
            f'{tank} -- {side}-side  frame {args.frame}  '
            f'yaw={fr["yaw_deg"]:+.0f}deg  '
            f'pitch={fr["pitch_deg"]:+.0f}deg  '
            f'speed={fr["speed_mps"]:+.2f} m/s\n'
            f'focus: {focus_name}  spin={L["wheel_angle_rad"]:+.2f} rad  '
            f's_off={L["s_offset_m"]:+.1f} m  '
            f'inner_t={L["inner_t_m"]:+.4f}',
            fontsize=10)
        # Draw all wheels on this side (using their residual_y to
        # approximate the hub Y -- the saved frame doesn't carry
        # the live hub directly, only local_xyz BIND + residual).
        # The focus wheel uses chain_focus hub which is the live
        # one.  Other wheels use a best-effort reconstruction.
        for w in side_wheels:
            lx, ly, lz = w['local_xyz']
            wy = ly + w['residual_y']
            # Native chassis-local -> renderer +Z forward: Z flip.
            wz_render = -lz
            col = STATE_COL.get(w['state'], 'gray')
            is_focus = (w['name'].startswith(focus_name)
                         or focus_name.startswith(w['name']))
            edge = 'magenta' if is_focus else col
            lw = 2.4 if is_focus else 1.0
            ax.add_patch(Circle(
                (wz_render, wy), max(0.05, R), fill=False,
                edgecolor=edge, linewidth=lw, alpha=0.7))
            ax.text(wz_render, wy,
                    w['name'].replace('_BlendBone', '').replace('W_', ''),
                    fontsize=7, ha='center', va='center', color=col)
        # Draw the chain pads in band, ordered by chain index.
        pads = sorted(L['pads'], key=lambda p: p['idx'])
        ys = [p['y'] for p in pads]
        zs = [p['z'] for p in pads]
        ax.plot(zs, ys, '-', color='orange', linewidth=1.0, alpha=0.7,
                zorder=5)
        for i, p in enumerate(pads):
            ax.plot(p['z'], p['y'], 'o', color='blue', markersize=6,
                    zorder=10)
            ax.annotate(f'{p["idx"]}', (p['z'], p['y']),
                         xytext=(6, 6), textcoords='offset points',
                         fontsize=7, color='darkblue')
            # Detect reversed segment (relative to chain index).
            if i > 0:
                dz = pads[i]['z'] - pads[i-1]['z']
                # A "reversed" pad is one whose Z went backwards
                # MORE than the chain ought to wrap.  Mark with
                # red arrow.
                ax.annotate(
                    '', xy=(p['z'], p['y']),
                    xytext=(pads[i-1]['z'], pads[i-1]['y']),
                    arrowprops=dict(
                        arrowstyle='->',
                        color='red' if dz < -0.001 else 'orange',
                        lw=1.5,
                        alpha=0.7),
                    zorder=8)
        # Legend with state codes.
        legend_lines = []
        for sk, sc in [('CONTACT', 'green'), ('HANGING', 'cyan'),
                       ('OVER_COMP', 'red'), ('focus', 'magenta')]:
            legend_lines.append(
                plt.Line2D([0], [0], color=sc, lw=2.0, label=sk))
        ax.legend(handles=legend_lines, loc='upper right',
                  fontsize=7)
        ax.grid(alpha=0.3)

    plt.tight_layout()
    out_path = os.path.join(
        OUT_DIR,
        f'f3_frame_{tank}_{args.frame:04d}.png')
    plt.savefig(out_path, dpi=120, facecolor='white')
    plt.close()
    print(f'wrote {out_path}')


if __name__ == '__main__':
    main()
