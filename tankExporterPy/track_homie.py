"""Runtime home-brewed track chain ("homie tracks").

Per Coffee 2026-05-10 ("F9 still changes spline color, never
switches between old spline or homie spline math"): this is the
runtime-callable version of the wheel-tangent + arc-share loop
math from `cust_tools/plot_wheel_tangent_pies.py` /
`plot_homie_tiger.py`.  Where `track_spline.py` reads engine-
authored .track files and runs centripetal Catmull-Rom through
them, this module computes the chain PURELY FROM CHASSIS BONES
+ WHEEL RADII -- no .track file, no spline, no per-tank artist
authoring.

API
---
`compute_homie_chain(bones, radii, roles, side, n_pads, seg_len,
                     gauge_x)`
    -> (positions, tangents)

`positions` and `tangents` are each (n_pads, 3) float32 arrays
in chassis-local XYZ.  X coordinate is +/- gauge_x/2 depending
on `side`.  Y and Z trace the homie chain in the chassis side-
view plane.  Tangents are unit vectors along chain-forward.

Returns (None, None) on any failure (missing data, degenerate
loop, etc.) so the caller can fall back to the spline.

Pipeline:
  1. Pull (Z, Y) hub + R from `bones` + `radii` for one side.
  2. Filter aux tensioners (R << median road wheel R).
  3. Order around loop: bottom-run (front -> rear) + rearmost
     return roller, then top-run (rear -> front).
  4. External common tangent contacts between consecutive
     wheels; angles -> in/out arc bounds.
  5. Apply round-3 R-scale: `k = 1 + err / sum_arc_active`
     so the geometric loop length lands on segLen * segCount.
  6. Walk the alternating arc/line segment chain at uniform
     arc-length pitch, drop n_pads pads.  Promote 2-D (Z, Y)
     to 3-D (X, Y, Z) using `gauge_x`.
"""
import math
import numpy as np


def _angle_yz(p, hub):
    return math.atan2(p[1] - hub[1], p[2] - hub[2])


def _angle_from_hub(point, hub):
    d = point - hub
    return math.atan2(d[1], d[0])


def _short_arc_signed_diff(a_in, a_out, deg_tol=0.5):
    diff = a_in - a_out
    while diff > math.pi:
        diff -= 2 * math.pi
    while diff <= -math.pi:
        diff += 2 * math.pi
    if abs(math.degrees(diff)) < deg_tol:
        return 0.0
    return diff


def _external_tangent_contacts(C1, r1, C2, r2, n_side=+1):
    v = C2 - C1
    d = float(np.linalg.norm(v))
    if d < 1e-9:
        return None
    u = v / d
    perp = np.array([-u[1], u[0]])
    n_par = (r1 - r2) / d
    if abs(n_par) > 1.0 - 1e-9:
        return None
    n_perp = math.sqrt(1.0 - n_par * n_par) * float(n_side)
    n = n_par * u + n_perp * perp
    return (C1 + r1 * n, C2 + r2 * n)


