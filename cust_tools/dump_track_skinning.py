"""Dump + visualise the track-skinning rig of a WoT chassis.

Walks a tank's chassis primitives + visual_processed to extract:

  * The renderSet bone palette for `track_<L|R>_Shape`.
  * The vertex stream's per-vertex bone INDEX BYTES + WEIGHTS (the
    `iii` / `ww` triplets from the SC_UBYTE4 layout).
  * The triangle index buffer.

For every vertex it converts the raw byte (`byte / 3` is the bone
INDEX into the renderSet's palette -- the SC_UBYTE4_REVERSE_PADDED
convention every Bigworld game uses) into a bone NAME, and emits
two artefacts side by side:

  1. A text dump grouped by dominant bone, sorted by (z, y) so you
     can read the track loop as a sequence of "first this wheel
     drives this stretch, then the next, ..." -- exactly the map
     you want for figuring out track sag.
  2. A 2-D PNG side view (z horizontal, y vertical) of the track
     mesh.  Every triangle is drawn as a thin grey edge.  Every
     vertex is drawn as a coloured dot, with one colour per
     dominant bone -- so the wheel-influence regions of the track
     loop pop visually.  The outer-X face (the LOOP that sags
     when something bends the track) is highlighted with thicker
     dots, and the bones are listed in a small legend.

Usage
-----
    python cust_tools/dump_track_skinning.py A83_T110E4
    python cust_tools/dump_track_skinning.py A83_T110E4 --side R
    python cust_tools/dump_track_skinning.py A83_T110E4 --out my_track.png

By default writes `<tag>_track_<side>_skinning.png` and a
`.txt` companion next to it in the current directory.
"""

import argparse
import os
import re
import struct
import sys
import tempfile
from collections import defaultdict

import numpy as np

# Allow `python cust_tools/dump_track_skinning.py` invocation:
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tankExporterPy.loaders import PkgExtractor, MeshParser
from tankExporterPy.common  import is_bwxml, decode_bwxml


# ---------------------------------------------------------------------------
# Visual-processed parser: pull bone palette per renderSet
# ---------------------------------------------------------------------------
def parse_renderset_bones(visual_xml_text):
    """Walk the (decoded) visual_processed XML text and return a dict
    `{geometry_base_name: [bone_name_in_palette_order, ...]}`.

    Geometry name = the `<vertices>` text, minus the `.vertices`
    suffix.  The bone palette is the list of `<node>` children
    that appear inside the same `<renderSet>` block, in
    declaration order -- vertex bone bytes index INTO this list
    (after the byte/3 decode).
    """
    out = {}
    in_rs = False
    nodes = []
    geom  = None
    lines = visual_xml_text.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        if ln == '<renderSet>':
            in_rs, nodes, geom = True, [], None
        elif ln == '</renderSet>':
            if geom and nodes:
                out[geom] = list(nodes)
            in_rs, nodes, geom = False, [], None
        elif in_rs and ln == '<node>':
            if i + 1 < len(lines):
                nm = lines[i + 1].strip()
                if nm and not nm.startswith('<'):
                    nodes.append(nm)
        elif in_rs and ln == '<vertices>' and i + 1 < len(lines):
            v = lines[i + 1].strip()
            if v.endswith('.vertices'):
                geom = v[:-len('.vertices')]
        i += 1
    return out


def pretty_xml_text(raw_bytes):
    """Decode a (possibly BWXML) bytes blob into newline-broken
    text suitable for line-based parsing.  Reuses the same recipe
    we use elsewhere in the project."""
    text = (decode_bwxml(raw_bytes) if is_bwxml(raw_bytes[:8])
            else raw_bytes.decode('utf-8', errors='replace'))
    text = re.sub(r'>(?!<)', r'>\n', text)
    text = re.sub(r'<', r'\n<', text)
    return '\n'.join(line.strip() for line in text.splitlines() if line.strip())


# ---------------------------------------------------------------------------
# Group helper -- pull the parsed dict for the named primitive group
# out of MeshParser.parse_primitives_processed's output.
# ---------------------------------------------------------------------------
def find_group(parsed, base_name):
    for g in parsed:
        if g.get('name') == base_name:
            return g
    return None


