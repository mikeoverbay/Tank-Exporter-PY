"""Track NURB-ish spline math + per-side track loader.

This is **Phase A1** of the kinematic-bone-driven track work
documented in `docs/TRACK_PHYSICS.md`.  It promotes the proven
algorithm from the standalone probe `_plot_t30_smooth.py` (root
of the experiment tree) into a real package module so the
runtime can build pad transforms once per frame from the wheel
+ Track_* bone state already produced by `tank_physics.py`.

Pipeline at a glance, per side:

    .track Collada (17 V_loc 4x4 matrices, cm)
        |  parse_track_vlocs()      -- regex pull translations
        v
    raw V_loc points                (cm in DX frame)
        |  to_chassis_frame()       -- cm -> m, Y flip
        v
    chassis-frame V_loc array       (m, aligned with chassis bones)
        |  centripetal_catmull_rom_closed(P)        alpha = 0.5
        v
    dense polyline                  (4352 pts on T30)
        |  resample_uniform(dense, n_pads)
        v
    pad transforms                  (position + tangent per pad)

The closed centripetal CR with alpha = 0.5 was selected because
uniform CR (alpha = 0) overshoots the sprocket / idler bends
~6.4 % on T30, producing self-intersecting loops; chord-length
(alpha = 1) under-shoots.  Centripetal (alpha = 0.5) passes
through every control point with no self-intersection and a
fully tame curvature on the wraparound bends.  See
`docs/TRACK_PHYSICS.md` "Resampling pipeline" for the numbers.

This module is intentionally independent of `tank_physics.py`
and `viewer.py`.  Phase A2 ("bone binding map") and Phase A3
("per-frame V_loc -> pad transforms") will sit on top of these
primitives without modifying them.
"""

from __future__ import annotations

import os
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np


__all__ = [
    'parse_track_vlocs',
    'to_chassis_frame',
    'centripetal_catmull_rom_closed',
    'resample_uniform',
    'parse_chassis_bone_world_positions',
    'TrackBoneBinding',
    'TrackSplineLoader',
    'TrackSplineSide',
]


# Regex for the Collada-flavoured `.track` XML.  Each V_loc node
# carries a 4x4 row-major matrix; we want the translation row's
# 3 entries (matrix indices 3, 7, 11 in row-major flatten).  The
# whole file lives in WoT's BWXML container -- caller is expected
# to have already passed the bytes through `decode_bwxml` (in
# `tankExporterPy.common`) so the input here is plain text XML.
#
# Two naming + encoding conventions appear in the wild:
#
#   T30 / higher-tier tanks
#       <name>V_loc0</name> ... <matrix>16 floats row-major</matrix>
#
#   M3 Stuart / older tanks (per Coffee 2026-05-09)
#       <name>Track_VD_L0_loc1</name> ... <position>x y z</position>
#       (also seen with <name>...someName_loc<N>...</name>)
#
# Both forms describe a control point on the closed track loop.
# The parser below matches EITHER convention -- the inner
# matched-text scanner picks up <matrix> if present, else
# <position>, and synthesises an identity-rotation 4x4 with the
# position triple in the translation column.
_VLOC_NAME_RE = re.compile(
    r'<name>([^<]*?_loc\d+|V_loc\d+)</name>([^<]*<[^<]*</[^<]*>)*?'
    r'\s*(?:<matrix>([-+\d\.eE\s]+)</matrix>'
    r'|<position>([-+\d\.eE\s]+)</position>)',
    re.DOTALL,
)


def parse_track_vlocs(text: str) -> List[Tuple[str, np.ndarray]]:
    """Parse track-spline anchor nodes out of a `.track` Collada-
    flavoured XML string.

    Tolerates both transform encodings the WoT pipeline ships:
        * `<matrix>` -- 16 floats, row-major 4x4 (T30 style)
        * `<position>` -- 3 floats; rotation is treated as identity
          (M3 Stuart style; rotation isn't used downstream anyway,
          only the translation column).

    Tolerates both naming conventions:
        * `V_loc<N>` (T30, higher-tier)
        * `<anything>_loc<N>` (M3 Stuart `Track_VD_L0_loc1`, etc.)

    Args:
        text: The XML text content of `vehicles/<n>/<tank>/track/
            {left,right}.track` after BWXML decoding.

    Returns:
        List of `(name, matrix4x4)` pairs in source order.  Each
        matrix is a numpy float64 (4, 4) row-major array.
        Translation lives at `M[0, 3]`, `M[1, 3]`, `M[2, 3]`.

        Units are **centimetres** in WoT's DirectX-handed frame.
        Apply `to_chassis_frame()` to convert into the runtime
        chassis-local metre frame.
    """
    out: List[Tuple[str, np.ndarray]] = []
    # Per Coffee 2026-05-09 ("seg spacing was touched.. why?"):
    # only `V_loc<N>` named nodes are real spline control points.
    # Both T30 and M3 Stuart ship `Track_<...>_loc<N>` auxiliary
    # nodes alongside but those are NOT part of the closed loop
    # (T30's pre-1.115.42 spline used 17 `V_loc<N>` anchors and
    # filled the bottom run via the chassis `Track_<L|R>i` bone
    # splice in `build_augmented_control_loop`; M3 Stuart works
    # the same way -- 7 top-run `V_loc<N>` anchors + 5 chassis
    # bottom-run bones).
    name_re     = re.compile(r'<name>([^<]+)</name>', re.DOTALL)
    matrix_re   = re.compile(r'<matrix>([-+\d\.eE\s]+)<', re.DOTALL)
    position_re = re.compile(r'<position>([-+\d\.eE\s]+)<', re.DOTALL)
    name_pattern = re.compile(r'^V_loc\d+$')
    for m in name_re.finditer(text):
        nm = m.group(1).strip()
        if not name_pattern.match(nm):
            continue
        # Scan window from this <name> up to the next <name>
        # for either a <matrix> (T30) or <position> (M3 Stuart).
        next_name = name_re.search(text, m.end())
        end_pos = next_name.start() if next_name else len(text)
        window = text[m.end():end_pos]
        M = None
        mm = matrix_re.search(window)
        if mm:
            nums = [float(x) for x in mm.group(1).split()][:16]
            if len(nums) >= 16:
                M = np.asarray(nums, dtype=np.float64).reshape((4, 4))
        if M is None:
            pm = position_re.search(window)
            if pm:
                nums = [float(x) for x in pm.group(1).split()][:3]
                if len(nums) >= 3:
                    # Identity rotation, position in translation
                    # column.  Downstream code only reads the
                    # translation; rotation is unused.
                    M = np.eye(4, dtype=np.float64)
                    M[0, 3] = nums[0]
                    M[1, 3] = nums[1]
                    M[2, 3] = nums[2]
        if M is None:
            continue
        out.append((nm, M))
    return out


