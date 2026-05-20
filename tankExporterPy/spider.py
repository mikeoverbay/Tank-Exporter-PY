"""Quest-spider target dummy.

Procedurally builds a 4-leg insectoid silhouette modelled after the
Jonny Quest "Robot Spider": one dark sphere body + four spindly
3-segment legs that go OUT-UP-OVER then DOWN to feet on the terrain.

Static spawn for now -- no IK, no gait, no AI.  Sits where it was
placed.  Later: hook leg-IK + wave gait + body-Y plane fit.

Render uses `SimpleColorShader` (per-vertex colour) so we don't
need PBR textures or skinning -- one VAO per segment, drawn with
the per-segment model matrix.
"""

from __future__ import annotations

import ctypes
import math

import numpy as np
from OpenGL.GL import (
    glGenVertexArrays, glBindVertexArray, glGenBuffers, glBindBuffer,
    glBufferData, glVertexAttribPointer, glEnableVertexAttribArray,
    glDrawElements, glDeleteBuffers, glDeleteVertexArrays,
    GL_ARRAY_BUFFER, GL_ELEMENT_ARRAY_BUFFER, GL_FLOAT, GL_FALSE,
    GL_STATIC_DRAW, GL_TRIANGLES, GL_UNSIGNED_INT,
)


# ---- mesh builders ----------------------------------------------------------

def _make_sphere(radius=0.5, stacks=12, slices=18, color=(0.05, 0.05, 0.05)):
    """UV sphere -- positions + colours + index buffer.

    Returns (verts (N,3) f32, colors (N,3) f32, indices (M,) u32).
    """
    verts  = []
    colors = []
    for st in range(stacks + 1):
        phi = math.pi * st / stacks                  # 0..pi
        y   = radius * math.cos(phi)
        r   = radius * math.sin(phi)
        for sl in range(slices + 1):
            th = 2.0 * math.pi * sl / slices         # 0..2pi
            x  = r * math.cos(th)
            z  = r * math.sin(th)
            verts.append((x, y, z))
            colors.append(color)
    indices = []
    for st in range(stacks):
        for sl in range(slices):
            a = st * (slices + 1) + sl
            b = a + slices + 1
            indices += [a, b, a + 1, b, b + 1, a + 1]
    return (
        np.asarray(verts,  dtype=np.float32),
        np.asarray(colors, dtype=np.float32),
        np.asarray(indices, dtype=np.uint32),
    )


def _make_cylinder(radius=0.04, length=1.0, slices=12,
                    color=(0.02, 0.02, 0.02)):
    """Vertical cylinder along +Y from origin to (0, length, 0).

    Returns (verts (N,3) f32, colors (N,3) f32, indices (M,) u32).
    """
    verts  = []
    colors = []
    for end in (0.0, length):
        for sl in range(slices + 1):
            th = 2.0 * math.pi * sl / slices
            x  = radius * math.cos(th)
            z  = radius * math.sin(th)
            verts.append((x, end, z))
            colors.append(color)
    indices = []
    for sl in range(slices):
        a = sl
        b = sl + slices + 1
        indices += [a, b, a + 1, b, b + 1, a + 1]
    # End caps -- single fan around centre.  Slightly inset on
    # colours so the cap face shows up at a glance.
    cap_color = tuple(min(1.0, c + 0.05) for c in color)
    centre_bottom = len(verts)
    verts.append((0.0, 0.0, 0.0))
    colors.append(cap_color)
    centre_top    = len(verts)
    verts.append((0.0, length, 0.0))
    colors.append(cap_color)
    for sl in range(slices):
        indices += [centre_bottom, sl + 1, sl]
        top_base = slices + 1
        indices += [centre_top, top_base + sl, top_base + sl + 1]
    return (
        np.asarray(verts,  dtype=np.float32),
        np.asarray(colors, dtype=np.float32),
        np.asarray(indices, dtype=np.uint32),
    )


# ---- VAO helpers -----------------------------------------------------------

def _upload_pos_color_vao(verts, colors, indices):
    vao = glGenVertexArrays(1)
    glBindVertexArray(vao)
    vbo_pos = glGenBuffers(1)
    glBindBuffer(GL_ARRAY_BUFFER, vbo_pos)
    glBufferData(GL_ARRAY_BUFFER, verts.nbytes, verts, GL_STATIC_DRAW)
    glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 12, ctypes.c_void_p(0))
    glEnableVertexAttribArray(0)
    vbo_col = glGenBuffers(1)
    glBindBuffer(GL_ARRAY_BUFFER, vbo_col)
    glBufferData(GL_ARRAY_BUFFER, colors.nbytes, colors, GL_STATIC_DRAW)
    glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 12, ctypes.c_void_p(0))
    glEnableVertexAttribArray(1)
    ebo = glGenBuffers(1)
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, ebo)
    glBufferData(GL_ELEMENT_ARRAY_BUFFER, indices.nbytes, indices,
                 GL_STATIC_DRAW)
    glBindVertexArray(0)
    return vao, vbo_pos, vbo_col, ebo, len(indices)


