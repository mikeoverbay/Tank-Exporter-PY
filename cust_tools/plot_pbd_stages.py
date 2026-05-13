"""Per-stage visualisation of the PBD chain solver on G78's drive
sprocket.  Produces one PNG per stage of the formula, in order,
zoomed on the drive wheel so the pad-by-pad action is readable.

Output: <root>/math_images/NN_pbd_*.png  (NN = 01..10 for sort).

Per Coffee 2026-05-13.  Math summarised E=mc^2-style; no
verbose derivations on the plots.  Geometry is exact to the
engine (loader -> _bone_yz -> compute_homie_chain -> Z-flip ->
PBD step), not a simulation.
"""
import json
import math
import os
import sys

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyArrowPatch
from matplotlib.collections import LineCollection

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from tankExporterPy.loaders import PkgExtractor, VehicleXMLLoader
from tankExporterPy.track_spline import (
    parse_chassis_bone_world_positions,
)
from tankExporterPy import track_homie

# ---- Config --------------------------------------------------------
TANK_PATH = ('vehicles/german/G78_Panther_M10/normal/'
             'lod0/Chassis.visual_processed')
TANK_XML  = ('scripts/item_defs/vehicles/germany/'
             'G78_Panther_M10.xml')
OUT_DIR   = 'C:/experiment/math_images'

# PBD tunings from track_chain_pbd.py
GRAVITY_SCALE  = 0.10
BIND_TOL       = 0.003
SKIP_K         = 8
BEND_STIFF     = 0.50
SEG_LEN_DT     = 1.0 / 60.0
# Style
SPROCKET_COLOR = '#d4af37'   # warm yellow
ROAD_COLOR     = '#5b86c2'   # blue-grey
IDLER_COLOR    = '#9b5b9b'   # muted purple
GRAVITY_COLOR  = '#3aa54a'   # green
BIND_COLOR     = '#d44a4a'   # red
BG_COLOR       = '#0a0a0a'
PAD_COLOR_FREE = '#a8a8a8'
PAD_COLOR_BIND = '#f5d33a'


