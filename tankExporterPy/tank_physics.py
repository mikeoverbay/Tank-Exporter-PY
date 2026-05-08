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
            wheel is on the ground.  9.8 by default.
    """

    def __init__(self, wheels_left, wheels_right,
                 radius=0.33, min_offset=-0.04, max_offset=+0.08,
                 gravity=58.92, track_thickness=0.016):
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

    # ------------------------------------------------------------------
    @classmethod
    def for_t110e4(cls):
        """Convenience factory pre-loaded with T110E4 wheel data.
        Kept for the legacy in-app default; new code should prefer
        `from_chassis_meshes` so the rig works for any tank."""
        d = T110E4_WHEELS
        return cls(d['wheels_left'], d['wheels_right'],
                   radius=d['radius'],
                   min_offset=d['min_offset'],
                   max_offset=d['max_offset'])

    # ------------------------------------------------------------------
    @classmethod
    def from_chassis_meshes(cls, chassis_meshes,
                            radius=0.33,
                            min_offset=-0.04,
                            max_offset=+0.08):
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

            # Z negate to match TEPY's render-side world-Z convention
            # (see T110E4_WHEELS comment).
            tup = (cx, cy, -cz, wheel_name)
            if side == 'left':
                wheels_left.append(tup)
            else:
                wheels_right.append(tup)

        # Y-band post-pass.  Some chassis carry "wheel-shaped"
        # decorative bones that sit ABOVE the actual road-wheel
        # band -- typically the drive sprocket (rear, cy ~ 0.75 on
        # Bourrasque) and idler (front, cy ~ 0.63).  These pass the
        # name + shape filter on the way through but they don't
        # bear weight against the ground -- the track does, sagging
        # between road wheels.  Reject anything more than 15 cm
        # above the LOWEST accepted wheel on the same side.
        #
        # On rigs where every wheel sits at the same Y (Hotchkiss
        # EBR -- all wheels at cy = 0.619), this is a no-op.  On
        # rigs with mixed heights (Bourrasque road wheels at 0.395,
        # WD_ sprocket / idler at 0.627 / 0.749), the WD_ entries
        # fall outside the 15 cm band and get dropped.
        Y_BAND = 0.15
        if wheels_left:
            min_y = min(w[1] for w in wheels_left)
            wheels_left  = [w for w in wheels_left  if w[1] <= min_y + Y_BAND]
        if wheels_right:
            min_y = min(w[1] for w in wheels_right)
            wheels_right = [w for w in wheels_right if w[1] <= min_y + Y_BAND]

        # Sort each side rear-to-front by NEGATED Z (visible Z).
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
            return cls(d['wheels_left'], d['wheels_right'],
                       radius=d['radius'],
                       min_offset=d['min_offset'],
                       max_offset=d['max_offset'])

        named_count = sum(1 for n in (names_left + names_right) if n)
        print(f"[tank_physics] extracted rig: {len(wheels_left)}L + "
              f"{len(wheels_right)}R wheels  radius={radius:.3f}  "
              f"({named_count} bone-named, "
              f"{len(names_left) + len(names_right) - named_count} "
              f"heuristic-only)")
        inst = cls(wheels_left, wheels_right,
                   radius=radius,
                   min_offset=min_offset,
                   max_offset=max_offset)
        # Stash the parallel name list so `bone_matrix_array` knows
        # which palette entries correspond to wheels we ACTUALLY
        # accepted (drops a tracked tank's WD_ idlers / sprockets
        # without breaking the Hotchkiss EBR's WD_ road wheels).
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
        """Return the current chassis pose as a 4x4 float32 matrix.

        Matrix composition order: T(pos) * Ry(yaw) * Rx(pitch) * Rz(roll).
        Apply it as: world_pos = chassis_matrix @ mesh.model_matrix @ vert.
        """
        # Yaw separately because user controls it.
        ty = mat4_translate(self.pos[0], self.pos[1], self.pos[2])
        ry = self._mat4_rotate_y(math.radians(self.yaw_deg))
        rx = mat4_rotate_x(math.radians(self.pitch_deg))
        rz = mat4_rotate_z(math.radians(self.roll_deg))
        return (ty @ ry @ rx @ rz).astype(np.float32)

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
        world_h = (self.chassis_matrix() @ local_h.T).T
        wx = world_h[:, 0]
        wz = world_h[:, 2]
        self.last_wheel_world[:, 0] = wx
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
        target_centre = ty + self.radius + self.track_thickness
        self.last_terrain_y = ty
        self.last_target_y  = target_centre

        # ---- 4. Classify each wheel via CURRENT chassis pose ------
        # Compute world Y under each wheel for the chassis_pose
        # we're STARTING the frame with.  delta = target - rigid_y
        # then tells us how much the wheel needs to compress (+ve)
        # or extend (-ve) from its current rendered position to
        # reach the terrain.  Compare against the suspension
        # envelope to bucket each wheel.
        m_in = self.chassis_matrix()
        local_h = np.column_stack([
            local[:, 0], local[:, 1], local[:, 2],
            np.ones(len(local), dtype=np.float64),
        ])
        rigid_y_in = (m_in @ local_h.T).T[:, 1]
        delta = target_centre - rigid_y_in
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
            test_rigid_y = (self.chassis_matrix() @ local_h.T).T[:, 1]
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
            self._compute_residual_y(target_centre)
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
            self._compute_residual_y(target_centre)
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
            m_iter = self.chassis_matrix()
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
        self._compute_residual_y(target_centre)
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
    def _compute_residual_y(self, target_centre):
        """Stash per-wheel mesh-local Y residuals into
        `self.last_residual_y`.

        For each wheel:
          * `target_world_y`  = terrain Y under wheel + wheel radius
          * `rigid_world_y`   = chassis_pose @ wheel_local_pos -> .y
          * `residual_world_y` = `-(target_world_y - rigid_world_y)`

        Sign convention (Professor Coffee, this session): the bone
        Y-translation in mesh-local space comes out FLIPPED relative
        to what the rigid-pose-to-target delta would naively suggest.
        Empirically: when terrain rises under a wheel (target > rigid)
        the wheel should COMPRESS upward into the hull, but the
        un-flipped residual translated the WHEEL GEOMETRY downward
        (i.e. away from the hull).  The flip below makes the
        rendered deflection track terrain in the correct direction.
        Why this happens: the bone matrix is consumed inside
        `mesh.vert` as a pre-multiplication on `position` BEFORE the
        model/view/projection chain, but the bone "moves the wheel"
        is conceptually equivalent to "moves the chassis-relative
        attachment point", and the relationship between those
        directions is signed-opposite for the suspension semantics
        we're modelling.  Cleanest patch: flip the residual sign at
        the source so every downstream consumer (shader, debug
        overlay) sees a consistent "+ve = wheel up" convention.
        TODO: trace this through the chassis_pose composition order
        if we ever add per-wheel arm rotation; for now the flip is
        correct for the translation-only path.

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
        # Sign-flipped: see docstring.  +ve = wheel pushed UP.
        residual = -(np.asarray(target_centre, dtype=np.float64)
                     - np.asarray(rigid_world_y, dtype=np.float64))

        # Envelope caps.  The engine's <groundNodes> stores
        # (minOffset, maxOffset).  Mapping into our +ve-up convention:
        #   max compression (wheel can rise) =  -self.min_offset
        #   max extension   (wheel can drop) =  -self.max_offset
        # Default values (-0.04, +0.08) -> compression cap +0.04 m,
        # extension cap -0.08 m.
        comp_cap = -self.min_offset
        ext_cap  = -self.max_offset

        # State-aware clamp:
        #   * CONTACT  -- residual is the post-fit lstsq error,
        #     should already be small; clamp to envelope as a
        #     safety net for big tilts where the small-angle
        #     approximation in `_compute_residual_y` breaks.
        #   * HANGING  -- terrain too far below; pin to ext_cap so
        #     the wheel droops to the full extension limit.
        #     Visually: wheel hangs below the chassis by ~8 cm.
        #   * OVER_COMP -- terrain too high; pin to comp_cap so the
        #     wheel sits at the full compression limit (bottomed
        #     out).  Visually: wheel pushed up into the hull by
        #     ~4 cm; the chassis itself is the rest of the lift.
        #   * NONE      -- pre-update default (no terrain ticked yet);
        #     leave at zero.
        states = getattr(self, 'last_wheel_state', None)
        if states is None or len(states) != len(residual):
            # First-frame fallback before update() ran -- treat
            # everything as CONTACT and clamp normally.
            residual = np.clip(residual, ext_cap, comp_cap)
        else:
            for i in range(len(residual)):
                s = int(states[i])
                if s == WHEEL_STATE_HANGING:
                    residual[i] = ext_cap
                elif s == WHEEL_STATE_OVER_COMP:
                    residual[i] = comp_cap
                elif s == WHEEL_STATE_CONTACT:
                    if residual[i] < ext_cap: residual[i] = ext_cap
                    elif residual[i] > comp_cap: residual[i] = comp_cap
                else:  # WHEEL_STATE_NONE -- be safe, clamp.
                    if residual[i] < ext_cap: residual[i] = ext_cap
                    elif residual[i] > comp_cap: residual[i] = comp_cap
        self.last_residual_y = residual

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
        # Use the asymmetric-damped residual (real-shock behaviour:
        # fast on compression, slow on extension) as the source for
        # bone Y translations.  Falls back to the raw residual when
        # the smoothed array hasn't been populated yet (first-frame).
        residual_src = (self.smoothed_residual_y
                        if hasattr(self, 'smoothed_residual_y')
                        and len(self.smoothed_residual_y) == len(self.last_residual_y)
                        else self.last_residual_y)

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
                ry            = float(residual_src[wheel_idx])
                out[pi]       = np.eye(4, dtype=np.float32)
                out[pi, 1, 3] = ry
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
            ry = float(residual_src[wheel_idx])
            out[pi]      = np.eye(4, dtype=np.float32)
            out[pi, 1, 3] = ry

        # Pass 3: drive sprocket + idler partial follow.
        # Without this, the track ribbon's wraparound section near
        # the front idler / rear drive sprocket stays put while
        # the road-wheel-side track segments deflect -- producing
        # a visible tear / kink at the seam (worst on tanks like
        # T30 with tight road-wheel spacing right next to the
        # tensioner).
        #
        # Recipe: scan the palette for `WD_<side>\d+(_BlendBone)?`
        # bones, identify which side they belong to + their
        # numeric index.  The LOWEST index per side is the drive
        # sprocket (rear); the HIGHEST is the idler (front).
        # Drive sprocket inherits 50% of the rearmost road
        # wheel's residual; idler inherits 50% of the frontmost
        # road wheel's residual.  50% gets the seam to blend
        # without making the wraparound visibly bob.
        WD_RX = re.compile(r'^WD_([LR])(\d+)(?:_BlendBone)?$')
        per_side_wd = {'L': [], 'R': []}   # (palette_idx, numeric_idx)
        for pi, name in enumerate(palette):
            if pi >= max_bones or not name:
                continue
            m = WD_RX.match(name)
            if m:
                per_side_wd[m.group(1)].append((pi, int(m.group(2))))

        # Index into self.wheels for rearmost / frontmost on each side.
        # `wheels_left` is sorted rear-to-front; same for right.
        SEAM_FOLLOW_FRACTION = 0.5
        rear_left   = 0                    if self.n_left  > 0 else None
        front_left  = self.n_left - 1      if self.n_left  > 0 else None
        rear_right  = self.n_left          if (len(self.wheels) - self.n_left) > 0 else None
        front_right = len(self.wheels) - 1 if (len(self.wheels) - self.n_left) > 0 else None

        for side, wd_list in per_side_wd.items():
            if not wd_list:
                continue
            wd_list.sort(key=lambda t: t[1])     # ascending by numeric index
            drive_palette_idx = wd_list[0][0]    # WD_<side>0 = drive sprocket
            idler_palette_idx = wd_list[-1][0]   # highest-N = idler
            rear_wheel_idx  = rear_left  if side == 'L' else rear_right
            front_wheel_idx = front_left if side == 'L' else front_right
            if rear_wheel_idx is not None:
                ry = (float(residual_src[rear_wheel_idx])
                      * SEAM_FOLLOW_FRACTION)
                out[drive_palette_idx]       = np.eye(4, dtype=np.float32)
                out[drive_palette_idx, 1, 3] = ry
            if front_wheel_idx is not None:
                ry = (float(residual_src[front_wheel_idx])
                      * SEAM_FOLLOW_FRACTION)
                out[idler_palette_idx]       = np.eye(4, dtype=np.float32)
                out[idler_palette_idx, 1, 3] = ry

        return out