def to_chassis_frame(vlocs_dx_cm: Iterable[Tuple[str, np.ndarray]],
                     *,
                     flip_y: bool = False,
                     flip_z: bool = False,
                     unit_scale: float = 0.01,
                     ) -> List[Tuple[str, np.ndarray]]:
    """Convert raw V_loc coords to the runtime chassis-local
    metre frame.

    `unit_scale` is the multiplier from raw values to metres.
    Default 0.01 matches T30's `<unit><meter>0.010000</meter>
    <name>centimeter</name></unit>` declaration.  Older /
    lower-tier tanks (M3 Stuart, etc.) ship .track files
    WITHOUT a `<unit>` tag and store positions already in
    metres -- pass `unit_scale=1.0` for those.  The caller
    (typically `TrackSplineLoader.from_pkg`) detects the unit
    from the .track file's `<unit><meter>...</meter></unit>`
    tag.

    Default conversion (matches the chassis-bone frame from
    `parse_chassis_bone_world_positions`, verified 2026-05-08
    against the gameplay-XML `<teethSyncs>` data):

        x_chassis = x_raw / 100
        y_chassis = y_raw / 100
        z_chassis = -z_raw / 100        <- NEGATED by default

    Why the Z-negate
    ----------------
    Pre-1.111: the conversion left Z unsigned, on the (untested)
    assumption that the .track file's frame was the same as the
    chassis-bone frame.  The spline LOOKED right because the
    V_loc set is Z-symmetric (front cluster <-> rear cluster),
    so a Z-flipped spline still drapes correctly over the wheels
    on a tank with Z-symmetric WD_L0 / WD_L9 spacing.

    Verified wrong by the teethSync-anchor experiment
    (`cust_tools/plot_t30_spline_math.py`):

      * For each drive wheel the gameplay XML carries
        `<startAngle>` + `<teethCount>`.  Tooth k=0 sits at
        `(wheel_Y - R*sin(angle), wheel_Z - R*cos(angle))` on the
        outer track-surface circle.
      * With Z UNFLIPPED the worst tooth-to-V_loc match is
        ~4.7 cm and the assumed front-tangent V_loc lands on the
        wrong end of the tank.
      * With Z NEGATED the matches drop to **machine precision**
        (WD_L0 tooth k=2 -> V_loc31 at 0.001 m; WD_L9 tooth k=12
        -> V_loc19 at 0.014 m).  Per-tooth angular spacing
        (24 deg on a 15-tooth wheel) lines up with adjacent V_locs
        across the entire wraparound -- impossible by coincidence.

    Side effect of the original sign error: the spline's "rear"
    end was being rendered at the chassis's front, so the
    inertia-lean pitch was applied with the WRONG sign at each
    end -- which is the symptom that made Coffee report
    "spline sinks at rear under accel, front under brake".
    Squat lifts the rear; with the spline rotated 180 deg, that
    rear-lift was being drawn as a sink at what looked like the
    rear.

    Args:
        vlocs_dx_cm: Output of `parse_track_vlocs()` -- iterable
            of `(name, 4x4 matrix in cm)`.
        flip_y: If True, negate Y at conversion.  Kept for the
            standalone probe plots that project YZ with Y-down
            as the natural image axis.  Runtime callers leave
            the default False.
        flip_z: If True (default), negate Z at conversion.
            Required for the runtime; opt out only when feeding
            the standalone-probe scripts that pre-date the fix.

    Returns:
        List of `(name, position xyz in m)` tuples.
    """
    sy = -1.0 if flip_y else +1.0
    sz = -1.0 if flip_z else +1.0
    s  = float(unit_scale)
    out: List[Tuple[str, np.ndarray]] = []
    for name, M in vlocs_dx_cm:
        p = np.asarray([
            float(M[0, 3]) * s,
            sy * float(M[1, 3]) * s,
            sz * float(M[2, 3]) * s,
        ], dtype=np.float64)
        out.append((name, p))
    return out


def detect_unit_scale(track_text: str) -> float:
    """Return the multiplier that converts raw .track values to
    metres.  Reads `<unit><meter>VALUE</meter>...</unit>` if
    present (T30 / high-tier convention; VALUE = 0.01 for
    centimeters).  Returns 1.0 when the tag is missing
    (M3 Stuart and other older tanks ship positions already in
    metres).
    """
    m = re.search(r'<unit>.*?<meter>\s*([-+\d\.eE]+)\s*</meter>',
                  track_text, re.DOTALL)
    if not m:
        return 1.0
    try:
        return float(m.group(1))
    except ValueError:
        return 1.0


# ----------------------------------------------------------------------
# Centripetal Catmull-Rom -- the heart of the spline pass.
# ----------------------------------------------------------------------