# ---- Data load -----------------------------------------------------
def load_data():
    with open('./tankExporterPy.json') as f:
        cfg = json.load(f)
    pe = PkgExtractor(wot_root='C:/Games/World_of_Tanks_NA',
                      pkg_dir=cfg.get('pkg_dir'))

    info = VehicleXMLLoader.parse_info(pe.extract(TANK_XML), pe)
    ch    = info.get('chassis', {})
    roles = ch.get('wheel_roles', {})
    radii = ch.get('wheel_radii', {})
    seg_len   = float(ch.get('segmentLength'))
    seg_count = int(ch.get('segmentsCount'))
    has_seg2  = bool((ch.get('track_segment_models') or {}).get(
                    'segment2ModelLeft'))
    n_pads = (seg_count // 2) if has_seg2 else seg_count

    visual_local = pe.extract(TANK_PATH)
    bones = parse_chassis_bone_world_positions(visual_local)

    # Build wheel data for left side in PBD-callsite order
    all_names = (roles.get('drive_sprockets_L', [])
               + roles.get('idlers_L', [])
               + roles.get('road_wheels_L', [])
               + roles.get('return_rollers_L', []))
    kind_for = {}
    for nm in roles.get('drive_sprockets_L', []): kind_for[nm] = 'sprocket'
    for nm in roles.get('idlers_L', []):          kind_for[nm] = 'idler'
    for nm in roles.get('road_wheels_L', []):     kind_for[nm] = 'road'
    for nm in roles.get('return_rollers_L', []):  kind_for[nm] = 'roller'

    wheels = []   # in PBD source order: each entry (name, kind, R, Y, Z_flipped)
    for nm in all_names:
        b = bones.get(nm)
        if b is None:
            b = bones.get(nm + '_BlendBone')
        if b is None: continue
        R = radii.get(nm)
        if R is None: continue
        wheels.append({
            'name': nm,
            'kind': kind_for.get(nm, '?'),
            'R':    float(R),
            'Y':    float(b[1]),
            'Z':    -float(b[2]),   # renderer +Z-forward flip
        })

    # Compute homie chain in native frame, then Z-flip to renderer
    l_xs = []
    for nm in radii:
        if 'L' not in nm: continue
        b = bones.get(nm)
        if b is None:
            b = bones.get(nm + '_BlendBone')
        if b is None: continue
        l_xs.append(float(b[0]))
    l_xs.sort()
    gauge_x = abs(l_xs[len(l_xs)//2]) * 2.0 if l_xs else 2.75

    pos, tan, hub_anch, on_arc = track_homie.compute_homie_chain(
        bones, radii, roles, side='left',
        n_pads=n_pads, seg_len=seg_len, gauge_x=gauge_x)
    if pos is None:
        raise SystemExit('homie returned None')

    pos = pos.copy()
    pos[:, 2] *= -1.0
    tan = tan.copy()
    tan[:, 2] *= -1.0

    return wheels, pos, seg_len, n_pads


# ---- Loop order from track_homie._order_loop -----------------------
def loop_order(wheels):
    """Mirror track_homie._order_loop with the post-flip hubs."""
    road_radii = sorted(w['R'] for w in wheels if w['kind'] == 'road')
    road_R = road_radii[len(road_radii)//2] if road_radii else 0.0
    R_min = 0.5 * road_R
    bottom = [w for w in wheels
              if w['kind'] in ('sprocket', 'idler', 'road')
              and (w['kind'] == 'road' or w['R'] >= R_min)]
    top    = [w for w in wheels
              if w['kind'] == 'roller'
              and w['R'] >= R_min * 0.4]
    bottom.sort(key=lambda w: w['Z'], reverse=True)
    top.sort   (key=lambda w: w['Z'])
    if top and bottom:
        bottom.append(top.pop(0))
    return bottom + top


# ---- Seed binding (PBD seed_from_homie) ----------------------------
def seed_bound(wheels, pos):
    """Return _bound_wheel array (N,) -- index into `wheels` per pad."""
    n = len(pos)
    pos_yz = np.column_stack([pos[:, 1], pos[:, 2]])
    hubs = np.array([[w['Y'], w['Z']] for w in wheels], dtype=np.float32)
    radii = np.array([w['R'] for w in wheels], dtype=np.float32)
    diff = pos_yz[:, None, :] - hubs[None, :, :]
    r    = np.linalg.norm(diff, axis=2)
    slack = r - radii[None, :]
    w_idx = np.argmin(slack, axis=1)
    min_slack = slack[np.arange(n), w_idx]
    allowed = np.array([w['kind'] in ('sprocket', 'idler')
                        for w in wheels], dtype=bool)
    kind_ok = allowed[w_idx]
    out = np.where((min_slack < BIND_TOL) & kind_ok,
                   w_idx, -1).astype(np.int32)
    return out, hubs, radii


# ---- Drawing helpers -----------------------------------------------
def setup_axes(zoom_to=None, title='', math_label=''):
    fig, ax = plt.subplots(figsize=(11, 7), facecolor=BG_COLOR)
    ax.set_facecolor(BG_COLOR)
    ax.set_aspect('equal')
    ax.tick_params(colors='#888')
    for spine in ax.spines.values():
        spine.set_color('#222')
    ax.set_xlabel('Z (m)  -- renderer +Z forward', color='#aaa')
    ax.set_ylabel('Y (m)  -- world up', color='#aaa')
    ax.set_title(title, color='#eee', fontsize=14, pad=12)
    if math_label:
        ax.text(0.985, 0.985, math_label,
                transform=ax.transAxes, ha='right', va='top',
                color='#f0f0a0', fontsize=12,
                family='monospace',
                bbox=dict(facecolor='#202020',
                          edgecolor='#444', pad=8))
    if zoom_to:
        z_c, y_c, span = zoom_to
        ax.set_xlim(z_c - span, z_c + span)
        ax.set_ylim(y_c - span * 7/11, y_c + span * 7/11)
    return fig, ax


def draw_wheels(ax, wheels, dim_others=False, focus_idx=None,
                show_labels=True):
    for i, w in enumerate(wheels):
        if w['kind'] == 'sprocket' and w['R'] > 0.30:
            col = SPROCKET_COLOR
        elif w['kind'] == 'sprocket':
            col = IDLER_COLOR
        elif w['kind'] == 'idler':
            col = IDLER_COLOR
        elif w['kind'] == 'road':
            col = ROAD_COLOR
        else:
            col = '#666'
        alpha = 0.95
        if dim_others and focus_idx is not None and i != focus_idx:
            alpha = 0.30
        c = Circle((w['Z'], w['Y']), w['R'],
                   fill=False, edgecolor=col,
                   linewidth=2.2 if alpha > 0.5 else 1.0,
                   alpha=alpha)
        ax.add_patch(c)
        ax.plot(w['Z'], w['Y'], 'o',
                color=col, markersize=5, alpha=alpha)
        if show_labels:
            ax.annotate(w['name'].replace('_BlendBone', ''),
                        (w['Z'], w['Y']),
                        xytext=(7, 7), textcoords='offset points',
                        color=col, fontsize=8, alpha=alpha)


def draw_pads(ax, pos, bound=None, focus_wheel_idx=None,
              show_all=True):
    """Plot every pad.  bound[i] = wheel idx the pad is bound to
    (or -1).  Colour bound pads gold, free pads grey."""
    n = len(pos)
    Z = pos[:, 2]
    Y = pos[:, 1]
    # Closed loop line
    ax.plot(np.append(Z, Z[0]), np.append(Y, Y[0]),
            color='#404040', linewidth=0.8, zorder=1)
    if bound is None:
        ax.scatter(Z, Y, s=16, c=PAD_COLOR_FREE,
                   zorder=2, edgecolors='none')
    else:
        is_bound = bound >= 0
        if focus_wheel_idx is not None:
            is_focus = bound == focus_wheel_idx
            ax.scatter(Z[~is_bound], Y[~is_bound], s=14,
                       c=PAD_COLOR_FREE, zorder=2,
                       edgecolors='none', alpha=0.55)
            ax.scatter(Z[is_bound & ~is_focus],
                       Y[is_bound & ~is_focus], s=18,
                       c='#88693a', zorder=3,
                       edgecolors='none', alpha=0.6)
            ax.scatter(Z[is_focus], Y[is_focus], s=42,
                       c=PAD_COLOR_BIND, zorder=4,
                       edgecolors='#000')
        else:
            ax.scatter(Z[~is_bound], Y[~is_bound], s=16,
                       c=PAD_COLOR_FREE, zorder=2,
                       edgecolors='none')
            ax.scatter(Z[is_bound], Y[is_bound], s=30,
                       c=PAD_COLOR_BIND, zorder=3,
                       edgecolors='#000', linewidths=0.5)


def save(fig, basename):
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, basename)
    fig.savefig(out, dpi=110, facecolor=fig.get_facecolor(),
                bbox_inches='tight')
    plt.close(fig)
    print(f'  -> {out}')


# ---- Stage drawings ------------------------------------------------
def stage_01_loop_order(wheels, pos, loop):
    fig, ax = setup_axes(title=('Stage 1  Wheel collection + '
                                'loop walk order'),
                          math_label=('order = bottom (Z desc) '
                                      '+ first_top + rest_top'))
    # Background: full chain very faint
    ax.plot(np.append(pos[:, 2], pos[0, 2]),
            np.append(pos[:, 1], pos[0, 1]),
            color='#202020', linewidth=0.8, zorder=1)
    draw_wheels(ax, wheels, show_labels=False)
    # Number the wheels in loop order
    for i, w in enumerate(loop):
        ax.annotate(f'{i}', (w['Z'], w['Y']),
                    xytext=(0, 0), textcoords='offset points',
                    ha='center', va='center',
                    color='#fff', fontsize=10, weight='bold',
                    bbox=dict(boxstyle='circle,pad=0.25',
                              facecolor='#222',
                              edgecolor='#f0f0a0',
                              linewidth=1.0))
    # Connect numbered wheels with arrows
    Z = [w['Z'] for w in loop]
    Y = [w['Y'] for w in loop]
    for i in range(len(loop) - 1):
        ax.annotate('',
                    xy=(Z[i+1], Y[i+1]),
                    xytext=(Z[i], Y[i]),
                    arrowprops=dict(arrowstyle='->',
                                    color='#888', lw=0.8,
                                    alpha=0.5))
    # closure
    ax.annotate('',
                xy=(Z[0], Y[0]),
                xytext=(Z[-1], Y[-1]),
                arrowprops=dict(arrowstyle='->',
                                color='#444', lw=0.8,
                                alpha=0.5,
                                connectionstyle='arc3,rad=0.3'))
    ax.set_xlim(-4, 4)
    ax.set_ylim(-0.6, 2.0)
    save(fig, '01_pbd_loop_order.png')


def stage_02_seed_chain(wheels, pos, loop, drive_idx):
    drive = wheels[drive_idx]
    fig, ax = setup_axes(
        zoom_to=(drive['Z'], drive['Y'], 0.9),
        title='Stage 2  Seed positions from homie chain',
        math_label='pos[i] = homie_chain[i]   (Z-flipped)')
    draw_wheels(ax, wheels, dim_others=True, focus_idx=drive_idx,
                show_labels=True)
    draw_pads(ax, pos)
    # Highlight drive wheel center
    ax.plot(drive['Z'], drive['Y'], '*',
            color=SPROCKET_COLOR, markersize=18, zorder=5)
    save(fig, '02_pbd_seed_chain.png')


def stage_03_seed_bind(wheels, pos, drive_idx):
    drive = wheels[drive_idx]
    bound, hubs, radii = seed_bound(wheels, pos)
    fig, ax = setup_axes(
        zoom_to=(drive['Z'], drive['Y'], 0.9),
        title=('Stage 3  Seed-time bind (kind-gated, '
               'tol = 3 mm)'),
        math_label=(' allowed = kind in {sprocket, idler}\n'
                    ' _bound_wheel[i] = j  '
                    'if min_slack[i,j] < 3mm & allowed[j]'))
    draw_wheels(ax, wheels, dim_others=True, focus_idx=drive_idx)
    draw_pads(ax, pos, bound, focus_wheel_idx=drive_idx)
    # Annotate drive-wheel bound pads
    bound_pads = np.where(bound == drive_idx)[0]
    for pi in bound_pads:
        ax.plot([drive['Z'], pos[pi, 2]],
                [drive['Y'], pos[pi, 1]],
                color=PAD_COLOR_BIND, linewidth=0.8, alpha=0.55,
                zorder=2)
    n_bound = int((bound == drive_idx).sum())
    ax.text(0.02, 0.02, f'{n_bound} pads bound to {drive["name"]}',
            transform=ax.transAxes, color=PAD_COLOR_BIND,
            fontsize=11, family='monospace')
    save(fig, '03_pbd_seed_bind.png')


def stage_04_verlet_gravity(wheels, pos, drive_idx):
    drive = wheels[drive_idx]
    fig, ax = setup_axes(
        zoom_to=(drive['Z'], drive['Y'], 0.9),
        title='Stage 4  Verlet predict (gravity bias)',
        math_label=(' v = pos - prev_pos\n'
                    ' pos += v + 0.5 g dt^2\n'
                    ' g  = R^T (0, -9.81, 0)  * 0.10'))
    draw_wheels(ax, wheels, dim_others=True, focus_idx=drive_idx)
    bound, _, _ = seed_bound(wheels, pos)
    draw_pads(ax, pos, bound, focus_wheel_idx=drive_idx)
    # Small downward arrows on every pad in the focus area
    drive_pads = []
    for i in range(len(pos)):
        dz = pos[i, 2] - drive['Z']
        dy = pos[i, 1] - drive['Y']
        if dz**2 + dy**2 < (drive['R'] * 2.2)**2:
            drive_pads.append(i)
    for i in drive_pads:
        ax.annotate('',
                    xy=(pos[i, 2], pos[i, 1] - 0.07),
                    xytext=(pos[i, 2], pos[i, 1]),
                    arrowprops=dict(arrowstyle='->',
                                    color=GRAVITY_COLOR, lw=1.0))
    # Big gravity vector legend
    ax.annotate('g (world)', xy=(drive['Z'] - 0.78,
                                  drive['Y'] - 0.5),
                xytext=(drive['Z'] - 0.78,
                        drive['Y'] - 0.1),
                arrowprops=dict(arrowstyle='->',
                                color=GRAVITY_COLOR, lw=2.5),
                color=GRAVITY_COLOR, fontsize=11, ha='center',
                weight='bold')
    save(fig, '04_pbd_verlet_gravity.png')


def stage_05_bind_gate(wheels, pos, drive_idx):
    drive = wheels[drive_idx]
    bound, hubs, radii = seed_bound(wheels, pos)
    fig, ax = setup_axes(
        zoom_to=(drive['Z'], drive['Y'], 0.9),
        title=('Stage 5  Gravity-aware bind gate '
               '(corr  dot  g > 0)'),
        math_label=(' d    = pos - hub\n'
                    ' corr = d (R/|d| - 1)\n'
                    ' if corr  dot  g  > 0  : pos = hub + R d/|d|'))
    draw_wheels(ax, wheels, dim_others=True, focus_idx=drive_idx)
    # Compute gate for drive-wheel bound pads (gravity = -Y in
    # chassis-local for level chassis)
    g_unit = np.array([-1.0, 0.0], dtype=np.float32)
    bound_to_drive = np.where(bound == drive_idx)[0]
    apply_pads = []
    skip_pads  = []
    for i in bound_to_drive:
        d = np.array([pos[i, 1] - drive['Y'],
                      pos[i, 2] - drive['Z']], dtype=np.float32)
        r = max(float(np.linalg.norm(d)), 1e-6)
        corr = d * (drive['R'] / r - 1.0)
        along = corr @ g_unit
        if along > 0.0:
            apply_pads.append(i)
        else:
            skip_pads.append(i)
    # Draw all pads grey first, then bound-drive ones in two colors
    draw_pads(ax, pos, bound, focus_wheel_idx=None)
    if apply_pads:
        ax.scatter(pos[apply_pads, 2], pos[apply_pads, 1],
                   s=60, c=BIND_COLOR, edgecolors='#fff',
                   linewidths=0.8, zorder=5, label='APPLY')
    if skip_pads:
        ax.scatter(pos[skip_pads, 2], pos[skip_pads, 1],
                   s=60, c='#5a5a5a', edgecolors='#fff',
                   linewidths=0.8, zorder=5, label='SKIP')
    # Draw gravity vector
    ax.annotate('', xy=(drive['Z'] + 0.65, drive['Y'] - 0.40),
                xytext=(drive['Z'] + 0.65, drive['Y'] - 0.05),
                arrowprops=dict(arrowstyle='->',
                                color=GRAVITY_COLOR, lw=2.5))
    ax.text(drive['Z'] + 0.66, drive['Y'] - 0.22, ' g',
            color=GRAVITY_COLOR, fontsize=11, weight='bold')
    # Draw correction arrows on a couple of pads
    for i in (apply_pads[:1] + skip_pads[:1]):
        d = np.array([pos[i, 2] - drive['Z'],
                      pos[i, 1] - drive['Y']])  # (Z, Y) for plot
        r = max(float(np.linalg.norm(d)), 1e-6)
        # corr direction in plot space (Z, Y)
        corr_plot = d * (drive['R'] / r - 1.0) * 4.0   # exaggerate
        ax.annotate('',
                    xy=(pos[i, 2] + corr_plot[0],
                        pos[i, 1] + corr_plot[1]),
                    xytext=(pos[i, 2], pos[i, 1]),
                    arrowprops=dict(arrowstyle='->',
                                    color='#fff', lw=1.4))
    ax.legend(loc='lower left', facecolor='#222',
              edgecolor='#444', labelcolor='#eee')
    ax.text(0.02, 0.95,
            f'apply={len(apply_pads)}  skip={len(skip_pads)}',
            transform=ax.transAxes, color='#eee',
            fontsize=10, family='monospace')
    save(fig, '05_pbd_bind_gate.png')


def stage_06_bending(wheels, pos, drive_idx):
    drive = wheels[drive_idx]
    bound, _, _ = seed_bound(wheels, pos)
    # Active bound (=apply set from stage 5)
    g_unit = np.array([-1.0, 0.0], dtype=np.float32)
    active = np.zeros(len(pos), dtype=bool)
    for i in range(len(pos)):
        w_idx = bound[i]
        if w_idx < 0: continue
        w = wheels[w_idx]
        d = np.array([pos[i, 1] - w['Y'],
                      pos[i, 2] - w['Z']], dtype=np.float32)
        r = max(float(np.linalg.norm(d)), 1e-6)
        corr = d * (w['R'] / r - 1.0)
        along = corr @ g_unit
        active[i] = along > 0.0
    free = ~active
    fig, ax = setup_axes(
        zoom_to=(drive['Z'], drive['Y'], 0.9),
        title='Stage 6  Bending (free pads -> midpoint of neighbours)',
        math_label=(' mid_i = 0.5 (prev_i + next_i)\n'
                    ' pos_i += 0.5 (mid_i - pos_i)   if free'))
    draw_wheels(ax, wheels, dim_others=True, focus_idx=drive_idx)
    draw_pads(ax, pos, bound, focus_wheel_idx=drive_idx)
    # Arrows on a sample of free drive-area pads
    sample = []
    for i in range(len(pos)):
        if not free[i]: continue
        dz = pos[i, 2] - drive['Z']
        dy = pos[i, 1] - drive['Y']
        if dz**2 + dy**2 < (drive['R'] * 1.8)**2:
            sample.append(i)
    sample = sample[:8]
    for i in sample:
        prev_i = (i - 1) % len(pos)
        next_i = (i + 1) % len(pos)
        mid = 0.5 * (pos[prev_i] + pos[next_i])
        delta = mid - pos[i]
        ax.annotate('',
                    xy=(pos[i, 2] + delta[2] * 0.5,
                        pos[i, 1] + delta[1] * 0.5),
                    xytext=(pos[i, 2], pos[i, 1]),
                    arrowprops=dict(arrowstyle='->',
                                    color='#5a9fd4', lw=1.4))
    save(fig, '06_pbd_bending.png')


def stage_07_distance(wheels, pos, drive_idx, seg_len):
    drive = wheels[drive_idx]
    bound, _, _ = seed_bound(wheels, pos)
    fig, ax = setup_axes(
        zoom_to=(drive['Z'], drive['Y'], 0.9),
        title=f'Stage 7  Distance constraint (rest = {seg_len:.3f} m)',
        math_label=(' d   = pos[i+1] - pos[i]\n'
                    ' err = |d| - L\n'
                    ' both ends move 0.5 err d/|d|'))
    draw_wheels(ax, wheels, dim_others=True, focus_idx=drive_idx)
    draw_pads(ax, pos, bound, focus_wheel_idx=drive_idx)
    # Draw the chain as connected segments with green = on-target,
    # red = off
    nearby = []
    for i in range(len(pos)):
        dz = pos[i, 2] - drive['Z']
        dy = pos[i, 1] - drive['Y']
        if dz**2 + dy**2 < (drive['R'] * 2.2)**2:
            nearby.append(i)
    for i in nearby:
        j = (i + 1) % len(pos)
        L = float(np.linalg.norm(pos[j] - pos[i]))
        err = abs(L - seg_len)
        col = '#3aa54a' if err < 0.005 else '#d44a4a'
        ax.plot([pos[i, 2], pos[j, 2]],
                [pos[i, 1], pos[j, 1]],
                color=col, linewidth=1.6, zorder=3)
    ax.text(0.02, 0.02, 'green: |d|≈L   red: needs correction',
            transform=ax.transAxes, color='#aaa',
            fontsize=10, family='monospace')
    save(fig, '07_pbd_distance.png')


def stage_08_skip_k(wheels, pos, drive_idx):
    drive = wheels[drive_idx]
    bound, _, _ = seed_bound(wheels, pos)
    fig, ax = setup_axes(
        zoom_to=(drive['Z'], drive['Y'], 1.2),
        title=f'Stage 8  Skip-K tensioner (K={SKIP_K})',
        math_label=(' rest_i = |seed[i+K] - seed[i]|    (frozen)\n'
                    ' pull (i, i+K) back toward rest at 0.30 stiff'))
    draw_wheels(ax, wheels, dim_others=True, focus_idx=drive_idx)
    draw_pads(ax, pos, bound, focus_wheel_idx=drive_idx)
    # Highlight a few drive-area skip-K cables
    for i in range(0, len(pos), 5):
        dz = pos[i, 2] - drive['Z']
        dy = pos[i, 1] - drive['Y']
        if dz**2 + dy**2 > (drive['R'] * 1.8)**2:
            continue
        j = (i + SKIP_K) % len(pos)
        ax.plot([pos[i, 2], pos[j, 2]],
                [pos[i, 1], pos[j, 1]],
                color='#d4a44a', linewidth=1.2, alpha=0.85,
                linestyle='--', zorder=3)
        ax.text(pos[i, 2], pos[i, 1] + 0.04, f'{i}',
                color='#d4a44a', fontsize=7, ha='center')
        ax.text(pos[j, 2], pos[j, 1] - 0.04, f'{i+SKIP_K}',
                color='#d4a44a', fontsize=7, ha='center')
    save(fig, '08_pbd_skip_k.png')


def stage_09_pushout(wheels, pos, drive_idx):
    drive = wheels[drive_idx]
    bound, _, _ = seed_bound(wheels, pos)
    # Synthesize "drifted" pads inside the rim to show push-out
    pos_drift = pos.copy()
    # Pick a couple of pads in the bottom-front region and push them
    # slightly INTO the drive wheel rim to demonstrate the unilateral
    # response.  Choose pads currently on the lower arc.
    drive_zone = []
    for i in range(len(pos)):
        d = np.array([pos[i, 1] - drive['Y'],
                      pos[i, 2] - drive['Z']])
        r = float(np.linalg.norm(d))
        if abs(r - drive['R']) < 0.06:
            drive_zone.append((i, r))
    # Drift the closest two by 4-5 cm INTO the rim
    drive_zone.sort(key=lambda t: t[1])
    drifted_idx = [t[0] for t in drive_zone[:3]]
    for i in drifted_idx:
        d = pos[i, [1, 2]] - np.array([drive['Y'], drive['Z']])
        r = float(np.linalg.norm(d))
        unit = d / max(r, 1e-6)
        # Move TOWARDS the hub by 5 cm (inside the rim)
        pos_drift[i, 1] -= unit[0] * 0.05
        pos_drift[i, 2] -= unit[1] * 0.05
    fig, ax = setup_axes(
        zoom_to=(drive['Z'], drive['Y'], 0.7),
        title=('Stage 9  Unilateral push-out '
               '(solid contact, free pads only)'),
        math_label=(' if |d| < R and pad is FREE:\n'
                    '   pos = hub + R d/|d|'))
    draw_wheels(ax, wheels, dim_others=True, focus_idx=drive_idx)
    draw_pads(ax, pos_drift, bound, focus_wheel_idx=drive_idx)
    for i in drifted_idx:
        # Arrow from drifted (inside) -> rim (outside)
        d = pos_drift[i, [1, 2]] - np.array([drive['Y'], drive['Z']])
        r = max(float(np.linalg.norm(d)), 1e-6)
        unit = d / r
        rim_y = drive['Y'] + unit[0] * drive['R']
        rim_z = drive['Z'] + unit[1] * drive['R']
        ax.annotate('',
                    xy=(rim_z, rim_y),
                    xytext=(pos_drift[i, 2], pos_drift[i, 1]),
                    arrowprops=dict(arrowstyle='->',
                                    color='#ff5050', lw=1.8))
    ax.text(0.02, 0.02,
            'red arrows: penetrating free pads pushed to rim',
            transform=ax.transAxes, color='#ff5050',
            fontsize=10, family='monospace')
    save(fig, '09_pbd_pushout.png')


def stage_10_converged(wheels, pos, drive_idx):
    drive = wheels[drive_idx]
    bound, _, _ = seed_bound(wheels, pos)
    fig, ax = setup_axes(
        zoom_to=(drive['Z'], drive['Y'], 0.9),
        title=('Stage 10  Converged after 14 relax iterations '
               '(seed = converged on uniform terrain)'),
        math_label=(' for _ in 14:\n'
                    '   active_bind / bend / dist / skipK / pushout'))
    draw_wheels(ax, wheels, dim_others=True, focus_idx=drive_idx)
    draw_pads(ax, pos, bound, focus_wheel_idx=drive_idx)
    # Show the chain bath in green
    drive_zone = []
    for i in range(len(pos)):
        d = np.array([pos[i, 1] - drive['Y'],
                      pos[i, 2] - drive['Z']])
        if float(np.linalg.norm(d)) < drive['R'] * 1.3:
            drive_zone.append(i)
    if drive_zone:
        drive_zone.sort()
        # Connect with thick line for visibility
        order = np.array(drive_zone)
        for k in range(len(order) - 1):
            i, j = order[k], order[k+1]
            if (j - i) > 5:   # avoid spanning the chain
                continue
            ax.plot([pos[i, 2], pos[j, 2]],
                    [pos[i, 1], pos[j, 1]],
                    color='#3aa54a', linewidth=2.0, zorder=2,
                    alpha=0.7)
    save(fig, '10_pbd_converged.png')


# ---- Entry point ---------------------------------------------------
def main():
    print('Loading G78 data...')
    wheels, pos, seg_len, n_pads = load_data()
    loop = loop_order(wheels)
    # Drive sprocket = WD_L0 in source order
    drive_idx = next(i for i, w in enumerate(wheels)
                     if w['name'] == 'WD_L0')
    print(f'wheels: {len(wheels)}   pads: {n_pads}   '
          f'drive idx: {drive_idx}  ({wheels[drive_idx]["name"]})')
    print('Rendering stages...')
    stage_01_loop_order(wheels, pos, loop)
    stage_02_seed_chain(wheels, pos, loop, drive_idx)
    stage_03_seed_bind(wheels, pos, drive_idx)
    stage_04_verlet_gravity(wheels, pos, drive_idx)
    stage_05_bind_gate(wheels, pos, drive_idx)
    stage_06_bending(wheels, pos, drive_idx)
    stage_07_distance(wheels, pos, drive_idx, seg_len)
    stage_08_skip_k(wheels, pos, drive_idx)
    stage_09_pushout(wheels, pos, drive_idx)
    stage_10_converged(wheels, pos, drive_idx)
    print(f'\nAll stages written to {OUT_DIR}/')


if __name__ == '__main__':
    main()
