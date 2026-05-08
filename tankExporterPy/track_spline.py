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

import re
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np


__all__ = [
    'parse_track_vlocs',
    'to_chassis_frame',
    'centripetal_catmull_rom_closed',
    'resample_uniform',
    'TrackSplineLoader',
    'TrackSplineSide',
]


# Regex for the Collada-flavoured `.track` XML.  Each V_loc node
# carries a 4x4 row-major matrix; we want the translation row's
# 3 entries (matrix indices 3, 7, 11 in row-major flatten).  The
# whole file lives in WoT's BWXML container -- caller is expected
# to have already passed the bytes through `decode_bwxml` (in
# `tankExporterPy.common`) so the input here is plain text XML.
_VLOC_RE = re.compile(
    r'<name>(V_loc\d+)</name>.*?<matrix>([-+\d\.eE\s]+)<',
    re.DOTALL,
)


def parse_track_vlocs(text: str) -> List[Tuple[str, np.ndarray]]:
    """Parse V_loc nodes out of a `.track` Collada-flavoured XML
    string.

    Args:
        text: The XML text content of `vehicles/<n>/<tank>/track/
            {left,right}.track` after BWXML decoding.

    Returns:
        List of `(name, matrix4x4)` pairs in the order they appear
        in the file.  Each matrix is a numpy float64 (4, 4) array
        in **row-major** layout (matches the file's `<matrix>` row
        order).  Translation lives at `M[0, 3]`, `M[1, 3]`,
        `M[2, 3]`.

        Units are **centimetres** in WoT's DirectX-handed frame.
        Apply `to_chassis_frame()` to convert into the runtime
        chassis-local metre frame.
    """
    out: List[Tuple[str, np.ndarray]] = []
    for m in _VLOC_RE.finditer(text):
        nums = [float(x) for x in m.group(2).split()][:16]
        if len(nums) < 16:
            continue
        M = np.asarray(nums, dtype=np.float64).reshape((4, 4))
        out.append((m.group(1), M))
    return out


def to_chassis_frame(vlocs_dx_cm: Iterable[Tuple[str, np.ndarray]],
                     *,
                     flip_y: bool = False,
                     ) -> List[Tuple[str, np.ndarray]]:
    """Convert raw V_loc cm coords to the runtime chassis-local
    metre frame.

    Default conversion (matches what the runtime chassis bones
    expect):

        x_chassis = x_raw / 100
        y_chassis = y_raw / 100
        z_chassis = z_raw / 100

    Verified on T30: raw V_loc Y values land in [+0.52 m,
    +1.26 m].  The runtime W_L<i> wheel hubs sit at Y = +0.448 m,
    chassis-vertex frame, and the V_loc top run at Y = +1.24 m
    sits ABOVE the wheels (top of the hull, where the actual
    return-rolled track surface should be).  No Y flip is needed
    because the chassis is no longer Z-flipped on load and its
    vertex frame is the SAME frame the .track file authored its
    V_loc matrices in (CLAUDE.md "Skinned bone-byte" note;
    `from_chassis_meshes` flip_z = False since v1.93.2).

    The standalone probes (`_plot_t30_*.py`) flip Y because they
    project to a YZ side-view image where Y-down is the natural
    image axis.  That's a PLOT convention, not a runtime one;
    don't propagate it here.

    Args:
        vlocs_dx_cm: Output of `parse_track_vlocs()` -- iterable
            of `(name, 4x4 matrix in cm chassis-aligned frame)`.
        flip_y: If True, negate Y at conversion time.  Kept as a
            kwarg only so future tooling that genuinely wants the
            probe's plot frame can opt in.  Runtime callers should
            leave the default `False`.

    Returns:
        List of `(name, position xyz in m)` tuples.  Only the
        translation row of each input matrix is consumed; the
        rotation block is intentionally dropped because the V_loc
        nodes carry only a position role in the kinematic-CR
        pipeline (the per-pad orientation is reconstructed from
        the resampled-curve TANGENT, not from the V_loc rotation).
    """
    sy = -1.0 if flip_y else +1.0
    out: List[Tuple[str, np.ndarray]] = []
    for name, M in vlocs_dx_cm:
        p = np.asarray([
            float(M[0, 3]) / 100.0,
            sy * float(M[1, 3]) / 100.0,
            float(M[2, 3]) / 100.0,
        ], dtype=np.float64)
        out.append((name, p))
    return out


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

    out: List[np.ndarray] = []
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
        # this segment entirely.  Better to lose 256 samples than
        # divide-by-zero.
        if t1 == t0 or t2 == t1 or t3 == t2:
            continue
        # Sample [t1, t2) at samples_per_seg uniform-in-u steps.
        # Endpoint-exclusive so the next segment's first sample is
        # exactly P[i+1] without a duplicate.
        for k in range(samples_per_seg):
            u = t1 + (t2 - t1) * (k / samples_per_seg)
            # Barry-Goldman pyramid form of CR.
            A1 = (t1 - u) / (t1 - t0) * p0 + (u - t0) / (t1 - t0) * p1
            A2 = (t2 - u) / (t2 - t1) * p1 + (u - t1) / (t2 - t1) * p2
            A3 = (t3 - u) / (t3 - t2) * p2 + (u - t2) / (t3 - t2) * p3
            B1 = (t2 - u) / (t2 - t0) * A1 + (u - t0) / (t2 - t0) * A2
            B2 = (t3 - u) / (t3 - t1) * A2 + (u - t1) / (t3 - t1) * A3
            C  = (t2 - u) / (t2 - t1) * B1 + (u - t1) / (t2 - t1) * B2
            out.append(C)
    return np.asarray(out, dtype=np.float64)


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

    pad_pos = np.empty((n_pads, dense.shape[1]), dtype=np.float64)
    pad_tan = np.empty((n_pads, dense.shape[1]), dtype=np.float64)
    for i, s in enumerate(targets):
        # Locate segment index j with cum[j] <= s < cum[j+1].
        j = int(np.searchsorted(cum, s, side='right') - 1)
        j = max(0, min(j, M - 1))
        seg_len = cum[j + 1] - cum[j]
        t = (s - cum[j]) / seg_len if seg_len > 0 else 0.0
        a = dense[j]
        b = dense[(j + 1) % M]
        pad_pos[i] = a + (b - a) * t
        # Tangent = unit forward chord.  For typical track loops
        # adjacent dense samples are 3-4 mm apart so the chord
        # tangent is essentially the analytical tangent.
        chord = b - a
        nrm = float(np.linalg.norm(chord))
        if nrm > 0:
            pad_tan[i] = chord / nrm
        else:
            # Degenerate -- copy previous tangent or unit-X.
            pad_tan[i] = (pad_tan[i - 1] if i > 0
                          else np.eye(1, dense.shape[1])[0])
    return pad_pos, pad_tan, total


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
            vlocs = to_chassis_frame(vlocs_dx)
            return TrackSplineSide(
                vlocs, side=side_name,
                alpha=alpha, samples_per_seg=samples_per_seg)

        return _load('left'), _load('right')