def centripetal_catmull_rom_closed(P: np.ndarray,
                                   samples_per_seg: int = 256,
                                   alpha: float = 0.5,
                                   ) -> np.ndarray:
    """Closed Catmull-Rom through control points P with the
    centripetal parameterisation (alpha = 0.5 default).

    Computes a dense polyline that:
      * starts at P[0],
      * passes exactly through every P[i] in order,
      * closes back to P[0] (loop topology),
      * has no self-intersections on tight bends (which uniform
        CR would produce on the drive-sprocket / idler wraparounds
        of every tracked tank we've measured).

    Math: standard Barry-Goldman pyramid form.  For each segment
    [P[i], P[i+1]] we look at four control points (P[i-1], P[i],
    P[i+1], P[i+2]) with knot times t0..t3 spaced by

        t_{k+1} - t_k = |P_{k+1} - P_k| ** alpha

    `alpha = 0`   -> uniform CR (constant knot spacing); overshoots.
    `alpha = 0.5` -> centripetal; this is what we want.
    `alpha = 1.0` -> chord-length; under-shoots.

    Args:
        P: (N, dim) array of control points (dim = 2 or 3).
        samples_per_seg: Number of samples per inter-control-point
            segment.  256 is the empirically-validated default
            (4352-pt polyline through 17 V_locs gives 0.3 mm pad
            spacing std after resample on T30).
        alpha: Parameterisation exponent.  Default 0.5
            (centripetal).

    Returns:
        (N * samples_per_seg, dim) numpy float64 array of dense
        polyline points.  Index 0 is exactly P[0]; the final point
        is one sample short of looping back to P[0] (which keeps
        the array length consistent with `samples_per_seg * N`).
    """
    P = np.asarray(P, dtype=np.float64)
    n = len(P)
    if n < 2:
        return P.copy()

    # Vectorised inner loop -- the per-sample `for k in range(S)`
    # body in earlier versions dispatched 6 tiny numpy ops on
    # 3-vectors per sample, dominated by interpreter overhead.
    # Microbenchmark on T30-shape control loop (26 ctrl x 64
    # samples) was 17.7 ms / call; vectorised version below is
    # ~0.4 ms.  Same Barry-Goldman pyramid form, just lifted to
    # broadcast across `samples_per_seg` per segment.
    S = int(samples_per_seg)
    if S < 1:
        return P.copy()
    ks = np.arange(S, dtype=np.float64) / float(S)
    out_segments: List[np.ndarray] = []
    for i in range(n):
        p0 = P[(i - 1) % n]
        p1 = P[i]
        p2 = P[(i + 1) % n]
        p3 = P[(i + 2) % n]
        # Knot times -- centripetal accumulator.
        t0 = 0.0
        t1 = t0 + float(np.linalg.norm(p1 - p0)) ** alpha
        t2 = t1 + float(np.linalg.norm(p2 - p1)) ** alpha
        t3 = t2 + float(np.linalg.norm(p3 - p2)) ** alpha
        # Degenerate run (two coincident control points) -> skip
        # this segment entirely.  Better to lose `samples_per_seg`
        # samples than divide-by-zero.
        if t1 == t0 or t2 == t1 or t3 == t2:
            continue
        # Sample [t1, t2) at S uniform-in-u steps.  Endpoint-exclusive
        # so the next segment's first sample is exactly P[i+1] without
        # a duplicate.  All weights computed as (S,) vectors then
        # broadcast against the (dim,) control points to give (S, dim)
        # interpolated stages.
        us = t1 + (t2 - t1) * ks                     # (S,)
        # Pre-compute per-stage weight pairs once.
        w_a1_p0 = (t1 - us) / (t1 - t0)
        w_a1_p1 = (us - t0) / (t1 - t0)
        w_a2_p1 = (t2 - us) / (t2 - t1)
        w_a2_p2 = (us - t1) / (t2 - t1)
        w_a3_p2 = (t3 - us) / (t3 - t2)
        w_a3_p3 = (us - t2) / (t3 - t2)
        w_b1_a1 = (t2 - us) / (t2 - t0)
        w_b1_a2 = (us - t0) / (t2 - t0)
        w_b2_a2 = (t3 - us) / (t3 - t1)
        w_b2_a3 = (us - t1) / (t3 - t1)
        w_c_b1  = (t2 - us) / (t2 - t1)
        w_c_b2  = (us - t1) / (t2 - t1)
        # Broadcast every weight (S,) against the control points
        # (dim,) -- result (S, dim).
        A1 = w_a1_p0[:, None] * p0 + w_a1_p1[:, None] * p1
        A2 = w_a2_p1[:, None] * p1 + w_a2_p2[:, None] * p2
        A3 = w_a3_p2[:, None] * p2 + w_a3_p3[:, None] * p3
        B1 = w_b1_a1[:, None] * A1 + w_b1_a2[:, None] * A2
        B2 = w_b2_a2[:, None] * A2 + w_b2_a3[:, None] * A3
        C  = w_c_b1[:, None]  * B1 + w_c_b2[:, None]  * B2
        out_segments.append(C)
    if not out_segments:
        return np.zeros((0, P.shape[1]), dtype=np.float64)
    return np.concatenate(out_segments, axis=0)


def resample_uniform(dense: np.ndarray,
                     n_pads: int,
                     ) -> Tuple[np.ndarray, np.ndarray, float]:
    """Resample a dense closed polyline at `n_pads` uniform
    arc-length intervals around the loop.

    Args:
        dense: (M, dim) array from
            `centripetal_catmull_rom_closed`.  Must be closed
            (last point should be adjacent to first; this routine
            wraps automatically).
        n_pads: Number of evenly-spaced output points.  For a
            tracked tank this is `<segmentsCount> / 2` per side
            from the gameplay XML's `<trackPair>` block.

    Returns:
        Tuple `(pad_pos, pad_tan, total_length)`:
          * pad_pos  -- (n_pads, dim) positions in source-frame
            units (m if `to_chassis_frame` was applied).
          * pad_tan  -- (n_pads, dim) UNIT tangent vectors at
            each pad, computed from forward chord between dense
            samples.  Used downstream by the per-pad mesh draw to
            orient each pad along the spline.
          * total_length -- float, total closed loop arc length.

        Verified on T30 with 17 V_locs / 117 pads / centripetal
        CR alpha=0.5: mean spacing 0.1292 m, std 0.3 mm across
        all 117 outputs.  Length 15.126 m vs gameplay-XML
        target 15.561 m (-2.79 % shortfall = the track slack
        budget; the spline path is rigid, the pad-rest sum has
        slack).
    """
    dense = np.asarray(dense, dtype=np.float64)
    M = len(dense)
    if M < 2 or n_pads < 1:
        return (np.zeros((0, dense.shape[1])),
                np.zeros((0, dense.shape[1])), 0.0)

    # Cumulative arc length, closed.  `cum[k]` = arc length from
    # dense[0] to dense[k]; `cum[M]` = total closed loop length
    # (back to dense[0]).
    diffs = np.diff(dense, axis=0, append=dense[:1])
    deltas = np.linalg.norm(diffs, axis=1)
    cum = np.concatenate(([0.0], np.cumsum(deltas)))
    total = float(cum[-1])

    # Sample N target arc lengths in [0, total).  endpoint=False
    # because the loop closes -- emitting one at total would
    # duplicate the index-0 sample.
    targets = np.linspace(0.0, total, n_pads, endpoint=False)

    # Vectorised lookup + interpolation.  np.searchsorted is
    # vectorised; the inner Python loop in earlier versions did
    # the same searchsorted + slice + norm per pad and the
    # numpy-on-3-vector overhead dominated.  All operations below
    # are (n_pads,) or (n_pads, dim) shaped.
    js = np.searchsorted(cum, targets, side='right') - 1
    js = np.clip(js, 0, M - 1)
    seg_len = cum[js + 1] - cum[js]
    # Where seg_len == 0 (coincident dense samples), fall through
    # to t=0 -- produces an exact copy of dense[j], matching the
    # original behaviour.
    safe = seg_len > 0
    ts = np.zeros_like(seg_len)
    ts[safe] = (targets[safe] - cum[js[safe]]) / seg_len[safe]

    a = dense[js]
    b = dense[(js + 1) % M]
    pad_pos = a + (b - a) * ts[:, None]

    # Central-difference tangent per Coffee 2026-05-10 ("last
    # or first link rotation is wrong").  Was forward chord
    # `b - a` (= dense[(js+1) % M] - dense[js]) which collapses
    # to near-zero on closed loops where the dense sample
    # immediately AFTER the closure happens to be coincident
    # with the sample BEFORE the closure (within float noise).
    # On those tanks the closure pad's chord normalised to
    # garbage, the fallback below copied the previous tangent,
    # and the rendered link at that one slot ended up rotated
    # backwards.
    #
    # Central diff is `dense[(js+1) % M] - dense[(js-1) % M]`
    # -- both endpoints are real spline samples, never zero
    # length in practice, and gives the same direction the
    # forward chord would on every well-conditioned segment.
    # The closure pad picks up a clean tangent equal to the
    # average of its incoming + outgoing chord direction.
    prev_dense = dense[(js - 1) % M]
    next_dense = dense[(js + 1) % M]
    chord = next_dense - prev_dense
    chord_nrm = np.linalg.norm(chord, axis=1)
    pad_tan = np.zeros_like(chord)
    valid = chord_nrm > 0
    pad_tan[valid] = chord[valid] / chord_nrm[valid, None]
    if not valid.all():
        # Forward-fill degenerate rows from the previous valid
        # tangent; if the very first row is degenerate use unit-X
        # (matches the original `np.eye(1, dim)[0]`).
        last = np.zeros(dense.shape[1], dtype=np.float64)
        last[0] = 1.0
        for i in range(n_pads):
            if valid[i]:
                last = pad_tan[i]
            else:
                pad_tan[i] = last
    return pad_pos, pad_tan, total


