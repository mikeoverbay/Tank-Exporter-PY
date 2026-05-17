"""Plot the homie chain + labeled wheels for any tank.

Per Coffee 2026-05-16 ("plot the GB44 spline and wheels labeled").

Usage::

    python cust_tools/plot_tank_chain.py GB44_Archer
    python cust_tools/plot_tank_chain.py A14_T30 --side R
    python cust_tools/plot_tank_chain.py --basename G102_Pz_III

Output: `math_images/tank_chain_<basename>_<side>.png`.

Renders one side (L by default) of the homie chain over the
wheel circles, with each wheel labeled and colored by role.
Pads are drawn as blue dots connected by an orange polyline.
Drive sprocket / idler / road / roller wheels distinguished by
fill color.
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
from matplotlib.patches import Circle

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tankExporterPy.loaders import PkgExtractor, VehicleXMLLoader
from tankExporterPy.track_spline import (
    parse_chassis_bone_world_positions,
)
from tankExporterPy import track_homie

OUT_DIR = os.path.join(ROOT, 'math_images')

# Nation tag (`tanks_index.txt`) -> pkg-folder nation name.
NATION_INDEX_TO_PKG = {
    'usa':      'american',
    'germany':  'german',
    'ussr':     'russian',
    'uk':       'british',
    'france':   'french',
    'china':    'china',
    'japan':    'japan',
    'czech':    'czech',
    'sweden':   'sweden',
    'italy':    'italian',
    'poland':   'poland',
}


def find_tank(basename):
    """Look up nation/tier from `tanks_index.txt`.  Returns
    `(nation_pkg_form, nation_xml_form, tier, friendly_name)`."""
    idx_path = os.path.join(ROOT, 'tanks_index.txt')
    with open(idx_path, encoding='utf-8') as f:
        for line in f:
            parts = line.rstrip('\n').split('\t')
            if len(parts) < 4:
                continue
            if parts[2] == basename:
                nation_xml = parts[0].lower()
                nation_pkg = NATION_INDEX_TO_PKG.get(
                    nation_xml, nation_xml)
                return (nation_pkg, nation_xml, parts[1], parts[3])
    return None


def load_chain(basename, side='left'):
    info = find_tank(basename)
    if info is None:
        raise SystemExit(f'{basename} not found in tanks_index.txt')
    nation_pkg, nation_xml, tier, friendly = info
    with open(os.path.join(ROOT, 'tankExporterPy.json')) as f:
        cfg = json.load(f)
    pd = cfg.get('pkg_dir', '').strip()
    pkg_dir = os.path.normpath(os.path.join(pd, '..', '..'))
    pe = PkgExtractor(pkg_dir,
                       lookup_xml=os.path.join(ROOT, 'TheItemList.xml'))
    xml_path = (f'scripts/item_defs/vehicles/{nation_xml}/'
                f'{basename}.xml')
    tmp = pe.extract(xml_path)
    if not tmp or not os.path.isfile(tmp):
        raise SystemExit(f'cannot extract {xml_path}')
    chassis_info = VehicleXMLLoader.parse_info(tmp, pkg_extractor=pe)
    ci = chassis_info.get('chassis', {})
    roles = ci.get('wheel_roles', {})
    radii = ci.get('wheel_radii', {})
    seg_len = float(ci.get('segmentLength') or 0.13)
    seg_count = int(ci.get('segmentsCount') or 100)
    has_seg2 = bool((ci.get('track_segment_models') or {}
                    ).get('segment2ModelLeft'))
    n_pads = (seg_count // 2) if has_seg2 else seg_count
    inner_t = float(ci.get('segmentsInnerThickness') or 0.0)

    vis_path = (f'vehicles/{nation_pkg}/{basename}/normal/lod0/'
                'Chassis.visual_processed')
    vis_local = pe.extract(vis_path)
    if not vis_local or not os.path.isfile(vis_local):
        raise SystemExit(f'cannot extract {vis_path}')
    bones = parse_chassis_bone_world_positions(vis_local)
    # Gauge
    side_token = 'L' if side.startswith('l') else 'R'
    xs = []
    for nm in radii:
        if side_token not in nm:
            continue
        b = bones.get(nm)
        if b is None:
            b = bones.get(nm + '_BlendBone')
        if b is None:
            continue
        xs.append(float(b[0]))
    xs.sort()
    gauge_x = abs(xs[len(xs) // 2]) * 2.0 if xs else 2.75
    # Run homie.  inner_t inflation matches production wrap.
    arcs_3 = track_homie.build_chain_segments(
        bones, radii, roles, side=side, n_pads=n_pads,
        seg_len=seg_len, inner_thickness=inner_t)
    if arcs_3 is None:
        raise SystemExit(
            f'build_chain_segments returned None  '
            f'(roles={list(roles.keys())}  '
            f'radii_keys={list(radii.keys())[:5]}...)')
    pos, tan, hubs, on_arc = track_homie.assemble_chain_arrays(
        arcs_3, gauge_x, side, n_pads, s_offset=0.0)
    return {
        'tank':        basename,
        'friendly':    friendly,
        'tier':        tier,
        'nation':      nation_pkg,
        'side':        side,
        'seg_len':     seg_len,
        'n_pads':      n_pads,
        'inner_t':     inner_t,
        'pos':         pos,
        'tan':         tan,
        'arcs_3':      arcs_3,
        'roles':       roles,
        'radii':       radii,
        'bones':       bones,
        'gauge_x':     gauge_x,
    }


ROLE_COLORS = {
    'sprocket': ('gold',         'darkgoldenrod'),
    'idler':    ('plum',         'purple'),
    'road':     ('lightblue',    'steelblue'),
    'roller':   ('lightsalmon',  'darkred'),
    'unknown':  ('lightgray',    'gray'),
}


def main():
    pa = argparse.ArgumentParser()
    pa.add_argument('basename', nargs='?',
                    help='tank basename, e.g. GB44_Archer')
    pa.add_argument('--basename', dest='basename_kw',
                    help='same, kw form')
    pa.add_argument('--side', default='left',
                    choices=['left', 'right'],
                    help='which side to plot')
    args = pa.parse_args()
    basename = args.basename or args.basename_kw
    if not basename:
        pa.error('missing tank basename')

    print(f'Loading {basename} ({args.side} side)...')
    out = load_chain(basename, args.side)
    print(f'  tier={out["tier"]} nation={out["nation"]}  '
          f'seg_len={out["seg_len"]:.4f}  n_pads={out["n_pads"]}  '
          f'inner_t={out["inner_t"]:+.4f}')
    roles = out['roles']
    side_token = 'L' if args.side.startswith('l') else 'R'
    role_of = {}
    for r, nms in roles.items():
        if not r.endswith(f'_{side_token}'):
            continue
        role_kind = r.replace(f'_{side_token}', '').replace('s', '')
        # Above strip is too aggressive: roles are stored as
        # 'drive_sprockets_L', 'idlers_L', 'road_wheels_L',
        # 'return_rollers_L', 'ground_bones_L'.  Map explicitly.
    role_map_keys = (
        (f'drive_sprockets_{side_token}', 'sprocket'),
        (f'idlers_{side_token}',          'idler'),
        (f'road_wheels_{side_token}',     'road'),
        (f'return_rollers_{side_token}',  'roller'),
    )
    for role_key, role_kind in role_map_keys:
        for nm in roles.get(role_key, []):
            role_of[nm] = role_kind

    bones = out['bones']
    radii = out['radii']
    # Z-flip native -> renderer for the plot (so +Z is forward,
    # same as the user sees in the viewer).
    fig, ax = plt.subplots(figsize=(13, 7))
    ax.set_aspect('equal')
    # Find render bounds.
    all_xs = []
    all_ys = []
    wheel_entries = []
    for nm, kind in role_of.items():
        b = bones.get(nm)
        if b is None:
            b = bones.get(nm + '_BlendBone')
        if b is None:
            continue
        R = radii.get(nm)
        if R is None:
            R = radii.get(nm.replace('_R', '_L', 1))
        if R is None:
            continue
        z_rend = -float(b[2])   # render +Z forward
        y      = float(b[1])
        all_xs.extend([z_rend - R, z_rend + R])
        all_ys.extend([y - R, y + R])
        wheel_entries.append((nm, kind, z_rend, y, R))
    pos = out['pos'].copy()
    pos[:, 2] *= -1.0   # render Z-flip to match wheel coords
    all_xs.extend(pos[:, 2].tolist())
    all_ys.extend(pos[:, 1].tolist())
    pad_xs = pos[:, 2]
    pad_ys = pos[:, 1]
    margin = 0.5
    ax.set_xlim(min(all_xs) - margin, max(all_xs) + margin)
    ax.set_ylim(min(all_ys) - margin, max(all_ys) + margin)
    # Wheels.
    for nm, kind, z_r, y, R in wheel_entries:
        face, edge = ROLE_COLORS.get(kind, ROLE_COLORS['unknown'])
        ax.add_patch(Circle((z_r, y), R, fill=True,
                             facecolor=face, edgecolor=edge,
                             linewidth=1.6, alpha=0.55))
        # Wheel label.
        short = nm.replace('_BlendBone', '')
        ax.annotate(
            short, (z_r, y), xytext=(0, 0),
            textcoords='offset points',
            ha='center', va='center', fontsize=8,
            fontweight='bold', color=edge)
    # Pads (line + dots).
    ax.plot(pad_xs, pad_ys, '-', color='orange', linewidth=1.0,
            alpha=0.85, zorder=4)
    ax.scatter(pad_xs, pad_ys, s=8, color='royalblue',
                zorder=5, alpha=0.7)
    # Mark a few pad indices for orientation.
    for k in (0, len(pad_xs) // 4, len(pad_xs) // 2,
              3 * len(pad_xs) // 4):
        ax.annotate(f'{k}',
                     (pad_xs[k], pad_ys[k]),
                     xytext=(4, 4),
                     textcoords='offset points',
                     fontsize=7, color='darkblue')
    # Annotate contact points on each wheel's arc.
    for entry in out['arcs_3']:
        w = entry['wheel']
        c_in = entry.get('contact_in')
        c_out = entry.get('contact_out')
        if c_in is not None:
            ax.plot(-c_in[0], c_in[1], 's', color='green',
                    markersize=5, zorder=6)
        if c_out is not None:
            ax.plot(-c_out[0], c_out[1], 's', color='red',
                    markersize=5, zorder=6)
    # Legend.
    handles = []
    for kind, (face, edge) in ROLE_COLORS.items():
        if kind == 'unknown':
            continue
        handles.append(plt.Line2D(
            [0], [0], marker='o', color='none',
            markerfacecolor=face, markeredgecolor=edge,
            markersize=10, label=kind))
    handles.append(plt.Line2D([0], [0], marker='o',
                               color='royalblue',
                               markersize=6, linestyle='-',
                               label='chain pad'))
    ax.legend(handles=handles, loc='lower left', fontsize=9,
              framealpha=0.85)
    ax.set_xlabel('Z (m) -- renderer +Z forward')
    ax.set_ylabel('Y (m) -- world up')
    ax.set_title(
        f'{basename}  ({out["friendly"]})  tier {out["tier"]}  '
        f'{args.side}-side chassis-local view\n'
        f'seg_len={out["seg_len"]:.4f} m   '
        f'n_pads={out["n_pads"]}   '
        f'inner_thickness={out["inner_t"]:+.4f} m   '
        f'gauge={out["gauge_x"]:.3f} m',
        fontsize=11)
    ax.grid(alpha=0.3)
    out_path = os.path.join(
        OUT_DIR,
        f'tank_chain_{basename}_{side_token}.png')
    os.makedirs(OUT_DIR, exist_ok=True)
    plt.tight_layout()
    plt.savefig(out_path, dpi=130, facecolor='white')
    plt.close()
    print(f'wrote {out_path}')


if __name__ == '__main__':
    main()
