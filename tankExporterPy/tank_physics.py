"""Per-wheel-on-terrain rigid-body physics for the loaded tank.

What this gives you
-------------------
* Sample the terrain Y under every wheel of the loaded tank,
  fit a plane through those points, and produce a chassis pose
  (4x4 matrix) that places the tank with its wheels visually
  resting on the ground.
* Per-wheel collision: if some wheels are above the terrain
  (cliff edge) and others aren't, the plane fit naturally
  tilts the tank toward the support points.  If EVERY wheel
  is above the terrain, gravity drops the chassis Y until at
  least one wheel catches.
* Suspension envelope clamp: each wheel's deflection is capped
  to the gameplay XML's `[minOffset, maxOffset]` so the chassis
  can't sink unrealistically far on soft ground.

What this does NOT do (yet)
---------------------------
* Per-vertex track sag.  The tank moves as a rigid body;
  individual track segments don't follow individual wheels
  (that needs the skinning shader, which is a separate arc).
* Wheel rotation around its own axis (driving the wheel disk
  spin during travel).  Not part of the suspension question
  the user is solving.
* Lateral / longitudinal friction, slope-induced sliding, etc.
  This is a kinematic positioner, not a full vehicle physics
  sim -- the goal is "tank reads as on the terrain", not
  "tank slides downhill".

Usage shape (from `Viewer.render`, once per frame)
--------------------------------------------------

    if self.tank_physics and self.terrain:
        chassis_pose = self.tank_physics.update(self.terrain, dt)
    else:
        chassis_pose = np.eye(4, dtype=np.float32)

    for mesh in self.meshes:
        # Compose: physics pose * load-time per-mesh transform
        active.set_mat4('model', chassis_pose @ mesh.model_matrix)

The composition `physics_pose @ mesh.model_matrix` keeps the
existing per-component layout (Hull / Chassis / Turret / Gun
each have their own load-time `model_matrix`) intact and just
prepends the physics motion as a rigid-body transform on top.
"""

import math
import re

import numpy as np


# ---------------------------------------------------------------------------
# Per-wheel state codes used by `last_wheel_state` (parallel to `wheels`).
# Consumed by:
#   * `bone_matrix_array` -- decides how to clamp the per-wheel bone Y
#     translation (CONTACT clamps to envelope; HANGING pins to extension
#     cap; OVER_COMPRESSED clamps to compression cap, same as CONTACT
#     but flagged separately so the highlight overlay can colour them
#     differently if we ever want to visualise bottoming-out).
#   * Viewer / shader -- maps to a colour overlay (red CONTACT,
#     green HANGING).
# ---------------------------------------------------------------------------
WHEEL_STATE_NONE       = 0   # not classified yet (first-frame default)
WHEEL_STATE_CONTACT    = 1   # touching ground, within suspension envelope
WHEEL_STATE_HANGING    = 2   # terrain too far below; droops at extension cap
WHEEL_STATE_OVER_COMP  = 3   # terrain too high; bottomed-out at compression cap


# ---------------------------------------------------------------------------
# Bone-name patterns recognised by `from_chassis_meshes` as ROAD WHEELS.
# Each pattern's first capture group resolves to the side ('L' or 'R').
#
#   * `W_<L|R>\d+_BlendBone`  -- standard tracked-tank rig.  T110E4 has
#     W_L0..W_L5 / W_R0..W_R5; T92 / E4 family extend to W_L6 / W_R6.
#     Eastern / Soviet TDs (Object 268/4) hit W_L9.
#   * `W_F_<L|R>_BlendBone`   -- wheeled front pair.  Used by the EBR
#     family (Panhard EBR 90 / 75 / 105, Hotchkiss EBR variants),
#     Bat-Chat Bourrasque, Lynx 6x6 on tanks where the artist was
#     careful with names.
#   * `W_R\d+_<L|R>_BlendBone` -- wheeled middle / rear axles on the
#     same vehicles.
#
# Edge case: the Hotchkiss EBR's renderSet tags ALL three road wheels
# per side as `W_L0` / `WD_L0` / `WD_L1` (one W_, two WD_) -- the WD_
# prefix is normally a "decorative wheel" (drive sprocket / idler /
# return roller) on TRACKED tanks but here the artist reused the
# tag for a real road wheel because the chassis only carries 7 bone
# slots and the names just happen to be wrong.  We catch this by
# also accepting `WD_<L|R>\d+_BlendBone` IF the post-name shape +
# position checks confirm the group is a real road wheel (low cy,
# disc-shaped bbox, hundreds of verts).  Tracked-tank WD_ bones
# (drive sprocket cy ~ 0.92, idler cy ~ 1.02, return rollers cy ~
# 1.10) all fail the cy <= 0.80 cut and stay rejected.
#
# We deliberately reject:
#   * `Track_<L|R>\d+_BlendBone` -- track ribbon segments (rest at the
#     bottom of the loop, cy ~ 0.07; not road wheels).
#   * `V_BlendBone`              -- the rigid hull bone for the chassis
#     superstructure.  Carries the side-skirts and other static dressing.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Force-balance constants (mirroring `cust_tools/test_force_balance.py`).
# Each wheel acts as a unit-stiffness spring; the chassis "weight" is
# normalised so that at full equal-load equilibrium every wheel sits
# at REST_COMPRESSION (the middle of its travel envelope), each
# delivering a force of `1.0` -- so total force at rest = N_wheels.
# ---------------------------------------------------------------------------
_FB_REST_LOAD_PER_WHEEL = 1.0   # normalised target spring force per wheel


def _force_balance_pos_y(target_y, local, pitch_deg, roll_deg,
                         comp_cap, ext_cap, max_inner=8):
    """Newton iteration for the chassis pos_y that satisfies vertical
    force balance.  Plane angles (pitch / roll) are FROZEN here;
    they're computed by the lstsq plane fit upstream and we only
    solve the vertical equilibrium.

    Each elastic-range wheel acts as a spring delivering force
    proportional to its compression; sum of spring forces must
    match the chassis weight.  Bottomed-out wheels (compression
    > comp_cap) deliver their max spring force; "extra" rigid-
    contact force beyond that is what the terrain floor handles
    afterwards.

    Returns the new pos_y.
    """
    REST_COMPRESSION_M = (comp_cap - ext_cap) * 0.5
    # Total weight = N * 1.0 (every wheel takes 1 unit at rest).
    # F_max per wheel = comp_cap / REST_COMPRESSION_M (~2.0 with defaults).
    if REST_COMPRESSION_M <= 0:
        REST_COMPRESSION_M = max(comp_cap, 0.02)
    N = len(target_y)
    W_total = N * _FB_REST_LOAD_PER_WHEEL

    # Compute per-wheel rigid_y under (pitch, roll, pos_y=0).
    pr = math.radians(pitch_deg)
    rr = math.radians(roll_deg)
    cp, sp = math.cos(pr), math.sin(pr)
    cr, sr = math.cos(rr), math.sin(rr)
    lx, ly, lz = local[:, 0], local[:, 1], local[:, 2]
    ly_post_rz = sr * lx + cr * ly
    ly_post_rx = cp * ly_post_rz - sp * lz   # this is rigid_y at pos_y=0
    rigid_y_no_pos = ly_post_rx
    comp_at_pos0   = target_y - rigid_y_no_pos    # comp if pos_y = 0

    # Initial guess: drop the chassis by REST_COMPRESSION so wheels
    # sit at neutral spring load.  Newton steps refine from there.
    pos_y = -REST_COMPRESSION_M
    for _ in range(max_inner):
        comp = comp_at_pos0 - pos_y
        comp_clipped = np.clip(comp, 0.0, comp_cap)
        # Force per wheel (normalised, K = 1).
        F = comp_clipped / REST_COMPRESSION_M
        sum_F = float(F.sum())
        err = sum_F - W_total
        # Sensitivity: d(sum_F)/d(pos_y) = -N_elastic / REST_COMPRESSION_M
        # (each elastic wheel's compression decreases by 1 when pos_y
        # rises by 1; over-cap and hanging wheels don't contribute).
        elastic = (comp > 0.0) & (comp <= comp_cap)
        n_elastic = int(elastic.sum())
        if n_elastic == 0:
            # Either all hanging (chassis way too high) or all
            # over-cap (way too low) -- both corner cases.
            # Bias the step in the direction of the error and let
            # the next outer iteration re-classify.
            pos_y += 0.01 if err > 0 else -0.01
            continue
        denom = n_elastic / REST_COMPRESSION_M
        d_pos = err / denom
        # Half-step damping: noisy classification across iterations
        # can swing the step around; 0.5 keeps convergence stable.
        pos_y += d_pos * 0.5
        if abs(d_pos) < 1e-5:
            break
    return pos_y


_WHEEL_NAME_PATTERNS = [
    # Tracked-rig: W_L0 / W_R6 / W_R7 / W_L9 ...  Side comes from group(1).
    # `confirm_with_shape=False` -- name alone is sufficient.
    (re.compile(r'^W_([LR])\d+(?:_BlendBone)?$'),    1, False),
    # Wheeled-rig front pair: W_F_L / W_F_R.  Side comes from group(1).
    (re.compile(r'^W_F_([LR])(?:_BlendBone)?$'),     1, False),
    # Wheeled-rig back axles: W_R1_L / W_R2_R ...  Side comes from group(1).
    (re.compile(r'^W_R\d+_([LR])(?:_BlendBone)?$'),  1, False),
    # Edge case: WD_<L|R>\d+ tagged as a real road wheel (Hotchkiss
    # EBR).  Name alone is ambiguous -- could also be a tracked-tank
    # decorative wheel.  Caller must confirm with shape + cy band.
    (re.compile(r'^WD_([LR])\d+(?:_BlendBone)?$'),   1, True),
]


def _wheel_side_from_name(name):
    """Return ``(side, needs_shape_confirmation)`` if `name` matches a
    known road-wheel bone pattern, else ``(None, False)``.

    `side` is 'left' or 'right'.  `needs_shape_confirmation` is True
    when the bone-name match is AMBIGUOUS (the WD_ case -- could be
    a real wheel on a wheeled tank or a decorative one on a tracked
    tank), so the caller must verify with bbox + cy checks before
    accepting the group.

    Pattern-order note: the tracked-rig `W_<L|R>\\d+` pattern is tested
    BEFORE the wheeled-rig patterns so a tracked `W_R5_BlendBone`
    matches the tracked path (side from `L|R` capture) and never
    falls through to the wheeled `W_R\\d+_<L|R>` regex (which would
    fail to find a trailing `_<L|R>` token anyway).
    """
    if not name:
        return None, False
    for rx, side_group, needs_confirm in _WHEEL_NAME_PATTERNS:
        m = rx.match(name)
        if m:
            side = 'left' if m.group(side_group) == 'L' else 'right'
            return side, needs_confirm
    return None, False


# ---------------------------------------------------------------------------
# Track-segment -> road-wheel name mapping
# ---------------------------------------------------------------------------
_TRACK_RX = re.compile(r'^Track_([LR])(\d+)(?:_BlendBone)?$')


def _track_to_wheel_name(name):
    """Map a `Track_<L|R><i>_BlendBone` track-segment bone name to
    the corresponding `W_<L|R><i>_BlendBone` road-wheel bone, or
    None if `name` isn't a track segment.

    Tracked tanks bind each contiguous chunk of the bottom track
    run to a `Track_<side><i>_BlendBone` bone whose mesh-local
    position sits directly under road wheel <i>.  Both bones must
    deflect together so the track stays touching the wheel; we
    achieve this by giving the track bone the same Y translation
    matrix as its companion road-wheel bone.

    Returns the COMPANION road-wheel name (string) or None.
    """
    if not name:
        return None
    m = _TRACK_RX.match(name)
    if not m:
        return None
    side, idx = m.group(1), m.group(2)
    return f'W_{side}{idx}_BlendBone'


def _looks_like_wheel(n_verts, ext, cy):
    """Shape + position check for "is this group a road wheel?".

    Used by both the heuristic-only path (no palette) and the
    name-confirmation path (WD_ bones whose name alone is ambiguous).

    Returns True when:
      * `n_verts >= 100`           -- a wheel mesh is non-trivial.
      * `0.30 <= dy <= 1.50`       -- disc diameter range.  Tracked
        road wheel ~0.66 m; Hotchkiss EBR road wheel ~1.21 m.  The
        upper bound 1.50 m gives a margin without admitting hull-sized
        bones (V_BlendBone hits dz ~ 5+ m).
      * `0.30 <= dz <= 1.50`       -- same disc diameter check on Z.
      * `cy <= 0.80`               -- wheel hubs sit near the chassis
        floor.  Tracked road wheel cy ~ 0.40; large wheeled-vehicle
        wheel cy ~ 0.62.  Tracked drive sprockets / idlers / return
        rollers all live at cy >= 0.85 and fail this cut.
    """
    if n_verts < 100:
        return False
    if not (0.30 <= ext[1] <= 1.50):
        return False
    if not (0.30 <= ext[2] <= 1.50):
        return False
    if cy > 0.80:
        return False
    return True


# ---------------------------------------------------------------------------
# T110E4 wheel-rig data.  Hardcoded for now -- the data we extracted from
# the chassis primitive analysis (mesh-local centroids per W_<side><i>
# group + main-road-wheel radius from the gameplay XML).  Every WoT tank
# follows the same overall shape; future work: parse this on tank load
# from the chassis primitives so it works for any tank.
# ---------------------------------------------------------------------------
T110E4_WHEELS = {
    # (mesh-local x, y, z), main-road-wheel radius from gameplay XML
    # (scripts/item_defs/vehicles/usa/A83_T110E4.xml -> wheelGroups).
    'radius':           0.32952,
    # Suspension travel envelope, also from the gameplay XML
    # (groundNodes -> minOffset, maxOffset).  Negative = wheel rises
    # ABOVE bind-pose neutral; positive = wheel drops BELOW.
    'min_offset':      -0.04,
    'max_offset':      +0.08,
    # Mesh-local centroid of each `W_<side><i>` chassis group.
    # Z VALUES NEGATED from the raw chassis-primitives dump because
    # TEPY's rendered world Z direction is opposite to the
    # primitives-file's own Z direction (per Professor Coffee's
    # observation: "Z is tracking front to as back of tank" --
    # wheels at primitives Z=-1.95 actually appear at the visible
    # FRONT of the tank in TEPY's render, so we negate the values
    # here so the physics samples terrain at the visually-correct
    # world XZ).  Sequence: visible REAR -> visible FRONT.
    'wheels_left':  [
        (-1.547, +0.425, +1.952),   # W_L0 (visible rear)
        (-1.547, +0.425, +1.261),   # W_L1
        (-1.547, +0.425, +0.481),   # W_L2
        (-1.547, +0.425, -0.287),   # W_L3
        (-1.547, +0.425, -1.058),   # W_L4
        (-1.547, +0.425, -1.901),   # W_L5 (visible front)
    ],
    'wheels_right': [
        (+1.547, +0.425, +1.952),   # W_R0 (visible rear)
        (+1.547, +0.425, +1.261),   # W_R1
        (+1.547, +0.425, +0.481),   # W_R2
        (+1.547, +0.425, -0.287),   # W_R3
        (+1.547, +0.425, -1.058),   # W_R4
        (+1.547, +0.425, -1.901),   # W_R5 (visible front)
    ],
}


