"""Enumerate chassis track meshes for a tank.

Diagnostic for "scrambled" / "partial" track rendering issues.
Coffee 2026-05-15 ("we need to start analyzing the chassis
tracks/segments. pl4 scrambled. gb174 has 4 tracks, 2 show.
many are ok.").

Walks a tank's chassis `.primitives_processed` + `.visual_processed`
and prints:

* All section names + sizes from the primitives table.
* Per-section breakdown for track-related groups: group count,
  vertex count, index count, bind position bounds (xyz min/max).
* The `visual_processed` renderSets that reference each track
  section, plus the bone-palette length so we can spot tracks
  that bind only to `V_BlendBone` vs ones that include
  `Track_<side><i>_BlendBone` lists.
* Visibility prediction per section based on the viewer's skip
  rule:
      mn.startswith('track_') and 'Shape' in mn
  (see `viewer.py:_on_mesh_visibility_toggled` / the picker.py
  filter).

Usage::

    # Run on each problem tank + a known-good one for comparison.
    python cust_tools/analyze_chassis_tracks.py Pl04_Pudel
    python cust_tools/analyze_chassis_tracks.py GB174_<full_tag>
    python cust_tools/analyze_chassis_tracks.py A83_T110E4

Output goes to stdout.  Pipe to a file for easy side-by-side
diffing::

    python cust_tools/analyze_chassis_tracks.py Pl04_Pudel > pl4.txt
    python cust_tools/analyze_chassis_tracks.py A83_T110E4 > t110e4.txt
    diff pl4.txt t110e4.txt
"""

import argparse
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tankExporterPy.loaders import PkgExtractor, MeshParser
from tankExporterPy.common  import is_bwxml, decode_bwxml


# ---------------------------------------------------------------------------
# Same nation-prefix table as dump_track_skinning.py.  Single-letter
# tag prefix maps to nation folder.  Add CZ / IT / PL multi-letter
# prefixes explicitly so the simple `[0]` lookup doesn't trap them.
# ---------------------------------------------------------------------------
NATION_MAP_1 = {
    'A': 'american', 'G': 'german',  'R': 'russian',
    'F': 'french',   'B': 'british', 'C': 'china',
    'J': 'japan',    'S': 'sweden',
}
NATION_MAP_2 = {
    'CZ': 'czech',   'IT': 'italian', 'PL': 'poland',
    'GB': 'british',
}


def resolve_nation(tag):
    """Pick the WoT vehicle-folder nation for a given tag prefix.

    Tries the 2-letter prefix first (PL, GB, IT, CZ), then falls back
    to the single-letter map.  Defaults to 'american' if nothing
    matches -- caller should check the extract returned a file.
    """
    p2 = tag[:2].upper()
    if p2 in NATION_MAP_2:
        return NATION_MAP_2[p2]
    p1 = tag[:1].upper()
    return NATION_MAP_1.get(p1, 'american')


def pretty_xml_text(raw):
    """Decode visual_processed XML.  Some are bwxml-packed, some are
    plain text -- handle both.
    """
    if is_bwxml(raw):
        return decode_bwxml(raw)
    return raw.decode('utf-8', errors='replace')


def viewer_would_skip(name):
    """Mirror the viewer's track-ribbon skip rule.  True iff the
    rendering pipeline would HIDE this mesh by default."""
    return (name or '').startswith('track_') and 'Shape' in (name or '')


# ---------------------------------------------------------------------------
# RenderSet extractor: which primitive groups each renderSet names
# ---------------------------------------------------------------------------
RX_RENDERSET = re.compile(
    r'<renderSet>(.*?)</renderSet>', re.DOTALL)
RX_PRIMNAME = re.compile(
    r'<primitive>([^<]+)</primitive>')
RX_NODE = re.compile(
    r'<node>([^<]+)</node>')