# ----------------------------------------------------------------------
# Phase A2 -- Chassis bone hierarchy walk + V_loc -> bone binding.
# ----------------------------------------------------------------------

def _parse_visual_node_xform(transform_el) -> np.ndarray:
    """Parse a single `<transform>` block out of a .visual_processed
    node into a 4x4 numpy matrix.

    The XML format uses four `<rowN>` children with three space-
    separated floats each.  Rows 0..2 are the rotation / scale
    block; row 3 is the translation.  Missing rows default to
    identity (matches what WoT does on its end -- e.g. pure-
    translation nodes omit the rotation rows).
    """
    M = np.eye(4, dtype=np.float64)
    if transform_el is None:
        return M
    for i, r in enumerate(('row0', 'row1', 'row2', 'row3')):
        e = transform_el.find(r)
        if e is None or not e.text:
            continue
        vals = [float(x) for x in e.text.split()]
        if len(vals) < 3:
            continue
        if i < 3:
            M[i, :3] = vals[:3]
        else:
            M[3, :3] = vals[:3]
    return M


def parse_chassis_bone_world_positions(
        visual_path: str,
        ) -> Dict[str, np.ndarray]:
    """Walk a .visual_processed file's node hierarchy and return a
    dict `{bone_name: world_position_xyz}` for every named node.

    The walk multiplies parent transforms down so each returned
    position is in the same chassis-local frame as the chassis
    primitives' vertex coords (and as the V_loc positions after
    `to_chassis_frame()`).  Both BWXML and plain-XML files are
    supported; the BWXML case is decoded via
    `tankExporterPy.common.decode_bwxml`.

    Args:
        visual_path: Absolute path to a Chassis.visual_processed
            file on disk (already extracted from a pkg).

    Returns:
        Dict mapping every named node's `<identifier>` text to its
        world translation as an `np.ndarray((3,), dtype=float64)`.
        Includes the entire hierarchy (HP_*, W_*, Track_L*,
        Track_VT_L*, Track_VD_L*, WD_*, V_*, etc.) -- callers
        filter as needed.  Empty dict if the file is missing or
        unparseable.
    """
    if not visual_path or not os.path.exists(visual_path):
        return {}

    # Local imports keep the hot path's import set thin and avoid
    # circular imports if loaders.py later wants to import us.
    import xml.etree.ElementTree as ET
    from .common import decode_bwxml, is_bwxml

    try:
        with open(visual_path, 'rb') as fh:
            raw = fh.read()
    except OSError:
        return {}

    try:
        if is_bwxml(raw):
            text = decode_bwxml(raw)
        else:
            text = raw.decode('utf-8', errors='replace')
    except Exception:
        return {}

    # Strip any default-namespace wrapper that ET would otherwise
    # carry through into every tag name (`{ns}node` -> `node`),
    # mirroring the same regex the standalone probes use.
    text = re.sub(r'<xmlns:[^>]*>[^<]*</xmlns:[^>]*>', '', text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}

    out: Dict[str, np.ndarray] = {}

    def _walk(node, parent_world: np.ndarray) -> None:
        ident = node.find('identifier')
        nm = (ident.text.strip()
              if ident is not None and ident.text else '')
        M = _parse_visual_node_xform(node.find('transform'))
        world = parent_world @ M
        if nm:
            out[nm] = np.asarray(
                [float(world[3, 0]),
                 float(world[3, 1]),
                 float(world[3, 2])], dtype=np.float64)
        for child in node.findall('node'):
            _walk(child, world)

    for n in root.findall('node'):
        _walk(n, np.eye(4, dtype=np.float64))
    return out


