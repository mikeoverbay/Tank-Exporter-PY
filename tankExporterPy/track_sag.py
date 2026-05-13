"""Sag profile extraction from the WoT skinned-track ribbon mesh.

Coffee 2026-05-11 ("there are weights in the tank_mat_r/l_skinned
for the rubber band tracks.  Usable?"  -> "i think they will drop
in the the sprocket positions ok").

The chassis primitives ship a `track_<side>_Shape` mesh that is
the rubber-band track ribbon, GPU-skinned to the
`Track_<side><i>_BlendBone` chassis bones.  Each vertex is bound
to one (or 2 via the `iii` byte triplet + `ww` weights) bones
in the renderSet palette.  The vertex POSITIONS are authored at
bind pose -- and on most tanks they encode the chain's NATURAL
SAG between road wheels (the chain dips below the bone-tangent
line where suspension can absorb the droop).

This module pulls those bind-pose vertex positions, filters to
the OUTER face of the ribbon (the visible chain edge that
touches ground), and builds a `(Z, Y)` curve along chassis-local
Z.  Runtime callers interpolate it to bias each chain pad's Y
downward in the bottom-run region.

The sag information lives in vertex POSITIONS, NOT bone weights
-- the weights are only the deformation rule for how the ribbon
follows bone motion.  See the dump tool
`cust_tools/dump_track_skinning.py` for a visual reference of
the skinning rig.
"""
import re
import struct

import numpy as np

from .loaders import MeshParser
from .common  import is_bwxml, decode_bwxml


# --------------------------------------------------------------------
# Visual-processed bone-palette parser (lifted from
# cust_tools/dump_track_skinning.py).  Needed only if a caller
# wants the per-vertex BONE NAME mapping; the sag-curve extractor
# below doesn't strictly need it because the outer-face Y profile
# is bone-agnostic.
# --------------------------------------------------------------------
def _parse_renderset_bones(visual_xml_text):
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


def _pretty_xml(raw_bytes):
    text = (decode_bwxml(raw_bytes) if is_bwxml(raw_bytes[:8])
            else raw_bytes.decode('utf-8', errors='replace'))
    text = re.sub(r'>(?!<)', r'>\n', text)
    text = re.sub(r'<', r'\n<', text)
    return '\n'.join(line.strip() for line in text.splitlines()
                     if line.strip())


def _find_group(parsed, base_name):
    for g in parsed:
        if g.get('name') == base_name:
            return g
    return None