def parse_rendersets(vis_text):
    """Return a list of (primitive_names, bone_palette) tuples, one
    per <renderSet> block.  Order preserved from file."""
    out = []
    for rs_text in RX_RENDERSET.findall(vis_text):
        prims = [m.strip() for m in RX_PRIMNAME.findall(rs_text)]
        bones = [m.strip() for m in RX_NODE.findall(rs_text)]
        out.append((prims, bones))
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(
        description="Enumerate chassis track meshes for a tank.")
    p.add_argument('tank',
        help="tank tag, e.g. `A83_T110E4`, `Pl04_Pudel`, "
             "`GB174_xxx`")
    p.add_argument('--pkg-dir', default=None,
        help="WoT install root (parent of res/packages); reads "
             "tankExporterPy.json if not given")
    p.add_argument('--lookup-xml', default=None,
        help="TheItemList.xml path; defaults to project root copy")
    args = p.parse_args(argv)

    # ---- Resolve pkg root + lookup ----
    if args.pkg_dir is None:
        cfg_path = os.path.join(ROOT, 'tankExporterPy.json')
        if os.path.isfile(cfg_path):
            import json
            with open(cfg_path, encoding='utf-8') as f:
                cfg = json.load(f)
            pd = (cfg.get('pkg_dir') or '').strip()
            args.pkg_dir = (os.path.normpath(os.path.join(pd, '..', '..'))
                            if pd else None)
    if not args.pkg_dir or not os.path.isdir(args.pkg_dir):
        p.error(f"Cannot find WoT install root.  Pass --pkg-dir or "
                f"set pkg_dir in tankExporterPy.json.  "
                f"Got: {args.pkg_dir!r}")
    if args.lookup_xml is None:
        args.lookup_xml = os.path.join(ROOT, 'TheItemList.xml')

    # ---- Locate chassis files via PkgExtractor ----
    nation = resolve_nation(args.tank)
    base = f'vehicles/{nation}/{args.tank}/normal/lod0'
    pe = PkgExtractor(args.pkg_dir, lookup_xml=args.lookup_xml)
    prim_path = pe.extract(f'{base}/Chassis.primitives_processed')
    vis_path  = pe.extract(f'{base}/Chassis.visual_processed')
    if not prim_path or not vis_path:
        p.error(f"Could not extract chassis files for {args.tank!r} "
                f"(nation={nation}).  Tag prefix wrong, or no LOD0 "
                f"under vehicles/{nation}/{args.tank}/.  Got "
                f"prim={prim_path!r} vis={vis_path!r}.")

    print(f"=== Chassis analysis: {args.tank} ({nation}) ===")
    print(f"  primitives_processed: {prim_path}")
    print(f"  visual_processed   : {vis_path}")

    # ---- Parse primitives + visual ----
    parsed = MeshParser.parse_primitives_processed(prim_path)
    with open(vis_path, 'rb') as f:
        raw_vis = f.read()
    vis_text = pretty_xml_text(raw_vis)
    rendersets = parse_rendersets(vis_text)

    # ---- All track-related sections ----
    track_groups = []
    for g in parsed:
        nm = g.get('name') or ''
        if 'track' in nm.lower() or 'chass' in nm.lower():
            track_groups.append(g)

    print(f"\n--- Track / chassis groups ({len(track_groups)}) ---")
    print(f"{'name':40s} {'verts':>7} {'tris':>7}  "
          f"{'x_range':>16} {'y_range':>16} {'z_range':>16}  "
          f"viewer_skips")
    for g in track_groups:
        nm = g.get('name') or ''
        verts = g.get('vertices', {}).get('positions')
        idx   = g.get('indices')
        n_v = 0 if verts is None else len(verts)
        n_i = 0 if idx is None   else len(idx)
        if n_v > 0:
            import numpy as _np
            arr = _np.asarray(verts, dtype=_np.float32)
            xr = f"[{arr[:,0].min():+5.2f},{arr[:,0].max():+5.2f}]"
            yr = f"[{arr[:,1].min():+5.2f},{arr[:,1].max():+5.2f}]"
            zr = f"[{arr[:,2].min():+5.2f},{arr[:,2].max():+5.2f}]"
        else:
            xr = yr = zr = '[--,--]'
        skip = 'SKIP' if viewer_would_skip(nm) else '    '
        print(f"{nm:40s} {n_v:>7d} {n_i//3:>7d}  "
              f"{xr:>16} {yr:>16} {zr:>16}  {skip}")

    # ---- RenderSet mapping ----
    print(f"\n--- RenderSets ({len(rendersets)}) ---")
    for i, (prims, bones) in enumerate(rendersets):
        # Filter to renderSets that reference any track group.
        relevant = [pr for pr in prims
                    if 'track' in pr.lower() or 'chass' in pr.lower()]
        if not relevant:
            continue
        # Trim bone list to track-related + V_BlendBone for clarity.
        tb = [b for b in bones
              if 'Track' in b or b == 'V_BlendBone'
              or 'Susp' in b or 'Wheel' in b]
        print(f"  [{i}] prims = {relevant}")
        print(f"      bones ({len(bones)} total, "
              f"{len(tb)} track-or-susp): "
              f"{tb if tb else bones[:6]}{'...' if not tb and len(bones) > 6 else ''}")

    print(f"\n--- Visibility prediction ---")
    visible = [g['name'] for g in track_groups
               if not viewer_would_skip(g.get('name') or '')]
    hidden  = [g['name'] for g in track_groups
               if viewer_would_skip(g.get('name') or '')]
    print(f"  WOULD RENDER ({len(visible)}): {visible}")
    print(f"  WOULD SKIP   ({len(hidden)}): {hidden}")


if __name__ == '__main__':
    main()