class TrackBoneBinding:
    """Per-side V_loc -> chassis bone binding map.

    Built once at vehicle load by `bind_to_chassis_bones()` on a
    `TrackSplineSide`.  Each entry records the V_loc's NEAREST
    chassis bone (by chassis-local Y/Z distance) plus the rigid
    OFFSET that bone-relative bind preserves.  At runtime the
    V_loc world position is

        V_loc.world = bone.world + offset

    For tanks where none of the V_loc parents (Track_VT_*,
    Track_VD_*, WD_*) actually deflect with terrain, this is just
    a static rebrand of the V_loc bind position.  We still go
    through the bone lookup because Phase A3's bottom-run
    insertion needs the wheel-attached Track_L<i> bones, and
    keeping the bookkeeping consistent across the top + bottom
    runs makes the per-frame deform pass uniform.

    Also captures the bottom-run gap detection: the two
    consecutive V_locs whose chord crosses the entire wheel-Z
    extent (V_loc2 -> V_loc15 on T30, a 6.76 m chord at Y ~ 0.55
    above the wheels).  Phase A3 splices Track_L<i> bone
    positions into this bracket in arc-length order.
    """

    # Bone-name regex for the per-side ground-contact ("bottom-
    # run") wheel-attached bones.  Captures the index so we can
    # sort by track position along the loop traversal.  T30 has
    # Track_L0..L8 (9 bones); other tanks may have more or fewer.
    _TRACK_LANE_BONE_RE = re.compile(r'^Track_([LR])(\d+)$')

    def __init__(self,
                 *,
                 side: str,
                 vloc_to_bone: List[Tuple[str, str, np.ndarray]],
                 bottom_run_after_idx: int,
                 bottom_run_before_idx: int,
                 bottom_run_bones: List[Tuple[str, np.ndarray]]):
        """Args:
            side: 'left' or 'right'.
            vloc_to_bone: Parallel to TrackSplineSide.vloc_names --
                one entry per V_loc, of the form
                `(vloc_name, bone_name, offset_xyz)` where offset
                is `vloc_pos - bone_pos` in chassis-local metres.
                When no bone is in range, `bone_name` is an empty
                string and `offset_xyz` is the V_loc's bind
                position (so Phase A3 can fall back to "stay at
                bind" gracefully).
            bottom_run_after_idx: V_loc index AFTER which the
                bottom-run insertion goes.  In source / arc-length
                order, the next entry in the augmented control list
                is the first synthesised wheel point (front-of-
                bottom).
            bottom_run_before_idx: V_loc index BEFORE which the
                last synthesised wheel point sits (rear-of-bottom).
                Equal to `bottom_run_after_idx + 1` when the gap
                straddles two adjacent V_locs in source order;
                stored explicitly to keep wrap-around math simple.
            bottom_run_bones: List of `(bone_name, bind_xyz)` for
                each Track_L<i> on this side, ordered by Z so that
                arc-length-order traversal of the augmented loop
                visits them front-to-rear (or rear-to-front
                depending on which way the wraparound at
                bottom_run_after_idx is going).  Phase A3 uses
                these names to look up runtime bone positions
                each frame.
        """
        self.side = str(side)
        self.vloc_to_bone = list(vloc_to_bone)
        self.bottom_run_after_idx = int(bottom_run_after_idx)
        self.bottom_run_before_idx = int(bottom_run_before_idx)
        self.bottom_run_bones = list(bottom_run_bones)
        # Resolved end-wheel handles per Coffee 2026-05-09 ("the
        # first and last of the W_ are end wheels").  Available
        # without re-deriving from indices: the first / last entry
        # in `bottom_run_bones` after Z-sort.  Useful when later
        # code wants explicit "this Track_<side>i is the rear end
        # wheel for this side" rather than re-running the index
        # arithmetic.  Both handles are name-only; positions
        # follow `bottom_run_bones[0/-1][1]`.
        if bottom_run_bones:
            # `bottom_run_bones[0]` is whichever Z-end the binding
            # walks first (= side of `bottom_run_after_idx`); the
            # other end is `[-1]`.  We don't try to label "rear"
            # vs "front" here because that flips with V_loc
            # traversal direction -- the runtime that cares which
            # is which can compare Z against `vloc_positions`.
            self.first_end_bone = str(bottom_run_bones[0][0])
            self.last_end_bone  = str(bottom_run_bones[-1][0])
        else:
            self.first_end_bone = ''
            self.last_end_bone  = ''

    # ------------------------------------------------------------------
    @classmethod
    def build(cls,
              spline_side: 'TrackSplineSide',
              chassis_bones: Dict[str, np.ndarray],
              ) -> 'TrackBoneBinding':
        """Build a `TrackBoneBinding` for one side.

        Algorithm:
          1. Filter `chassis_bones` to the requested side
             (`L` for left / `R` for right) by suffix match on
             names like `Track_VT_LN`, `WD_LN`, `Track_LN`, etc.
          2. For each V_loc on this side, find the chassis bone
             with smallest 2-D (Y, Z) distance.  X is essentially
             constant on a side (T30 left = -1.480 across every
             track-related bone) so including X in the distance
             metric just adds zero noise -- skip it.
          3. Detect the bottom-run gap: the largest chord between
             consecutive V_locs in SOURCE order is the bottom run
             (V_loc2 -> V_loc15 on T30, 6.76 m).  Verified on T30
             where the next-largest gap is ~0.4 m (top-run
             between adjacent return rollers).
          4. Order the Track_L<i> bones for bottom-run insertion
             by Z so arc-length traversal between the gap V_locs
             walks them sequentially.

        Args:
            spline_side: `TrackSplineSide` with V_locs already in
                chassis frame (m).
            chassis_bones: Output of
                `parse_chassis_bone_world_positions()`.

        Returns:
            A `TrackBoneBinding` instance.
        """
        side_letter = 'L' if spline_side.side.lower().startswith('l') else 'R'

        # ---- Step 1: filter bones to this side -------------------
        # Side resolution: a bone belongs to this side iff its name
        # matches one of:
        #   Track_<L|R>\d+         (ground-contact)
        #   Track_VT_<L|R>\d+      (top-run sag)
        #   Track_VD_<L|R>\d+      (sprocket / idler wraparound)
        #   WD_<L|R>\d+            (sprocket / idler / return rollers)
        # Anything not matching one of these (Scene Root, HP_*,
        # turret bones, etc.) is irrelevant to the track.
        side_re = re.compile(
            rf'^(Track_VT_{side_letter}|Track_VD_{side_letter}'
            rf'|Track_{side_letter}|WD_{side_letter})(\d+)$')
        side_bones: Dict[str, np.ndarray] = {
            n: p for n, p in chassis_bones.items() if side_re.match(n)
        }

        # ---- Step 2: V_loc -> nearest bone by (Y, Z) -------------
        vloc_to_bone: List[Tuple[str, str, np.ndarray]] = []
        if side_bones:
            bone_names = list(side_bones.keys())
            bone_yz = np.stack(
                [np.array([side_bones[b][1], side_bones[b][2]],
                          dtype=np.float64)
                 for b in bone_names], axis=0)  # (B, 2)
            for vname, vpos in zip(spline_side.vloc_names,
                                    spline_side.vloc_positions):
                vyz = np.array([vpos[1], vpos[2]], dtype=np.float64)
                d2 = ((bone_yz - vyz) ** 2).sum(axis=1)
                k = int(np.argmin(d2))
                bn = bone_names[k]
                offset = vpos - side_bones[bn]
                vloc_to_bone.append((vname, bn, offset))
        else:
            # No bones found on this side -- fall back to "stay at
            # bind" entries.  Empty bone name signals Phase A3 to
            # not bother looking up runtime positions for these.
            for vname, vpos in zip(spline_side.vloc_names,
                                    spline_side.vloc_positions):
                vloc_to_bone.append((vname, '', vpos.copy()))

        # ---- Step 3: detect the bottom-run gap -------------------
        # Walk consecutive V_locs in SOURCE order (which is the
        # order they appear in the .track file = arc-length order
        # around the loop) and find the largest chord.  T30: 6.76 m
        # gap between V_loc2 and V_loc15; next-largest 0.40 m.
        n_v = len(spline_side.vloc_positions)
        gap_sizes = np.zeros(n_v, dtype=np.float64)
        if n_v >= 2:
            for i in range(n_v):
                a = spline_side.vloc_positions[i]
                b = spline_side.vloc_positions[(i + 1) % n_v]
                gap_sizes[i] = float(np.linalg.norm(b - a))
        bottom_run_after = int(np.argmax(gap_sizes)) if n_v >= 2 else 0
        bottom_run_before = (bottom_run_after + 1) % n_v

        # ---- Step 4: order Track_L<i> for bottom-run insertion ---
        # Pull every `Track_<side>\d+$` bone (no VT / VD prefix --
        # those are top-run / wraparound, not ground-contact) and
        # sort by Z so arc-length traversal between the gap V_locs
        # visits them sequentially.
        lane_re = cls._TRACK_LANE_BONE_RE
        lane_bones: List[Tuple[str, np.ndarray]] = []
        for n, p in chassis_bones.items():
            m = lane_re.match(n)
            if m and m.group(1) == side_letter:
                lane_bones.append((n, p))

        # Decide direction by checking which V_loc anchor the
        # bottom-run-after-idx is at: if the V_loc at that index
        # has a smaller Z than the one at before-idx, we walk from
        # smaller-Z (front, in chassis-local where +Z = rear) to
        # larger-Z, sorting bones by ascending Z.  Otherwise the
        # opposite.  This makes the binding side-agnostic and
        # robust to .track files whose V_locs were authored in
        # the opposite traversal direction.
        if n_v >= 2:
            after_z = float(spline_side.vloc_positions[bottom_run_after][2])
            before_z = float(spline_side.vloc_positions[bottom_run_before][2])
            ascending = (before_z > after_z)
        else:
            ascending = True
        lane_bones.sort(key=lambda t: float(t[1][2]),
                        reverse=not ascending)

        return cls(
            side=spline_side.side,
            vloc_to_bone=vloc_to_bone,
            bottom_run_after_idx=bottom_run_after,
            bottom_run_before_idx=bottom_run_before,
            bottom_run_bones=lane_bones,
        )


