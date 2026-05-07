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
    # Mesh-local centroid of each `W_<side><i>` chassis group.  Y is
    # the bind-pose wheel CENTRE Y (which is what we want -- the
    # bone pivot, not the contact point).  Sequence: rear -> front.
    'wheels_left':  [
        (-1.547, +0.425, -1.952),   # W_L0 (rear)
        (-1.547, +0.425, -1.261),   # W_L1
        (-1.547, +0.425, -0.481),   # W_L2
        (-1.547, +0.425, +0.287),   # W_L3
        (-1.547, +0.425, +1.058),   # W_L4
        (-1.547, +0.425, +1.901),   # W_L5 (front)
    ],
    'wheels_right': [
        (+1.547, +0.425, -1.952),   # W_R0 (rear)
        (+1.547, +0.425, -1.261),   # W_R1
        (+1.547, +0.425, -0.481),   # W_R2
        (+1.547, +0.425, +0.287),   # W_R3
        (+1.547, +0.425, +1.058),   # W_R4
        (+1.547, +0.425, +1.901),   # W_R5 (front)
    ],
}


# ---------------------------------------------------------------------------
# Plane fit + Euler conversion.  Same recipe as
# cust_tools/demo_terrain_corners.py -- factored here so it's
# shared between the offline demo and the runtime physics.
# ---------------------------------------------------------------------------
def fit_plane(pts):
    """Least-squares plane fit through 3+ points in R^3.
    Returns `(normal, centroid)` with normal forced to +Y so
    pitch/roll signs are intuitive."""
    pts  = np.asarray(pts, dtype=np.float64)
    cen  = pts.mean(axis=0)
    cov  = np.cov((pts - cen).T)
    w, v = np.linalg.eigh(cov)
    n    = v[:, 0]
    if n[1] < 0:
        n = -n
    return n / np.linalg.norm(n), cen


def normal_to_pitch_roll(n):
    """Plane normal -> (pitch_deg, roll_deg).

    Pitch = rotation around X (forward/back lean).  +ve = nose-up.
    Roll  = rotation around Z (side-to-side).         +ve = right-side-up.
    """
    nx, ny, nz = float(n[0]), float(n[1]), float(n[2])
    pitch_rad  = math.atan2(-nz, ny)
    roll_rad   = math.atan2(nx, ny)
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
        """Convenience factory pre-loaded with T110E4 wheel data."""
        d = T110E4_WHEELS
        return cls(d['wheels_left'], d['wheels_right'],
                   radius=d['radius'],
                   min_offset=d['min_offset'],
                   max_offset=d['max_offset'])

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
