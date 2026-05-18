"""Plot the wheel-vs-pad-vs-chain rotation discrepancy from an
F3 manual-record JSON.

Per Coffee 2026-05-16 ("can you plot that odd rotation over
time"): the three signals that SHOULD agree (wheel spin, chain
flow / R_pitch, pad arc motion) diverge by 2-5x and even flip
sign when the tank moves.  This tool plots all three over time
for one wheel so the disagreement is visible at a glance.

Usage:
    python cust_tools/plot_f3_wheel_rotation.py <path.json>
        [--wheel W_R0] [--out plot.png]

Reads schema v2 chain_focus payload.  Output PNG goes to
`<input_dir>/plots/<tank>_<wheel>.png` unless --out specified.
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


def _short_signed(d):
    while d >  math.pi: d -= 2.0 * math.pi
    while d <= -math.pi: d += 2.0 * math.pi
    return d


def extract(d, wheel_name):
    """Walk frames, return per-frame arrays for `wheel_name`."""
    side = 'L' if '_L' in wheel_name else 'R'
    rows = []
    for f in d['frames']:
        sd = f['chain_focus'].get(side)
        if not sd:
            continue
        match = None
        for w in sd['wheels']:
            if w['wheel_name'] == wheel_name:
                match = w
                break
        if match is None:
            continue
        arc_pads = {p['idx']: p['angle_rad']
                    for p in match['pads']
                    if p.get('on_arc')}
        rows.append({
            't_s':       f['t_s'],
            'dt_s':      f['dt_s'],
            'speed_mps': f['speed_mps'],
            's_offset':  sd['s_offset_m'],
            'wheel_ang': match['wheel_angle_rad'],
            'arc_pads':  arc_pads,
        })
    return rows


def integrate_pad_arc(rows):
    """Cumulative arc travel for whichever pad happens to be on
    the arc in consecutive frames.  Pads enter / leave the arc;
    each handover stitches the cumulative trace together by
    short-arc-signed delta of any pad common to both frames."""
    cum = [0.0]
    valid = [bool(rows[0]['arc_pads'])]
    for i in range(1, len(rows)):
        prev_p = rows[i - 1]['arc_pads']
        cur_p  = rows[i    ]['arc_pads']
        common = set(prev_p) & set(cur_p)
        if not common:
            cum.append(cum[-1])
            valid.append(False)
            continue
        # Use median of common pads' deltas -- robust to one pad
        # straddling the arc boundary.
        deltas = [_short_signed(cur_p[k] - prev_p[k])
                  for k in common]
        cum.append(cum[-1] + float(np.median(deltas)))
        valid.append(True)
    return np.array(cum), np.array(valid)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('path', help='F3 JSON path.')
    ap.add_argument('--wheel', default='W_R0',
                    help='Wheel name to analyse (default W_R0).')
    ap.add_argument('--out', default=None,
                    help='Output PNG path.')
    args = ap.parse_args()

    inp = os.path.abspath(args.path)
    d = json.load(open(inp, encoding='utf-8'))
    meta = d['meta']
    if int(meta.get('schema_version') or 0) < 2:
        print('warning: schema < 2; on_arc data absent, '
              'arc-pad track will be empty.', file=sys.stderr)

    inner_t = float(meta.get('segmentsInnerThickness') or 0.0)
    R = float(meta.get('wheel_radii', {}).get(args.wheel) or 0.0)
    if R <= 0.0:
        print(f'wheel {args.wheel} not in meta.wheel_radii',
              file=sys.stderr)
        return 2
    R_pitch = R + inner_t

    rows = extract(d, args.wheel)
    if not rows:
        print(f'no frames mention {args.wheel}', file=sys.stderr)
        return 2

    t       = np.array([r['t_s']       for r in rows])
    speed   = np.array([r['speed_mps'] for r in rows])
    s_off   = np.array([r['s_offset']  for r in rows])
    wheel_a = np.array([r['wheel_ang'] for r in rows])

    # Baseline everything at t=0.
    wheel_rel = wheel_a - wheel_a[0]
    # s_offset converted to angular travel via R_pitch.  Negate
    # so a forward-driving tank's s_offset increase shows as a
    # negative angle (same sign convention as wheel spin).
    s_ang_rel = -(s_off - s_off[0]) / R_pitch
    # Pad arc cumulative -- starts at 0 by construction.
    pad_cum, pad_valid = integrate_pad_arc(rows)

    # ----- Plot -----
    tank = meta.get('tank', '?')
    ver  = meta.get('tepy_version', '?')
    fig, (ax_cum, ax_rate) = plt.subplots(
        2, 1, figsize=(11, 8), sharex=True)

    ax_cum.plot(t, wheel_rel, color='#cc3333',
                label='wheel_angle_rad (tp.wheel_angles_rad)',
                lw=1.6)
    ax_cum.plot(t, s_ang_rel, color='#cc8800',
                label=f'-(s_offset - s_offset[0]) / R_pitch  '
                      f'(R_pitch={R_pitch:.4f})',
                lw=1.6, alpha=0.8)
    # Plot pad arc only where valid; break the line where it isn't.
    pad_plot = pad_cum.copy()
    pad_plot[~pad_valid] = np.nan
    ax_cum.plot(t, pad_plot, color='#2266cc',
                label='pad arc travel around hub  '
                      '(integrated from on_arc pads)',
                lw=1.8)

    # Right axis: speed_mps as context.
    ax_speed = ax_cum.twinx()
    ax_speed.plot(t, speed, color='#888888', lw=0.9,
                  alpha=0.6, label='speed_mps')
    ax_speed.set_ylabel('speed_mps', color='#888888')
    ax_speed.tick_params(axis='y', colors='#888888')

    ax_cum.set_ylabel('cumulative rotation (rad)')
    ts = meta.get('timestamp', '?')
    ax_cum.set_title(
        f'{tank}  {args.wheel}  cumulative rotation '
        f'(rec {ts}, tepy {ver}, '
        f'{len(rows)} frames, R={R} m, inner_t={inner_t})')
    ax_cum.grid(True, alpha=0.3)
    h1, l1 = ax_cum.get_legend_handles_labels()
    h2, l2 = ax_speed.get_legend_handles_labels()
    ax_cum.legend(h1 + h2, l1 + l2, loc='upper left', fontsize=9)

    # ----- Rate panel -----
    dt = np.diff(t)
    dt = np.where(dt > 1e-6, dt, 1e-6)
    rate_wheel = np.diff(wheel_rel) / dt
    rate_sang  = np.diff(s_ang_rel) / dt
    rate_pad   = np.diff(pad_cum)   / dt
    rate_pad_valid = pad_valid[1:] & pad_valid[:-1]
    rate_pad[~rate_pad_valid] = np.nan
    t_mid = 0.5 * (t[1:] + t[:-1])

    ax_rate.plot(t_mid, rate_wheel, color='#cc3333',
                  label='d(wheel_angle)/dt', lw=1.2)
    ax_rate.plot(t_mid, rate_sang,  color='#cc8800',
                  label='-d(s_offset)/dt / R_pitch', lw=1.2,
                  alpha=0.7)
    ax_rate.plot(t_mid, rate_pad,   color='#2266cc',
                  label='d(pad_arc)/dt', lw=1.4)
    ax_rate.axhline(0.0, color='#444', lw=0.5)
    ax_rate.set_xlabel('time (s)')
    ax_rate.set_ylabel('angular rate (rad/s)')
    ax_rate.set_title('per-frame angular rate (signal '
                       'disagreement is the bug)')
    ax_rate.grid(True, alpha=0.3)
    ax_rate.legend(loc='upper left', fontsize=9)

    plt.tight_layout()

    if args.out:
        out = os.path.abspath(args.out)
    else:
        plot_dir = os.path.join(os.path.dirname(inp), 'plots')
        os.makedirs(plot_dir, exist_ok=True)
        out = os.path.join(plot_dir,
                            f'{tank}_{args.wheel}_rotation.png')
    plt.savefig(out, dpi=120)
    print(f'wrote {out}')
    print()
    print(f'  baseline wheel_angle: {wheel_a[0]:+.4f} rad')
    print(f'  baseline s_offset:    {s_off[0]:+.4f} m')
    print(f'  end wheel  d:         {wheel_rel[-1]:+.4f} rad')
    print(f'  end s_ang  d:         {s_ang_rel[-1]:+.4f} rad')
    print(f'  end pad arc d:        {pad_cum[-1]:+.4f} rad  '
          f'({np.sum(pad_valid)}/{len(pad_valid)} valid frames)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