# ---------------------------------------------------------------------------
# Plane fit + Euler conversion.  Same recipe as
# cust_tools/demo_terrain_corners.py -- factored here so it's
# shared between the offline demo and the runtime physics.
# ---------------------------------------------------------------------------
def fit_plane(pts):
    """Least-squares plane fit through 3+ points in R^3.

    Fits `y = a*x + b*z + c` via `np.linalg.lstsq` -- a constrained
    formulation that ALWAYS produces a normal with a positive
    Y component (since the model is Y as a function of X, Z).
    Returns `(unit_normal, centroid)`.

    Why not the eigenvector-of-smallest-covariance approach: that
    one assumes the smallest-variance direction IS the plane
    normal, which is true for a generic 3-D point cloud but
    DEGENERATES when the cloud is approximately a 2-D strip
    (terrain that varies dominantly in one horizontal direction
    -- e.g. driving straight across a long ridge).  In that case
    the smallest-variance direction is the OTHER horizontal axis,
    not the normal, and the resulting "normal" is horizontal -->
    pitch/roll go to +/-90 deg.  The lstsq form is immune to that
    because the unknown is always Y; the fit can't tip into
    "the plane is vertical".
    """
    pts = np.asarray(pts, dtype=np.float64)
    if len(pts) < 3:
        return np.array([0.0, 1.0, 0.0]), pts.mean(axis=0)
    A = np.column_stack([pts[:, 0], pts[:, 2], np.ones(len(pts))])
    y = pts[:, 1]
    coeffs, *_ = np.linalg.lstsq(A, y, rcond=None)
    a, _b, _c = coeffs[0], coeffs[1], coeffs[2]
    b = coeffs[1]
    # Plane: y - a*x - b*z = c.  Normal pointing "up" =
    # (-a, 1, -b), then normalise.
    n = np.array([-a, 1.0, -b], dtype=np.float64)
    n = n / np.linalg.norm(n)
    cen = pts.mean(axis=0)
    return n, cen


def normal_to_pitch_roll(n):
    """Plane normal -> (pitch_deg, roll_deg) in standard aviation
    convention (per the diagram Professor Coffee posted):

      * Pitch -- rotation around the X (lateral) axis.  +ve =
        nose UP.  Drives `mat4_rotate_x`.
      * Roll  -- rotation around the Z (longitudinal) axis.  +ve =
        right-side / right wing UP.  Drives `mat4_rotate_z`.

    Sign derivation (aviation convention + GL right-handed-ish
    coords with +Y up, +Z forward, +X right):

      * Tank on an UPHILL slope (terrain rising in driving
        direction = +Z): wheels at +Z are on higher terrain ->
        plane normal tilts toward -Z (nz < 0) -> aviation +pitch
        (nose UP).  We want `mat4_rotate_x` to lift a point at
        +Z, which requires NEGATIVE `pitch_rad` (the matrix
        rotates +Z down for positive angles in our convention).
        atan2(+nz, ny) with nz < 0 -> negative -> correct.

      * Tank with right-side terrain HIGHER (rising on +X): wheels
        at +X are on higher terrain -> plane normal tilts toward
        -X (nx < 0) -> aviation +roll (right wing UP).  We want
        `mat4_rotate_z` to lift +X point, which requires
        POSITIVE `roll_rad` (the matrix rotates +X up for
        positive angles).  atan2(-nx, ny) with nx < 0 ->
        atan2(+, +) -> positive -> correct.

    Both signs were wrong in the v1.80.0 first cut; they're
    fixed here together so the chassis behaves correctly under
    both pitch and roll.  See git log for the meandering path
    that got us here.
    """
    nx, ny, nz = float(n[0]), float(n[1]), float(n[2])
    pitch_rad  = math.atan2(+nz, ny)
    roll_rad   = math.atan2(-nx, ny)
    return math.degrees(pitch_rad), math.degrees(roll_rad)


# ---------------------------------------------------------------------------
# 4x4 matrix composition helpers.  All matrices are row-major float32 to
# match the existing shader uniform-upload path (TerrainShader,
# ShaderProgram both call glUniformMatrix4fv with `transpose=GL_TRUE`).
# ---------------------------------------------------------------------------
def mat4_translate(x, y, z):
    m = np.eye(4, dtype=np.float32)
    m[0, 3] = float(x); m[1, 3] = float(y); m[2, 3] = float(z)
    return m


def mat4_rotate_x(rad):
    c, s = math.cos(rad), math.sin(rad)
    m = np.eye(4, dtype=np.float32)
    m[1, 1] =  c; m[1, 2] = -s
    m[2, 1] =  s; m[2, 2] =  c
    return m


def mat4_rotate_z(rad):
    c, s = math.cos(rad), math.sin(rad)
    m = np.eye(4, dtype=np.float32)
    m[0, 0] =  c; m[0, 1] = -s
    m[1, 0] =  s; m[1, 1] =  c
    return m