# ---------------------------------------------------------------------------
# Dominant-bone helper: pick the bone slot with the highest weight
# per vertex.  Returns (bone_idx_after_div3, weight, raw_byte).
# ---------------------------------------------------------------------------
def dominant_bone(bi_row, bw_row):
    """bi_row: 4 raw bytes (np.uint8).  bw_row: 4 weights (float32).

    Returns a tuple (bone_palette_idx, weight, raw_byte).  Falls
    back to bone 0 / weight 0 / byte 0 if every weight is zero
    (defensive -- shouldn't happen for skinned verts).
    """
    if bw_row.sum() <= 0.0:
        return 0, 0.0, int(bi_row[0])
    k = int(np.argmax(bw_row))
    return int(bi_row[k]) // 3, float(bw_row[k]), int(bi_row[k])


# ---------------------------------------------------------------------------
# PNG side-view rendering
# ---------------------------------------------------------------------------
def render_png(out_path, positions, indices, dom_bones, dom_weights,
               outer_mask, palette_names, title=''):
    """Render a 2D (z, y) side-view of the track:

      * thin grey lines for every triangle edge (mesh wireframe);
      * one coloured dot per vertex, colour = dominant-bone-index;
      * outer-X face vertices drawn larger so the load-bearing
        loop pops;
      * per-bone legend along the bottom listing
        `[idx]  bone_name` in the same colour as the dots.

    `positions` is (N, 3) float; we project to (z, y) -> screen.
    """
    from PIL import Image, ImageDraw, ImageFont

    W, H = 1600, 700
    LEG_H = 240
    img   = Image.new('RGB', (W, H + LEG_H), (24, 26, 30))
    drw   = ImageDraw.Draw(img)

    # Project (z, y) into screen-space.  Margin around the bbox so
    # the geometry doesn't run flush to the edge.  Y is inverted
    # because screen coords go down.
    z = positions[:, 2]
    y = positions[:, 1]
    z0, z1 = float(z.min()), float(z.max())
    y0, y1 = float(y.min()), float(y.max())
    pad = 60
    sx  = (W - 2 * pad) / max(z1 - z0, 1e-6)
    sy  = (H - 2 * pad) / max(y1 - y0, 1e-6)
    s   = min(sx, sy)
    cx  = pad + (W - 2 * pad - (z1 - z0) * s) * 0.5 - z0 * s
    cy  = H - pad - (-y0 * s)
    def project(zz, yy):
        return (cx + zz * s, cy - yy * s)
    sxy = np.zeros((len(positions), 2), dtype=np.float32)
    for i in range(len(positions)):
        sxy[i] = project(positions[i, 2], positions[i, 1])

    # ---- 1. Triangle edges (light grey) ----------------------------
    tris = indices.reshape(-1, 3)
    edge_set = set()
    for a, b, c in tris:
        for u, v in ((a, b), (b, c), (c, a)):
            edge_set.add((min(u, v), max(u, v)))
    grey = (60, 64, 70)
    for u, v in edge_set:
        drw.line([tuple(sxy[u]), tuple(sxy[v])], fill=grey, width=1)

    # ---- 2. Per-bone palette colours (HSV walk) --------------------
    n_bones = max(dom_bones.max() + 1, 1)
    colours = []
    for i in range(n_bones):
        # spaced hues, full saturation, value 0.85
        hue = (i * 0.61803398875) % 1.0   # golden ratio for spread
        # HSV -> RGB
        h6 = hue * 6.0
        c  = 0.85
        x_ = c * (1 - abs((h6 % 2) - 1))
        m  = 0.85 - c
        if   h6 < 1: r, g, b = c, x_, 0
        elif h6 < 2: r, g, b = x_, c, 0
        elif h6 < 3: r, g, b = 0, c, x_
        elif h6 < 4: r, g, b = 0, x_, c
        elif h6 < 5: r, g, b = x_, 0, c
        else:        r, g, b = c, 0, x_
        colours.append((int((r + m) * 255),
                        int((g + m) * 255),
                        int((b + m) * 255)))

    # ---- 3. Vertex dots -------------------------------------------
    for i in range(len(positions)):
        col = colours[dom_bones[i]]
        if outer_mask[i]:
            r = 4
            drw.ellipse([sxy[i][0] - r, sxy[i][1] - r,
                          sxy[i][0] + r, sxy[i][1] + r],
                         fill=col, outline=(255, 255, 255))
        else:
            r = 1
            drw.rectangle([sxy[i][0] - r, sxy[i][1] - r,
                            sxy[i][0] + r, sxy[i][1] + r],
                           fill=col)

    # ---- 4. Title + legend ----------------------------------------
    try:
        font = ImageFont.truetype('arial.ttf', 14)
        font_lg = ImageFont.truetype('arial.ttf', 18)
    except Exception:
        font = ImageFont.load_default()
        font_lg = font
    drw.text((pad, 20), title, fill=(220, 220, 220), font=font_lg)

    # Legend grid: 3 columns, ~5 rows depending on bone count
    LEG_X0 = pad
    LEG_Y0 = H + 20
    cols   = 3
    col_w  = (W - 2 * pad) // cols
    row_h  = 22
    for i, name in enumerate(palette_names):
        col = i % cols
        row = i // cols
        x   = LEG_X0 + col * col_w
        y   = LEG_Y0 + row * row_h
        rgb = colours[i] if i < len(colours) else (180, 180, 180)
        drw.rectangle([x, y + 4, x + 14, y + 18], fill=rgb)
        drw.text((x + 22, y + 2), f'[{i:2d}] {name}',
                 fill=(220, 220, 220), font=font)

    img.save(out_path)