def _collect_wheels(bones, radii, roles, side):
    side_token = 'L' if side.lower().startswith('l') else 'R'
    role_lookup = {}
    for nm in roles.get(f'drive_sprockets_{side_token}', []):
        role_lookup[nm] = 'sprocket'
    for nm in roles.get(f'idlers_{side_token}', []):
        role_lookup[nm] = 'idler'
    for nm in roles.get(f'road_wheels_{side_token}', []):
        role_lookup[nm] = 'road'
    for nm in roles.get(f'return_rollers_{side_token}', []):
        role_lookup[nm] = 'roller'
    side_names = (
        roles.get(f'drive_sprockets_{side_token}', [])
        + roles.get(f'idlers_{side_token}', [])
        + roles.get(f'road_wheels_{side_token}', [])
        + roles.get(f'return_rollers_{side_token}', []))

    def _mirror(nm):
        if side_token == 'L':
            return nm
        return nm.replace('_R', '_L', 1)

    wheels = []
    for nm in side_names:
        R = radii.get(nm)
        if R is None:
            R = radii.get(_mirror(nm))
        if R is None:
            continue
        b = bones.get(nm)
        if b is None:
            b = bones.get(nm + '_BlendBone')
        if b is None:
            continue
        # Per Coffee 2026-05-10 ("are you using math that isn't
        # exactly the same as the chassis create and render our
        # code?"): the chassis MESH renderer applies NO Z-flip
        # to chassis primitives or to chassis bones -- both
        # share the primitives' native Z.  Our earlier negate-
        # at-input was inconsistent with that pipeline and was
        # the likely cause of the L vs R rendering asymmetry.
        # Now we use bones' native Z verbatim, so the homie
        # chain goes through `chassis_pose @ pad` in the SAME
        # frame the chassis mesh is in.
        wheels.append({
            'name': nm,
            'hub':  np.array([b[2], b[1]], dtype=np.float64),
            'hub_x': float(b[0]),
            'R':    float(R),
            'role': role_lookup.get(nm, 'unknown'),
        })
    return wheels