# ---- math helpers ----------------------------------------------------------

def _translate(t):
    m = np.eye(4, dtype=np.float32)
    m[0, 3] = t[0]; m[1, 3] = t[1]; m[2, 3] = t[2]
    return m


def _rotate_axis_angle(axis, angle):
    a = np.asarray(axis, dtype=np.float64)
    a /= max(np.linalg.norm(a), 1e-9)
    c = math.cos(angle); s = math.sin(angle); t = 1.0 - c
    x, y, z = a
    return np.asarray([
        [t*x*x + c,    t*x*y - s*z,  t*x*z + s*y, 0.0],
        [t*x*y + s*z,  t*y*y + c,    t*y*z - s*x, 0.0],
        [t*x*z - s*y,  t*y*z + s*x,  t*z*z + c,   0.0],
        [0.0,          0.0,          0.0,         1.0],
    ], dtype=np.float32)


def _align_y_to_dir(direction):
    """Build a rotation that maps +Y to `direction`.  4x4 row-major."""
    d = np.asarray(direction, dtype=np.float64)
    n = np.linalg.norm(d)
    if n < 1e-9:
        return np.eye(4, dtype=np.float32)
    d /= n
    y = np.array([0.0, 1.0, 0.0])
    dot = float(np.dot(y, d))
    if dot > 0.99999:
        return np.eye(4, dtype=np.float32)
    if dot < -0.99999:
        return _rotate_axis_angle((1.0, 0.0, 0.0), math.pi)
    axis = np.cross(y, d)
    angle = math.acos(dot)
    return _rotate_axis_angle(axis, angle)


# ---- Spider ----------------------------------------------------------------