# ----------------------------------------------------------------------
# Per-side / whole-tank loaders.
# ----------------------------------------------------------------------

class TrackSplineSide:
    """Per-side track spline package: V_locs in chassis-local
    metres, plus the lazy-computed dense polyline / pad transforms.

    Held by `TrackSplineLoader` -- one instance per `left` / right`.
    """

    def __init__(self,
                 vlocs: List[Tuple[str, np.ndarray]],
                 *,
                 side: str,
                 alpha: float = 0.5,
                 samples_per_seg: int = 256):
        """Args:
            vlocs: List of (name, position xyz in metres,
                chassis-local frame) -- output of
                `to_chassis_frame()`.  Names typically `V_loc0` ..
                `V_locN`; preserved for the bind step in Phase A2.
            side: 'left' or 'right' -- recorded for caller
                bookkeeping; this class does no flipping based on
                it.
            alpha: Catmull-Rom parameterisation, default 0.5
                (centripetal).
            samples_per_seg: Dense-polyline samples per V_loc
                segment.  Tune up if the eventual pad count
                exceeds ~250 per side.
        """
        self.side = str(side)
        self.alpha = float(alpha)
        self.samples_per_seg = int(samples_per_seg)
        # Parallel arrays: name list + (N, 3) positions.  Kept as
        # numpy for fast downstream wheel-binding nearest-neighbour.
        self.vloc_names: List[str] = [n for n, _ in vlocs]
        if vlocs:
            self.vloc_positions = np.stack(
                [p for _, p in vlocs], axis=0).astype(np.float64)
        else:
            self.vloc_positions = np.zeros((0, 3), dtype=np.float64)

        # Lazy caches -- recomputed when V_loc positions change
        # (Phase A3: bone deflection updates V_locs each frame).
        self._dense: Optional[np.ndarray] = None
        self._pad_pos: Optional[np.ndarray] = None
        self._pad_tan: Optional[np.ndarray] = None
        self._total_length: float = 0.0
        self._n_pads_cached: int = -1

        # Phase A2 binding -- attached via `attach_binding()` after
        # the chassis .visual_processed has been parsed.  None until
        # then; bottom-run insertion + bone-driven V_loc updates in
        # Phase A3 require this to be present.
        self.binding: Optional[TrackBoneBinding] = None

    # ------------------------------------------------------------------
    def attach_binding(self,
                       chassis_bones: Dict[str, np.ndarray]) -> None:
        """Build and attach a `TrackBoneBinding` for this side from
        the parsed chassis bone world positions.  Runs the binding
        algorithm in `TrackBoneBinding.build()`.

        Idempotent: replaces any previously-attached binding so
        the caller can re-bind on a tank reload without
        instantiating a new `TrackSplineSide`.
        """
        self.binding = TrackBoneBinding.build(self, chassis_bones)

    # ------------------------------------------------------------------
    def build_augmented_control_loop(
            self,
            current_bone_positions: Dict[str, np.ndarray],
            *,
            wheel_radius: Optional[float] = None,
            ) -> np.ndarray:
        """Compose the full per-frame control point loop for the
        Catmull-Rom pass: original V_locs (driven by their bound
        chassis bones) PLUS the synthesised bottom-run points
        spliced between `bottom_run_after_idx` and
        `bottom_run_before_idx`.

        This is the Phase A3 hook -- each frame the caller supplies
        a `{bone_name: current_world_position}` dict (typically
        derived from the chassis pose + per-wheel residual), and
        gets back an `(M, 3)` array ready to feed straight into
        `centripetal_catmull_rom_closed()`.

        Args:
            current_bone_positions: Current chassis-local-frame
                positions for every bone the binding references.
                Missing entries fall back to bind-pose offsets
                (V_loc stays at its bind position).
            wheel_radius: If not None, inject a wheel-wrap tangent
                anchor at the rearmost AND frontmost bottom-run
                bones, on their wheels' circumferences at 90 deg
                from the ground contact (i.e. directly behind /
                ahead of the wheel hub at hub-height).  This forces
                the centripetal CR to enter / leave each end road
                wheel **tangent to its circle**, so the pads at
                Track_<side>0 / Track_<side>(N-1) sit flat on the
                ground instead of ramping up at the steep V_loc
                wraparound chord.  Per Coffee 2026-05-09: "you are
                not including the tangent for the first and last
                load bearing wheels."

        Returns:
            (M, 3) array.  M = len(V_locs) + len(bottom_run_bones)
            ( + 2 if wheel_radius is provided and bottom-run is
            non-empty).  T30 left without wrap = 17 + 9 = 26
            control points; with wrap = 28.  Order: traversal-
            order around the closed loop, starting at V_loc index
            0.

        Phase A2 builds the binding; this method is the explicit
        bridge to Phase A3 so the runtime never has to know about
        the bottom-run insertion arithmetic -- it just calls
        `build_augmented_control_loop(bones)` and feeds the
        result to the CR.
        """
        if self.binding is None:
            # No binding -- return raw V_locs.  Phase A1 fallback;
            # produces the bottom-run-less spline through 17
            # source points.  Useful when the visual_processed
            # parse failed for some reason.
            return self.vloc_positions.copy()

        b = self.binding
        n_v = len(self.vloc_names)
        bottom_pts: List[np.ndarray] = []
        for bn, bind_pos in b.bottom_run_bones:
            cur = current_bone_positions.get(bn)
            bottom_pts.append(np.asarray(cur, dtype=np.float64)
                              if cur is not None
                              else np.asarray(bind_pos, dtype=np.float64))

        # Build optional wheel-wrap tangent anchors for the first
        # and last bottom-run bones.  See the docstring `wheel_radius`
        # arg for the rationale.  Geometry: the wheel hub sits
        # `wheel_radius` Y above each Track_<side>i ground bone.
        # The anchor goes on the wheel circle at the 9 o'clock /
        # 3 o'clock position relative to the hub -- i.e. shifted
        # `+R` in Y AND `+/-R` in the OUTWARD chassis-local Z
        # direction (whichever way points away from the bottom
        # run at that end).  This places one extra control point
        # such that the CR tangent at the end Track_<side>i bone
        # is parallel to the bottom-run direction (= horizontal
        # under bind), so the rendered pad sits flat on the
        # ground at the wheel contact instead of inheriting the
        # steep chord-tangent from the V_loc-above wraparound.
        wrap_first: Optional[np.ndarray] = None
        wrap_last: Optional[np.ndarray] = None
        first_ground_idx = -1
        last_ground_idx = -1
        if (wheel_radius is not None
                and wheel_radius > 0.0
                and len(bottom_pts) >= 2):
            R = float(wheel_radius)
            # Per Coffee 2026-05-09 ("it isn't part of the spline,
            # it is a clamp to stop the spline from bending in to
            # the front wheel.. it need to be at the next point
            # so it anchors the front idler wheel"): some chassis
            # author an extra Track_<side>i bone as an anti-
            # collision CLAMP above the front wheel (T30: front
            # clamp at Y=+0.261).  That bone STAYS in the spline
            # control list -- removing it would let the CR dive
            # into the wheel -- but it must NOT be the source
            # for the wheel-wrap tangent anchor.  Walk inward
            # from each end past any non-ground (Y > tol) clamp
            # bones; the FIRST true-ground bone found from each
            # end is the source for that end's wrap anchor.
            GROUND_Y_TOL = 0.10
            n_bp = len(bottom_pts)
            for i in range(n_bp):
                if abs(float(bottom_pts[i][1])) < GROUND_Y_TOL:
                    first_ground_idx = i
                    break
            for i in range(n_bp - 1, -1, -1):
                if abs(float(bottom_pts[i][1])) < GROUND_Y_TOL:
                    last_ground_idx = i
                    break

            # First-end anchor: based at bottom_pts[first_ground_idx].
            # Outward direction = sign of chord-to-next-inward
            # ground bone, so the anchor pushes AWAY from the
            # bottom-run interior (toward whatever clamps + V_loc
            # come before).
            if (first_ground_idx >= 0
                    and first_ground_idx + 1 < n_bp):
                src = bottom_pts[first_ground_idx]
                nxt = bottom_pts[first_ground_idx + 1]
                dz = float(src[2] - nxt[2])
                sign = 1.0 if dz >= 0.0 else -1.0
                wrap_first = np.array(
                    [src[0], src[1] + R, src[2] + sign * R],
                    dtype=np.float64)

            # Last-end anchor: based at bottom_pts[last_ground_idx].
            if (last_ground_idx > 0
                    and last_ground_idx < n_bp):
                src = bottom_pts[last_ground_idx]
                prv = bottom_pts[last_ground_idx - 1]
                dz = float(src[2] - prv[2])
                sign = 1.0 if dz >= 0.0 else -1.0
                wrap_last = np.array(
                    [src[0], src[1] + R, src[2] + sign * R],
                    dtype=np.float64)

        # Build the bottom-run sub-sequence with wheel-wrap anchors
        # placed BETWEEN any clamp bones and the first / last
        # actual ground bone:
        #   [clamp..., wrap_first, ground_first, ..., ground_last,
        #    wrap_last, clamp...]
        # When there are no clamps the wrap anchors land at the
        # very ends, identical to the pre-clamp-aware behaviour.
        bottom_seq: List[np.ndarray] = []
        for i, pt in enumerate(bottom_pts):
            if (wrap_first is not None
                    and i == first_ground_idx):
                bottom_seq.append(wrap_first)
            bottom_seq.append(pt)
            if (wrap_last is not None
                    and i == last_ground_idx):
                bottom_seq.append(wrap_last)

        # Walk V_loc indices in source order, splicing bottom-run
        # points after the gap-after index.  Bone-driven V_loc
        # positions: V_loc.world = bone.world + offset (where
        # offset was captured at bind time).  Falls back to the
        # V_loc's bind position when the bone wasn't found in the
        # current dict.
        out: List[np.ndarray] = []
        for i in range(n_v):
            vname, bn, offset = b.vloc_to_bone[i]
            if bn:
                cur = current_bone_positions.get(bn)
                if cur is not None:
                    out.append(np.asarray(cur, dtype=np.float64)
                               + np.asarray(offset, dtype=np.float64))
                else:
                    out.append(self.vloc_positions[i].copy())
            else:
                out.append(self.vloc_positions[i].copy())
            # Splice the bottom-run sub-sequence (with anchors
            # already in place) right after the gap-after V_loc.
            if i == b.bottom_run_after_idx:
                out.extend(bottom_seq)
        return np.asarray(out, dtype=np.float64)

    # ------------------------------------------------------------------
    def update_vloc_positions(self, positions: np.ndarray) -> None:
        """Replace V_loc positions in-place and invalidate caches.

        Phase A3 calls this each frame after pulling each V_loc's
        bound bone world transform from the chassis.  After
        `update_vloc_positions(new)` the next `dense_polyline()`
        and `pad_transforms(n)` calls re-run the CR + resample
        with the new control points.
        """
        positions = np.asarray(positions, dtype=np.float64)
        if positions.shape != self.vloc_positions.shape:
            raise ValueError(
                f'update_vloc_positions: shape mismatch '
                f'{positions.shape} vs {self.vloc_positions.shape}')
        self.vloc_positions = positions
        self._dense = None
        self._pad_pos = None
        self._pad_tan = None
        self._n_pads_cached = -1

    # ------------------------------------------------------------------
    def dense_polyline(self) -> np.ndarray:
        """Compute / cache and return the dense centripetal CR
        polyline through `self.vloc_positions`.  See
        `centripetal_catmull_rom_closed`.
        """
        if self._dense is None:
            self._dense = centripetal_catmull_rom_closed(
                self.vloc_positions,
                samples_per_seg=self.samples_per_seg,
                alpha=self.alpha)
        return self._dense

    # ------------------------------------------------------------------
    def pad_transforms(self, n_pads: int,
                       ) -> Tuple[np.ndarray, np.ndarray, float]:
        """Compute / cache and return the resampled pad transforms.

        Returns:
            (pad_pos, pad_tan, total_length) -- see
            `resample_uniform`.
        """
        if (self._pad_pos is None or self._pad_tan is None
                or self._n_pads_cached != n_pads):
            pad_pos, pad_tan, total = resample_uniform(
                self.dense_polyline(), n_pads)
            self._pad_pos = pad_pos
            self._pad_tan = pad_tan
            self._total_length = total
            self._n_pads_cached = n_pads
        return self._pad_pos, self._pad_tan, self._total_length

    # ------------------------------------------------------------------
    @property
    def loop_length(self) -> float:
        """Total closed-loop arc length in metres.  Available
        after `pad_transforms()` has been called at least once."""
        return float(self._total_length)


