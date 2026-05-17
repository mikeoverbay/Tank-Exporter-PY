"""Scan an F3 recording for chain-angle FLIPS around the focus wheel.

Per Coffee 2026-05-16 ("i think the trick is to watch for when the
angles on the wheel points flips.. scan for that in the recording").

For each frame, compute the angle (atan2(dY, dZ)) of every focus-band
pad around its focus wheel hub.  Walk the angle sequence in chain
order; any pair where the angular advance has the WRONG SIGN (= the
chain reverses on the wheel rim) is a FLIP event.

Sign convention: a "normal" chain wrap around the bottom of a wheel
in the chain loop order should advance MONOTONICALLY (= all
increasing OR all decreasing) over the in-band pads.  A pair where
the SIGN of the angular advance differs from the dominant advance
direction across the rest of the pads is the flip.

Outputs:
  * Console summary: count of flipped frames, per-side breakdown.
  * Per-flip detail: frame index, side, pad pair, angular deltas,
    wheel state, residual, speed, yaw / pitch.
  * `math_images/scan_f3_chain_flips.png` -- timeline plot of flip
    events alongside speed / pitch / focus-wheel residual.

Usage::

    python cust_tools/scan_f3_chain_flips.py <recording.json>
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, 'math_images')


def angle_around_hub(p_y, p_z, hub_y, hub_z):
    return math.degrees(math.atan2(p_y - hub_y, p_z - hub_z))


def detect_flips(angles, indices):
    """Return list of (pair_i, delta_signed) entries where the chain
    walked the WRONG WAY on the wheel.

    Method: split `angles` into RUNS of CONSECUTIVE chain indices
    (= gaps in `indices` mark loop-wrap breaks; we don't treat
    those as flips).  Within each run, compute signed angle
    deltas, pick the dominant direction (sign of mean), and flag
    any delta whose sign differs from dom by more than 1deg.
    """
    if len(angles) < 3:
        return []
    # Build runs.
    runs = []
    cur = [0]
    for k in range(1, len(indices)):
        if indices[k] == indices[k-1] + 1:
            cur.append(k)
        else:
            runs.append(cur)
            cur = [k]
    runs.append(cur)
    flips = []
    for run in runs:
        if len(run) < 3:
            continue
        deltas = []
        for j in range(1, len(run)):
            d = angles[run[j]] - angles[run[j-1]]
            while d > 180.0: d -= 360.0
            while d <= -180.0: d += 360.0
            deltas.append(d)
        mean_d = np.mean(deltas)
        dominant = +1.0 if mean_d > 0 else -1.0
        for j, d in enumerate(deltas):
            if d * dominant < 0.0 and abs(d) > 1.0:
                # `run[j]` is the index in the ORIGINAL angles list
                # (= same index space the caller's `pads` list uses).
                flips.append((run[j], d))
    return flips


def main():
    pa = argparse.ArgumentParser()
    pa.add_argument('json_path')
    pa.add_argument('--detail-limit', type=int, default=20,
                    help='max per-flip detail lines to print')
    args = pa.parse_args()

    with open(args.json_path) as f:
        d = json.load(f)
    meta = d['meta']
    frames = d['frames']
    tank = meta.get('tank', 'unknown')
    n = len(frames)
    print(f'=== F3 chain-flip scan ===')
    print(f'tank: {tank}  frames: {n}  duration: {meta["duration_s"]:.2f} s')

    # Walk frames, collect flip events.
    flips_L = []   # list of (frame_idx, pair_i, delta, focus_state, residual)
    flips_R = []
    series_speed     = []
    series_pitch     = []
    series_residual_L = []   # focus-L wheel residual_y
    series_residual_R = []
    series_state_L   = []    # focus-L wheel state code
    series_state_R   = []
    for fi, fr in enumerate(frames):
        series_speed.append(fr['speed_mps'])
        series_pitch.append(fr['pitch_deg'])
        for side, store, state_store, res_store in (
                ('L', flips_L, series_state_L, series_residual_L),
                ('R', flips_R, series_state_R, series_residual_R)):
            cf_side = fr.get('chain_focus', {}).get(side, [])
            # Back-compat: older recordings stored a single dict
            # per side instead of a list.
            if isinstance(cf_side, dict):
                cf_side = [cf_side] if cf_side else []
            if not cf_side:
                state_store.append(0)
                res_store.append(0.0)
                continue
            # Telemetry series uses the MIDDLE road wheel.
            mid = cf_side[len(cf_side) // 2]
            focus_name_series = mid.get('wheel_name', '')
            fw_series = next((w for w in fr['wheels']
                              if w['side'] == side
                              and (w['name'].startswith(focus_name_series)
                                   or focus_name_series.startswith(
                                       w['name']))),
                             None)
            state_code_series = {
                'CONTACT': 1, 'HANGING': 2, 'OVER_COMP': 3
            }.get((fw_series or {}).get('state', ''), 0)
            state_store.append(state_code_series)
            res_store.append((fw_series or {}).get('residual_y', 0.0))
            # Scan EVERY road wheel for flip events.
            for cf in cf_side:
                hub_y, hub_z = cf['hub_chassis_yz']
                pads = sorted(cf['pads'], key=lambda p: p['idx'])
                if len(pads) < 3:
                    continue
                angs = [angle_around_hub(p['y'], p['z'], hub_y, hub_z)
                        for p in pads]
                idxs = [p['idx'] for p in pads]
                fl = detect_flips(angs, idxs)
                focus_name = cf['wheel_name']
                fw = next((w for w in fr['wheels']
                           if w['side'] == side
                           and (w['name'].startswith(focus_name)
                                or focus_name.startswith(w['name']))),
                          None)
                state = (fw['state'] if fw else '?')
                residual = (fw['residual_y'] if fw else 0.0)
                for (pair_i, dlt) in fl:
                    p_a = pads[pair_i]; p_b = pads[pair_i + 1]
                    store.append({
                        'frame':       fi,
                        'pair':        (p_a['idx'], p_b['idx']),
                        'angles':      (angs[pair_i], angs[pair_i + 1]),
                        'delta_deg':   dlt,
                        'state':       state,
                        'residual_y':  residual,
                        'speed_mps':   fr['speed_mps'],
                        'pitch_deg':   fr['pitch_deg'],
                        'yaw_deg':     fr['yaw_deg'],
                        'focus_wheel': focus_name,
                    })

    print(f'\n=== Flip totals ===')
    print(f'  L side: {len(flips_L)} flip events in {n} frames')
    print(f'  R side: {len(flips_R)} flip events in {n} frames')
    # Count UNIQUE frames with flips (a frame may have >1 flip pair).
    frames_with_L_flip = sorted({e['frame'] for e in flips_L})
    frames_with_R_flip = sorted({e['frame'] for e in flips_R})
    print(f'  L frames with at least 1 flip: {len(frames_with_L_flip)} '
          f'({100*len(frames_with_L_flip)/n:.1f}%)')
    print(f'  R frames with at least 1 flip: {len(frames_with_R_flip)} '
          f'({100*len(frames_with_R_flip)/n:.1f}%)')

    print(f'\n=== Flip event breakdown by focus-wheel state ===')
    from collections import Counter
    state_cnt_L = Counter(e['state'] for e in flips_L)
    state_cnt_R = Counter(e['state'] for e in flips_R)
    print(f'  L focus-wheel state when flipped: {dict(state_cnt_L)}')
    print(f'  R focus-wheel state when flipped: {dict(state_cnt_R)}')
    wheel_cnt_L = Counter(e['focus_wheel'] for e in flips_L)
    wheel_cnt_R = Counter(e['focus_wheel'] for e in flips_R)
    print(f'  L flips per wheel: {dict(wheel_cnt_L)}')
    print(f'  R flips per wheel: {dict(wheel_cnt_R)}')

    print(f'\n=== First {args.detail_limit} flip events (L) ===')
    for e in flips_L[:args.detail_limit]:
        print(f'  fr {e["frame"]:4d}  pair {e["pair"][0]:3d}-{e["pair"][1]:3d}  '
              f'angles {e["angles"][0]:+6.1f}->{e["angles"][1]:+6.1f}  '
              f'd_ang={e["delta_deg"]:+6.1f}deg  '
              f'state={e["state"]:9s}  res={e["residual_y"]:+.4f}  '
              f'speed={e["speed_mps"]:+.2f}  pitch={e["pitch_deg"]:+5.1f}')
    print(f'\n=== First {args.detail_limit} flip events (R) ===')
    for e in flips_R[:args.detail_limit]:
        print(f'  fr {e["frame"]:4d}  pair {e["pair"][0]:3d}-{e["pair"][1]:3d}  '
              f'angles {e["angles"][0]:+6.1f}->{e["angles"][1]:+6.1f}  '
              f'd_ang={e["delta_deg"]:+6.1f}deg  '
              f'state={e["state"]:9s}  res={e["residual_y"]:+.4f}  '
              f'speed={e["speed_mps"]:+.2f}  pitch={e["pitch_deg"]:+5.1f}')

    # Timeline plot.
    os.makedirs(OUT_DIR, exist_ok=True)
    fig, axes = plt.subplots(3, 1, figsize=(13, 7), sharex=True)
    t = np.arange(n)
    # Top: speed + flip markers.
    axes[0].plot(t, series_speed, color='royalblue', label='speed (m/s)')
    axes[0].set_ylabel('speed (m/s)')
    axes[0].grid(alpha=0.3)
    for e in flips_L:
        axes[0].axvline(e['frame'], color='red', alpha=0.18, lw=0.6)
    for e in flips_R:
        axes[0].axvline(e['frame'], color='orange', alpha=0.18, lw=0.6)
    axes[0].set_title(
        f'{tank} F3 recording -- chain-flip scan\n'
        f'red bars = L-side flip, orange bars = R-side flip')
    # Middle: pitch + chassis state.
    axes[1].plot(t, series_pitch, color='purple', label='pitch (deg)')
    axes[1].set_ylabel('pitch (deg)')
    axes[1].grid(alpha=0.3)
    # Bottom: focus-wheel residual + state.
    axes[2].plot(t, series_residual_L, color='darkred',
                  label='L focus residual_y')
    axes[2].plot(t, series_residual_R, color='darkorange',
                  label='R focus residual_y', alpha=0.7)
    axes[2].set_ylabel('residual_y (m)')
    axes[2].set_xlabel('frame')
    axes[2].axhline(0, color='gray', lw=0.5)
    axes[2].grid(alpha=0.3)
    axes[2].legend(fontsize=8)
    plt.tight_layout()
    out_path = os.path.join(OUT_DIR, 'scan_f3_chain_flips.png')
    plt.savefig(out_path, dpi=120, facecolor='white')
    plt.close()
    print(f'\nwrote {out_path}')


if __name__ == '__main__':
    main()