def _order_loop(wheels):
    road_radii = sorted(w['R'] for w in wheels if w['role'] == 'road')
    road_R = road_radii[len(road_radii) // 2] if road_radii else 0.0
    R_min = 0.5 * road_R
    bottom = [w for w in wheels
              if w['role'] in ('sprocket', 'idler', 'road')
              and (w['role'] == 'road' or w['R'] >= R_min)]
    top = [w for w in wheels
           if w['role'] == 'roller' and w['R'] >= R_min * 0.4]
    bottom.sort(key=lambda w: w['hub'][0], reverse=True)
    top.sort   (key=lambda w: w['hub'][0])
    if top and bottom:
        bottom.append(top.pop(0))
    return bottom + top


def _compute_pie_arcs(loop):
    n = len(loop)
    if n < 2:
        return []
    out = [{} for _ in range(n)]
    for i in range(n):
        j = (i + 1) % n
        wA, wB = loop[i], loop[j]
        contacts = _external_tangent_contacts(
            wA['hub'], wA['R'], wB['hub'], wB['R'], n_side=+1)
        if contacts is None:
            out[i]['contact_out'] = None
            out[i]['tangent_to_next'] = None
            continue
        c_out, c_in_next = contacts
        out[i]['contact_out']     = c_out
        out[j]['contact_in']      = c_in_next
        out[i]['tangent_to_next'] = (c_out, c_in_next)
    for i, w in enumerate(loop):
        c_in  = out[i].get('contact_in')
        c_out = out[i].get('contact_out')
        out[i]['wheel'] = w
        out[i]['a_in']  = (_angle_from_hub(c_in,  w['hub'])
                            if c_in  is not None else None)
        out[i]['a_out'] = (_angle_from_hub(c_out, w['hub'])
                            if c_out is not None else None)
    return out


def _measure_loop(loop):
    arcs = _compute_pie_arcs(loop)
    arc_sum = lin_sum = 0.0
    active = []
    for entry in arcs:
        wh = entry['wheel']
        a_in  = entry.get('a_in')
        a_out = entry.get('a_out')
        if a_in is not None and a_out is not None:
            d = _short_arc_signed_diff(a_in, a_out)
            arc_sum += wh['R'] * abs(d)
            # Per Coffee 2026-05-10 ("When you force the fit on
            # the ground wheels, it makes the z distance too
            # tight.  do not use z as part of the fit on ground
            # wheels"): EXCLUDE road wheels from the active set
            # used by `_correct_R` for chain-length adjustment.
            # Scaling a road wheel's R subtly pulls its tangent
            # contact points -- on adjacent road wheels the
            # tangent line tilts and the inter-wheel Z spacing
            # of the contacts compresses, which the math then
            # propagates downstream as the 180-rotation
            # transition we were chasing.  Only sprocket /
            # idler / roller radii get to absorb the fit error
            # now; road wheels stay at their authored R.
            if (abs(math.degrees(d)) > 0.5
                    and wh.get('role') != 'road'):
                active.append(wh)
        tt = entry.get('tangent_to_next')
        if tt is not None:
            lin_sum += float(np.linalg.norm(tt[1] - tt[0]))
    return arcs, arc_sum, lin_sum, active


def _correct_R(loop, arcs, active, err):
    arc_total = 0.0
    for entry in arcs:
        w = entry['wheel']
        if w not in active:
            continue
        a_in  = entry.get('a_in')
        a_out = entry.get('a_out')
        if a_in is None or a_out is None:
            continue
        arc_total += w['R'] * abs(
            _short_arc_signed_diff(a_in, a_out))
    if arc_total <= 0:
        return 1.0
    k = 1.0 + err / arc_total
    for w in active:
        w['R'] *= k
    return k


def compute_chain_total(arcs_3):
    """Sum of all line + arc segment lengths in the closed chain.

    Per Coffee 2026-05-16 ("They both jump from time to time.  It
    looks like they jump backwards"): the wheel-position cache in
    `viewer._chain_for_side` invalidates whenever a wheel hub moves
    by >0.1 mm.  When that happens `arcs_3` is rebuilt and the
    new geometry can land at a slightly different `total` chain
    length.  Inside `_place_pads` the line
        s_off = s_offset % total
    means the same accumulated `s_offset` lands at a different
    `target` when `total` changes -- every pad jumps along the
    chain.

    This helper exposes the loop-length math `_place_pads` uses
    internally so the caller can scale its persistent `s_offset`
    by `new_total / last_total` BEFORE the next pad-placement,
    preserving the fractional position across the rebuild.

    Args:
        arcs_3 -- output of `_measure_loop` (the cache payload
                  `build_chain_segments` returns).  None or
                  empty -> returns 0.0.

    Returns:
        float -- summed length of every line + non-degenerate arc
                 segment around the closed chain (metres).
    """
    if not arcs_3:
        return 0.0
    DEGEN_ARC_RAD = 0.005
    total = 0.0
    for i, entry in enumerate(arcs_3):
        # Line segment from this wheel's exit contact to the next
        # wheel's entry contact.
        tt = entry.get('tangent_to_next')
        if tt is not None:
            total += float(np.linalg.norm(tt[1] - tt[0]))
        # Arc segment wrapping the NEXT wheel.
        nxt = arcs_3[(i + 1) % len(arcs_3)]
        a_in  = nxt.get('a_in')
        a_out = nxt.get('a_out')
        if a_in is None or a_out is None:
            continue
        d = _short_arc_signed_diff(a_in, a_out)
        if abs(d) < DEGEN_ARC_RAD:
            continue
        total += abs(d) * float(nxt['wheel']['R'])
    return float(total)


def _place_pads(arcs, n_pads, s_offset=0.0):
    """Walk the closed loop of alternating line + arc segments
    at uniform arc-length pitch.  Returns [(pos_2d, tan_2d), ...].

    Per Coffee 2026-05-10 ("look at image at the wheel" + 66 cm
    closure-pad gap): the arc walker now starts at `a_in` (where
    the previous line ENDS) and walks toward `a_out` (where the
    next line BEGINS) in the SHORT arc direction.  Old code had
    a_start = a_out which created a discontinuity at every line-
    to-arc seam -- harmless on small road-wheel arcs, catastrophic
    at sprocket wraps where IN and OUT are 170+ degrees apart.

    Per Coffee 2026-05-10 ("can you try and match the track
    speed to ground speed so the tracks are moving?"): the
    `s_offset` argument shifts every pad's arc-length sampling
    position by a constant.  Pad k samples the loop at
    `(k * pitch + s_offset) mod total_length` instead of just
    `k * pitch mod total_length`.  Animating `s_offset` over
    time (= integrating tank speed) makes the rendered chain
    flow along the loop in real-time.  Default 0.0 keeps the
    old static behaviour, so offline plot tools that call
    `compute_homie_chain` directly aren't affected.
    """
    # Each arc segment now stores (kind, hub, R, a_in, direction)
    # where direction is +1 (CCW) or -1 (CW) depending on which way
    # the SHORT arc from a_in to a_out goes.
    # Per Coffee 2026-05-10 ("all segs X on a track side has to
    # face the same way after transform" -- a single
    # backward-pointing pad showed up at the bottom of W_L1 / W_R1
    # on the offline diagnostic plot): the cause is a degenerate
    # pie arc where `a_in` and `a_out` are nearly equal -- the
    # chain only KISSES that wheel (no real wrap), the
    # `_short_arc_signed_diff` becomes numerically noisy, and the
    # SIGN of `direction` can land either way.  A pad sampled on
    # that micro-arc inherits a tangent that's 180 deg out from
    # its neighbours.
    #
    # Threshold: 0.005 rad ~= 0.3 deg.  Smaller arcs are tangent
    # touches, not real wraps -- skip them, let the adjacent
    # lines join directly at the contact point.  The arc-share
    # R-correction in `_correct_R` rebalances the loop's total
    # length against active wheels, so dropping a sub-mm arc
    # doesn't measurably shift the chain.
    DEGEN_ARC_RAD = 0.005
    segments = []
    for i, entry in enumerate(arcs):
        tt = entry.get('tangent_to_next')
        if tt is not None:
            segments.append(('line', tt[0], tt[1]))
        nxt = arcs[(i + 1) % len(arcs)]
        a_in  = nxt.get('a_in')
        a_out = nxt.get('a_out')
        if a_in is None or a_out is None:
            continue
        d = _short_arc_signed_diff(a_in, a_out)
        if abs(d) < DEGEN_ARC_RAD:
            # Degenerate / touch-point arc.  Skip -- no pad
            # should land here; the adjacent line tangents
            # carry continuously through the contact point.
            continue
        # d > 0  -> a_in is CCW of a_out  -> short arc CW (a decreases)
        # d < 0  -> a_in is CW of a_out   -> short arc CCW (a increases)
        direction = -1 if d > 0 else +1
        arc_len   = abs(d) * float(nxt['wheel']['R'])
        segments.append((
            'arc', nxt['wheel']['hub'],
            float(nxt['wheel']['R']), a_in, direction, arc_len))

    seg_lens = []
    for seg in segments:
        if seg[0] == 'line':
            seg_lens.append(
                float(np.linalg.norm(seg[2] - seg[1])))
        else:
            seg_lens.append(seg[5])   # precomputed arc_len
    total = sum(seg_lens)
    if total <= 0 or n_pads <= 0:
        return [], 0.0
    cum = []
    s = 0.0
    for L in seg_lens:
        cum.append(s)
        s += L
    pitch = total / n_pads
    pads = []
    # Wrap the s_offset into [0, total) so the modulo below has
    # well-defined behaviour for negative or large offsets
    # (Python's `%` is sign-friendly but normalising once at
    # entry keeps the per-pad loop body minimal).
    s_off = float(s_offset) % total if total > 0 else 0.0
    for k in range(n_pads):
        target = (k * pitch + s_off) % total
        seg_idx = len(segments) - 1
        for i, cs in enumerate(cum):
            if cs <= target < cs + seg_lens[i] + 1e-12:
                seg_idx = i
                break
        local_t = target - cum[seg_idx]
        seg = segments[seg_idx]
        if seg[0] == 'line':
            p0, p1 = seg[1], seg[2]
            sl = seg_lens[seg_idx]
            u = (p1 - p0) / max(sl, 1e-9)
            pos = p0 + local_t * u
            tan = u
            anchor_hub = None         # line pad -- no single wheel
        else:
            _, hub, R, a_in_seg, direction, _alen = seg
            # Walk from a_in toward a_out in the short direction.
            a = a_in_seg + direction * (local_t / R)
            pos = hub + R * np.array([math.cos(a), math.sin(a)])
            # Tangent = ccw-rotate radial by 90 deg, then sign by
            # direction so the chain tangent always points along
            # the chain's traversal sense (not the arc parameter
            # increase direction).
            tan = direction * np.array(
                [-math.sin(a), math.cos(a)])
            anchor_hub = hub          # arc pad -- carries its wheel
        # Per Coffee 2026-05-10 ("grab the line hub to wheel and
        # seg point and hinge point and take the tangent"): emit
        # the per-pad anchor alongside pos/tan so the renderer's
        # orientation pass can derive `up` from
        # `pad_center - hub` for arc pads (= exact radial outward
        # at the wheel that pad is currently wrapping), and fall
        # back to chord-perpendicular for line pads.  Three-
        # tuple per pad: (pos_2d, tan_2d, hub_2d_or_None).
        pads.append((pos, tan, anchor_hub))
    return pads, total


def build_chain_segments(bones, radii, roles, side, n_pads,
                          seg_len, inner_thickness=0.0):
    """Build the chain's line+arc segment list -- the heavy
    half of `compute_homie_chain`.  Per Coffee 2026-05-10
    ("once the spline shape be recycled?"): split out so a
    cache-aware caller can SKIP this work when the wheel
    bone positions haven't changed frame-to-frame and just
    re-call `_place_pads` on the cached segments.

    Per Coffee 2026-05-16 ("add pad inner offset to R"): the
    chain spline rides at `R + segmentsInnerThickness` -- a
    plain additive offset that pushes the wrap circle out by
    the pad's wheel-facing thickness so the rendered chain
    sits at the chain-link inner face instead of the wheel
    rim.  No sagitta / pad-centre correction (we tried that
    in v1.213.0; reverted because it was the wrong math
    after the user mapped out the geometry).
    `inner_thickness` defaults to 0.0 (= old behaviour) so
    callers that don't yet pass it keep working.

    Steps:
      _collect_wheels -> [R += inner_thickness per wheel]
        -> _order_loop -> _measure_loop

    Returns the post-correction `arcs_3` segment list, OR
    `None` on failure (missing inputs / degenerate loop).
    The wheel objects inside arcs_3 carry their CORRECTED
    radii after this call -- re-using them is the cache's
    raison d'etre, but caller MUST treat the returned data
    as frozen (re-running the shift on it would compound
    the correction).
    """
    if not bones or not radii or not roles:
        return None
    if not n_pads or n_pads <= 0 or not seg_len or seg_len <= 0:
        return None
    wheels = _collect_wheels(bones, radii, roles, side)
    if len(wheels) < 3:
        return None
    # Per Coffee 2026-05-16: shift every wheel's wrap radius
    # outward by the pad's inner thickness.  Applies to every
    # role (sprocket / idler / road / roller) because every
    # wheel in the chain's pie-arc loop has the chain wrap it
    # at the same offset from its rim.
    if inner_thickness and inner_thickness > 0.0:
        inner_t = float(inner_thickness)
        for w in wheels:
            w['R'] = float(w['R']) + inner_t
    loop = _order_loop(wheels)
    if len(loop) < 3:
        return None
    # Per Coffee 2026-05-11 ("our spline diameters are off..
    # make sure the correct dia wheels are use in the proper
    # locations of our spline"): SKIP `_correct_R` so every
    # wheel keeps its authored radius (= chassis XML's
    # `<wheelGroup><groupRadius>` minus segmentsInnerThickness
    # applied upstream).  The old _correct_R scaled active
    # wheels (sprockets / rollers) up by a factor `k` to make
    # the geometric loop length match `segmentLength *
    # segmentsCount` exactly -- but the cost was inflated
    # sprockets that no longer matched their authored visible
    # diameter.  Now the chain's natural geometric length is
    # used verbatim; pad pitch falls out as
    # `natural_length / n_pads`, typically within 1-2 % of
    # `segmentLength`.
    arcs_3, _, _, _ = _measure_loop(loop)
    return arcs_3


def assemble_chain_arrays(arcs_3, gauge_x, side, n_pads,
                           s_offset=0.0):
    """Per-frame light half: sample pads from a (possibly
    cached) `arcs_3`, build the chassis-local (pos, tan,
    hubs, on_arc) ndarrays.  Per Coffee 2026-05-10 ("once
    the spline shape be recycled?") -- this is the only
    work that has to happen every frame; `arcs_3` itself
    can be recycled when wheel bones haven't moved.

    Returns:
        (pos, tan, hubs, on_arc) -- same layout as
        `compute_homie_chain`'s return, or
        (None, None, None, None) on failure.
    """
    pads_2d, _ = _place_pads(arcs_3, n_pads,
                              s_offset=float(s_offset))
    if not pads_2d:
        return None, None, None, None
    is_left = side.lower().startswith('l')
    side_x  = (-0.5 * gauge_x) if is_left else (+0.5 * gauge_x)
    N = len(pads_2d)
    pos     = np.zeros((N, 3), dtype=np.float32)
    tan     = np.zeros((N, 3), dtype=np.float32)
    hubs    = np.zeros((N, 3), dtype=np.float32)
    on_arc  = np.zeros(N, dtype=bool)
    for i, (xy_pos, xy_tan, hub_or_none) in enumerate(pads_2d):
        pz, py = xy_pos[0], xy_pos[1]
        tz, ty = xy_tan[0], xy_tan[1]
        pos[i] = (side_x, float(py), float(pz))
        tan[i] = (0.0,    float(ty), float(tz))
        if hub_or_none is not None:
            hubs[i] = (side_x,
                       float(hub_or_none[1]),
                       float(hub_or_none[0]))
            on_arc[i] = True
    # Per Coffee 2026-05-11 ("you can never flip the direction
    # vector.. that's the you are home signal"): the v1.118.x
    # tangent-continuity sweep that flipped anti-aligned tan
    # vectors is REMOVED.  A flip between consecutive pad
    # tangents is a real signal -- the chain closing back on
    # itself, or a genuine direction discontinuity -- not a
    # noise artefact to be masked over.  Downstream code can
    # detect a flip via `dot(tan[i], tan[i-1]) < 0` and treat
    # it as the loop's "home" marker.
    return pos, tan, hubs, on_arc


def compute_homie_chain(bones, radii, roles, side, n_pads,
                         seg_len, gauge_x, s_offset=0.0):
    """Build the home-brewed track chain for one side.

    Args:
        bones      -- {bone_name: (x, y, z) array-like}
        radii      -- {bone_name: float}
        roles      -- chassis['wheel_roles'] dict
        side       -- 'left' or 'right'
        n_pads     -- pad count per side (= chassis['segmentsCount'])
        seg_len    -- chassis['segmentLength']
        gauge_x    -- inter-track gauge.  L gets X = -gauge_x/2,
                      R gets +gauge_x/2.

    Returns:
        (positions, tangents, hub_anchors, on_arc) -- the first
        two are (n_pads, 3) float32 in chassis-local XYZ as
        before; `hub_anchors` is (n_pads, 3) float32 carrying
        each arc-pad's wheel hub position (zeros for line
        pads); `on_arc` is (n_pads,) bool flagging arc pads.
        Caller uses (hub_anchors, on_arc) to derive a per-pad
        radial-outward up direction.  Returns
        (None, None, None, None) on failure.
    """
    arcs_3 = build_chain_segments(bones, radii, roles, side,
                                    n_pads, seg_len)
    if arcs_3 is None:
        return None, None, None, None
    return assemble_chain_arrays(arcs_3, gauge_x, side,
                                  n_pads, s_offset)
