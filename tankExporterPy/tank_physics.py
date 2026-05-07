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

import numpy as np


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
                 gravity=9.8):
        self.wheels = np.asarray(
            list(wheels_left) + list(wheels_right), dtype=np.float64)
        self.n_left  = len(wheels_left)
        self.n_right = len(wheels_right)
        self.radius     = float(radius)
        self.min_offset = float(min_offset)
        self.max_offset = float(max_offset)
        self.gravity    = float(gravity)

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
        wheels_left  = []
        wheels_right = []
        for mesh in chassis_meshes or ():
            bi = getattr(mesh, 'bone_indices',  None)
            bw = getattr(mesh, 'bone_weights',  None)
            palette = getattr(mesh, 'bone_palette', None)
            positions = getattr(mesh, 'positions', None)
            if (bi is None or bw is None or positions is None
                    or len(positions) == 0):
                continue
            # Group vertex indices by dominant iii byte.
            groups = {}
            for v in range(len(positions)):
                if bw[v].sum() <= 0:
                    continue
                slot = int(np.argmax(bw[v]))
                by = int(bi[v][slot])
                groups.setdefault(by, []).append(v)

            for by, vert_idxs in groups.items():
                idx = by // 3
                name = (palette[idx]
                        if palette is not None and idx < len(palette)
                        else None)
                # Filter to W_<side><i>_BlendBone groups only --
                # those are the suspension wheels.  WD_ are
                # decorative (drive sprocket / idler / return
                # rollers); they don't bob with the suspension.
                # When we don't have palette names, fall back to
                # X-side discrimination (Y filtered to "low"
                # roughly = road wheel band).
                if name is not None:
                    if not (name.startswith('W_L') or name.startswith('W_R')):
                        continue
                    side = 'left' if name.startswith('W_L') else 'right'
                else:
                    # Heuristic fallback when bone_palette wasn't
                    # plumbed through.  We want main road wheels
                    # (the W_<side><i> groups) and nothing else.
                    # Three criteria, all must pass:
                    #
                    #   1. COMPACT bbox -- a wheel is roughly disc-
                    #      shaped, so dz / dy / both should be
                    #      ~ 2 * radius (< ~1 m).  Excludes the
                    #      V_BlendBone hull (long Z extent).
                    #
                    #   2. LOW Y centroid -- road wheels sit near
                    #      the chassis bottom; drive sprocket /
                    #      idler / return rollers all live higher.
                    #      Threshold 0.55 m catches the road-wheel
                    #      Y band (~0.43 on T110E4) but excludes
                    #      WD_L0 (drive sprocket Y ~ 0.92), WD_L6
                    #      (idler Y ~ 1.02) and return rollers
                    #      (Y ~ 1.11).
                    #
                    #   3. DECENT vert count -- a real road wheel
                    #      has hundreds of verts; a stray bone
                    #      group with one or two influence verts
                    #      shouldn't pass.
                    p = np.asarray(positions)[vert_idxs]
                    if len(vert_idxs) < 50:
                        continue
                    ext = p.max(axis=0) - p.min(axis=0)
                    if ext[1] > 1.0 or ext[2] > 1.0:
                        continue   # not disc-shaped
                    cy = float(p[:, 1].mean())
                    if cy > 0.55:
                        continue   # too high to be a road wheel
                    cx = float(p[:, 0].mean())
                    side = 'left' if cx < 0 else 'right'

                p = np.asarray(positions)[vert_idxs]
                c = p.mean(axis=0)
                # Z negate to match TEPY's render-side world-Z
                # convention (see T110E4_WHEELS comment).
                tup = (float(c[0]), float(c[1]), -float(c[2]))
                if side == 'left':
                    wheels_left.append(tup)
                else:
                    wheels_right.append(tup)

        # Sort each side rear-to-front by NEGATED Z (visible Z).
        wheels_left.sort (key=lambda t: t[2])
        wheels_right.sort(key=lambda t: t[2])

        # Fall back to T110E4 hardcoded data if extraction failed.
        if not wheels_left or not wheels_right:
            print("[tank_physics] auto-extract found no W_ wheel "
                  "groups; falling back to T110E4 hardcoded rig")
            d = T110E4_WHEELS
            return cls(d['wheels_left'], d['wheels_right'],
                       radius=d['radius'],
                       min_offset=d['min_offset'],
                       max_offset=d['max_offset'])

        print(f"[tank_physics] extracted rig: {len(wheels_left)}L + "
              f"{len(wheels_right)}R wheels  radius={radius:.3f}")
        return cls(wheels_left, wheels_right,
                   radius=radius,
                   min_offset=min_offset,
                   max_offset=max_offset)

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
        """Run one physics tick.

        Pipeline:

        1. Project each wheel's mesh-local position into world XZ using
           the current yaw + (X, Z) translation.
        2. Sample the terrain Y under each wheel.
        3. Target wheel centre = terrain Y + wheel radius.
        4. First-pass guess: flat chassis Y = mean(target_centre) -
           mean(local_y).  Determine which wheels can REACH the ground
           via their suspension travel (delta within envelope).
        5. If NO wheels reach -- the tank is in free-fall.  Drop chassis
           Y by gravity * dt.
        6. Otherwise, fit a least-squares plane through the SUPPORTED
           wheels' targets -> chassis pitch + roll.  Set chassis Y
           so the average supported wheel sits at its target.
        7. Reset vy (we just landed) and return the new pose.

        Args:
            terrain : object with `sample_heights(xs, zs)` or
                      `sample_height(x, z)`.  None -> fallback Y=0.
            dt      (float): seconds since the last call.

        Returns:
            np.ndarray: the chassis pose matrix (4x4 row-major float32).
        """
        # ---- 1. Mesh-local wheel -> world XZ ----------------------
        yaw_rad = math.radians(self.yaw_deg)
        cy, sy  = math.cos(yaw_rad), math.sin(yaw_rad)
        local = self.wheels
        wx = cy * local[:, 0] + sy * local[:, 2] + self.pos[0]
        wz = -sy * local[:, 0] + cy * local[:, 2] + self.pos[2]
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

        # ---- 3. Target wheel centre -------------------------------
        target_centre = ty + self.radius
        self.last_terrain_y = ty
        self.last_target_y  = target_centre

        # ---- 4. Identify supported wheels via flat-chassis guess --
        # Suspension delta convention (ours): delta = target - neutral.
        # +ve = wheel rises (compression); -ve = wheel drops (extension).
        # Engine's `<groundNodes>` envelope:
        #   minOffset (-0.04 typical) = wheel rises to compression limit
        #   maxOffset (+0.08 typical) = wheel drops to extension limit
        # In OUR convention these become:
        #   delta upper bound = -min_offset  (= +0.04, max compression)
        #   delta lower bound = -max_offset  (= -0.08, max extension)
        # A wheel is SUPPORTED iff its delta is >= the lower bound
        # (i.e. the suspension can reach the ground).  Beyond the
        # upper bound the wheel is over-compressed but still bearing
        # weight, so still "supported" for plane-fit purposes.
        envelope_lo = -self.max_offset      # most-negative delta allowed
        chassis_y_guess = (target_centre.mean()
                           - float(local[:, 1].mean()))
        guess_neutral = chassis_y_guess + local[:, 1]
        delta_guess   = target_centre - guess_neutral
        self.last_supported = delta_guess >= envelope_lo

        # ---- 5. All airborne -> gravity drop ----------------------
        if not np.any(self.last_supported):
            self.vy -= self.gravity * dt
            self.pos[1] += self.vy * dt
            return self.chassis_matrix()

        # ---- 6. Plane fit through supported targets ---------------
        targets3d = np.column_stack([wx, target_centre, wz])
        n, p0 = fit_plane(targets3d[self.last_supported])
        pitch_deg, roll_deg = normal_to_pitch_roll(n)

        # Set chassis Y so the average supported wheel sits at its
        # target.  Pitch/roll add a small per-wheel Y delta that we
        # ignore here (good for tilts up to ~10 deg; beyond that the
        # solve would need iteration).
        chassis_y = (
            float(target_centre[self.last_supported].mean())
            - float(local[self.last_supported, 1].mean()))

        # ---- 7. Velocity-limited Y move ---------------------------
        # If the target chassis Y is far below the current pos --
        # i.e. the tank just drove off a cliff or got teleported
        # above the terrain -- DON'T snap.  Integrate gravity
        # instead so the user sees a real fall.  Threshold: any
        # drop > suspension travel (12 cm) = "fall".
        delta_y = chassis_y - self.pos[1]
        FALL_THRESHOLD = (self.max_offset - self.min_offset)
        if delta_y < -FALL_THRESHOLD:
            # Falling.  Integrate velocity, advance position.
            self.vy   -= self.gravity * dt
            new_y      = self.pos[1] + self.vy * dt
            # Catch on the way down -- if we cross the kinematic
            # target during this tick, snap to it and reset vy.
            if new_y <= chassis_y:
                new_y = chassis_y
                self.vy = 0.0
            self.pos[1] = new_y
            # Pitch / roll still update so the tank tilts as it
            # falls onto uneven ground -- visually correct.
            self.pitch_deg = float(pitch_deg)
            self.roll_deg  = float(roll_deg)
            return self.chassis_matrix()

        # Resting / small move -- snap.
        self.pos[1]    = chassis_y
        self.pitch_deg = float(pitch_deg)
        self.roll_deg  = float(roll_deg)
        self.vy        = 0.0
        return self.chassis_matrix()
