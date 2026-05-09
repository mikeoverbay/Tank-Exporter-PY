"""Analyse a tank's track NURB spline pipeline end-to-end.

Runs `tankExporterPy.track_spline` against a real tank's pkg
data and reports every piece of the kinematic-bone-driven NURB
flow:

  - .track Collada parse (V_loc list)
  - chassis-frame conversion
  - .visual_processed bone hierarchy walk
  - V_loc -> bone binding (TrackBoneBinding.build)
  - Phase A1 spline (V_locs only) length + pad stats
  - Phase A2 spline (augmented with Track_L<i>) length +
    pad stats vs gameplay XML target
  - optional simulated wheel deflection to verify the
    bottom-run pads track suspension residuals
  - optional PNG side-view plot

Usage:
    python cust_tools/analyze_track_spline.py <tank>
    python cust_tools/analyze_track_spline.py <tank> --plot
    python cust_tools/analyze_track_spline.py <tank> \
            --bump <track_bone> <dy_m> [--bump <track_bone> <dy_m> ...]

Where <tank> is one of:
    A14_T30                      -- xml basename (auto-resolved)
    vehicles/american/A14_T30    -- full pkg path

Examples:
    python cust_tools/analyze_track_spline.py A14_T30
    python cust_tools/analyze_track_spline.py A14_T30 --plot
    python cust_tools/analyze_track_spline.py A14_T30 \
            --bump Track_L4 0.05 --plot

The --bump flag injects synthetic Y deflections into named
bottom-run bones BEFORE running the augmented-loop pass so you
can verify the spline deforms the right way without needing the
viewer running.
"""

import argparse
import json
import os
import sys

# Allow running from repo root without "pip install -e .".
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

import numpy as np

from tankExporterPy.loaders import PkgExtractor, VehicleXMLLoader
from tankExporterPy.track_spline import (
    TrackSplineLoader,
    parse_chassis_bone_world_positions,
    centripetal_catmull_rom_closed,
    resample_uniform,
)


# --------------------------------------------------------------------
# Tank resolver -- accept basename, full path, or xml path.
# --------------------------------------------------------------------

def resolve_vehicle_path(pe: PkgExtractor, arg: str) -> str:
    """Turn a user-supplied tank identifier into the canonical pkg
    base path `vehicles/<nation>/<basename>`.

    Inputs supported (in order of preference):
        - already a full pkg base path: `vehicles/american/A14_T30`
        - .xml path:                    `vehicles/usa/A14_T30.xml`
        - bare basename:                `A14_T30`

    For the bare basename path we scan the pkg lookup XML for a
    `vehicles/<nation>/<basename>/normal/lod0/Chassis.primitives_processed`
    entry, which uniquely identifies the nation folder.

    Args:
        pe:  The configured PkgExtractor (its lookup XML is the
             source of truth for valid paths).
        arg: User-supplied identifier.

    Returns:
        Canonical pkg base path string.

    Raises:
        ValueError: When no candidate path is found in the lookup.
    """
    a = arg.replace('\\', '/').strip().lstrip('/')
    if a.lower().endswith('.xml'):
        a = a[:-4]
    if a.lower().startswith('vehicles/'):
        return a

    # Bare basename: scan the pkg lookup for any entry of the form
    # `vehicles/*/<basename>/normal/lod0/Chassis.primitives_processed`.
    # The lookup dict on PkgExtractor is `_file_to_pkg` --
    # path -> pkg-archive-name; we just need the keys.
    target_suffix = (
        f'/{a}/normal/lod0/Chassis.primitives_processed').lower()
    lookup = getattr(pe, '_file_to_pkg', None)
    if lookup:
        for entry in lookup.keys():
            ent_lower = entry.lower()
            if ent_lower.endswith(target_suffix):
                parts = entry.split('/')
                if (len(parts) >= 3
                        and parts[0].lower() == 'vehicles'):
                    return '/'.join(parts[:3])

    raise ValueError(
        f'Could not resolve "{arg}" to a vehicle pkg path.  '
        f'Try the full form vehicles/<nation>/<basename>.')


# --------------------------------------------------------------------
# Reporting helpers.
# --------------------------------------------------------------------

def header(s: str) -> None:
    print()
    print('=' * 70)
    print(s)
    print('=' * 70)


def report_vlocs(side, label):
    print(f'  V_locs ({len(side.vloc_names)}):')
    for n, p in zip(side.vloc_names, side.vloc_positions):
        print(f'    {n:8s}  ({p[0]:+7.3f}, {p[1]:+7.3f}, {p[2]:+7.3f}) m')