class Spider:
    """Static 4-leg Quest-style target.  One body sphere + 4 legs.

    Each leg has 3 segments: hip → upper-joint → lower-joint → foot.
    Joints are placed in a fixed `T-pose` -- legs angle OUT-UP first,
    then DOWN.  No animation yet; future work hooks IK + gait.

    Coordinate frame: spider-local, body centre at origin.  +Y is up.
    The viewer composes `world = spider.world_matrix @ spider_local`
    so the body lands at the spawn position with feet on terrain.
    """

    BODY_RADIUS         = 0.40
    LEG_BASE_RADIUS     = 0.04
    LEG_TIP_RADIUS      = 0.025
    # Hips clustered near the NORTH POLE of the body sphere
    # (Quest spider design: legs come out of the top).  Each
    # leg sits at angle HIP_POLE_OFFSET_DEG off the +Y pole
    # so the four anchors are almost-but-not-quite touching.
    HIP_POLE_OFFSET_DEG = 18.0
    LEG_OUT_DIST        = 0.8   # how far OUT the first joint sits
    LEG_KNEE_HEIGHT     = 1.6   # how HIGH above body the knee is
    LEG_MID_OUT         = 2.0   # mid-joint outward distance
    LEG_MID_HEIGHT      = -0.2  # mid joint slightly below body
    LEG_FOOT_OUT        = 2.2   # foot outward distance
    LEG_FOOT_HEIGHT     = -1.5  # foot below body
    LEG_SEG_LEN         = 1.5   # cylinder pre-length (scaled per segment)
    LEG_COUNT           = 4

    # ---- physics + pose tunables ----
    GRAVITY             = 9.81  # m/s^2 downward
    JUMP_PEAK_HEIGHT    = 1.50
    JUMP_CROUCH_DROP    = 0.25
    JUMP_CROUCH_SEC     = 0.45
    JUMP_LAND_SEC       = 0.30
    JUMP_REST_SEC       = 0.50  # standing pause between behaviours
    # Per Coffee 2026-05-19 ("hit by shot squat.  Sleep/waiting.
    # save the jump"): cycle through STAND, JUMP, SLEEP, HIT.
    # Poses are tuples (body_y_off, foot_xz_scale, foot_y_off):
    #   - body_y_off    : metres offset from rest standing height.
    #   - foot_xz_scale : multiplier on the leg's rest XZ outward
    #                     distance.  <1 pulls feet in (sleep), >1
    #                     splays them out (squat).
    #   - foot_y_off    : foot Y in spider-local; ignored when the
    #                     leg is `anchored` (foot Y locked to ground).
    # Per Coffee 2026-05-19 ("more spread on legs in jump and
    # sleep modes"): widen the foot XZ scale for STAND / CROUCH
    # and SLEEP so the silhouette reads wider in profile.  The
    # HIT squat stays at 1.5× (it was already the splayed pose).
    POSE_STAND  = (0.00, 1.30, -1.50)
    POSE_CROUCH = (-0.25, 1.30, -1.50)
    POSE_SLEEP  = (-0.60, 0.65, -0.30)
    POSE_SQUAT  = (-1.10, 1.50, -0.20)
    # Per Coffee 2026-05-19 ("you need bend in the mid... Id say
    # 150 degrees in side angle"): mid joint INTERIOR angle.
    # 150 deg = 30 deg deflection from straight (slight outward
    # kink), matching the Quest cartoon's spindly leg silhouette.
    MID_BEND_ANGLE_DEG = 150.0
    SLEEP_HOLD_SEC = 3.00
    SQUAT_HOLD_SEC = 1.50
    POSE_BLEND_SEC = 0.50
    HIT_IN_SEC     = 0.15   # rapid slam-down on hit
    # Per Coffee 2026-05-19 ("simulated pressing down on eye
    # (sphere) and releasing.  It should have some bounce"):
    # the body sphere is pushed down by a hand-like force, held
    # briefly, then released so it springs back with a damped
    # oscillation.  Tunables:
    PRESS_DEPTH    = 0.40   # m -- how far the press pushes the body down
    PRESS_IN_SEC   = 0.55   # slow ease-down (= hand pushing)
    PRESS_HOLD_SEC = 0.50   # squeeze held at the bottom
    # Spring/damper for the release.  ω = 2π/T_bounce; ζ = damping.
    PRESS_BOUNCE_T   = 0.45  # natural period (sec) of one full oscillation
    PRESS_BOUNCE_ZETA = 0.22 # damping ratio (0 = ideal spring, 1 = critically damped)
    PRESS_SETTLE_THRESH = 0.005  # |y| below this + slow → done

    def __init__(self, world_pos=(0.0, 0.0, 0.0), build_gl=True):
        self.world_pos = np.asarray(world_pos, dtype=np.float32)
        # Body orientation -- only yaw matters for now; identity to start.
        self.yaw_rad   = 0.0
        # When False, skip the VAO uploads.  Used by offline
        # diagnostic plots that import the geometry math without
        # an active OpenGL context.
        self._has_gl   = bool(build_gl)
        # Animation time accumulator + jump physics state.  The
        # spider obeys real gravity once it leaves the ground;
        # `_y_off` is the body's Y offset from the standing rest
        # height, `_vel_y` is the integrated vertical velocity,
        # and `_mode` selects which phase of the jump cycle is
        # active.  `_rest_y` is the world Y the spider settles
        # to (set by `set_terrain_height`).
        self._t          = 0.0
        self._rest_y     = float(world_pos[1])
        self._y_off      = 0.0
        self._vel_y      = 0.0
        self._mode       = 'stand'
        self._mode_start = 0.0
        # Pose blending state: source/target pose tuples between
        # which to interpolate inside the *_in / *_out modes.
        self._pose_src   = self.POSE_STAND
        self._pose_tgt   = self.POSE_STAND
        # Counter so the behaviour cycle alternates jump→sleep→
        # jump→hit→jump→sleep→...
        self._behaviour_idx = 0
        # Live joint positions -- start at the static T-pose and get
        # rewritten by `step()` each frame.  Legs are populated below.
        self._legs_rest = []
        self._legs      = []

        # Build body sphere VAO + reusable leg cylinder VAO.  Only
        # if we actually have a GL context (offline plots skip
        # these and call the IK math directly).
        if self._has_gl:
            bv, bc, bi = _make_sphere(
                radius=self.BODY_RADIUS,
                color=(0.04, 0.04, 0.05))
            self._body_vao = _upload_pos_color_vao(bv, bc, bi)
            cv, cc, ci = _make_cylinder(
                radius=self.LEG_BASE_RADIUS,
                length=1.0,
                color=(0.02, 0.02, 0.02))
            self._leg_vao = _upload_pos_color_vao(cv, cc, ci)
        else:
            self._body_vao = None
            self._leg_vao  = None

        # Per-leg joint positions in spider-local coords.  Each leg
        # has 4 points (hip, knee, mid, foot) -- 3 segments between
        # consecutive points.  Hips cluster near the NORTH POLE
        # of the body sphere (Quest design: legs come out the
        # TOP, not the equator).  In sphere coords with the pole
        # at +Y, a hip at angle phi off the pole sits at:
        #     y = R cos(phi)
        #     out_radius = R sin(phi)   (radial in XZ plane)
        # Per Coffee 2026-05-19 ("spider legs don't stretch.
        # spring in the joints"): segments are RIGID.  Store
        # per-leg segment lengths and a bend-plane outward
        # direction once at init; `step()` recomputes knee and
        # mid via 2-link IK each frame so the joints flex but
        # the cylinder lengths stay constant.
        phi = math.radians(self.HIP_POLE_OFFSET_DEG)
        hip_y   = self.BODY_RADIUS * math.cos(phi)
        hip_xz  = self.BODY_RADIUS * math.sin(phi)
        self._leg_seg_lens = []   # (L1, L2, L3) per leg
        self._leg_out_dir  = []   # unit XZ outward direction per leg
        for i in range(self.LEG_COUNT):
            theta = 2.0 * math.pi * i / self.LEG_COUNT + math.pi / 4.0
            cx = math.cos(theta)
            cz = math.sin(theta)
            hip  = np.asarray([
                hip_xz * cx,
                hip_y,
                hip_xz * cz,
            ], dtype=np.float32)
            knee = np.asarray([
                self.LEG_OUT_DIST * cx,
                self.LEG_KNEE_HEIGHT,
                self.LEG_OUT_DIST * cz,
            ], dtype=np.float32)
            mid  = np.asarray([
                self.LEG_MID_OUT * cx,
                self.LEG_MID_HEIGHT,
                self.LEG_MID_OUT * cz,
            ], dtype=np.float32)
            foot = np.asarray([
                self.LEG_FOOT_OUT * cx,
                self.LEG_FOOT_HEIGHT,
                self.LEG_FOOT_OUT * cz,
            ], dtype=np.float32)
            self._legs_rest.append((hip, knee, mid, foot))
            self._legs.append((hip.copy(), knee.copy(),
                                mid.copy(), foot.copy()))
            # Segment lengths and the per-leg outward XZ
            # direction (+ stays the same regardless of pose --
            # serves as the bend-plane reference so IK keeps the
            # knee sticking OUT from the body, not collapsed
            # inward).
            L1 = float(np.linalg.norm(knee - hip))
            L2 = float(np.linalg.norm(mid  - knee))
            L3 = float(np.linalg.norm(foot - mid))
            self._leg_seg_lens.append((L1, L2, L3))
            self._leg_out_dir.append(
                np.asarray([cx, 0.0, cz], dtype=np.float32))

    # ---- accessors ---------------------------------------------------------

    def world_matrix(self):
        """Compose translate + yaw for the spider as a whole."""
        s = math.sin(self.yaw_rad); c = math.cos(self.yaw_rad)
        m = np.eye(4, dtype=np.float32)
        m[0, 0] =  c; m[0, 2] =  s
        m[2, 0] = -s; m[2, 2] =  c
        m[0, 3] = self.world_pos[0]
        m[1, 3] = self.world_pos[1]
        m[2, 3] = self.world_pos[2]
        return m

    def set_terrain_height(self, terrain):
        """Drop the body so the lowest foot just touches the terrain
        directly under it.  Call once after creation (or whenever the
        spider moves to a new spawn point)."""
        if terrain is None:
            return
        if not hasattr(terrain, 'sample_height'):
            return
        # Each leg's foot in WORLD space (no Y translation yet).
        body = self.world_matrix()
        ground_y = -1e9
        for hip, knee, mid, foot in self._legs:
            f_local = np.array([foot[0], foot[1], foot[2], 1.0],
                               dtype=np.float32)
            f_world = body @ f_local
            ty = float(terrain.sample_height(float(f_world[0]),
                                              float(f_world[2])))
            # We want foot world Y to land at ty.  Body Y currently is
            # world_pos[1]; foot world Y = body_y + foot[1].  So lift
            # body by (ty - foot_world_y) for the WORST (highest)
            # foot.
            need = ty - float(f_world[1])
            if need > ground_y:
                ground_y = need
        if ground_y > -1e8:
            self.world_pos[1] += ground_y
        # Cache as the resting Y so jump animation knows where
        # "standing" is.
        self._rest_y = float(self.world_pos[1])

    # ---- animation ---------------------------------------------------------

    def _enter_mode(self, mode):
        self._mode       = mode
        self._mode_start = self._t

    @staticmethod
    def _lerp(a, b, t):
        return a + (b - a) * t

    @staticmethod
    def _ease(p):
        # Smoothstep: 3p² − 2p³.  Eases in/out around the midpoint
        # so pose transitions look spring-loaded rather than
        # linear.
        p = max(0.0, min(p, 1.0))
        return p * p * (3.0 - 2.0 * p)

    def _blend_poses(self, src, tgt, p):
        e = self._ease(p)
        return (self._lerp(src[0], tgt[0], e),
                self._lerp(src[1], tgt[1], e),
                self._lerp(src[2], tgt[2], e))

    def step(self, dt):
        """Advance the jump state machine + physics by `dt`.

        Per Coffee 2026-05-19 ("it needs to get off the ground.
        it has mass at its body"): real gravity drives the
        airborne arc -- launch applies an upward impulse, AIR
        integrates `vel_y -= g*dt`, and the foot positions
        detach from the ground so the WHOLE spider rides up
        with the body.

        Mode sequence:
            rest    : standing on terrain for JUMP_REST_SEC
            crouch  : kinematic dip to -JUMP_CROUCH_DROP
                      over JUMP_CROUCH_SEC (legs flex via IK
                      with feet planted)
            air     : impulse applied (vel_y = sqrt(2 g h));
                      vel_y integrates against gravity; feet
                      DETACHED from terrain, ride with body
            land    : kinematic absorb back to rest over
                      JUMP_LAND_SEC (feet plant at rest world
                      again, legs absorb via IK)
            ... loop ...
        """
        try:
            self._t += float(dt)
        except (TypeError, ValueError):
            return
        elapsed = self._t - self._mode_start

        # Defaults overwritten by each mode below.
        body_y_off  = 0.0
        foot_xz     = 1.0
        foot_y      = self.LEG_FOOT_HEIGHT
        anchored    = True

        if self._mode == 'stand':
            body_y_off, foot_xz, foot_y = self.POSE_STAND
            anchored = True
            if elapsed >= self.JUMP_REST_SEC:
                # Behaviour rotation: jump → sleep → hit →
                # press → repeat.
                self._behaviour_idx += 1
                slot = self._behaviour_idx % 4
                if slot == 1:
                    self._pose_src = self.POSE_STAND
                    self._pose_tgt = self.POSE_SLEEP
                    self._enter_mode('sleep_in')
                elif slot == 2:
                    self._pose_src = self.POSE_STAND
                    self._pose_tgt = self.POSE_SQUAT
                    self._enter_mode('hit_in')
                elif slot == 3:
                    self._enter_mode('press_in')
                else:
                    self._enter_mode('crouch')

        elif self._mode == 'crouch':
            p = min(elapsed / max(self.JUMP_CROUCH_SEC, 1e-6),
                     1.0)
            body_y_off = -self.JUMP_CROUCH_DROP * (
                0.5 - 0.5 * math.cos(p * math.pi))
            foot_xz, foot_y = 1.0, self.LEG_FOOT_HEIGHT
            anchored = True
            if elapsed >= self.JUMP_CROUCH_SEC:
                self._vel_y = math.sqrt(
                    2.0 * self.GRAVITY * (
                        self.JUMP_PEAK_HEIGHT
                        + self.JUMP_CROUCH_DROP))
                self._y_off = body_y_off
                self._enter_mode('air')

        elif self._mode == 'air':
            self._vel_y -= self.GRAVITY * float(dt)
            self._y_off += self._vel_y * float(dt)
            body_y_off = self._y_off
            foot_xz, foot_y = 1.0, self.LEG_FOOT_HEIGHT
            anchored = False
            if self._y_off <= 0.0 and self._vel_y < 0.0:
                self._y_off = 0.0
                self._vel_y = 0.0
                self._enter_mode('land')

        elif self._mode == 'land':
            p = min(elapsed / max(self.JUMP_LAND_SEC, 1e-6),
                     1.0)
            body_y_off = -0.5 * self.JUMP_CROUCH_DROP * math.sin(
                p * math.pi)
            foot_xz, foot_y = 1.0, self.LEG_FOOT_HEIGHT
            anchored = True
            if elapsed >= self.JUMP_LAND_SEC:
                self._enter_mode('stand')

        elif self._mode == 'sleep_in':
            p = elapsed / max(self.POSE_BLEND_SEC, 1e-6)
            body_y_off, foot_xz, foot_y = self._blend_poses(
                self._pose_src, self._pose_tgt, p)
            anchored = False
            if p >= 1.0:
                self._enter_mode('sleep')

        elif self._mode == 'sleep':
            body_y_off, foot_xz, foot_y = self.POSE_SLEEP
            anchored = False
            if elapsed >= self.SLEEP_HOLD_SEC:
                self._pose_src = self.POSE_SLEEP
                self._pose_tgt = self.POSE_STAND
                self._enter_mode('sleep_out')

        elif self._mode == 'sleep_out':
            p = elapsed / max(self.POSE_BLEND_SEC, 1e-6)
            body_y_off, foot_xz, foot_y = self._blend_poses(
                self._pose_src, self._pose_tgt, p)
            anchored = False
            if p >= 1.0:
                self._enter_mode('stand')

        elif self._mode == 'hit_in':
            # Rapid slam-down: body crashes to the squat pose
            # in HIT_IN_SEC.  Feet splay outward and land on the
            # ground.  Anchored=False so the foot XZ can move
            # freely to the splayed position; once we land in
            # the squat the next mode pins them.
            p = elapsed / max(self.HIT_IN_SEC, 1e-6)
            body_y_off, foot_xz, foot_y = self._blend_poses(
                self._pose_src, self._pose_tgt, p)
            anchored = False
            if p >= 1.0:
                self._enter_mode('squat')

        elif self._mode == 'squat':
            body_y_off, foot_xz, foot_y = self.POSE_SQUAT
            anchored = True   # legs pinned to ground while pinned
            if elapsed >= self.SQUAT_HOLD_SEC:
                self._pose_src = self.POSE_SQUAT
                self._pose_tgt = self.POSE_STAND
                self._enter_mode('hit_out')

        elif self._mode == 'hit_out':
            p = elapsed / max(self.POSE_BLEND_SEC, 1e-6)
            body_y_off, foot_xz, foot_y = self._blend_poses(
                self._pose_src, self._pose_tgt, p)
            anchored = False
            if p >= 1.0:
                self._enter_mode('stand')

        elif self._mode == 'press_in':
            # Slow ease-down: body drops by PRESS_DEPTH over
            # PRESS_IN_SEC.  Feet anchored (= legs flex) so it
            # really looks like a hand pushing the eye down on
            # the planted spider.
            p = self._ease(elapsed / max(self.PRESS_IN_SEC, 1e-6))
            body_y_off = -self.PRESS_DEPTH * p
            foot_xz, foot_y = (
                self.POSE_STAND[1], self.POSE_STAND[2])
            anchored = True
            if elapsed >= self.PRESS_IN_SEC:
                # Seed the spring-damper state for the release.
                self._y_off = -self.PRESS_DEPTH
                self._vel_y = 0.0
                self._enter_mode('press_hold')

        elif self._mode == 'press_hold':
            body_y_off = -self.PRESS_DEPTH
            foot_xz, foot_y = (
                self.POSE_STAND[1], self.POSE_STAND[2])
            anchored = True
            if elapsed >= self.PRESS_HOLD_SEC:
                # Release.  Spring physics kicks in next frame.
                self._enter_mode('press_release')

        elif self._mode == 'press_release':
            # Damped harmonic oscillator about the rest height.
            # y'' = -ω² · y - 2ζω · y'
            T   = max(self.PRESS_BOUNCE_T, 1e-3)
            zeta = max(min(self.PRESS_BOUNCE_ZETA, 0.99), 0.0)
            omega = 2.0 * math.pi / T
            accel = (-omega * omega * self._y_off
                      - 2.0 * zeta * omega * self._vel_y)
            self._vel_y += accel * float(dt)
            self._y_off += self._vel_y * float(dt)
            body_y_off = self._y_off
            foot_xz, foot_y = (
                self.POSE_STAND[1], self.POSE_STAND[2])
            anchored = True
            # Settle: when y is small AND we're moving slow,
            # snap to rest and continue.
            if (abs(self._y_off) < self.PRESS_SETTLE_THRESH
                    and abs(self._vel_y) < 0.05
                    and elapsed > 0.3):
                self._y_off = 0.0
                self._vel_y = 0.0
                self._enter_mode('stand')

        else:
            self._enter_mode('stand')
            body_y_off, foot_xz, foot_y = self.POSE_STAND

        self.world_pos[1] = self._rest_y + float(body_y_off)
        self._update_leg_ik(float(body_y_off),
                              float(foot_xz),
                              float(foot_y),
                              bool(anchored))

    def _update_leg_ik(self, body_y_off, foot_xz_scale, foot_y_off,
                        anchored):
        """Re-solve each leg with rigid segments + pose targets.

        Args:
            body_y_off    : body's Y offset from rest.  Used to
                            adjust the spider-local foot Y when
                            `anchored=True` so the foot world Y
                            stays at the ground.
            foot_xz_scale : multiplier on the leg's rest XZ
                            outward distance.  <1 pulls feet in
                            (sleep), >1 splays them out (squat).
            foot_y_off    : foot Y in spider-local; used only when
                            `anchored=False`.
            anchored      : when True, foot world Y is locked to
                            the rest ground level
                            (`LEG_FOOT_HEIGHT - body_y_off` in
                            spider-local).  When False, foot Y =
                            `foot_y_off` directly.

        2-link IK with bone-A = L1, bone-B = L2+L3.  Mid sits on
        the knee->foot line at distance L2 from the knee, so
        segment 2 keeps its rest length and segment 3 absorbs
        the rest.
        """
        for i, (hip_r, knee_r, mid_r, foot_r) in enumerate(
                self._legs_rest):
            L1, L2, L3 = self._leg_seg_lens[i]
            Lreach = L1 + L2 + L3
            out_dir = self._leg_out_dir[i]   # unit XZ outward
            cx, _, cz = float(out_dir[0]), 0.0, float(out_dir[2])
            hip  = hip_r
            # Target foot in spider-local: XZ scaled, Y per pose
            # (or per anchor rule).
            target_xz = self.LEG_FOOT_OUT * foot_xz_scale
            if anchored:
                foot_y_local = self.LEG_FOOT_HEIGHT - body_y_off
            else:
                foot_y_local = foot_y_off
            foot = np.asarray([
                target_xz * cx,
                foot_y_local,
                target_xz * cz,
            ], dtype=np.float32)
            # Vector hip -> foot.
            delta   = foot - hip
            d_len   = float(np.linalg.norm(delta))
            if d_len < 1e-6:
                self._legs[i] = (hip, knee_r.copy(),
                                  mid_r.copy(), foot)
                continue
            # If we'd overstretch, lift the foot back along the
            # hip->foot ray to exactly Lreach.  This keeps the
            # leg straight + fully extended pointing in the rest
            # direction; the foot effectively rides up with the
            # body in midair.
            if d_len > Lreach:
                foot = hip + (Lreach / d_len) * delta
                delta = foot - hip
                d_len = Lreach
            # Per Coffee 2026-05-19 ("you need bend in the mid...
            # 150 degrees in side angle"): mid joint has a FIXED
            # interior bend of MID_BEND_ANGLE_DEG.  L2 and L3
            # form a closed triangle with the knee-foot virtual
            # segment via law of cosines:
            #     L_bend^2 = L2^2 + L3^2 - 2 L2 L3 cos(α)
            mid_alpha = math.radians(self.MID_BEND_ANGLE_DEG)
            L_bend = math.sqrt(max(
                L2 * L2 + L3 * L3
                - 2.0 * L2 * L3 * math.cos(mid_alpha),
                1e-12))
            # Per Coffee 2026-05-19 ("hips cant bend past 90'
            # straight up.  it folds oddly in sleep mode"):
            # minimum-reach clamp.  If the foot is closer to
            # the hip than |L1 - L_bend|, the 2-link IK has
            # no forward solution and collapses the knee BEHIND
            # the hip (= the weird fold).  Push the foot out
            # along the hip->foot ray to exactly |L1 - L_bend|
            # so the leg is fully folded but still bends in the
            # forward / upward half-space.
            d_min = abs(L1 - L_bend)
            if d_len < d_min:
                foot = hip + (d_min / max(d_len, 1e-6)) * delta
                delta = foot - hip
                d_len = d_min
            # 2-link IK: cosine rule for angle at hip between
            # bone-A (L1) and the hip-foot line.
            proj = (L1 * L1 + d_len * d_len - L_bend * L_bend) / (
                2.0 * d_len)
            proj = max(min(proj, L1), -L1)
            h_sq = L1 * L1 - proj * proj
            h    = math.sqrt(max(h_sq, 0.0))
            # Per Coffee 2026-05-19 ("hips cant bend past 90'
            # straight up"): the bend direction biases UPWARD,
            # not XZ-outward.  Take +Y projected perpendicular
            # to `along` so the knee always lifts above the
            # hip-foot line.  When the leg points nearly
            # straight up (along ~ +Y), fall back to the leg's
            # XZ outward direction so the knee picks a
            # consistent side.
            along = delta / d_len
            out   = self._leg_out_dir[i]
            up    = np.asarray([0.0, 1.0, 0.0], dtype=np.float32)
            perp  = up - float(np.dot(up, along)) * along
            pn    = float(np.linalg.norm(perp))
            if pn < 1e-3:
                # along is almost vertical; fall back to outward
                # XZ direction (which is automatically
                # perpendicular to +Y).
                perp = out
                pn = float(np.linalg.norm(perp))
                if pn < 1e-6:
                    perp = np.asarray([1.0, 0.0, 0.0],
                                       dtype=np.float32)
                else:
                    perp = perp / pn
            else:
                perp = perp / pn
            # Knee = hip + proj * along + h * perp_up.  The +h
            # bias along the upward-projected perpendicular
            # keeps the knee in the upper half-space of the
            # hip -- it never wraps behind / below the hip,
            # so the leg can't fold backward through the body.
            knee = (hip
                     + proj * along
                     + h * perp).astype(np.float32)
            # Per Coffee 2026-05-19 ("you need bend in the
            # mid"): mid is OFF the knee-foot line.  Inside the
            # (knee, mid, foot) triangle we know all three side
            # lengths (L2, L3, L_bend) so the cosine rule gives
            # the angle at the knee between the knee->mid and
            # the knee->foot directions:
            #   cos(θ_knee) = (L2^2 + L_bend^2 - L3^2)
            #                 / (2 * L2 * L_bend)
            # Place mid at L2 from the knee, rotated by θ_knee
            # off the knee-foot line toward `perp_out` -- the
            # same bend-plane direction used for the knee.
            kf      = foot - knee
            kf_len  = float(np.linalg.norm(kf))
            if kf_len > 1e-6:
                along_kf = kf / kf_len
                # Same upward-bias rule as the knee: bend the
                # mid in the SAME half-space as the knee bend
                # (= reuse `perp` from above when valid; else
                # +Y projection of along_kf).  This keeps mid
                # consistent with the knee so the leg doesn't
                # zig-zag.
                perp_kf  = perp - float(
                    np.dot(perp, along_kf)) * along_kf
                pn_kf    = float(np.linalg.norm(perp_kf))
                if pn_kf < 1e-6:
                    perp_kf = perp
                else:
                    perp_kf = perp_kf / pn_kf
                cos_kn = (L2 * L2 + L_bend * L_bend - L3 * L3
                            ) / (2.0 * L2 * L_bend)
                cos_kn = max(min(cos_kn, 1.0), -1.0)
                sin_kn = math.sqrt(max(
                    1.0 - cos_kn * cos_kn, 0.0))
                mid = (knee
                        + L2 * (cos_kn * along_kf
                                + sin_kn * perp_kf)
                        ).astype(np.float32)
            else:
                mid = knee.copy()
            self._legs[i] = (hip, knee, mid, foot)

    # ---- render ------------------------------------------------------------

    def render(self, color_shader, view, projection):
        body_world = self.world_matrix()
        color_shader.use()
        color_shader.set_mat4('view',       view)
        color_shader.set_mat4('projection', projection)

        # Body.
        color_shader.set_mat4('model', body_world)
        vao, *_ , icount = self._body_vao
        glBindVertexArray(vao)
        glDrawElements(GL_TRIANGLES, icount, GL_UNSIGNED_INT, None)
        glBindVertexArray(0)

        # Legs -- 3 segments per leg.
        leg_vao, *_, leg_icount = self._leg_vao
        glBindVertexArray(leg_vao)
        for hip, knee, mid, foot in self._legs:
            for a, b in ((hip, knee), (knee, mid), (mid, foot)):
                seg_vec    = b - a
                seg_len    = float(np.linalg.norm(seg_vec))
                if seg_len < 1e-6:
                    continue
                # Translate to `a`, rotate +Y to seg_vec, scale +Y by len.
                rot = _align_y_to_dir(seg_vec)
                scl = np.eye(4, dtype=np.float32)
                scl[1, 1] = seg_len
                model_local = _translate(a) @ rot @ scl
                color_shader.set_mat4(
                    'model',
                    (body_world @ model_local).astype(np.float32))
                glDrawElements(GL_TRIANGLES, leg_icount,
                               GL_UNSIGNED_INT, None)
        glBindVertexArray(0)

    # ---- cleanup -----------------------------------------------------------

    def cleanup(self):
        for (vao, vbo_pos, vbo_col, ebo, _icount) in (
                self._body_vao, self._leg_vao):
            if vbo_pos:
                glDeleteBuffers(1, [vbo_pos])
            if vbo_col:
                glDeleteBuffers(1, [vbo_col])
            if ebo:
                glDeleteBuffers(1, [ebo])
            if vao:
                glDeleteVertexArrays(1, [vao])