class TrackSplineLoader:
    """Top-level loader: reads `left.track` + `right.track`
    Collada-flavoured XML out of a tank's pkg, decodes BWXML,
    parses the V_loc list per side, applies the DX-cm to
    chassis-local-m conversion, and returns per-side
    `TrackSplineSide` instances.

    Designed to be called once at vehicle load.  Pad transforms
    are computed on demand via `TrackSplineSide.pad_transforms`.
    """

    @staticmethod
    def from_pkg(pkg_extractor,
                 vehicle_path: str,
                 *,
                 alpha: float = 0.5,
                 samples_per_seg: int = 256,
                 ) -> Tuple[Optional[TrackSplineSide],
                            Optional[TrackSplineSide]]:
        """Load both sides of a tank's track spline.

        Args:
            pkg_extractor: A `loaders.PkgExtractor` instance.
            vehicle_path: Path prefix into the pkg, e.g.
                `'vehicles/american/A14_T30'`.  No trailing
                slash; we append `'/track/{left,right}.track'`.
            alpha, samples_per_seg: Forwarded to
                `TrackSplineSide`.

        Returns:
            Tuple `(left, right)`.  Either side may be `None` if
            the corresponding `.track` file is missing -- some
            mods or DLC tanks ship only one side; we don't want
            to crash the whole load over it.

        Raises:
            ImportError: if `tankExporterPy.common.decode_bwxml`
                isn't importable (would mean the package is
                misconfigured; we don't try to fall back).
        """
        # Local import to keep the module standalone-importable
        # for tooling / probes that just want the math (no pkg I/O).
        from .common import decode_bwxml

        def _load(side_name: str) -> Optional[TrackSplineSide]:
            track_path = f'{vehicle_path}/track/{side_name}.track'
            try:
                local_path = pkg_extractor.extract(track_path)
            except Exception:
                return None
            if not local_path:
                return None
            try:
                with open(local_path, 'rb') as fh:
                    raw = fh.read()
            except OSError:
                return None
            text = decode_bwxml(raw)
            vlocs_dx = parse_track_vlocs(text)
            if not vlocs_dx:
                return None
            unit_scale = detect_unit_scale(text)
            # Z-flip heuristic per Coffee 2026-05-09 ("t-30 is
            # ok.. m3 stuart flipped"):
            #
            # T30-style .track files declare `<unit>` and ship
            # `<matrix>` transforms in centimeters.  Positions in
            # the translation column already match chassis-local
            # Z (verified at machine precision against teethSync
            # anchors); no flip needed.
            #
            # M3 Stuart-style .track files OMIT `<unit>` and ship
            # `<position>` triples in metres (no rotation).  Those
            # positions are authored in a frame mirrored along Z
            # relative to chassis-local, so they need flipping to
            # come out the right way around.  Detected here by
            # `unit_scale != 0.01`: T30 = 0.01 (cm declared), M3
            # Stuart = 1.0 (no tag).  If a future tank ships
            # matrices but no unit tag, this rule will mis-flag
            # it -- in that case promote to a per-tank flag.
            needs_z_flip = (abs(unit_scale - 0.01) > 1e-9)
            vlocs = to_chassis_frame(
                vlocs_dx, unit_scale=unit_scale,
                flip_z=needs_z_flip)
            return TrackSplineSide(
                vlocs, side=side_name,
                alpha=alpha, samples_per_seg=samples_per_seg)

        return _load('left'), _load('right')