def report_binding(side, label):
    b = side.binding
    if b is None:
        print(f'  no binding -- visual_processed bones missing')
        return
    print(f'  V_loc -> bound bone (nearest by 2-D Y/Z):')
    for vname, bn, off in b.vloc_to_bone:
        bn_str = bn or '<no bone>'
        print(f'    {vname:8s} -> {bn_str:20s}  '
              f'offset=({off[0]:+.3f}, {off[1]:+.3f}, {off[2]:+.3f}) m')
    ai = b.bottom_run_after_idx
    bi = b.bottom_run_before_idx
    chord = float(np.linalg.norm(
        side.vloc_positions[bi] - side.vloc_positions[ai]))
    print(f'  bottom-run gap: idx {ai} ({side.vloc_names[ai]}) -> '
          f'{bi} ({side.vloc_names[bi]})  chord={chord:.2f} m')
    print(f'  bottom-run bones to splice ({len(b.bottom_run_bones)}):')
    for n, p in b.bottom_run_bones:
        print(f'    {n:14s}  bind=({p[0]:+.3f}, {p[1]:+.3f}, {p[2]:+.3f}) m')


def report_spline(label, control_pts, n_pads, target_m=None):
    """Run CR + resample on `control_pts` and print length / spacing
    diagnostics.  `target_m` is the gameplay XML's
    `(segmentsCount/2) * segmentLength` if known."""
    if len(control_pts) < 4:
        print(f'  [{label}] only {len(control_pts)} control points -- '
              f'CR needs >= 4')
        return
    dense = centripetal_catmull_rom_closed(control_pts,
                                            samples_per_seg=256,
                                            alpha=0.5)
    pad_pos, pad_tan, total = resample_uniform(dense, n_pads)
    gaps = np.linalg.norm(
        np.diff(pad_pos, axis=0, append=pad_pos[:1]), axis=1)
    print(f'  [{label}] control={len(control_pts):2d}  '
          f'length={total:.4f} m  '
          f'pads={n_pads}  '
          f'spacing mean={gaps.mean():.4f}  '
          f'std={gaps.std():.5f}  '
          f'min={gaps.min():.4f}  max={gaps.max():.4f}')
    if target_m is not None:
        diff = total - target_m
        print(f'           vs target {target_m:.4f} m: '
              f'{diff:+.4f} m ({100 * diff / target_m:+.2f} %)')
    print(f'           Y range  [{pad_pos[:, 1].min():+.3f}, '
          f'{pad_pos[:, 1].max():+.3f}] m')
    return pad_pos, pad_tan, total


# --------------------------------------------------------------------
# Optional PNG side-view plot.
# --------------------------------------------------------------------