# ---------------------------------------------------------------------------
# TankPhysics -- the per-frame update + chassis-matrix consumer.
# ---------------------------------------------------------------------------
class TankPhysics:
    """Per-wheel-on-terrain rigid-body solver.

    Per-frame `update()` samples the terrain Y under every wheel,
    fits a plane through them, and produces a chassis pose (4x4
    matrix in row-major float32).  Falling-when-unsupported is
    handled by integrating a vertical velocity when every wheel
    is above the terrain -- gravity ramps the chassis Y down
    until at least one wheel catches.

    Args:
        wheels_left  (list[(x, y, z)]): mesh-local centres for the
            left-side wheels.
        wheels_right (list[(x, y, z)]): same for the right side.
        radius       (float): wheel radius in metres.  Used to
            translate "ground Y under the wheel" into "wheel-centre
            target Y".
        min_offset, max_offset (float): suspension travel envelope
            in metres (relative to bind-pose).  Negative = rise.
        gravity      (float): m/s^2 downward acceleration when no
            wheel is on the ground.  Default 29.43 m/s^2 = 3x Earth
            (was 58.92 = 6x).  TEPY's models are visually smaller
            than the real-world tanks they represent, so a higher-
            than-Earth gravity scale gives "tank-feel-heavy" fall
            timing without falls feeling too fast/snappy.  Knob
            tunable per session via `--gravity` if added later.
    """

    def __init__(self, wheels_left, wheels_right,
                 radius=0.33, min_offset=-0.04, max_offset=+0.08,
                 gravity=29.43, track_thickness=0.016,
                 mass_kg=30000.0,
                 max_yaw_rate_dps=60.0):
        self.wheels = np.asarray(
            list(wheels_left) + list(wheels_right), dtype=np.float64)
        self.n_left  = len(wheels_left)
        self.n_right = len(wheels_right)
        self.radius     = float(radius)
        self.min_offset = float(min_offset)
        self.max_offset = float(max_offset)
        self.gravity    = float(gravity)
        # Track ribbon thickness in metres.  Real-world tank
        # tracks sit ~2-4 cm below the wheel CENTRE (the wheel
        # rolls on the inner face of the track, outer face
        # contacts ground).  T110E4's gameplay XML has
        # `<renderModelOffset>0.016214</renderModelOffset>` in
        # the `<tracks><trackPair><trackDebris><physicalParams>`
        # block -- about 1.6 cm.
        #
        # Default bumped to 60 mm because the WoT renderModelOffset
        # alone is too thin to read at TEPY's rendering scale --
        # 1.6 cm of ground gap blends into a few-mm aliasing band
        # at typical camera distances.  60 mm reads visually
        # without making the chassis sit unrealistically high.
        # Per-tank parsing of the XML field can override later.
        self.track_thickness = float(track_thickness)

        # Per-tank lift to raise the chassis by HALF a track-pad's
        # height, so the pads' lowest face contacts the terrain
        # rather than the rubber-band-thickness line.  Set by the
        # viewer at tank load (after pad meshes load) from the
        # measured pad bbox; left at 0 for tanks without per-pad
        # mesh data so the original physics behaviour is unchanged
        # in that case.  Added to the wheel-center target Y in
        # `update()` and the render-side terrain floor in
        # `_step_pose_integrator`.
        self.pad_lift = 0.0

        # World pose state -- translation + rotation (pitch X, roll Z).
        # Yaw is user-driven and exposed separately so external
        # controls (camera / keyboard) can steer the tank without
        # the physics overriding the heading.
        self.pos     = np.array([0.0, 0.0, 0.0], dtype=np.float64)
        self.yaw_deg = 0.0
        self.pitch_deg = 0.0
        self.roll_deg  = 0.0
        # Vertical velocity for the falling case (m/s).
        self.vy      = 0.0

        # Last per-wheel debug data so external code (overlays)
        # can render contact-point markers without re-querying
        # the terrain.
        self.last_wheel_world = np.zeros((len(self.wheels), 3),
                                          dtype=np.float64)
        self.last_terrain_y   = np.zeros(len(self.wheels),
                                          dtype=np.float64)
        self.last_target_y    = np.zeros(len(self.wheels),
                                          dtype=np.float64)
        self.last_supported   = np.zeros(len(self.wheels),
                                          dtype=bool)
        # Parallel to `self.wheels`: each entry is one of WHEEL_STATE_*.
        # Used by `bone_matrix_array` to pick the right per-wheel bone
        # matrix (compression-clamped vs droop-clamped) and by the
        # viewer to upload a red/green highlight.
        self.last_wheel_state = np.zeros(len(self.wheels),
                                          dtype=np.int32)
        # Per-wheel mesh-local Y residual after the rigid plane fit.
        # Drives the GPU skinning shader's per-wheel deflection.
        # Populated by `update()` -> `_compute_residual_y()`; left
        # at zero before the first tick so a freshly-loaded tank
        # with no terrain is rendered at bind pose.
        self.last_residual_y  = np.zeros(len(self.wheels),
                                          dtype=np.float64)
        # Per-wheel asymmetric-damped value used to drive the shader
        # bone matrix.  Kept distinct from `last_residual_y` (which
        # holds the pre-damp lstsq output for diagnostics) so we can
        # always rebuild the smoothed value from a known starting
        # state if the user toggles physics off / on mid-session.
        self.smoothed_residual_y = np.zeros(len(self.wheels),
                                             dtype=np.float64)
        # Per-wheel UNCLAMPED contact-classifier delta (target - rigid
        # Y for the target chassis pose).  This is the raw "wheel
        # wants to compress (+) or extend (-)" amount the classifier
        # reads each frame BEFORE the envelope clamp.  Exposed for
        # the F3 manual recorder so it can compute hysteresis-edge
        # proximity per wheel (`d - (ext_cap +/- HYST)` etc.) and
        # flag frames where a wheel is sitting on the threshold of
        # flipping CONTACT <-> HANGING <-> OVER_COMP.  Populated by
        # `update()` immediately after the classifier runs.
        self.last_delta_y = np.zeros(len(self.wheels),
                                      dtype=np.float64)
        # Integrator telemetry.  Snapshotted at the END of
        # `_step_pose_integrator()` so external diagnostics can read
        # the EFFECTIVE pitch / roll target the inertia spring is
        # chasing (= solver target + lateral / longitudinal lean
        # offsets) plus the per-axis error terms (`e_p`, `e_r`,
        # `e_y`) the spring is integrating.  These are the inputs
        # to the second-order ODE step; non-zero values that fail
        # to decay over multiple frames are the canonical signature
        # of an oscillation feedback loop.
        self._last_target_pitch_with_lean = 0.0
        self._last_target_roll_with_lean  = 0.0
        self._last_e_pitch_deg            = 0.0
        self._last_e_roll_deg             = 0.0
        self._last_e_y_m                  = 0.0
        self._last_lift_needed_m          = 0.0   # render-floor lift this frame
        # Bone names that were accepted as road wheels during
        # auto-extract.  Set by `from_chassis_meshes`; left empty
        # for the legacy `for_t110e4()` factory (which doesn't have
        # palette names to record).  `bone_matrix_array` consults
        # this set to decide which bones get a residual translation
        # vs which stay at identity -- crucially, this lets us
        # ACCEPT a Hotchkiss EBR's `WD_L0_BlendBone` (real wheel,
        # confirmed by shape) and REJECT a tracked tank's
        # `WD_L0_BlendBone` (drive sprocket, rejected by the Y-band
        # post-pass) under the same name.  Without this set we'd
        # have to re-run the entire shape + Y-band pipeline every
        # frame to make the right call.
        self.wheel_bone_names = []

        # ---- Mass + center-of-mass --------------------------------
        # `mass_kg` is the total tank weight from the gameplay XML's
        # per-component `<weight>` summed by `VehicleXMLLoader`.
        # T30 = 61 t; M60 ~ 50 t.  Drives the inertia-damped pose
        # integrator below: heavier tank -> slower angular response.
        #
        # `com_local` is the chassis-local centroid of all wheel
        # positions -- the simplest CoM placement the user requested
        # ("center of plane") that doesn't need any extra data.  For
        # symmetric road-wheel layouts (most tracked tanks) this is
        # ~(0, ~0.43, 0) -- middle of the wheels, at hub height.
        # Asymmetric tanks (T30's rear casemate, the gun overhang)
        # would benefit from a component-weighted CoM later -- this
        # is the v1 placement, good enough to start the inertia work.
        self.mass_kg   = float(mass_kg)
        self.com_local = (self.wheels.mean(axis=0).copy()
                          if len(self.wheels) > 0
                          else np.zeros(3, dtype=np.float64))

        # ---- Yaw rate cap (per-tank) ------------------------------
        # Source: chassis `<rotationSpeed>` from the gameplay XML,
        # in DEGREES PER SECOND (verified vs T110E4 = 26 dps; turret
        # `<rotationSpeed>` in the same unit).  Caps both manual yaw
        # input (A/Q/D/E keys) and auto-circle yaw (omega = v / R) so
        # the tank can never spin faster than the real chassis would.
        # Default 60 dps (= the previous hardcoded constant in
        # viewer.handle_input) when XML doesn't supply one -- that's
        # roughly an Abrams / E5-class fast hull, a safe cap for any
        # tank that doesn't publish rotationSpeed.
        #
        # Real-tank values from WoT XMLs (dps):
        #   T30        ~22  (heavy, slow turner)
        #   T110E4      26  (heavy TD)
        #   M60         48  (medium)
        #   AMX-50B     54  (autoloader heavy)
        # Auto-circle effects: a tight radius (small R) at high
        # speed demands omega = v / R that can exceed the cap.  When
        # clamped, the visible arc widens -- a 50 kph tank with a 26
        # dps cap can't make a 25 m circle; viewer clamps and the
        # tank traces a wider arc.  This is physically correct.
        self.max_yaw_rate_dps = float(max_yaw_rate_dps)

        # ---- Inertia-damped pose integrator state -----------------
        # Pre-2026-05-08 the chassis pose was hard-snapped to the
        # plane-fit + force-balance result every frame.  That made
        # the chassis instantly track the contact set's lstsq plane
        # -- works fine on smooth terrain, fails catastrophically
        # when terrain features lift most wheels off ground in one
        # frame: the plane fit through the few remaining contacts
        # can return a near-vertical normal, and the chassis
        # instantly snaps to a 89-degree roll (= "tank on its side")
        # in a single frame.  Witnessed in M60 turn-test data at
        # frame 737 (auto-circle radius 25 m, top speed 60 kph,
        # crossing a Perlin-terrain hill peak).
        #
        # Fix: treat the solver's output as a TARGET pose; integrate
        # the ACTUAL chassis pose toward it via a critically-damped
        # second-order spring.  Mass scales the natural frequency
        # so heavier tanks respond more slowly -- 61-tonne T30 can
        # NEVER pitch from 0 to 89 degrees in 13 ms because doing so
        # requires angular acceleration of ~115,000 deg/s^2 that the
        # spring will not deliver.
        #
        # Why this didn't work in v1.93.0: the contact classifier
        # AND the iterative-refinement loop were reading the lagged
        # integrated pose, then refitting the plane through wheels
        # classified against THAT pose -- positive feedback.  Fix
        # this time: solver computes target on TARGET locals only,
        # never reads `self.pitch_deg / roll_deg / pos[1]` while
        # building the target.  Integrator runs once at the end
        # with the locked-in target.
        self.omega_pitch_dps = 0.0    # deg/s, chassis pitch rate
        self.omega_roll_dps  = 0.0    # deg/s, chassis roll  rate
        # Render-pose state (lagged behind the solver's target via
        # the second-order integrator).  `chassis_matrix()` returns
        # a matrix built from these (the visible chassis) -- the
        # solver internals use `_chassis_matrix_target()` which
        # reads `self.pos / pitch_deg / roll_deg` (the target).
        self._render_pos_y     = 0.0
        self._render_pitch_deg = 0.0
        self._render_roll_deg  = 0.0
        self._render_vy        = 0.0  # m/s, integrator vertical vel

        # ---- Centripetal lean ("kid out a side window") ------------
        # Each frame we measure yaw rate (degrees/sec) + forward
        # speed (m/s) from chassis-state deltas, derive lateral
        # acceleration `a_lat = speed * yaw_rate_rad/s`, and apply
        # a steady-state lean target = atan(a_lat / g) to the
        # INTEGRATOR (not the solver) so the contact classifier
        # never sees this lean -- avoids re-creating the v1.93.0
        # feedback bug on the roll axis.
        #
        # Sign convention (per-axis tested on M60 turn run):
        #   yaw_rate > 0  -> right turn (chassis-yaw clockwise
        #                   viewed from above)
        #   centripetal   -> -X chassis-local (toward turn centre,
        #                   i.e. tank's left side)
        #   outward lean  -> +X chassis-local (tank's right side
        #                   drops) -> roll_deg becomes NEGATIVE
        #   so target_lean_roll_deg = -atan(a_lat / g) [degrees]
        #
        # `lateral_lean_gain` scales this between 0 (rigid -- no
        # lean) and 1 (full geometric).  Real tanks are stiffer than
        # cars; 0.5-0.8 typical.  Default 0.7 = noticeable lean
        # without overdriving the integrator.
        self.last_yaw_deg_for_lat        = 0.0
        self.last_pos_x_for_lat          = 0.0
        self.last_pos_z_for_lat          = 0.0
        self.lateral_lean_gain           = 0.7
        self._last_a_lat_mps2            = 0.0   # telemetry
        self._last_speed_mps             = 0.0   # telemetry
        # Per Coffee 2026-05-16 ("one is speeding up and slowing
        # down...  jump from time to time"): the chain-animation
        # code reads `_last_yaw_rate_dps` to differentiate per-side
        # speeds (v_L = v - omega*b, v_R = v + omega*b).  The raw
        # `Δyaw / dt` is noisy at low speed -- tiny yaw jitter
        # divided by tiny dt produces a flickering omega which
        # shows up as one track speeding up while the other slows.
        # Field now carries an EMA-smoothed value (alpha = 0.20,
        # 5-frame trailing average) so downstream consumers get
        # a clean signal.  The raw per-frame value is preserved at
        # `_last_yaw_rate_dps_raw` for any diagnostic / telemetry
        # consumer that genuinely wants instantaneous deltas.
        self._last_yaw_rate_dps          = 0.0   # smoothed (EMA)
        self._last_yaw_rate_dps_raw      = 0.0   # raw Δyaw / dt
        self._target_lat_lean_roll_deg   = 0.0   # integrator offset

        # Per Coffee 2026-05-13 ("we are adding wheel rotations"):
        # one accumulated angle per road-wheel bone, in radians.
        # Caller advances each frame via `advance_wheel_angles
        # (v_L, v_R, dt)`; `bone_matrix_array` reads it to
        # compose a rotation about each wheel's hub into the
        # bone's skin matrix.  Parallel to `wheel_bone_names`.
        self.wheel_angles_rad = np.zeros(
            len(self.wheels), dtype=np.float32)
        # Per Coffee 2026-05-13 ("rotate all wheels"): extras --
        # drive sprockets / idlers / return rollers.  Populated
        # via `set_extra_rotating_wheels`; bone_matrix_array
        # applies the same hub-centred Rx rotation to these as
        # to road wheels.  No Y residual (they're chassis-rigid).
        self.extra_rotating_bones      = []
        self.extra_rotating_hubs       = np.zeros((0, 3),
                                                    dtype=np.float32)
        self.extra_rotating_radii      = np.zeros((0,),
                                                    dtype=np.float32)
        self.extra_rotating_angles_rad = np.zeros((0,),
                                                    dtype=np.float32)
        self._extra_rot_name_to_idx    = {}

        # ---- Longitudinal lean (squat under accel, dive under brake) -
        # Mirror of the lateral-lean: derive longitudinal accel from
        # chassis-forward signed speed delta, apply atan(a_long/g)
        # to the integrator-side PITCH target so the contact
        # classifier never sees this lean either.
        #
        # Sign convention (EMPIRICALLY VERIFIED on the M60 run,
        # 2026-05-08 -- earlier comments here were wrong):
        #   pitch_render > 0  =>  nose UP    on screen
        #   pitch_render < 0  =>  nose DOWN  on screen
        # so target_pitch_offset = +atan(a_long / g) gives the
        # right sign: accel forward (a_long>0) -> +offset ->
        # nose lifts (squat); brake (a_long<0) -> -offset ->
        # nose dives.  See `_update_lateral_lean` for the actual
        # formula.
        #
        # `longitudinal_lean_gain` 0.5 default -- tank suspension is
        # stiffer in the longitudinal direction than the lateral
        # (long arm = full tank length; lateral arm = half track
        # width).  0.5 reads as "weight transfer" without overshoot.
        self.last_signed_speed_mps         = 0.0
        self.longitudinal_lean_gain        = 0.5
        self._last_a_long_mps2             = 0.0  # telemetry
        self._last_signed_speed_mps        = 0.0  # telemetry
        self._target_long_lean_pitch_deg   = 0.0  # integrator offset

        # ---- Smooth-speed input from the drive layer --------------
        # `cur_forward_mps` is set externally each frame by the
        # viewer's drive code (the ramped `_current_forward`,
        # negated to flip viewer's "forward = negative" convention
        # to physics's "forward = positive +").  Used by
        # `_update_lateral_lean` instead of deriving speed from
        # chassis position deltas, which had ~2x per-frame noise
        # from float-precision jitter + dt jitter (12-27 ms range
        # in the M60 turn-test recording, t=0..11s).  See the
        # 2026-05-08 turn-test analysis: 9% of frames had
        # |a_long|>200 m/s^2 from the position-derived speed
        # alone; with `cur_forward_mps` the noise floor drops
        # to single-digit m/s^2.
        self.cur_forward_mps = 0.0
        # EMA-smoothed accelerations (alpha = 0.20 -> 5-frame
        # trailing average at 60 fps) so the atan(a/g) lean
        # targets don't pump on dt jitter or speed-ramp
        # discontinuities.
        self._smoothed_a_lat_mps2  = 0.0
        self._smoothed_a_long_mps2 = 0.0

        # Per Coffee 2026-05-11 ("get the physics to use this
        # spline"): chassis-local XYZ of the homie chain bottom
        # run pads per side.  Set ONCE per tank load via
        # `set_homie_bottom_run` after the viewer has built the
        # bind-pose homie chain.  When non-None, the plane fit
        # in `update()` uses these points (instead of wheel
        # hubs) as terrain-sample sites -- the chain bottom run
        # is the actual track-on-ground contact line, so its
        # terrain profile drives the chassis pitch/roll more
        # faithfully than discrete wheel hubs do.
        #
        # Per-wheel suspension classification still runs on
        # wheel hubs as before -- only the lstsq plane fit
        # source is swapped out.
        self._homie_bottom_local_L = None   # (N, 3) np.float64
        self._homie_bottom_local_R = None   # (M, 3) np.float64
        # Cached concatenation built on first use -- (N+M, 3).
        self._homie_bottom_local   = None

    def set_homie_bottom_run(self, local_L, local_R):
        """Receive the bind-pose homie chain bottom-run pad
        positions in chassis-local XYZ -- one (N, 3) array per
        side, or None to disable chain-based plane fitting.

        The viewer calls this once per tank load (right after
        computing the bind-pose homie chain).  Per Coffee
        2026-05-11 ("get the physics to use this spline").
        """
        def _to_arr(p):
            if p is None or len(p) == 0:
                return None
            return np.asarray(p, dtype=np.float64).reshape(-1, 3)
        self._homie_bottom_local_L = _to_arr(local_L)
        self._homie_bottom_local_R = _to_arr(local_R)
        parts = [a for a in (self._homie_bottom_local_L,
                              self._homie_bottom_local_R)
                 if a is not None]
        if parts:
            self._homie_bottom_local = np.concatenate(parts, axis=0)
        else:
            self._homie_bottom_local = None

    # ------------------------------------------------------------------
    @classmethod
    def for_t110e4(cls):
        """Convenience factory pre-loaded with T110E4 wheel data.
        Kept for the legacy in-app default; new code should prefer
        `from_chassis_meshes` so the rig works for any tank."""
        d = T110E4_WHEELS
        # See note in the from_chassis_meshes T110E4 fallback:
        # T110E4_WHEELS data is Z pre-negated and must be un-negated
        # at the use site to align with the post-GPU-skinning chassis
        # frame.  Negate once here; the data file stays as-is.
        wl = [(x, y, -z) for (x, y, z) in d['wheels_left']]
        wr = [(x, y, -z) for (x, y, z) in d['wheels_right']]
        return cls(wl, wr,
                   radius=d['radius'],
                   min_offset=d['min_offset'],
                   max_offset=d['max_offset'])

    # ------------------------------------------------------------------
    @classmethod
    def from_chassis_meshes(cls, chassis_meshes,
                            radius=0.33,
                            min_offset=-0.04,
                            max_offset=+0.08,
                            track_thickness=0.016,
                            mass_kg=30000.0,
                            max_yaw_rate_dps=60.0,
                            wheel_roles=None):
        """Build a TankPhysics rig by walking already-loaded chassis
        sub-meshes (the `Mesh` objects in `Viewer.meshes` whose
        `component == 'chassis'`).  Discovers wheel groups by
        clustering verts by their dominant `iii` byte and pairing
        the resulting groups against the renderSet bone palette.

        For each wheel of the form `W_<side><i>_BlendBone`, the
        group's vertex CENTROID becomes the suspension-arm
        attachment point.  Returns a fully-wired `TankPhysics`
        with `wheels_left` and `wheels_right` populated, plus the
        Z values NEGATED to match TEPY's rendered world-Z
        convention (visible front of the tank lands at world -Z
        because the hull is Z-flipped on load while the skinned
        chassis is not).

        Falls back to T110E4 hardcoded data if no chassis sub-
        meshes are found OR no W_ wheel groups are detected.

        Args:
            chassis_meshes: iterable of `Mesh` instances whose
                `component == 'chassis'`.  Each must carry
                `bone_indices` / `bone_weights` (skinned), and
                optionally `bone_palette` (a list of bone names
                in renderSet declaration order).  When
                `bone_palette` is missing we still extract by
                grouping but can't filter to W_ vs WD_ vs V_
                groups, so we keep everything.
            radius      (float): default wheel radius in metres
                if the gameplay XML wasn't parsed.  T110E4 uses
                0.33; close enough for most tanks.
            min_offset, max_offset (float): suspension envelope
                in metres; same defaults as T110E4 (-0.04, +0.08).

        Returns:
            TankPhysics
        """
        # Aggregate per-vertex positions by KEY across every chassis
        # sub-mesh before computing centroids.  The KEY is the bone
        # NAME when `mesh.bone_palette` is populated -- that's the
        # canonical path; the heuristic fallback below uses a synthetic
        # `__byte_<idx>__<mesh.name>` key when no palette is available
        # (rare; happens when the visual_processed walks failed).
        #
        # Why aggregate before centroiding: a chassis primitive group
        # gets split into N sub-meshes by `_split_into_submeshes` (one
        # per material), each becoming its own Mesh object.  A wheel's
        # verts can straddle multiple sub-meshes (typically rim / hub /
        # tyre share one bone bind but separate materials).  Computing
        # a centroid per sub-mesh then appending each as its own wheel
        # would produce duplicated (and slightly displaced) wheel
        # positions -- bad for the plane fit.  Aggregating first makes
        # the answer independent of how the source was split.
        #
        # Track ribbon meshes (`track_<L|R>_Shape`) DO contribute
        # vertex bytes pointing into Track_*_BlendBone bones; those are
        # filtered OUT below by the name-based check.  Bookkeeping the
        # full chassis here is fine.
        key_to_pts = {}
        for mesh in chassis_meshes or ():
            bi = getattr(mesh, 'bone_indices',  None)
            bw = getattr(mesh, 'bone_weights',  None)
            palette = getattr(mesh, 'bone_palette', None)
            positions = getattr(mesh, 'positions', None)
            mesh_name = getattr(mesh, 'name', '?')
            if (bi is None or bw is None or positions is None
                    or len(positions) == 0):
                continue
            for v in range(len(positions)):
                if bw[v].sum() <= 0:
                    continue
                slot = int(np.argmax(bw[v]))
                by   = int(bi[v][slot])
                idx  = by // 3
                if palette is not None and idx < len(palette):
                    key = palette[idx]
                else:
                    # Heuristic fallback bucketing -- bone byte plus
                    # source mesh name so distinct meshes contributing
                    # the same byte don't merge into one wheel.
                    key = f'__byte_{idx}__{mesh_name}'
                key_to_pts.setdefault(key, []).append(
                    (float(positions[v][0]),
                     float(positions[v][1]),
                     float(positions[v][2])))

        # Tuples stored as (cx, cy, cz_negated, bone_name_or_None)
        # so the Y-band post-pass + the rear-to-front sort can keep
        # the per-wheel name together with its position.  Heuristic-
        # only path (no palette) stores `None` here; that's what
        # `bone_matrix_array` checks against to decide deflection
        # vs identity per bone.
        wheels_left  = []
        wheels_right = []

        # ---- XML-driven fast path -------------------------------
        # Per Coffee 2026-05-09 ("is there nothing to tell us in
        # the xml files for the tank? yes please"): if the caller
        # provided `wheel_roles` (parsed from the gameplay XML's
        # `<wheels>` block), use it to filter `key_to_pts`
        # directly -- no Y-band hack, no WD_drop hack, no shape
        # heuristic.  road_wheels_L / road_wheels_R are the
        # AUTHORITATIVE list of which bones are weight-bearing
        # road wheels for suspension physics; sprockets, idlers,
        # and return rollers are excluded by virtue of being in
        # OTHER role lists.  Bone-palette names sometimes carry a
        # `_BlendBone` suffix; allow both forms.
        xml_path_used = False
        if wheel_roles:
            allowed_L = set(wheel_roles.get('road_wheels_L') or [])
            allowed_R = set(wheel_roles.get('road_wheels_R') or [])
            # Also accept the `_BlendBone` suffix form some chassis
            # palettes use.
            allowed_L |= {n + '_BlendBone' for n in list(allowed_L)}
            allowed_R |= {n + '_BlendBone' for n in list(allowed_R)}
            if allowed_L or allowed_R:
                xml_path_used = True
                for key, pts in key_to_pts.items():
                    if key not in allowed_L and key not in allowed_R:
                        continue
                    arr = np.asarray(pts, dtype=np.float64)
                    cx = float(arr[:, 0].mean())
                    cy = float(arr[:, 1].mean())
                    cz = float(arr[:, 2].mean())
                    tup = (cx, cy, cz, key)
                    if key in allowed_L:
                        wheels_left.append(tup)
                    else:
                        wheels_right.append(tup)
                # XML-driven path is authoritative -- skip the
                # heuristic blocks below entirely.  Sort each
                # side front-to-rear by ascending Z (chassis
                # convention: front = -Z).
                wheels_left.sort (key=lambda t: t[2])
                wheels_right.sort(key=lambda t: t[2])
                print(f"[tank_physics] XML-driven wheel pick: "
                      f"L={len(wheels_left)}, R={len(wheels_right)} "
                      f"(road_wheels from <wheels> block)")
        if xml_path_used:
            # Skip the heuristic chain by jumping straight to the
            # post-processing that builds the TankPhysics instance.
            # Mirrors the tail of the heuristic path below; updates
            # there should be reflected here.
            return cls._build_from_wheel_tuples(
                wheels_left, wheels_right,
                radius=radius,
                min_offset=min_offset, max_offset=max_offset,
                track_thickness=track_thickness,
                mass_kg=mass_kg,
                max_yaw_rate_dps=max_yaw_rate_dps)
        for key, pts in key_to_pts.items():
            arr = np.asarray(pts, dtype=np.float64)
            cx, cy, cz = (float(arr[:, 0].mean()),
                          float(arr[:, 1].mean()),
                          float(arr[:, 2].mean()))
            ext = (arr.max(axis=0) - arr.min(axis=0))
            n_verts = len(pts)
            wheel_name = None     # populated only on the name-based path

            if not key.startswith('__byte_'):
                # Name-based path.  Recognises:
                #   * tracked rig:  `W_L0`, `W_R6`, ... (any digit count)
                #   * wheeled rig:  `W_F_L`, `W_R1_L`, `W_R2_R`, ...
                #   * WD_<side><i>: ambiguous -- on tracked tanks it's
                #     drive sprocket / idler / return roller (NOT a
                #     road wheel); on the Hotchkiss EBR it's a real
                #     road wheel that just happens to use the same
                #     prefix.  Resolved by re-running the shape +
                #     cy check from `_looks_like_wheel`.
                # Anything else (Track_*, V_, named decorative
                # bones) -> reject.
                side, needs_confirm = _wheel_side_from_name(key)
                if side is None:
                    continue
                if needs_confirm and not _looks_like_wheel(n_verts, ext, cy):
                    continue
                wheel_name = key
            else:
                # Heuristic fallback (no palette).  Same shape + cy
                # check as above, plus a side derivation from cx
                # since we don't have a name to consult.  A real
                # road wheel passes; track plates fail dy >= 0.30
                # (their dy is ~0.14-0.20); drive sprockets / idlers
                # / return rollers fail cy <= 0.80 (their cy is >=
                # 0.85).
                if not _looks_like_wheel(n_verts, ext, cy):
                    continue
                side = 'left' if cx < 0 else 'right'

            # NO Z negation here.  Skinned chassis primitives have
            # `flip_z = False` on load (loaders.py:336 "skinned
            # meshes are authored with forward = -Z and we must NOT
            # flip them") -- so the raw chassis vert Z is already
            # in GL frame (front = -Z).  The earlier `-cz` here
            # double-flipped to +Z and mirrored the wheel markers
            # / contact hit-points relative to the rendered chassis
            # (2026-05-08 user report on T30: "the marker points
            # are backwards in z").  T110E4_WHEELS hardcoded
            # fallback data is documented as "pre-negated" and is
            # un-negated at the fallback site below for consistency.
            tup = (cx, cy, cz, wheel_name)
            if side == 'left':
                wheels_left.append(tup)
            else:
                wheels_right.append(tup)

        # If a side has at least one accepted `W_` road-wheel bone,
        # drop every accepted `WD_` candidate from THAT SIDE.  WD_
        # bones on tracked tanks are the drive sprocket (rear) +
        # idler / tensioner (front) + return rollers -- they ride
        # the track ribbon, not the ground, and shouldn't bias the
        # plane fit.  T30 in particular has WD_L0 (drive sprocket)
        # within ~10 cm Y of the road wheels, so the Y-band post-
        # pass below leaks it through; this explicit drop kills it
        # before the band check runs.  Hotchkiss-EBR-style rigs
        # with NO W_ bones (only WD_) are untouched -- their WD_
        # entries ARE the road wheels.  (2026-05-08 user report: T30
        # was showing 8 + 9 markers because the rear sprocket got
        # accepted as wheel index 0.)
        def _drop_wd_if_w_present(wheels):
            has_w = any(re.match(r'^W_', w[3] or '') for w in wheels)
            if has_w:
                return [w for w in wheels
                        if not re.match(r'^WD_', w[3] or '')]
            return wheels
        wheels_left  = _drop_wd_if_w_present(wheels_left)
        wheels_right = _drop_wd_if_w_present(wheels_right)

        # Y-band post-pass.  Some chassis carry "wheel-shaped"
        # decorative bones that sit ABOVE the actual road-wheel
        # band -- typically the drive sprocket (rear) and idler
        # (front).  These pass the name + shape filter on the way
        # through but they don't bear weight against the ground --
        # the track does, sagging between road wheels.  Reject
        # anything more than `Y_BAND` above the LOWEST accepted
        # wheel on the same side.
        #
        # Tightened from 0.15 -> 0.06 m on 2026-05-08 because T30
        # has `W_L8` / `W_R8` (drive-sprocket-or-idler that the
        # artist named with a `W_` prefix instead of `WD_`) sitting
        # at Y = 0.533 / 0.566 vs the eight real road wheels at
        # Y = 0.421 .. 0.430 -- a 10.5 cm gap that fell INSIDE the
        # old 15 cm band.  The F3 manual recorder caught it: those
        # two slots reported HANGING 68 % / 97 % of frames with
        # delta_y ~14 cm below the envelope (idler hovers above
        # ground) but on the 3-32 % of frames they DID touch they
        # entered the plane fit and yanked the chassis pose
        # (Coffee's "using the damn idler as a weight wheel"
        # complaint, 2026-05-08).
        #
        # 0.06 m is wide enough for legitimate suspension-geometry
        # variation across real road wheels (T30 spans 7 mm,
        # T110E4 spans ~10 mm) but tight enough to catch T30's
        # 105 mm misnamed-idler gap.  Bourrasque (road wheels at
        # 0.395, WD_ sprocket / idler at 0.627 / 0.749) still
        # works -- its smallest gap is 232 mm, far past 60 mm.
        # Hotchkiss EBR (all wheels at cy = 0.619) is unchanged
        # (gap = 0).
        Y_BAND = 0.06
        if wheels_left:
            min_y = min(w[1] for w in wheels_left)
            wheels_left  = [w for w in wheels_left  if w[1] <= min_y + Y_BAND]
        if wheels_right:
            min_y = min(w[1] for w in wheels_right)
            wheels_right = [w for w in wheels_right if w[1] <= min_y + Y_BAND]

        # Sort each side FRONT-to-rear by ascending raw Z.  Chassis-
        # local frame has front at -Z (GL convention; see the
        # `flip_z = False` rule for skinned meshes in loaders.py).
        # So ascending Z = front-to-rear, with self.wheels[0] = the
        # frontmost wheel on the left side.  (Was rear-to-front
        # pre-Z-fix when wheels carried negated Z; updated 2026-05-08.)
        wheels_left.sort (key=lambda t: t[2])
        wheels_right.sort(key=lambda t: t[2])

        # Split (x, y, z, name) tuples back into parallel lists --
        # the constructor only accepts (x, y, z) coords; the names
        # get attached as `wheel_bone_names` after construction.
        names_left  = [w[3] for w in wheels_left]
        names_right = [w[3] for w in wheels_right]
        wheels_left  = [(w[0], w[1], w[2]) for w in wheels_left]
        wheels_right = [(w[0], w[1], w[2]) for w in wheels_right]

        # Fall back to T110E4 hardcoded data if extraction failed.
        if not wheels_left or not wheels_right:
            print("[tank_physics] auto-extract found no W_ wheel "
                  "groups; falling back to T110E4 hardcoded rig "
                  f"(saw {len(key_to_pts)} bone groups, "
                  f"{sum(1 for k in key_to_pts if not k.startswith('__byte_'))} "
                  "with palette names)")
            d = T110E4_WHEELS
            # T110E4_WHEELS stores Z pre-negated (the data was
            # authored back when chassis was non-skinned and
            # got z-flipped on load).  GPU skinning made flip_z
            # False for the chassis, so the runtime now expects
            # raw chassis-frame Z (front = -Z).  Un-negate here.
            wl = [(x, y, -z) for (x, y, z) in d['wheels_left']]
            wr = [(x, y, -z) for (x, y, z) in d['wheels_right']]
            return cls(wl, wr,
                       radius=d['radius'],
                       min_offset=d['min_offset'],
                       max_offset=d['max_offset'],
                       track_thickness=track_thickness,
                       mass_kg=mass_kg,
                       max_yaw_rate_dps=max_yaw_rate_dps)

        named_count = sum(1 for n in (names_left + names_right) if n)
        print(f"[tank_physics] extracted rig: {len(wheels_left)}L + "
              f"{len(wheels_right)}R wheels  radius={radius:.3f}  "
              f"({named_count} bone-named, "
              f"{len(names_left) + len(names_right) - named_count} "
              f"heuristic-only)")
        inst = cls(wheels_left, wheels_right,
                   radius=radius,
                   min_offset=min_offset,
                   max_offset=max_offset,
                   track_thickness=track_thickness,
                   mass_kg=mass_kg,
                   max_yaw_rate_dps=max_yaw_rate_dps)
        # Stash the parallel name list so `bone_matrix_array` knows
        # which palette entries correspond to wheels we ACTUALLY
        # accepted (drops a tracked tank's WD_ idlers / sprockets
        # without breaking the Hotchkiss EBR's WD_ road wheels).
        inst.wheel_bone_names = names_left + names_right
        return inst

    # ------------------------------------------------------------------
    @classmethod
    def _build_from_wheel_tuples(cls, wheels_left, wheels_right, *,
                                  radius, min_offset, max_offset,
                                  track_thickness, mass_kg,
                                  max_yaw_rate_dps):
        """Build a TankPhysics from already-validated `(x, y, z,
        name)` tuples.  Used by the XML-driven fast path in
        `from_chassis_meshes`; mirrors the post-processing tail of
        the heuristic path.  Names are attached to
        `wheel_bone_names` so `bone_matrix_array` can look up
        per-wheel deflection.
        """
        names_left  = [w[3] for w in wheels_left]
        names_right = [w[3] for w in wheels_right]
        wl = [(w[0], w[1], w[2]) for w in wheels_left]
        wr = [(w[0], w[1], w[2]) for w in wheels_right]
        if not wl or not wr:
            # Fall through to the T110E4 fallback to keep behaviour
            # consistent with the heuristic path's empty-extract
            # case.  Should be vanishingly rare on the XML path.
            d = T110E4_WHEELS
            wl_fb = [(x, y, -z) for (x, y, z) in d['wheels_left']]
            wr_fb = [(x, y, -z) for (x, y, z) in d['wheels_right']]
            return cls(wl_fb, wr_fb,
                       radius=d['radius'],
                       min_offset=d['min_offset'],
                       max_offset=d['max_offset'],
                       track_thickness=track_thickness,
                       mass_kg=mass_kg,
                       max_yaw_rate_dps=max_yaw_rate_dps)
        inst = cls(wl, wr,
                   radius=radius,
                   min_offset=min_offset,
                   max_offset=max_offset,
                   track_thickness=track_thickness,
                   mass_kg=mass_kg,
                   max_yaw_rate_dps=max_yaw_rate_dps)
        inst.wheel_bone_names = names_left + names_right
        return inst

    # ------------------------------------------------------------------
    def corner_wheel_indices(self):
        """Return up to 4 wheel-array indices for the four CORNER
        wheels (rear-left, rear-right, front-left, front-right).

        Wheels are stored sorted REAR -> FRONT within each side
        (sort key: negated chassis-primitive Z).  So:

          * rear-left   = `self.wheels[0]`
          * front-left  = `self.wheels[self.n_left - 1]`
          * rear-right  = `self.wheels[self.n_left]`
          * front-right = `self.wheels[-1]`

        Tanks with fewer than two wheels per side collapse the
        relevant corner pair into a single index (or returns < 4
        entries when a side is empty).  Used by the contact-wheel
        highlight pass to identify "the 4 wheels that anchor the
        plane fit" -- the user's mental model is the four corners
        of the suspension footprint, regardless of how many road
        wheels sit between them.
        """
        idxs = []
        if self.n_left >= 1:
            idxs.append(0)
            if self.n_left >= 2:
                idxs.append(self.n_left - 1)
        n_right = len(self.wheels) - self.n_left
        if n_right >= 1:
            idxs.append(self.n_left)
            if n_right >= 2:
                idxs.append(len(self.wheels) - 1)
        return idxs

    def corner_wheel_bone_names(self):
        """Return up to 4 bone names for the corner contact wheels,
        or `None` slots when no name was recorded (auto-extract ran
        on the heuristic-only path with no palette).

        Caller (viewer) uses these to look up the palette index in
        each chassis sub-mesh's own `bone_palette` -- the index
        differs across primitive groups since each renderSet
        declares its own ordering.
        """
        names = []
        for i in self.corner_wheel_indices():
            if i < len(self.wheel_bone_names):
                names.append(self.wheel_bone_names[i])
            else:
                names.append(None)
        return names

    # ------------------------------------------------------------------
    def chassis_matrix(self):
        """Return the chassis pose as a 4x4 float32 matrix in the
        VISIBLE / RENDERED frame -- this is the inertia-damped pose
        the integrator drives toward the solver's target.

        Matrix composition order: T(pos) * Ry(yaw) * Rx(pitch) * Rz(roll).
        Apply it as: world_pos = chassis_matrix @ mesh.model_matrix @ vert.

        The X / Z translation comes from `self.pos` (driven directly
        by the user-controlled drive logic).  Y / pitch / roll come
        from the render-pose fields (`_render_pos_y`,
        `_render_pitch_deg`, `_render_roll_deg`) which are integrated
        by `_step_pose_integrator()` toward the solver's target each
        frame.

        Solver internals use `_chassis_matrix_target()` instead --
        that one reads `self.pos[1] / pitch_deg / roll_deg` (the
        target the solver writes) so the contact classifier and
        iterative refinement work on a SOLVER-CONSISTENT pose, not
        on a lagged integrator state (which is what produced the
        v1.93.0 wobble).
        """
        ty = mat4_translate(self.pos[0], self._render_pos_y, self.pos[2])
        ry = self._mat4_rotate_y(math.radians(self.yaw_deg))
        rx = mat4_rotate_x(math.radians(self._render_pitch_deg))
        rz = mat4_rotate_z(math.radians(self._render_roll_deg))
        return (ty @ ry @ rx @ rz).astype(np.float32)

    def _chassis_matrix_target(self):
        """Return the chassis pose at the SOLVER TARGET (no inertia
        damping).  Used by the iterative-refinement loop and
        `_compute_residual_y` so they see the pose that the contact
        classifier expects, not the lagged integrator state."""
        ty = mat4_translate(self.pos[0], self.pos[1], self.pos[2])
        ry = self._mat4_rotate_y(math.radians(self.yaw_deg))
        rx = mat4_rotate_x(math.radians(self.pitch_deg))
        rz = mat4_rotate_z(math.radians(self.roll_deg))
        return (ty @ ry @ rx @ rz).astype(np.float32)

    def _update_lateral_lean(self, dt):
        """Estimate inertial accelerations from chassis-state deltas
        and store the steady-state body-lean offsets:

          * lateral G  -> roll  offset (`_target_lat_lean_roll_deg`)
          * longitudinal G -> pitch offset (`_target_long_lean_pitch_deg`)

        Both offsets are applied INTEGRATOR-side only -- the solver
        never sees them, so the contact classifier and plane fit
        operate purely on terrain-driven targets and the inertial
        leans don't feed back into the solver.

        Reads:  self.yaw_deg, self.pos[0], self.pos[2], self.gravity,
                 self.lateral_lean_gain, self.longitudinal_lean_gain,
                 dt, last_*_for_lat fields, last_signed_speed_mps.
        Writes: telemetry fields + the two integrator offsets +
                the persistent last_*_for_lat tracking state.
        """
        g = max(self.gravity, 0.1)
        # Forward speed comes from the drive-layer ramp, NOT from
        # chassis position deltas.  See `cur_forward_mps` field
        # docstring: position-derived speed had 2x per-frame noise
        # (M60 turn-test 2026-05-08), `cur_forward_mps` is the
        # smooth ramped output of viewer's _current_forward and
        # is essentially noise-free.
        signed_speed_mps = float(self.cur_forward_mps)

        # ---- Lateral (roll) lean ----------------------------------
        # Yaw rate (deg/s).  Wrap-aware so yaw_deg wrapping at
        # +/-180 doesn't produce a phantom 360 deg/s spike.
        d_yaw = ((self.yaw_deg - self.last_yaw_deg_for_lat
                  + 540.0) % 360.0) - 180.0
        yaw_rate_dps   = d_yaw / max(dt, 1e-3)
        yaw_rate_rad_s = math.radians(yaw_rate_dps)
        self.last_yaw_deg_for_lat = float(self.yaw_deg)
        # Lateral accel: a_lat = forward_speed * yaw_rate_rad.
        # Sign: yaw_rate > 0 => right turn (chassis-clockwise from
        # above), centripetal toward LEFT of chassis (-X), body
        # leans RIGHT (+X drops, roll<0).  Coding
        # `target_lean = -atan(a/g)` gives that sign automatically.
        a_lat_raw = signed_speed_mps * yaw_rate_rad_s

        # ---- Longitudinal (pitch) lean ----------------------------
        # Longitudinal accel = d(signed_speed)/dt.  +ve = accel
        # forward; -ve = braking or accel reverse.  Body inertia =>
        # nose lifts on accel, nose dives on brake.
        a_long_raw = ((signed_speed_mps - self.last_signed_speed_mps)
                      / max(dt, 1e-3))
        self.last_signed_speed_mps = signed_speed_mps

        # ---- EMA smoothing ----------------------------------------
        # 5-frame trailing average (alpha=0.20) so the atan(a/g)
        # leans don't pump on dt jitter or speed-ramp inflection
        # points.  `_smoothed_a_*` is what the integrator actually
        # consumes; raw values still flow into telemetry for the
        # turn-test JSON so we can see both.
        ALPHA = 0.20
        self._smoothed_a_lat_mps2  = (
            ALPHA * a_lat_raw  + (1.0 - ALPHA) * self._smoothed_a_lat_mps2)
        self._smoothed_a_long_mps2 = (
            ALPHA * a_long_raw + (1.0 - ALPHA) * self._smoothed_a_long_mps2)

        # ---- Lean targets (from smoothed accelerations) -----------
        # Sign rules (verified empirically 2026-05-08):
        #   pitch_render > 0 = nose UP   (squat under accel)
        #   pitch_render < 0 = nose DOWN (dive under brake)
        #   roll_render  bigger absolute = lean OUTWARD on a turn
        # Coding `-atan(a_lat/g)` and `+atan(a_long/g)` produces
        # those sign mappings.

        lat_target_raw  = (
            -math.degrees(math.atan(self._smoothed_a_lat_mps2 / g))
            * self.lateral_lean_gain)
        long_target_raw = (
            +math.degrees(math.atan(self._smoothed_a_long_mps2 / g))
            * self.longitudinal_lean_gain)

        # Pitch-lean gate based on RENDER-pose wheel compression
        # (per Coffee 2026-05-08).  The solver's `last_wheel_state`
        # would seem like the obvious source, but it never sees the
        # lean (lean is integrator-side only) -- so the solver
        # classifies wheels purely by terrain compression and
        # almost never reports OVER_COMP from the lean tilt that's
        # actually pushing them through the ground.
        #
        # Direct check: project each wheel through the CURRENT
        # render chassis pose (= prior frame's integrator output,
        # which is the visible chassis pose).  Compare its world Y
        # to this frame's target Y.  Over-compression = wheel needs
        # to rise more than `comp_cap` to reach target -- which
        # means the bone-matrix residual saturates and the visible
        # wheel sits BELOW ground.
        #
        # Gate: if any wheel on the loaded side is over-compressed
        # in the render pose, suppress further lean toward that
        # side.  Releases automatically as terrain rolls under the
        # tank or the integrator decays the lean.
        if (hasattr(self, 'last_target_y')
                and len(self.last_target_y) == len(self.wheels)):
            comp_cap = -self.min_offset
            local = self.wheels
            local_h = np.column_stack([
                local[:, 0], local[:, 1], local[:, 2],
                np.ones(len(local), dtype=np.float64)])
            m_render  = self.chassis_matrix()
            render_y  = (m_render @ local_h.T).T[:, 1]
            target_y  = np.asarray(self.last_target_y, dtype=np.float64)
            # delta>0  => wheel needs to compress UP to reach target.
            # delta>comp_cap => bone matrix saturates -> visible
            # wheel sits below ground.
            comp_delta = target_y - render_y
            is_over    = comp_delta > comp_cap

            local_z  = local[:, 2]
            is_rear  = local_z > 0.0
            is_front = local_z < 0.0
            any_rear_over  = bool((is_over & is_rear).any())
            any_front_over = bool((is_over & is_front).any())
            if long_target_raw > 0.0 and any_rear_over:
                long_target_raw = 0.0
            if long_target_raw < 0.0 and any_front_over:
                long_target_raw = 0.0
            # Stash for telemetry / debugging.
            self._gate_any_rear_over_render  = any_rear_over
            self._gate_any_front_over_render = any_front_over
        else:
            self._gate_any_rear_over_render  = False
            self._gate_any_front_over_render = False

        self._target_lat_lean_roll_deg   = lat_target_raw
        self._target_long_lean_pitch_deg = long_target_raw

        # ---- Telemetry --------------------------------------------
        # Raw (per-frame, noisy) accels for diagnostic plots; the
        # smoothed values are what actually drive the leans.
        self._last_a_lat_mps2        = float(a_lat_raw)
        self._last_a_long_mps2       = float(a_long_raw)
        self._last_speed_mps         = float(abs(signed_speed_mps))
        self._last_signed_speed_mps  = float(signed_speed_mps)
        # Per Coffee 2026-05-16: smooth the yaw rate too.  Same
        # EMA alpha (0.20) the accel signals use.  The chain-
        # animation code reads `_last_yaw_rate_dps` to compute
        # per-side speed -- a noisy raw value here was the
        # primary cause of one track speeding up while the other
        # slowed at low chassis speed.  Raw value preserved for
        # diagnostic consumers.
        self._last_yaw_rate_dps_raw  = float(yaw_rate_dps)
        self._last_yaw_rate_dps      = (
            ALPHA * float(yaw_rate_dps)
            + (1.0 - ALPHA) * self._last_yaw_rate_dps)

    def _step_pose_integrator(self, dt, terrain=None):
        """Critically-damped second-order pull of the rendered pose
        toward the solver's target pose.

        Reads:
            self.pos[1], self.pitch_deg, self.roll_deg     (target)
            self._render_pos_y, _render_pitch_deg, _render_roll_deg
            self._render_vy, omega_pitch_dps, omega_roll_dps
            self.mass_kg, dt

        Writes:
            self._render_pos_y, _render_pitch_deg, _render_roll_deg
            self._render_vy, omega_pitch_dps, omega_roll_dps

        Natural frequency scales with `sqrt(M_REF / M)` so heavier
        tanks respond more slowly.  Critically damped (zeta = 1) so
        the pose approaches the target without overshoot.

        Why this matters: pre-integrator, the chassis pose was hard-
        snapped to the solver's target every frame -- which works
        fine on smooth terrain but produces catastrophic spikes
        when terrain features lift most wheels off ground in one
        frame (M60 turn-test frame 737: solver returned an 89-deg
        roll target because only 3 wheels were in contact and their
        plane fit went near-vertical).  With inertia, that 89-deg
        target only pushes the chassis ~0.5 deg in the one frame
        before the next frame's solver returns to a sensible
        target -- the spike is filtered out without ever being
        rendered.
        """
        M_REF = 30000.0
        # Clamp tiny mass values so sqrt doesn't explode.
        m = max(self.mass_kg, M_REF * 0.1)
        mass_scale = math.sqrt(M_REF / m)
        # Natural frequencies (rad/s).  Tuned for tank body-roll
        # period ~0.7 s on 60 t (omega_n ~ 9 rad/s).  Y axis is
        # slightly stiffer because vertical body-bounce is faster
        # than yaw / pitch (real tank suspension natural freq is
        # ~1.5 Hz vertical, ~1 Hz body-roll).
        omegan_p = 9.0  * mass_scale
        omegan_r = 11.0 * mass_scale
        omegan_y = 14.0 * mass_scale
        zeta = 1.0
        # Roll  target = solver's plane-fit roll  + lateral-lean offset.
        # Pitch target = solver's plane-fit pitch + longitudinal-lean offset.
        # Y     target = solver's plane-fit y     (no inertial offset).
        # Both leans are integrator-side only -- the solver never
        # writes them into self.roll_deg / self.pitch_deg, so the
        # contact classifier and plane fit see only the terrain-
        # driven targets.  Avoids the v1.93.0 feedback trap on
        # both axes.
        target_roll_with_lean  = (float(self.roll_deg)
                                  + float(self._target_lat_lean_roll_deg))
        target_pitch_with_lean = (float(self.pitch_deg)
                                  + float(self._target_long_lean_pitch_deg))
        e_p = target_pitch_with_lean - self._render_pitch_deg
        e_r = target_roll_with_lean  - self._render_roll_deg
        e_y = float(self.pos[1])     - self._render_pos_y
        # Snapshot for the F3 recorder.  Cheap (six float assigns)
        # and lets the recorder distinguish "solver target moved"
        # from "integrator hasn't caught up" -- the classic
        # signatures of solver / integrator divergence.
        self._last_target_pitch_with_lean = float(target_pitch_with_lean)
        self._last_target_roll_with_lean  = float(target_roll_with_lean)
        self._last_e_pitch_deg            = float(e_p)
        self._last_e_roll_deg             = float(e_r)
        self._last_e_y_m                  = float(e_y)
        a_p = (omegan_p * omegan_p * e_p
               - 2.0 * zeta * omegan_p * self.omega_pitch_dps)
        a_r = (omegan_r * omegan_r * e_r
               - 2.0 * zeta * omegan_r * self.omega_roll_dps)
        a_y = (omegan_y * omegan_y * e_y
               - 2.0 * zeta * omegan_y * self._render_vy)
        self.omega_pitch_dps   += a_p * dt
        self.omega_roll_dps    += a_r * dt
        self._render_vy        += a_y * dt
        self._render_pitch_deg += self.omega_pitch_dps * dt
        self._render_roll_deg  += self.omega_roll_dps  * dt
        self._render_pos_y     += self._render_vy      * dt

        # Render-side terrain floor (per Coffee 2026-05-08, the
        # master physics override: "wheel contact can not be
        # allowed to go below terrain").  Sample the terrain at
        # each wheel's RENDER-pose XZ (NOT target-pose XZ -- those
        # differ when chassis pitch / roll differ between solver
        # and integrator) and lift `_render_pos_y` so no wheel
        # needs more than `comp_cap` of compression to reach its
        # render-pose ground sample.
        #
        # The TARGET-XZ floor (`last_target_y`-based) was missing
        # the case where chassis-pose lean / integrator lag put the
        # actual rendered wheel over a different terrain feature
        # than the target sampled.  Result was visible 10 cm
        # penetration spikes during inertial-lean transients.
        # Render-XZ sampling closes that gap.
        if terrain is not None and len(self.wheels) > 0:
            comp_cap = -self.min_offset
            local = self.wheels
            local_h = np.column_stack([
                local[:, 0], local[:, 1], local[:, 2],
                np.ones(len(local), dtype=np.float64)])
            # Project wheels through the CURRENT render chassis
            # pose to get world XZ + rigid render Y.
            m_render = self.chassis_matrix()
            world_h  = (m_render @ local_h.T).T
            wx_r = world_h[:, 0]
            wy_r = world_h[:, 1]
            wz_r = world_h[:, 2]
            # Sample terrain at each wheel's actual render XZ.
            if hasattr(terrain, 'sample_heights'):
                ty_r = np.asarray(
                    terrain.sample_heights(wx_r, wz_r),
                    dtype=np.float64)
            elif hasattr(terrain, 'sample_height'):
                ty_r = np.array(
                    [float(terrain.sample_height(float(x), float(z)))
                     for x, z in zip(wx_r, wz_r)],
                    dtype=np.float64)
            else:
                ty_r = np.zeros(len(self.wheels), dtype=np.float64)
            # Render-XZ wheel-centre target Y (terrain + r + tt
            # + pad_lift -- mirrors the solver-side target so the
            # render-side terrain floor doesn't fight the new
            # pad-thickness offset).  `getattr` default keeps
            # older TankPhysics instances safe.
            target_y_r = (ty_r + self.radius + self.track_thickness
                          + float(getattr(self, 'pad_lift', 0.0)))
            # required = target_y_at_render_xz - comp_cap - ly_post_rx
            # but ly_post_rx is just `wy_r - chassis_y` (since
            # chassis pos translates uniformly).  So
            #   required[i] = target_y_r[i] - comp_cap - (wy_r[i] - chassis_y)
            #             chassis_y_min = target_y_r[i] - comp_cap - wy_r[i] + chassis_y
            # Equivalent: how much MORE compression than comp_cap
            # is currently needed?  positive = floor lift required.
            comp_needed = target_y_r - wy_r       # >0 = wheel below target
            over        = comp_needed - comp_cap  # >0 = wheel SUNK past comp_cap
            lift_needed = float(np.max(over))
            if lift_needed > 0.0:
                # Lift the chassis just enough so the worst wheel
                # exactly fits within comp_cap.  Zero vertical vel
                # so the integrator doesn't push back through.
                self._render_pos_y += lift_needed
                self._render_vy     = 0.0
                self._last_lift_needed_m = lift_needed
            else:
                self._last_lift_needed_m = 0.0
        else:
            self._last_lift_needed_m = 0.0

    @staticmethod
    def _mat4_rotate_y(rad):
        c, s = math.cos(rad), math.sin(rad)
        m = np.eye(4, dtype=np.float32)
        m[0, 0] =  c; m[0, 2] =  s
        m[2, 0] = -s; m[2, 2] =  c
        return m

    # ------------------------------------------------------------------
    def update(self, terrain, dt):
        """Run one physics tick (per-wheel contact-aware suspension).

        Model
        -----
        * Each wheel is independently classified as CONTACT, HANGING,
          or OVER_COMPRESSED based on its current chassis-pose-relative
          target Y vs the wheel's neutral Y, against the suspension
          envelope `(min_offset, max_offset)`.

        * The CHASSIS POSE (pitch / roll / Y) is then fit ONLY to
          the wheels classified as CONTACT.  Wheels that aren't
          touching can't bear weight, so they shouldn't bias the
          tilt -- a previous wheel-in-the-air bug had the chassis
          dropping its nose because the front wheels were dragging
          a phantom contribution to the lstsq fit.

        * If there are FEWER THAN 3 contacting wheels we don't
          have a defined plane, so the chassis falls under gravity.
          Each frame the chassis drops a bit, more wheels reach
          the ground, and the plane fit re-asserts.

        * After the plane is set, HANGING wheels droop to the
          extension cap (visibly drooped) and OVER_COMPRESSED
          wheels clamp to the compression cap.  CONTACT wheels
          take their lstsq residual.

        Iteration
        ---------
        Per-frame the chassis_y settles based on which wheels
        currently contact.  A wheel that just lost contact
        (drove off a cliff edge) immediately switches to HANGING
        and stops biasing the plane fit; the chassis then tilts
        toward the still-contacting side.  A wheel that just
        gained contact (drove onto a kerb) switches to CONTACT
        and tightens the plane.  No spring-damper, no oscillation.

        Args:
            terrain : object with `sample_heights(xs, zs)` or
                      `sample_height(x, z)`.  None -> fallback Y=0.
            dt      (float): seconds since the last call.

        Returns:
            np.ndarray: the chassis pose matrix (4x4 row-major float32).
        """
        # ---- 1. Mesh-local wheel -> world XZ ----------------------
        # Use the FULL chassis_matrix (translation + yaw + pitch +
        # roll) to project each wheel into world XZ, not just yaw.
        # Pitch / roll shift the wheel's world XZ noticeably on
        # tilted ground -- a 4 deg roll on a wheel at chassis-
        # local lx = +-1.5 m moves its world X by ~3 cm via the
        # rotation, and that's enough to put the terrain sample
        # several centimetres off the actual rendered wheel
        # position.  Visible as the contact-overlay pink-star
        # markers (which read this `last_wheel_world` array)
        # drifting away from the rendered wheels on uneven
        # ground -- the bug Professor Coffee just flagged.
        local = self.wheels
        local_h = np.column_stack([
            local[:, 0], local[:, 1], local[:, 2],
            np.ones(len(local), dtype=np.float64),
        ])
        # Use the TARGET pose (not the rendered/integrated pose) so
        # the contact classifier and plane fit work on a self-
        # consistent solver state -- not on a lagged integrator
        # value (that's the v1.93.0 wobble trap).
        world_h = (self._chassis_matrix_target() @ local_h.T).T
        wx = world_h[:, 0]
        wy = world_h[:, 1]
        wz = world_h[:, 2]
        self.last_wheel_world[:, 0] = wx
        self.last_wheel_world[:, 1] = wy
        self.last_wheel_world[:, 2] = wz

        # ---- 2. Terrain sample ------------------------------------
        if terrain is None:
            ty = np.zeros(len(self.wheels), dtype=np.float64)
        elif hasattr(terrain, 'sample_heights'):
            ty = np.asarray(terrain.sample_heights(wx, wz), dtype=np.float64)
        elif hasattr(terrain, 'sample_height'):
            ty = np.array([float(terrain.sample_height(float(x), float(z)))
                           for x, z in zip(wx, wz)],
                          dtype=np.float64)
        else:
            ty = np.zeros(len(self.wheels), dtype=np.float64)

        # ---- 3. Target wheel-centre Y -----------------------------
        # Add `track_thickness` to lift the wheel CENTRE above
        # terrain by (radius + track_thickness) -- so the OUTER
        # face of the track ribbon (not the wheel hub) is what
        # rests on the ground.  Without this, the track sinks
        # into the terrain by its own thickness.
        # +pad_lift: raises the wheel-centre target by half a
        # pad's height so pads (centered on the spline at chassis-
        # local hub_y - radius - track_thickness) touch terrain
        # with their bottom face instead of penetrating it.
        # `getattr` default 0 keeps older TankPhysics instances
        # (constructed before the field landed) safe.
        target_centre = (ty + self.radius + self.track_thickness
                         + float(getattr(self, 'pad_lift', 0.0)))
        self.last_terrain_y = ty
        self.last_target_y  = target_centre

        # ---- 4. Classify each wheel via CURRENT chassis pose ------
        # Compute world Y under each wheel for the chassis_pose
        # we're STARTING the frame with.  delta = target - rigid_y
        # then tells us how much the wheel needs to compress (+ve)
        # or extend (-ve) from its current rendered position to
        # reach the terrain.  Compare against the suspension
        # envelope to bucket each wheel.
        m_in = self._chassis_matrix_target()
        local_h = np.column_stack([
            local[:, 0], local[:, 1], local[:, 2],
            np.ones(len(local), dtype=np.float64),
        ])
        rigid_y_in = (m_in @ local_h.T).T[:, 1]
        delta = target_centre - rigid_y_in
        # Snapshot the unclamped delta for the F3 manual recorder.
        # The recorder reads this to compute hysteresis-edge
        # proximity per wheel (`d - (ext_cap +/- HYST)` etc.) so a
        # frame where a wheel is sitting on the threshold of
        # flipping CONTACT <-> HANGING shows up in the trace.
        self.last_delta_y = delta.copy()
        # `delta` sign convention here (NOT the post-flip shader
        # convention): +ve = wheel needs to RISE to meet terrain
        # (compression).  -ve = wheel needs to DROP (extension).
        # Envelope:
        #   compression cap (max wheel can rise into hull) =
        #     -self.min_offset = +0.04 m typical
        #   extension cap (max wheel can drop below hull) =
        #     -self.max_offset = -0.08 m typical
        comp_cap = -self.min_offset    # +ve max delta
        ext_cap  = -self.max_offset    # -ve max delta (below pose)

        # Hysteresis on the contact-set classification.  Without it,
        # a wheel that's borderline-in / borderline-out of the
        # suspension envelope flaps between CONTACT and HANGING every
        # frame, taking the plane fit's pitch / roll with it -- the
        # tank visibly oscillates in roll on uneven terrain.
        #
        # Recipe: a wheel that was CONTACT last frame stays CONTACT
        # until its delta clears the envelope by more than `HYST`.
        # A wheel that was HANGING last frame stays HANGING until
        # its delta has clearly re-entered the envelope by `HYST`.
        # `HYST` is ~25% of the envelope width so we don't get a
        # spurious latch on a wheel barely touching.
        HYST = 0.020   # 2 cm margin around the envelope edges
        prev = (getattr(self, 'last_wheel_state', None)
                if hasattr(self, 'last_wheel_state')
                and len(getattr(self, 'last_wheel_state', [])) == len(delta)
                else None)
        if prev is None:
            # First frame -- no history.  Use the strict envelope.
            contact_mask  = (delta >= ext_cap) & (delta <= comp_cap)
            hanging_mask  = delta <  ext_cap
            over_mask     = delta >  comp_cap
        else:
            contact_mask = np.zeros(len(delta), dtype=bool)
            hanging_mask = np.zeros(len(delta), dtype=bool)
            over_mask    = np.zeros(len(delta), dtype=bool)
            for i in range(len(delta)):
                d  = float(delta[i])
                ps = int(prev[i])
                if ps == WHEEL_STATE_HANGING:
                    # Was hanging.  Need to climb >= ext_cap + HYST
                    # to switch to CONTACT.  Otherwise stay HANGING.
                    if d >= ext_cap + HYST:
                        if d > comp_cap + HYST:
                            over_mask[i] = True
                        else:
                            contact_mask[i] = True
                    else:
                        hanging_mask[i] = True
                elif ps == WHEEL_STATE_OVER_COMP:
                    # Was over-compressed.  Need to drop below
                    # comp_cap - HYST to relax to CONTACT, or below
                    # ext_cap - HYST to fully release to HANGING.
                    if d < ext_cap - HYST:
                        hanging_mask[i] = True
                    elif d <= comp_cap - HYST:
                        contact_mask[i] = True
                    else:
                        over_mask[i] = True
                else:
                    # Was CONTACT (or NONE / first-frame).  Need to
                    # exceed the envelope by HYST in either direction
                    # to leave the contact band.
                    if d < ext_cap - HYST:
                        hanging_mask[i] = True
                    elif d > comp_cap + HYST:
                        over_mask[i] = True
                    else:
                        contact_mask[i] = True

        # Stash for shader / overlay consumers.  CONTACT includes
        # over-compressed by the user's red/green spec ("touching
        # red, no touching green") -- bottoming-out IS touching.
        states = np.where(contact_mask | over_mask,
                          WHEEL_STATE_CONTACT,
                          np.where(hanging_mask,
                                   WHEEL_STATE_HANGING,
                                   WHEEL_STATE_NONE))
        # OVER_COMP gets distinguished afterwards so debug code
        # can spot it without changing the shader colour.
        states[over_mask] = WHEEL_STATE_OVER_COMP
        states[contact_mask & ~over_mask] = WHEEL_STATE_CONTACT
        self.last_wheel_state = states.astype(np.int32)
        # Legacy alias.  Other code paths still consult this; keep
        # it pointing at the contact set.
        self.last_supported = (contact_mask | over_mask)

        # ---- 5. <3 contacting wheels -> gravity drop --------------
        # A plane through 2 or fewer points is degenerate (or has
        # an arbitrary axis).  Fall the chassis in Y until more
        # wheels reach the ground.
        n_contact = int((contact_mask | over_mask).sum())
        if n_contact < 3:
            # Emergency plane fit through ALL wheels (ignoring the
            # hysteresis-driven contact mask).  Two cases this
            # rescues:
            #
            # 1. A "stuck-tilted" tank where the previous frame's
            #    pitch / roll caused the classifier to misread
            #    most wheels as HANGING.  An ALL-wheels fit may
            #    produce a flatter pose where >= 3 wheels actually
            #    sit inside their envelope -- in that case the
            #    chassis recovers.
            #
            # 2. Genuine free-fall: the all-wheels fit produces a
            #    pose where < 3 wheels touch (terrain too far
            #    below).  We reject the emergency fit and do a
            #    normal gravity step, preserving the previous
            #    pitch / roll (free-falling tanks don't level
            #    themselves -- realistic; we don't model angular
            #    momentum).
            saved_pos    = float(self.pos[1])
            saved_pitch  = float(self.pitch_deg)
            saved_roll   = float(self.roll_deg)
            try_targets  = np.column_stack([
                local[:, 0], target_centre, local[:, 2],
            ])
            try_n, _     = fit_plane(try_targets)
            try_pitch, try_roll = normal_to_pitch_roll(try_n)
            try_chassis_y = (float(target_centre.mean())
                             - float(local[:, 1].mean()))
            self.pos[1]    = try_chassis_y
            self.pitch_deg = float(try_pitch)
            self.roll_deg  = float(try_roll)
            # Test how many wheels would be in their envelope under
            # this pose -- ignore hysteresis (we're testing a
            # candidate, not committing).
            test_rigid_y = (self._chassis_matrix_target() @ local_h.T).T[:, 1]
            test_delta   = target_centre - test_rigid_y
            n_test       = int(((test_delta >= ext_cap)
                                & (test_delta <= comp_cap + comp_cap)
                                # comp_cap+comp_cap = include OVER
                                # in the count too: a wheel pushed
                                # past the compression cap is still
                                # touching, just bottomed out.
                                ).sum())
            if n_test >= 3:
                # Commit the emergency fit.  Reset velocity (we
                # caught contact) and apply the floor.
                self.vy = 0.0
                self._apply_terrain_floor(target_centre)
            else:
                # No good -- restore previous pose, do gravity fall.
                self.pos[1]    = saved_pos
                self.pitch_deg = saved_pitch
                self.roll_deg  = saved_roll
                self.vy       -= self.gravity * dt
                self.pos[1]   += self.vy * dt
                self._apply_terrain_floor(target_centre)
            # Run the inertia-damped pose integrator + residual
            # compute against the rendered (integrated) pose, then
            # return the rendered chassis matrix to the caller.
            self._update_lateral_lean(dt)
            self._step_pose_integrator(dt, terrain=terrain)
            self._compute_residual_y(target_centre, terrain=terrain)
            return self.chassis_matrix()

        # We're landed enough that vertical velocity isn't growing.
        self.vy = 0.0

        # ---- 6. Plane fit through CONTACT wheels' targets ---------
        # CRITICAL: fit in CHASSIS-LOCAL XZ, not world XZ.  The
        # `chassis_matrix()` composition is `T(pos) @ Ry(yaw) @
        # Rx(pitch) @ Rz(roll)` -- pitch and roll are applied
        # BEFORE yaw, i.e. around CHASSIS-LOCAL X and Z axes.  If
        # we fit the plane in world XZ and read pitch/roll off the
        # world-frame normal, those values are correct for a yaw=0
        # tank but get misapplied as the tank turns: a 90-degree
        # yaw maps world-X (left/right) to chassis-local-Z, so
        # "world pitch" comes out as "chassis roll" on the
        # rendered tank, with the tilt visibly going the wrong way
        # when the tank drives across a slope at an angle.
        #
        # Solution: feed the plane-fit chassis-LOCAL XZ
        # (`local[:, 0]` / `local[:, 2]` -- the bind-pose
        # coordinates that the wheel storage already keeps
        # rotation-free) along with WORLD target Y (terrain is in
        # world space; Y is yaw-invariant so no transform needed).
        # The lstsq solves `target_y_world = a*local_x +
        # b*local_z + c` and the resulting normal `(-a, 1, -b)` is
        # in the chassis-local frame, so `normal_to_pitch_roll`
        # produces the chassis-local pitch / roll that
        # `chassis_matrix()` then yaws into world correctly.
        contact = contact_mask | over_mask
        # Per Coffee 2026-05-11 ("get the physics to use this
        # spline"): when the viewer has handed us a bind-pose
        # homie chain bottom run, use those pad positions as
        # the plane-fit terrain sample sites instead of the
        # wheel hubs.  The chain bottom run is the actual
        # track-on-ground contact line -- many more samples
        # (~30/side vs 6 wheels) along the true contact
        # footprint => smoother pitch/roll fit.  Wheel hubs
        # are still classified per-wheel above for the
        # CONTACT/HANGING/OVER_COMP suspension state.
        chain_local = self._homie_bottom_local
        if (chain_local is not None
                and len(chain_local) >= 3
                and terrain is not None):
            # Project chain pads into world XZ via current
            # target pose (yaw + pitch + roll + pos), same
            # transform we use for wheels above.  Sample terrain
            # at those XZ; build plane-fit input in chassis-
            # local XZ + world Y same shape as the wheel form.
            ch_h = np.column_stack([
                chain_local[:, 0],
                chain_local[:, 1],
                chain_local[:, 2],
                np.ones(len(chain_local), dtype=np.float64),
            ])
            ch_world = (m_in @ ch_h.T).T
            ch_wx = ch_world[:, 0]
            ch_wz = ch_world[:, 2]
            if hasattr(terrain, 'sample_heights'):
                ch_ty = np.asarray(
                    terrain.sample_heights(ch_wx, ch_wz),
                    dtype=np.float64)
            elif hasattr(terrain, 'sample_height'):
                ch_ty = np.array([
                    float(terrain.sample_height(float(x), float(z)))
                    for x, z in zip(ch_wx, ch_wz)],
                    dtype=np.float64)
            else:
                ch_ty = np.zeros(len(chain_local),
                                  dtype=np.float64)
            # Target world Y for each chain pad = terrain Y at
            # that pad's world XZ (chain bottom must sit on
            # ground).  Note the chain bottom in chassis-local
            # already includes the (-radius) offset from wheel
            # hub, so no extra lift here -- a `c` term swap
            # absorbed by the force-balance Y solver below.
            targets_local = np.column_stack([
                chain_local[:, 0],
                ch_ty,
                chain_local[:, 2],
            ])
        else:
            targets_local = np.column_stack([
                local[contact, 0],
                target_centre[contact],
                local[contact, 2],
            ])
        n_vec, _  = fit_plane(targets_local)
        pitch_deg, roll_deg = normal_to_pitch_roll(n_vec)

        # ---- 7. Chassis Y via force balance ----------------------
        # Replaces the geometric "mean(target) - mean(local_y)"
        # snap with a Newton iteration that satisfies vertical
        # spring-force equilibrium: chassis weight pushes wheels
        # into the ground until their summed spring forces match.
        # Result: chassis sits a couple cm LOWER than the
        # geometric mean would suggest, with each wheel at its
        # rest-compression -- physically realistic.
        #
        # Run on the CURRENT contact set; bottomed-out wheels
        # are handled cleanly because the per-wheel force is
        # capped at F_max within the iteration, and the terrain
        # floor below catches any residual constraint violation.
        # Falls back to the geometric snap if force balance fails
        # to converge within its inner-iteration budget (rare).
        try:
            chassis_y_target = _force_balance_pos_y(
                target_centre[contact],
                local[contact],
                pitch_deg, roll_deg,
                comp_cap=comp_cap, ext_cap=ext_cap)
        except Exception:
            chassis_y_target = (
                float(target_centre[contact].mean())
                - float(local[contact, 1].mean()))

        # Velocity-limited Y move when the chassis would drop a lot
        # in one frame (driving off a cliff edge).  Same threshold
        # as the previous implementation.
        delta_y = chassis_y_target - self.pos[1]
        FALL_THRESHOLD = (self.max_offset - self.min_offset)
        if delta_y < -FALL_THRESHOLD:
            self.vy    -= self.gravity * dt
            new_y       = self.pos[1] + self.vy * dt
            if new_y <= chassis_y_target:
                new_y, self.vy = chassis_y_target, 0.0
            self.pos[1]    = new_y
            self.pitch_deg = float(pitch_deg)
            self.roll_deg  = float(roll_deg)
            # Hard floor (see _apply_terrain_floor).  Catches the
            # case where the chassis was falling fast and would
            # otherwise slip below the constraint surface.
            self._apply_terrain_floor(target_centre)
            # Same integrator step as the early-fall return above:
            # render the lagged chassis pose, compute residuals
            # against it.
            self._update_lateral_lean(dt)
            self._step_pose_integrator(dt, terrain=terrain)
            self._compute_residual_y(target_centre, terrain=terrain)
            return self.chassis_matrix()

        # Constraint snap.  The contact-set hysteresis (added in
        # 1.86.2) already kills the per-frame contact-set flapping
        # that was the root cause of oscillation, so we don't need
        # to damp the chassis pose itself -- the previous
        # asymmetric-alpha layer was a band-aid that made the
        # tank feel like it was floating.  Snap the chassis to
        # the plane-fit pose directly so gravity-driven landing
        # and constraint-driven lift are immediate.
        #
        # Per-wheel residual damping (`smoothed_residual_y`) is
        # KEPT because that drives the shader bone-deflection
        # animation, which benefits from a few frames of visual
        # smoothing without affecting the physics solve.
        self.pos[1]    = chassis_y_target
        self.pitch_deg = float(pitch_deg)
        self.roll_deg  = float(roll_deg)
        self.vy        = 0.0

        # Hard floor: even after the plane fit has settled, an
        # individual wheel sitting on a terrain spike higher than
        # the suspension can absorb forces the chassis ITSELF
        # higher.  See `_apply_terrain_floor` docstring -- the
        # "broken-axle / chassis-on-dirt" case.
        self._apply_terrain_floor(target_centre)

        # ---- Iterative refinement (PBD-style) -------------------
        # The single plane-fit + floor pass is a good first
        # approximation, but it can leave the pose tilted in a way
        # that's correct for the ORIGINAL contact set yet stale
        # once the floor has lifted the chassis.  Specifically:
        # after the floor lifts pos_y to relieve the most-
        # compressed wheel, some wheels that were CONTACT in the
        # initial plane fit are now HANGING (their target is
        # too far below the lifted chassis to reach).  The pitch
        # / roll values were derived against the old contact set
        # and don't match the post-lift situation.
        #
        # Solution: prioritise the wheel with the most compression
        # need (the user's explicit ask: "wheels with most
        # compression first -- likely to run out of travel
        # first"), pin it to its target, refit the plane through
        # the surviving contacts, re-floor.  Up to 3 iterations
        # (overkill in practice; convergence usually hits in 1).
        #
        # Approach: each iter, re-derive the per-wheel delta from
        # the current pose.  Find the wheel with the LARGEST
        # delta (most compressed -- most likely to need full
        # travel).  Re-classify everyone using current pose +
        # hysteresis.  If contact set changed and >= 3 wheels
        # remain, refit and floor.  Stop when stable.
        prev_n_contact = int((contact_mask | over_mask).sum())
        for _refine in range(3):
            # TARGET-pose matrix (NOT the render-lagged one) -- the
            # refinement is solving for the next target, so it must
            # see the in-progress target the loop has been writing
            # into self.pos / pitch_deg / roll_deg.  Reading the
            # render pose here is the v1.93.0 feedback bug.
            m_iter = self._chassis_matrix_target()
            rigid_y_iter = (m_iter @ local_h.T).T[:, 1]
            delta_iter = target_centre - rigid_y_iter
            # Re-classify with hysteresis against the LATEST
            # `last_wheel_state` (= the post-pose-update reading
            # from `_compute_residual_y` -- but we haven't called
            # that yet for this frame, so use the classifier's
            # `states` from the start of this update() call).
            # Hysteresis margins kept identical to step 4 above.
            new_contact = np.zeros(len(delta_iter), dtype=bool)
            new_over    = np.zeros(len(delta_iter), dtype=bool)
            new_hang    = np.zeros(len(delta_iter), dtype=bool)
            for i in range(len(delta_iter)):
                d  = float(delta_iter[i])
                ps = int(states[i])
                if ps == WHEEL_STATE_HANGING:
                    if d >= ext_cap + HYST:
                        if d > comp_cap + HYST:
                            new_over[i] = True
                        else:
                            new_contact[i] = True
                    else:
                        new_hang[i] = True
                elif ps == WHEEL_STATE_OVER_COMP:
                    if d < ext_cap - HYST:
                        new_hang[i] = True
                    elif d <= comp_cap - HYST:
                        new_contact[i] = True
                    else:
                        new_over[i] = True
                else:
                    if d < ext_cap - HYST:
                        new_hang[i] = True
                    elif d > comp_cap + HYST:
                        new_over[i] = True
                    else:
                        new_contact[i] = True

            new_contact_set = new_contact | new_over
            n_new_contact = int(new_contact_set.sum())
            # Stop if the contact set didn't change OR we'd lose
            # the plane (< 3 supporting wheels means we shouldn't
            # refit; the floor + the previous pose are the best
            # we can do this frame).
            if n_new_contact == prev_n_contact:
                break
            if n_new_contact < 3:
                break
            # Refit plane through the NEW contact set.  Most-
            # compressed wheels (`new_over`) get included; that
            # forces the plane up to relieve them as much as the
            # tilt allows.
            targets_local_iter = np.column_stack([
                local[new_contact_set, 0],
                target_centre[new_contact_set],
                local[new_contact_set, 2],
            ])
            n_vec_i, _ = fit_plane(targets_local_iter)
            pitch_i, roll_i = normal_to_pitch_roll(n_vec_i)
            chassis_y_i = (
                float(target_centre[new_contact_set].mean())
                - float(local[new_contact_set, 1].mean()))
            self.pitch_deg = float(pitch_i)
            self.roll_deg  = float(roll_i)
            self.pos[1]    = chassis_y_i
            self._apply_terrain_floor(target_centre)
            # Update the master classification + bookkeeping so
            # `_compute_residual_y` below sees the refined state.
            contact_mask = new_contact
            over_mask    = new_over
            hanging_mask = new_hang
            states_new = np.where(new_contact | new_over,
                                  WHEEL_STATE_CONTACT,
                                  np.where(new_hang,
                                           WHEEL_STATE_HANGING,
                                           WHEEL_STATE_NONE))
            states_new[new_over] = WHEEL_STATE_OVER_COMP
            states_new[new_contact & ~new_over] = WHEEL_STATE_CONTACT
            states                = states_new
            self.last_wheel_state = states.astype(np.int32)
            self.last_supported   = new_contact_set
            prev_n_contact        = n_new_contact

        # ---- 8. Per-wheel residual for skinning -------------------
        # CONTACT wheels: residual from the post-fit pose vs target
        # (small; lstsq fit error).
        # HANGING wheels: residual = extension cap (full droop).
        # OVER_COMP wheels: residual = compression cap (bottomed out).
        # All of this lives in `_compute_residual_y` so the shader
        # path stays one read of `self.last_residual_y`.
        # Run the inertia-damped pose integrator FIRST so the
        # rendered pose (used by both _compute_residual_y and the
        # returned chassis_matrix) reflects this frame's smoothed
        # position rather than the raw target snap.
        self._update_lateral_lean(dt)
        self._step_pose_integrator(dt, terrain=terrain)
        self._compute_residual_y(target_centre, terrain=terrain)
        return self.chassis_matrix()

    # ------------------------------------------------------------------
    def _apply_terrain_floor(self, target_centre):
        """Hard constraint: NO wheel centre can be more than `comp_cap`
        below its terrain target.

        This implements the user's "wheels can't go below the surface"
        rule.  Each wheel can compress up to `comp_cap` (= `-min_offset`,
        typically +0.04 m) into the hull.  Beyond that, the suspension
        is exhausted and the chassis itself has to rise to make room
        -- physically: a fully-compressed wheel transmits force
        directly to the chassis instead of through the spring.
        Visually: the tank's belly sits on whatever's pushing the
        wheel up.

        For each wheel `i`:
          required_pos_y[i] = target_y[i] - rotation_only_local_y[i]
                              - comp_cap
        where `rotation_only_local_y[i]` is the Y component of the
        wheel's chassis-local position after the current pitch + roll
        rotations are applied (no yaw -- yaw doesn't affect Y -- and
        no translation).  We then take the max across all wheels;
        `pos[1]` gets clamped to that floor.

        When this clamp activates, vertical velocity also resets to
        zero -- the tank just landed on a spike / piece of terrain
        too high to drive over, can't sink further until the user
        steers off it.
        """
        if len(self.wheels) == 0:
            return
        pitch_rad = math.radians(self.pitch_deg)
        roll_rad  = math.radians(self.roll_deg)
        Rx = mat4_rotate_x(pitch_rad).astype(np.float64)
        Rz = mat4_rotate_z(roll_rad).astype(np.float64)
        M  = Rx @ Rz
        local = self.wheels
        local_h = np.column_stack([
            local[:, 0], local[:, 1], local[:, 2],
            np.ones(len(local), dtype=np.float64),
        ])
        y_zero = (M @ local_h.T).T[:, 1]

        comp_cap = -self.min_offset
        required = (np.asarray(target_centre, dtype=np.float64)
                    - y_zero - comp_cap)
        pos_y_min = float(required.max())

        if self.pos[1] < pos_y_min:
            self.pos[1] = pos_y_min
            # Hit the constraint floor.  Vertical velocity resets
            # because the chassis can't continue dropping; some wheel
            # is at full compression and the hull is what's stopping
            # the fall.
            self.vy = 0.0

    # ------------------------------------------------------------------
    def _compute_residual_y(self, target_centre, terrain=None):
        """Stash per-wheel mesh-local Y residuals into
        `self.last_residual_y`.

        For each wheel:
          * `target_world_y`  = terrain Y under wheel + wheel radius
          * `rigid_world_y`   = chassis_pose @ wheel_local_pos -> .y
          * `residual_world_y` =  target_world_y - rigid_world_y

        Sign convention -- `last_residual_y[i] > 0` means the wheel
        needs to RISE (UP, into the hull, compression direction);
        `< 0` means the wheel needs to DROP (DOWN, away from the
        hull, extension direction).  This matches normal mechanical
        intuition AND the GL skinning math: the shader does
        `world = model * (T(0, ry, 0) * position)`, so bone Y > 0
        moves the visible vertex UP.  No sign flip anywhere — what
        we publish here is what the bone matrix carries through.

        (An earlier docstring claimed there was a "shader-side sign
        quirk" requiring a flip.  That was a misdiagnosis -- the
        wheels were sinking because of the unrelated sign error in
        bone_matrix_array, not because the shader flipped anything.
        See viewer.py `_upload_skinning` and shaders/mesh.vert.)

        Suspension envelope clamp: physical tanks have a finite
        suspension travel range -- defined per-tank in the gameplay
        XML's `<groundNodes>` block as `(minOffset, maxOffset)`,
        typical `(-0.04, +0.08)`.  Our convention (see the
        `update()` docstring for the long form):
          * `+ residual` = wheel rises into the hull (compression),
            limited by `-min_offset` (= max compression upward).
          * `- residual` = wheel drops below neutral (extension),
            limited by `-max_offset` (= max extension downward).
        Anything beyond either bound is clamped, so a wheel that
        would otherwise "punch through the chassis" or "stretch
        infinitely into a chasm" instead bottoms-out at the
        respective limit.  The chassis itself doesn't drop further
        for the over-extended case here -- that's the rigid-pose's
        FALL_THRESHOLD path's job above.
        """
        m = self.chassis_matrix()                         # (4, 4)
        local = self.wheels                               # (n, 3)
        local_h = np.column_stack([
            local[:, 0], local[:, 1], local[:, 2],
            np.ones(len(local), dtype=np.float64),
        ])                                                # (n, 4)
        world = (m @ local_h.T).T                         # (n, 4)
        rigid_world_y = world[:, 1]
        # `target_centre` was sampled at the SOLVER's target-pose
        # wheel XZ.  But each wheel's RENDERED XZ may differ
        # (chassis pitch / roll between target and render).  For
        # zero penetration in the rendered view we need to
        # re-sample terrain at each wheel's RENDER XZ and compute
        # the residual against THAT.  Otherwise the bone matrix
        # would move the wheel to the target-XZ ground sample,
        # which is nowhere near where the visible wheel is.
        # Per Coffee 2026-05-08: master override = wheel-on-ground.
        if terrain is not None:
            wx_r = world[:, 0]
            wz_r = world[:, 2]
            if hasattr(terrain, 'sample_heights'):
                ty_r = np.asarray(
                    terrain.sample_heights(wx_r, wz_r),
                    dtype=np.float64)
            elif hasattr(terrain, 'sample_height'):
                ty_r = np.array(
                    [float(terrain.sample_height(float(x), float(z)))
                     for x, z in zip(wx_r, wz_r)],
                    dtype=np.float64)
            else:
                ty_r = None
            if ty_r is not None:
                target_centre = (ty_r + self.radius + self.track_thickness
                                 + float(getattr(self, 'pad_lift', 0.0)))

        # Clean sign: +ve = wheel needs to RISE (UP, compression).
        # The shader-side sign flip is applied inside
        # bone_matrix_array Pass 1, NOT here, so consumers reading
        # last_residual_y directly (debug overlays, NURB-track
        # V_loc binder) get an intuitive value.
        residual = (np.asarray(target_centre, dtype=np.float64)
                    - np.asarray(rigid_world_y, dtype=np.float64))

        # Envelope caps.  The engine's <groundNodes> stores
        # (minOffset, maxOffset).  Mapping into our +ve-up convention:
        #   max compression (wheel can rise) =  -self.min_offset
        #   max extension   (wheel can drop) =  -self.max_offset
        # Default values (-0.04, +0.08) -> compression cap +0.04 m,
        # extension cap -0.08 m.
        comp_cap = -self.min_offset
        ext_cap  = -self.max_offset

        # Two-rule clamp (per Coffee 2026-05-08, the master physics
        # override):
        #
        #   1. wheel can NEVER be below terrain (no penetration),
        #   2. wheel can NEVER extend more than `ext_cap` below its
        #      bind (max droop) or compress more than `comp_cap`
        #      above (bottomed out into hull).
        #
        # The bone-path residual gets its envelope clamp BACK here.
        # Rule (1) is enforced by the render-side terrain floor in
        # `_step_pose_integrator`, which raises the rendered chassis
        # Y so no wheel needs more than `comp_cap` of compression to
        # reach the ground.  After that floor runs, the unclamped
        # raw residual would naturally fall in [ext_cap, comp_cap]
        # for any wheel that's actually contacting terrain; the
        # clamp here is the safety net for HANGING wheels (residual
        # would be < ext_cap because terrain is too far below ->
        # wheel droops fully extended, visible gap to ground).
        self.last_residual_y = np.clip(residual, ext_cap, comp_cap)

        # Asymmetric low-pass filter per-wheel: same compression-
        # fast / extension-slow recipe as the chassis pose, applied
        # to each wheel's residual so the shader animation matches
        # real shock-absorber behaviour.
        #   * residual rising    (wheel pushed UP into hull)   = comp
        #   * residual falling   (wheel dropping toward ground) = ext
        # Wheels at full droop (HANGING) hit the extension cap and
        # then stop changing -- the slow-extension alpha keeps that
        # transition visually smooth instead of snapping the wheel
        # to the bottom of its travel in one frame.
        ALPHA_COMP = 0.50
        ALPHA_EXT  = 0.15
        if (not hasattr(self, 'smoothed_residual_y')
                or len(self.smoothed_residual_y) != len(residual)):
            # First-frame fallback or rig-change: snap initial
            # smoothed value to current residual, no blending.
            self.smoothed_residual_y = residual.copy()
        else:
            for i in range(len(residual)):
                old = float(self.smoothed_residual_y[i])
                new = float(residual[i])
                a   = ALPHA_COMP if new >= old else ALPHA_EXT
                self.smoothed_residual_y[i] = a * new + (1.0 - a) * old

    # ------------------------------------------------------------------
    def set_extra_rotating_wheels(self, names, hubs, radii):
        """Register additional bones that should spin under
        `advance_wheel_angles`: drive sprockets, idlers, return
        rollers.  Road wheels stay handled via `self.wheels` /
        `self.wheel_bone_names`.

        Per Coffee 2026-05-13 ("rotate all wheels").

        Args:
            names: list of bone names (typically `WD_<side><i>
                   _BlendBone`).
            hubs:  (M, 3) chassis-local bind positions.
            radii: (M,) per-bone wheel radii in metres.
        """
        if not names:
            self.extra_rotating_bones      = []
            self.extra_rotating_hubs       = np.zeros((0, 3),
                                                       dtype=np.float32)
            self.extra_rotating_radii      = np.zeros((0,),
                                                       dtype=np.float32)
            self.extra_rotating_angles_rad = np.zeros((0,),
                                                       dtype=np.float32)
            self._extra_rot_name_to_idx    = {}
            return
        self.extra_rotating_bones = list(names)
        self.extra_rotating_hubs  = np.asarray(
            hubs, dtype=np.float32).reshape(-1, 3)
        self.extra_rotating_radii = np.asarray(
            radii, dtype=np.float32).reshape(-1)
        self.extra_rotating_angles_rad = np.zeros(
            len(names), dtype=np.float32)
        self._extra_rot_name_to_idx = {
            nm: i for i, nm in enumerate(names) if nm}

    def advance_wheel_angles(self, v_L, v_R, dt):
        """Advance each rotating wheel's spin angle by
        (v_track / R) * dt.  Covers BOTH road wheels (W_<side><i>
        bones via `self.wheels`) and any extras registered via
        `set_extra_rotating_wheels` (sprockets / idlers /
        rollers).

        Per Coffee 2026-05-13 ("we are adding wheel rotations" +
        "rotate all wheels" + "spinning backwards").  Sign on
        the angle delta is NEGATIVE so the visible spin matches
        the chain advance direction (Coffee verified after the
        v1.118.113 first pass).

        Side derived from each bone's chassis-local X sign:
        X < 0 -> left -> use v_L; X >= 0 -> right -> use v_R.
        """
        # Per Coffee 2026-05-13 ("radius miscalculation"): for a
        # tracked vehicle, the wheel rolls on the CHAIN'S inner
        # face at radius R (the bare wheel rim) -- the chain's
        # OUTER face is what touches the ground.  With no-slip at
        # both contacts:
        #   omega_wheel * R = chain_speed     (no slip wheel-chain)
        #   chain_speed     = chassis_speed   (no slip chain-ground)
        # so omega_wheel = v / R.  `track_thickness` does NOT enter
        # the wheel-spin rate (only relevant if the wheel rolled
        # directly on ground, which it doesn't here).

        # ---- Road wheels --------------------------------------
        if (self.wheel_angles_rad is None
                or len(self.wheel_angles_rad) != len(self.wheels)):
            self.wheel_angles_rad = np.zeros(
                len(self.wheels), dtype=np.float32)
        R_road = max(float(self.radius), 1e-3)
        for i in range(len(self.wheels)):
            x = float(self.wheels[i, 0])
            v = float(v_L) if x < 0.0 else float(v_R)
            self.wheel_angles_rad[i] += -(v / R_road) * float(dt)

        # ---- Extra rotating wheels ----------------------------
        if (getattr(self, 'extra_rotating_bones', None)
                and getattr(self, 'extra_rotating_hubs', None) is not None
                and getattr(self, 'extra_rotating_radii', None) is not None):
            hubs  = self.extra_rotating_hubs
            radii = self.extra_rotating_radii
            for i in range(len(self.extra_rotating_bones)):
                x = float(hubs[i, 0])
                v = float(v_L) if x < 0.0 else float(v_R)
                R_i = max(float(radii[i]), 1e-3)
                self.extra_rotating_angles_rad[i] += (
                    -(v / R_i) * float(dt))

    def bone_matrix_array(self, palette, max_bones=64):
        """Build the per-bone matrix array for the skinning shader.

        Args:
            palette (list[str] | None): list of bone names in
                renderSet declaration order -- each entry is the
                NAME a vertex's `iii` byte (after `byte // 3`)
                indexes into.  Comes from `mesh.bone_palette`.
                None -> identity array (no skinning data available;
                upload but the shader will see u_skinned == 0
                anyway).
            max_bones (int): array slot count.  Must match the
                shader's `MAX_BONES` (= 64).

        Returns:
            np.ndarray of shape (max_bones, 4, 4), dtype float32.

        Bone semantics:
          * `W_<L|R>\\d+_BlendBone`,
            `W_F_<L|R>_BlendBone`,
            `W_R\\d+_<L|R>_BlendBone` -- road-wheel bones.  Translate
            in mesh-local Y by the wheel's residual.
          * `WD_<L|R>\\d+_BlendBone`  -- ambiguous.  If the bone was
            classified as a road wheel during auto-extract (in
            `last_residual_y`), it gets the wheel residual; else
            identity.
          * `Track_<L|R>\\d+_BlendBone` -- track segment under wheel
            <i>.  Follows the corresponding W_<side><i> wheel via a
            naming-pattern lookup; if no match, identity.
          * Anything else (V_BlendBone, decorative bones) -> identity.

        Mapping note: we re-derive the bone-name -> wheel-index
        mapping every call because it's only N entries (typical
        N = 7-23) and rebuilds at ~microsecond timescales -- not
        worth caching across frames.  Centralising the lookup here
        keeps the shader path free of any name parsing.
        """
        out = np.tile(np.eye(4, dtype=np.float32), (max_bones, 1, 1))
        if palette is None or self.last_residual_y is None:
            return out
        # Use the RAW residual (target - render_rigid_y) directly.
        # Per Coffee 2026-05-08, wheel-on-ground is the master
        # override -- the asymmetric "shock-absorber feel"
        # smoothing (smoothed_residual_y) introduces a 110 ms
        # extension lag that visibly lets wheels float above /
        # drive through small terrain bumps.  Raw residual snaps
        # the wheel to terrain every frame, no lag, no penetration.
        # `smoothed_residual_y` is still computed below for any
        # legacy consumer (debug overlays etc.) but is no longer
        # on the visible-render path.
        residual_src = self.last_residual_y

        # Build a NAME -> wheel-index map from the auto-extract's
        # bookkeeping (`self.wheel_bone_names`).  This is the source
        # of truth: only bones whose name appears in this list got
        # accepted as road wheels (with the Hotchkiss EBR special-
        # case shape confirmation already resolved during extract).
        # Tracked tanks' WD_<side><i> idlers / drive sprockets /
        # return rollers were dropped by the Y-band post-pass and
        # therefore aren't in this map -- they stay identity here
        # and don't deflect.
        #
        # Heuristic-only path (no palette during extract): the names
        # are None.  In that case there's nothing to map, so the
        # skinning falls back to identity for the whole array (the
        # rigid `chassis_matrix()` pose still drives the per-mesh
        # model_matrix, so the tank still places correctly -- just
        # no per-wheel deflection).
        name_to_wheel = {n: i for i, n in enumerate(self.wheel_bone_names)
                         if n}

        # Pass 1: every wheel whose bone name is in `name_to_wheel`
        #         gets a Y-translation residual matrix.
        # Pass 2: track-segment bones (Track_<side><i>_BlendBone)
        #         inherit the matrix of the wheel underneath them
        #         (W_<side><i>_BlendBone) so the track stays glued
        #         to the wheel as it deflects.
        track_targets = []                                  # (palette_idx, target_W_name)

        for pi, name in enumerate(palette):
            if pi >= max_bones:
                break
            if not name:
                continue

            # Road-wheel bone we accepted during extract?
            wheel_idx = name_to_wheel.get(name)
            if wheel_idx is not None:
                # Per Coffee 2026-05-13 ("spin the verts in ZY
                # about center of each wheel"): compose a rotation
                # about the wheel's hub on the X axis (YZ plane)
                # into the bone matrix, in addition to the existing
                # Y residual.
                #
                # Matrix: T(hub + (0, ry, 0)) . Rx(theta) . T(-hub)
                #   shifts a bind-pose vertex to hub-centred,
                #   rotates about chassis-local +X by theta, then
                #   translates back to (hub + Y residual).
                #
                # +residual = wheel up.  Shader does `world = model
                # * skin * position` with no sign flip (verified
                # on T30's "wheels sinking to the rim" bug
                # 2026-05-08).  Bone Y > 0 -> visible wheel UP.
                ry  = float(residual_src[wheel_idx])
                hub = self.wheels[wheel_idx]
                theta = float(self.wheel_angles_rad[wheel_idx]
                              if self.wheel_angles_rad is not None
                              else 0.0)
                c = math.cos(theta)
                s = math.sin(theta)
                # Rx(theta):
                #   [ 1  0  0  0 ]
                #   [ 0  c -s  0 ]
                #   [ 0  s  c  0 ]
                #   [ 0  0  0  1 ]
                # T(hub + ry) . Rx . T(-hub):
                #   row 0:  [ 1  0  0  hub_x - hub_x ]                = [1, 0, 0, 0]
                #   row 1:  [ 0  c -s  hub_y + ry - (c*hub_y - s*hub_z) ]
                #   row 2:  [ 0  s  c  hub_z         - (s*hub_y + c*hub_z) ]
                #   row 3:  [ 0  0  0  1 ]
                m = np.eye(4, dtype=np.float32)
                m[1, 1] = c
                m[1, 2] = -s
                m[2, 1] = s
                m[2, 2] = c
                m[1, 3] = (float(hub[1]) + ry
                           - (c * float(hub[1]) - s * float(hub[2])))
                m[2, 3] = (float(hub[2])
                           - (s * float(hub[1]) + c * float(hub[2])))
                out[pi] = m
                continue

            # Per Coffee 2026-05-13 ("rotate all wheels"): extras
            # (drive sprockets / idlers / return rollers) get a
            # hub-centred Rx rotation, no Y residual (chassis-
            # rigid).  Looked up by exact bone name via the dict
            # built in `set_extra_rotating_wheels`.
            ex_idx = (self._extra_rot_name_to_idx.get(name)
                      if self._extra_rot_name_to_idx else None)
            if ex_idx is not None:
                hub_ex = self.extra_rotating_hubs[ex_idx]
                theta = float(self.extra_rotating_angles_rad[ex_idx])
                c = math.cos(theta)
                s = math.sin(theta)
                m = np.eye(4, dtype=np.float32)
                m[1, 1] =  c; m[1, 2] = -s
                m[2, 1] =  s; m[2, 2] =  c
                m[1, 3] = (float(hub_ex[1])
                           - (c * float(hub_ex[1])
                              - s * float(hub_ex[2])))
                m[2, 3] = (float(hub_ex[2])
                           - (s * float(hub_ex[1])
                              + c * float(hub_ex[2])))
                out[pi] = m
                continue

            # Track segment bone?  Defer until after pass 1.
            tname = _track_to_wheel_name(name)
            if tname is not None:
                track_targets.append((pi, tname))

        # Pass 2: track-segment bones follow the wheel they sit
        # under.  `_track_to_wheel_name(track_name)` returns the
        # corresponding W_<side><i>_BlendBone; we look up that
        # bone's wheel_idx and copy the same Y translation.
        for pi, target_name in track_targets:
            wheel_idx = name_to_wheel.get(target_name)
            if wheel_idx is None:
                # Track bone with no matching W_ bone (rare; some
                # rigs use Track_<i>_BlendBone with W_<j>_BlendBone
                # at j != i).  Leave identity.
                continue
            # Direct pass-through (same as Pass 1 -- no sign flip).
            ry = float(residual_src[wheel_idx])
            out[pi]      = np.eye(4, dtype=np.float32)
            out[pi, 1, 3] = ry

        return out