# ---------------------------------------------------------------------------
# Outer-X mask: vertices on the OUTER face of the track ribbon
# ---------------------------------------------------------------------------
def outer_x_mask(positions, side):
    """Return a boolean mask of vertices belonging to the outer-X
    face of the track ribbon.  `side` is 'L' or 'R'.

    For the LEFT track the outer face is at MOST-NEGATIVE X (away
    from tank centre); for the RIGHT track the outer face is at
    MOST-POSITIVE X.  A small tolerance (5 % of the track ribbon
    width) catches the verts that sit on a slight bevel without
    pulling in the inner-face ring.
    """
    x = positions[:, 0]
    if side.upper() == 'L':
        thresh = float(x.min()) + (float(x.max()) - float(x.min())) * 0.05
        return x < thresh
    else:
        thresh = float(x.max()) - (float(x.max()) - float(x.min())) * 0.05
        return x > thresh


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Dump + visualise track skinning bones for a WoT chassis.")
    parser.add_argument('tank',
        help="tank tag, e.g. `A83_T110E4`")
    parser.add_argument('--side', default='L', choices=['L', 'R'],
        help="which side to dump (default: L)")
    parser.add_argument('--part', default='track',
        choices=['track', 'chass'],
        help="which chassis sub-mesh to analyse: 'track' (the "
             "deforming track ribbon -- expect ~40 % 2-bone "
             "blends) or 'chass' (the rigid wheel / sprocket / "
             "idler / return-roller sub-meshes -- expect 100 % "
             "single-bone bound).  Default 'track'.")
    parser.add_argument('--out',  default=None,
        help="output PNG path (default: <tank>_track_<side>_skinning.png)")
    parser.add_argument('--pkg-dir', default=None,
        help="WoT install root (parent of res/packages); reads "
             "tankExporterPy.json if not given")
    parser.add_argument('--lookup-xml', default=None,
        help="TheItemList.xml path; defaults to project root copy")
    args = parser.parse_args(argv)

    # ---- 1. Locate WoT install + lookup -----------------------------
    if args.pkg_dir is None:
        cfg_path = os.path.join(ROOT, 'tankExporterPy.json')
        if os.path.isfile(cfg_path):
            import json
            with open(cfg_path, encoding='utf-8') as f:
                cfg = json.load(f)
            pd = (cfg.get('pkg_dir') or '').strip()
            # cfg pkg_dir is res/packages -- step up two levels for the install root
            args.pkg_dir = os.path.normpath(os.path.join(pd, '..', '..')) if pd else None
    if not args.pkg_dir or not os.path.isdir(args.pkg_dir):
        parser.error(
            f"Cannot find WoT install root.  Pass --pkg-dir or set "
            f"pkg_dir in tankExporterPy.json.  Got: {args.pkg_dir!r}")
    if args.lookup_xml is None:
        args.lookup_xml = os.path.join(ROOT, 'TheItemList.xml')

    # ---- 2. Extract chassis primitives + visual ---------------------
    pe = PkgExtractor(args.pkg_dir, lookup_xml=args.lookup_xml)
    # Tank-tag -> nation folder mapping.  WoT uses lowercase first-
    # letter prefix: A=usa(american), G=germany, R=ussr, etc.
    NATION_MAP = {
        'A': 'american', 'G': 'german',  'R': 'russian',
        'F': 'french',   'B': 'british', 'C': 'china',
        'J': 'japan',    'CZ': 'czech',  'S': 'sweden',
        'IT': 'italian', 'PL': 'poland',
    }
    # Try the simple "first-letter" lookup; user can expand if any
    # tank uses a multi-letter prefix.
    pref1 = args.tank[:1].upper()
    nation = NATION_MAP.get(pref1, 'american')
    base = f'vehicles/{nation}/{args.tank}/normal/lod0'
    prim_path = pe.extract(f'{base}/Chassis.primitives_processed')
    vis_path  = pe.extract(f'{base}/Chassis.visual_processed')
    if not prim_path or not vis_path:
        parser.error(
            f"Could not extract chassis files for {args.tank}.  Check "
            f"the tag prefix -> nation mapping at the top of main().")

    # ---- 3. Parse primitives ----------------------------------------
    # Auto-resolve geometry name across the WoT authoring variants.
    # The dominant patterns we've seen:
    #
    #   * `track_<side>_Shape`    -- T110E4, T92, Bat-Chat, AMX 13, ...
    #   * `track_<side>Shape`     -- AMX 50B and other older French tanks
    #     (Maya export quirk -- no underscore between side and Shape).
    #
    #   * `exportChass<side>1_Shape` -- modern default (T110E4 family).
    #   * `exportChass<side>1_Shape1` -- Object 268/4 family (extra "1").
    #   * `chassis_<side>Shape_split_<N>` -- AMX 50B and others
    #     (the chassis was split into multiple sub-meshes during
    #     authoring).  Multiple groups; we pick the first that
    #     matches and analyse just it -- the other splits typically
    #     hold sub-parts of the same surface (different materials).
    parsed = MeshParser.parse_primitives_processed(prim_path)
    avail = [g.get('name') for g in parsed]
    side  = args.side

    if args.part == 'track':
        candidates = [
            f'track_{side}_Shape',
            f'track_{side}Shape',
        ]
    else:
        candidates = [
            f'exportChass{side}1_Shape',
            f'exportChass{side}1_Shape1',
            f'chassis_{side}Shape_split_0',
            f'chassis_{side}Shape',
            f'chassis_{side}_Shape',
        ]

    geom_name = None
    for c in candidates:
        if any(name == c for name in avail):
            geom_name = c
            break
    if geom_name is None:
        parser.error(
            f"No matching {args.part} group for side {side}.\n"
            f"  tried: {candidates}\n"
            f"  available: {avail}\n"
            f"Use --part to switch, or extend the candidate list "
            f"in dump_track_skinning.py:main().")
    grp = find_group(parsed, geom_name)
    if not grp:
        parser.error(f"Group {geom_name!r} found in name list but "
                     f"failed to fetch.  Bug?")

    positions = np.asarray(grp['vertices']['positions'], dtype=np.float32)
    indices   = np.asarray(grp['indices'], dtype=np.uint32)
    bi        = grp['vertices'].get('bone_indices')
    bw        = grp['vertices'].get('bone_weights')
    if bi is None or bw is None:
        parser.error(f"{geom_name!r} has no bone data -- not skinned?")

    # ---- 4. Parse visual_processed for the bone palette -------------
    with open(vis_path, 'rb') as f:
        raw_vis = f.read()
    vis_text = pretty_xml_text(raw_vis)
    palettes = parse_renderset_bones(vis_text)
    if geom_name not in palettes:
        parser.error(
            f"Bone palette for {geom_name!r} not found in "
            f"{os.path.basename(vis_path)}.  Available: {list(palettes)}")
    palette = palettes[geom_name]

    # ---- 5. Per-vertex dominant bone --------------------------------
    n   = len(positions)
    dom_bones   = np.zeros(n, dtype=np.int32)
    dom_weights = np.zeros(n, dtype=np.float32)
    dom_bytes   = np.zeros(n, dtype=np.int32)
    for i in range(n):
        idx, w, by = dominant_bone(bi[i], bw[i])
        dom_bones[i]   = idx
        dom_weights[i] = w
        dom_bytes[i]   = by

    # Defensive clamp in case any vertex byte exceeds the palette
    # size after div-by-3 (shouldn't happen but real files
    # occasionally carry stray bone-byte values).
    dom_bones = np.clip(dom_bones, 0, len(palette) - 1)

    # ---- 6. Outer-X mask --------------------------------------------
    # Track ribbon: thin in X, so the OUTER face (-X for left side,
    # +X for right) is the visible "loop" we want to highlight.
    # Chassis sub-meshes (wheels / sprockets / rollers): full 3D
    # discs, no outer-X face -- treat every vert as "in the loop"
    # for the visual.
    if args.part == 'track':
        outer = outer_x_mask(positions, args.side)
    else:
        outer = np.ones(len(positions), dtype=bool)

    # ---- 7. Text dump -----------------------------------------------
    out_png = (args.out
               or f'{args.tank}_{args.part}_{args.side}_skinning.png')
    out_txt = os.path.splitext(out_png)[0] + '.txt'

    by_bone = defaultdict(list)
    for i in range(n):
        if outer[i]:
            by_bone[dom_bones[i]].append(i)

    # Per-group summary: centroid + bbox.  Useful at a glance for
    # spotting which bone owns which wheel / roller / sprocket.
    summary_rows = []
    for k in sorted(by_bone):
        verts = by_bone[k]
        p = positions[verts]
        c = p.mean(axis=0)
        ext = p.max(axis=0) - p.min(axis=0)
        name = palette[k] if k < len(palette) else f'<idx {k}>'
        summary_rows.append((k, name, len(verts), c, ext))

    # Multi-bone slot fill counts.
    slot_nz = (bw > 0.001).sum(axis=1)

    with open(out_txt, 'w', encoding='utf-8') as f:
        kind = ('Track ribbon' if args.part == 'track'
                else 'Chassis sub-meshes (wheels / sprockets / rollers)')
        f.write(f'-- {kind} skinning dump -- {args.tank} side {args.side}\n')
        f.write(f'   geometry: {geom_name}\n')
        f.write(f'   verts: {n}   tris: {len(indices)//3}\n')
        f.write(f'   verts in scope (outer-X / all): {int(outer.sum())}\n')
        f.write(f'   slot fill: '
                f'1-slot={int((slot_nz==1).sum())}  '
                f'2-slot={int((slot_nz==2).sum())}  '
                f'3-slot={int((slot_nz==3).sum())}  '
                f'4-slot={int((slot_nz==4).sum())}\n\n')
        f.write('Bone palette:\n')
        for k, name in enumerate(palette):
            f.write(f'  [{k:2d}]  {name}\n')
        f.write('\n')
        # Compact summary table.
        f.write('Vertex-group summary (centroid + bbox extent per bone):\n')
        f.write(f'  {"byte":>4s} {"idx":>3s}  {"bone":<28s}  '
                f'{"verts":>5s}  centroid (x, y, z)         '
                f'bbox-XYZ extent\n')
        for k, name, vc, c, ext in summary_rows:
            f.write(f'  {k*3:>4d} {k:>3d}  {name:<28s}  {vc:>5d}  '
                    f'({c[0]:+.3f}, {c[1]:+.3f}, {c[2]:+.3f})  '
                    f'(dx={ext[0]:.3f}, dy={ext[1]:.3f}, dz={ext[2]:.3f})\n')
        f.write('\n')
        # Per-vertex detail.
        f.write('Verts per group (sorted by Z, rear -> front):\n\n')
        for k in sorted(by_bone):
            verts = by_bone[k]
            verts.sort(key=lambda i: positions[i, 2])
            name = palette[k] if k < len(palette) else f'<idx {k}>'
            f.write(f'  bone [{k:2d}] {name}  ({len(verts)} verts)\n')
            for i in verts:
                p = positions[i]
                f.write(
                    f'    vert {i:5d}  pos=({p[0]:+.3f}, {p[1]:+.3f}, '
                    f'{p[2]:+.3f})  dom_byte={dom_bytes[i]:3d}  '
                    f'weight={dom_weights[i]:.3f}\n')
            f.write('\n')

    # ---- 8. PNG render ----------------------------------------------
    title = (f'{args.tank}  -  {geom_name}  -  '
             f'{n} verts / {len(indices)//3} tris  -  '
             f'colour = dominant bone'
             + ('  (outlined dots = outer-X face)'
                if args.part == 'track' else ''))
    render_png(out_png, positions, indices, dom_bones, dom_weights,
               outer, palette, title=title)

    print(f'wrote {out_txt}')
    print(f'wrote {out_png}')


if __name__ == '__main__':
    sys.exit(main() or 0)