def render_side_view(out_path, sides_data, tank_label,
                      W=1600, H=720):
    """Render a YZ side-view PNG showing V_loc dots, augmented
    control loop, resampled pads, and ground reference.  Each
    side is colour-coded.

    Args:
        sides_data: list of dicts with keys
            'label' (str), 'vloc_pos' (N,3), 'control' (M,3),
            'pads' (P,3), 'colour' (rgb tuple).
        tank_label: header string.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print('[plot] Pillow not available -- skipping plot')
        return

    # Aggregate Y / Z extents across all sides for a unified frame.
    all_pts = []
    for s in sides_data:
        for arr in (s.get('vloc_pos'), s.get('control'), s.get('pads')):
            if arr is not None and len(arr):
                all_pts.append(arr[:, 1:3])  # Y, Z columns
    if not all_pts:
        print('[plot] no data')
        return
    pts = np.concatenate(all_pts, axis=0)
    ymin, ymax = float(pts[:, 0].min()), float(pts[:, 0].max())
    zmin, zmax = float(pts[:, 1].min()), float(pts[:, 1].max())
    py = (ymax - ymin) * 0.10
    pz = (zmax - zmin) * 0.05
    ymin -= py; ymax += py; zmin -= pz; zmax += pz

    HEAD, PAD_X, PAD_Y = 76, 60, 36
    avail_w = W - 2 * PAD_X
    avail_h = H - HEAD - 2 * PAD_Y
    scale = min(avail_w / max(zmax - zmin, 1e-6),
                avail_h / max(ymax - ymin, 1e-6))
    data_w = (zmax - zmin) * scale
    data_h = (ymax - ymin) * scale
    ox = PAD_X + (avail_w - data_w) * 0.5 - zmin * scale
    oy_b = HEAD + PAD_Y + (avail_h + data_h) * 0.5

    def proj(z, y):
        return (ox + z * scale, oy_b - (y - ymin) * scale)

    img = Image.new('RGB', (W, H), (24, 26, 30))
    drw = ImageDraw.Draw(img)

    # Fonts.
    try:
        f_lg = ImageFont.truetype('arial.ttf', 22)
        f_md = ImageFont.truetype('arial.ttf', 14)
        f_sm = ImageFont.truetype('arial.ttf', 11)
    except Exception:
        f_lg = ImageFont.load_default()
        f_md = f_lg
        f_sm = f_lg

    # Header.
    drw.rectangle([0, 0, W, HEAD], fill=(36, 40, 48))
    drw.text((PAD_X, 8), tank_label,
              fill=(235, 235, 235), font=f_lg)
    legend = '  '.join(s['label'] for s in sides_data)
    drw.text((PAD_X, 40), f'Side view (YZ).  {legend}',
              fill=(200, 200, 200), font=f_md)

    # Grid every 0.5 m.
    GRID = (60, 64, 72)
    import math
    zg = math.floor(zmin / 0.5) * 0.5
    while zg <= zmax + 0.5:
        drw.line([proj(zg, ymin), proj(zg, ymax)], fill=GRID, width=1)
        zg += 0.5
    yg = math.floor(ymin / 0.5) * 0.5
    while yg <= ymax + 0.5:
        drw.line([proj(zmin, yg), proj(zmax, yg)], fill=GRID, width=1)
        yg += 0.5

    # Y=0 ground reference -- bright line so you can see where the
    # bottom run should sit.
    drw.line([proj(zmin, 0.0), proj(zmax, 0.0)],
              fill=(140, 140, 140), width=2)
    drw.text((proj(zmin, 0.0)[0] + 4, proj(zmin, 0.0)[1] + 2),
              'Y = 0 (ground)', fill=(160, 160, 160), font=f_sm)

    # Per-side draw.
    for s in sides_data:
        col_smooth = s['colour']
        col_dim    = tuple(int(c * 0.6) for c in col_smooth)
        col_pad    = tuple(min(255, c + 40) for c in col_smooth)

        # Augmented control loop (thin, dim).
        ctrl = s.get('control')
        if ctrl is not None and len(ctrl) >= 2:
            for i in range(len(ctrl)):
                a = proj(ctrl[i][2], ctrl[i][1])
                b = proj(ctrl[(i + 1) % len(ctrl)][2],
                         ctrl[(i + 1) % len(ctrl)][1])
                drw.line([a, b], fill=col_dim, width=1)

        # Resampled pads (bright, thicker line strip).
        pads = s.get('pads')
        if pads is not None and len(pads) >= 2:
            for i in range(len(pads)):
                a = proj(pads[i][2], pads[i][1])
                b = proj(pads[(i + 1) % len(pads)][2],
                         pads[(i + 1) % len(pads)][1])
                drw.line([a, b], fill=col_smooth, width=2)
            for p in pads:
                px, py = proj(p[2], p[1])
                drw.ellipse([px - 1.5, py - 1.5, px + 1.5, py + 1.5],
                              fill=col_pad)

        # V_locs (yellow with white outline).
        vloc = s.get('vloc_pos')
        if vloc is not None:
            for p in vloc:
                px, py = proj(p[2], p[1])
                drw.ellipse([px - 4, py - 4, px + 4, py + 4],
                             fill=(255, 220, 90),
                             outline=(255, 255, 255))

    img.save(out_path)
    print(f'[plot] wrote {out_path}')


# --------------------------------------------------------------------
# Main.
# --------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(
        description='Analyse a tank track NURB spline.')
    p.add_argument('tank', help='Tank xml basename or full pkg path.')
    p.add_argument('--cfg', default='./tankExporterPy.json',
                   help='Path to tankExporterPy.json (for pkg_dir).')
    p.add_argument('--bump', action='append', nargs=2,
                   metavar=('BONE', 'DY'),
                   help='Inject Y deflection (m) into a Track_<side>'
                        '<i> bone before building the augmented loop.'
                        '  Repeatable: --bump Track_L4 0.05 '
                        '--bump Track_L5 -0.03')
    p.add_argument('--plot', action='store_true',
                   help='Write PNG side-view plot to '
                        '<basename>_track_spline_analysis.png.')
    p.add_argument('--n-pads', type=int, default=64,
                   help='Number of pads to resample (default 64).')
    args = p.parse_args(argv)

    if not os.path.exists(args.cfg):
        print(f'config not found: {args.cfg}')
        return 2
    with open(args.cfg, encoding='utf-8') as f:
        cfg = json.load(f)
    pd = (cfg.get('pkg_dir') or '').strip()
    pkg_dir = (os.path.normpath(os.path.join(pd, '..', '..'))
               if pd else None)
    pe = PkgExtractor(pkg_dir, lookup_xml='./TheItemList.xml')

    vehicle_path = resolve_vehicle_path(pe, args.tank)
    parts = vehicle_path.split('/')
    tank_basename = parts[-1] if parts else args.tank

    header(f'Tank: {vehicle_path}')

    # ---- Load splines + bones ------------------------------------
    L, R = TrackSplineLoader.from_pkg(pe, vehicle_path)
    if L is None and R is None:
        print('No .track files found for this tank -- aborting.')
        return 1
    visual_local = pe.extract(
        f'{vehicle_path}/normal/lod0/Chassis.visual_processed')
    chassis_bones = (parse_chassis_bone_world_positions(visual_local)
                     if visual_local else {})
    print(f'parsed chassis bones: {len(chassis_bones)} named nodes')

    if L is not None and chassis_bones:
        L.attach_binding(chassis_bones)
    if R is not None and chassis_bones:
        R.attach_binding(chassis_bones)

    # ---- Try to pull gameplay-XML target if present --------------
    target_per_side = None
    try:
        # Search the lookup for a vehicles/<nation>/<basename>.xml.
        nation = vehicle_path.split('/')[1]
        xml_local = pe.extract(f'{vehicle_path}.xml')
        if xml_local:
            info = VehicleXMLLoader.parse_info(xml_local, pe)
            ch = info.get('chassis', {}) if info else {}
            # We didn't yet pull <segmentLength> + <segmentsCount>
            # via parse_info; re-grep them straight off the chassis
            # element here so callers can ship without touching the
            # main loader.  Best-effort.
            import xml.etree.ElementTree as ET
            from tankExporterPy.common import decode_bwxml, is_bwxml
            with open(xml_local, 'rb') as f:
                raw = f.read()
            txt = (decode_bwxml(raw) if is_bwxml(raw)
                   else raw.decode('utf-8', errors='replace'))
            import re as _re
            txt = _re.sub(r'<xmlns:[^>]*>[^<]*</xmlns:[^>]*>', '', txt)
            r = ET.fromstring(txt)
            seg_len = None
            seg_cnt = None
            for c in r.iter('chassis'):
                for sl in c.iter('segmentLength'):
                    try:
                        seg_len = float(sl.text.strip())
                        break
                    except Exception:
                        pass
                for sc in c.iter('segmentsCount'):
                    try:
                        seg_cnt = int(sc.text.strip())
                        break
                    except Exception:
                        pass
                if seg_len and seg_cnt:
                    break
            if seg_len and seg_cnt:
                target_per_side = (seg_cnt / 2) * seg_len
                print(f'gameplay XML: segmentsCount={seg_cnt}  '
                      f'segmentLength={seg_len}  '
                      f'-> per-side target={target_per_side:.3f} m')
    except Exception as exc:
        print(f'[xml-target] skipped: {type(exc).__name__}: {exc}')

    # ---- Apply --bump deflections -------------------------------
    bumped_bones = dict(chassis_bones)
    if args.bump:
        print()
        print('--bump deflections applied:')
        for bn, dy in args.bump:
            try:
                dy_f = float(dy)
            except ValueError:
                print(f'  skip {bn}: not a number {dy!r}')
                continue
            base = bumped_bones.get(bn)
            if base is None:
                print(f'  skip {bn}: not found in chassis bones')
                continue
            bumped_bones[bn] = np.array(
                [base[0], base[1] + dy_f, base[2]],
                dtype=np.float64)
            print(f'  {bn}: Y {base[1]:+.3f} -> '
                  f'{bumped_bones[bn][1]:+.3f}  (dy={dy_f:+.3f})')

    # ---- Per-side analysis ---------------------------------------
    sides_data = []
    palette = [
        ('LEFT',  L, (255, 215,  60)),
        ('RIGHT', R, ( 80, 215, 255)),
    ]
    for label, side, colour in palette:
        if side is None:
            continue
        header(f'{label} side')
        report_vlocs(side, label)
        report_binding(side, label)

        # Phase A1 (V_locs only).
        report_spline('A1 (V_locs only)',
                      side.vloc_positions,
                      args.n_pads,
                      target_m=target_per_side)

        # Phase A2 (augmented).
        ctrl = side.build_augmented_control_loop(bumped_bones)
        result = report_spline('A2 (augmented)', ctrl, args.n_pads,
                                target_m=target_per_side)
        if result is not None:
            pad_pos, _pad_tan, _total = result
        else:
            pad_pos = None

        sides_data.append({
            'label':    label,
            'vloc_pos': side.vloc_positions,
            'control':  ctrl,
            'pads':     pad_pos,
            'colour':   colour,
        })

    # ---- Plot -----------------------------------------------------
    if args.plot:
        out = f'{tank_basename}_track_spline_analysis.png'
        bump_tag = ''
        if args.bump:
            bump_tag = '  bumps=' + ','.join(
                f'{bn}{float(dy):+g}' for bn, dy in args.bump)
        render_side_view(out, sides_data,
                          f'{vehicle_path}{bump_tag}')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