# --------------------------------------------------------------------
# Public extractor
# --------------------------------------------------------------------
def extract_sag_curve(prim_path, vis_path, side):
    """Read the authored sag profile of a tank's track-ribbon mesh.

    Args:
        prim_path : abs path to `Chassis.primitives_processed`
        vis_path  : abs path to `Chassis.visual_processed`
                    (used only for the renderSet/palette lookup
                    -- not strictly needed for the geometry, but
                    we use it to confirm the named group exists)
        side      : 'L' or 'R'

    Returns:
        (zs, ys) : two 1-D `np.float32` arrays of equal length,
                   sorted by ascending Z.  `ys[i]` is the
                   OUTER-FACE LOWEST Y at chassis-local Z `zs[i]`.
                   On a typical tank this traces the bottom edge
                   of the track ribbon -- so the chain BOTTOM RUN
                   curve, sag included.

        Returns `(None, None)` when:
            * The named track group isn't present in the file
            * The vertex stream has no positions
            * The outer-face mask filters to zero verts (no
              ribbon, e.g. a tank without a skinned track).

    Pipeline:
        1. Parse `.primitives_processed` -> dict per group.
        2. Find the group named `track_<side>_Shape` (with one
           authoring-variant fallback).
        3. Filter positions to OUTER face: side==L picks the
           most-negative X verts, side==R picks the most-positive,
           within a 5 % bbox-X tolerance.
        4. Bin those outer verts by chassis-local Z and reduce
           each bin to its MIN Y.  This rejects the top half of
           the ribbon (the part wrapping over the top return-
           run) and gives us a clean BOTTOM-EDGE Y(Z) curve.
        5. Return sorted (Z, Y).
    """
    try:
        parsed = MeshParser.parse_primitives_processed(prim_path)
    except Exception as exc:
        print(f'[track_sag] parse failed for {prim_path}: '
              f'{type(exc).__name__}: {exc}')
        return None, None

    # Geometry-name auto-resolve.  WoT has shipped several
    # authoring layouts for the skinned-track ribbon:
    #   * `track_<side>_Shape`            -- T110E4, T92, Bat-Chat,
    #                                        AMX 13, ...
    #   * `track_<side>Shape`             -- AMX 50B (Maya export
    #                                        without underscore)
    #   * `track_<side>Shape_split_<N>`   -- Tiger I (ribbon
    #                                        split into multiple
    #                                        sub-meshes during
    #                                        authoring -- typical
    #                                        of older tanks with
    #                                        high vert count)
    # We merge ALL matching groups' vertex positions into one
    # array; the bottom-edge sag profile is the union of every
    # outer-face vert from every split.
    side = side.upper()
    exact_candidates = [f'track_{side}_Shape',
                        f'track_{side}Shape']
    split_prefix = f'track_{side}Shape_split_'

    pos_blocks = []
    for nm in exact_candidates:
        g = _find_group(parsed, nm)
        if g is not None:
            p = g.get('vertices', {}).get('positions')
            if p is not None and len(p) > 0:
                pos_blocks.append(np.asarray(p, dtype=np.float32))
    for g in parsed:
        gnm = g.get('name', '') or ''
        if gnm.startswith(split_prefix):
            p = g.get('vertices', {}).get('positions')
            if p is not None and len(p) > 0:
                pos_blocks.append(np.asarray(p, dtype=np.float32))
    if not pos_blocks:
        return None, None
    positions = np.concatenate(pos_blocks, axis=0)

    # Outer-face mask: side L = most-negative X; side R = most-
    # positive X.  5 % bbox-X tolerance catches verts on a slight
    # bevel without pulling in the inner-face ring.
    x = positions[:, 0]
    x_min, x_max = float(x.min()), float(x.max())
    x_span = max(x_max - x_min, 1e-6)
    if side == 'L':
        thresh = x_min + 0.05 * x_span
        outer  = x < thresh
    else:
        thresh = x_max - 0.05 * x_span
        outer  = x > thresh
    if not outer.any():
        return None, None

    z_outer = positions[outer, 2]
    y_outer = positions[outer, 1]

    # Bin verts by Z (1 cm bins), reduce to min Y per bin.  This
    # gives us the BOTTOM EDGE of the outer ribbon at each Z.  We
    # later interpolate this in the runtime when biasing chain
    # pads in the bottom run.
    BIN_M = 0.01
    z_min = float(z_outer.min())
    z_max = float(z_outer.max())
    n_bins = max(8, int(np.ceil((z_max - z_min) / BIN_M)) + 1)
    bin_edges = np.linspace(z_min, z_max + 1e-6, n_bins + 1)
    bin_idx   = np.clip(
        np.searchsorted(bin_edges, z_outer, side='right') - 1,
        0, n_bins - 1)
    min_y_per_bin = np.full(n_bins, np.inf, dtype=np.float32)
    np.minimum.at(min_y_per_bin, bin_idx, y_outer)
    valid = np.isfinite(min_y_per_bin)
    if not valid.any():
        return None, None

    bin_centers = 0.5 * (bin_edges[:-1] + bin_edges[1:])
    zs = bin_centers[valid].astype(np.float32)
    ys = min_y_per_bin[valid].astype(np.float32)
    # Defensive sort (np.searchsorted output is already monotone,
    # but a downstream change could break that assumption).
    order = np.argsort(zs)
    return zs[order], ys[order]


# --------------------------------------------------------------------
# Runtime helper: lookup sag Y at an arbitrary chassis-local Z by
# linear interpolation of an `extract_sag_curve` result.  Out-of-
# range Z extrapolates with the curve's edge Y values (= no extra
# sag past the authored endpoints).
# --------------------------------------------------------------------
def interp_sag_y(zs_curve, ys_curve, zs_query):
    """Vectorised np.interp wrapper.  Same edge-clamp behaviour
    as np.interp (left/right defaults), so a query Z outside the
    track ribbon's authored Z range just returns the nearest
    endpoint Y -- no wild extrapolation."""
    return np.interp(zs_query, zs_curve, ys_curve).astype(
        np.float32)
