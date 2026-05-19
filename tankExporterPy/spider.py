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

    # ---- jump animation tunables (per Coffee 2026-05-19
    # "can we make it do something?  jump maybe?") --------
    JUMP_CYCLE_SEC      = 3.0   # total jump+rest period
    JUMP_CROUCH_FRAC    = 0.20  # 0..frac : crouch
    JUMP_LAUNCH_FRAC    = 0.06  # frac..+launch : push off
    JUMP_AIR_FRAC       = 0.34  # +air : ballistic up-and-down
    JUMP_LAND_FRAC      = 0.08  # +land : settle
    JUMP_REST_FRAC      = 0.32  # +rest : standing pause (= remainder)
    JUMP_CROUCH_DROP    = 0.25  # metres body drops below stand height
    JUMP_PEAK_HEIGHT    = 1.50  # metres body apex above stand height
    JUMP_KNEE_FLEX      = 0.40  # extra knee-height multiplier on crouch

    def __init__(self, world_pos=(0.0, 0.0, 0.0)):
        self.world_pos = np.asarray(world_pos, dtype=np.float32)
        # Body orientation -- only yaw matters for now; identity to start.
        self.yaw_rad   = 0.0
        # Animation time accumulator and the resting (= ground-stand)
        # body Y; the jump animation modulates self.world_pos[1] around
        # this baseline so the spider returns to the same spot each
        # cycle.  `_rest_y` is filled in by `set_terrain_height`.
        self._t       = 0.0
        self._rest_y  = float(world_pos[1])
        # Live joint positions -- start at the static T-pose and get
        # rewritten by `step()` each frame.  Legs are populated below.
        self._legs_rest = []
        self._legs      = []

        # Build body sphere VAO.
        bv, bc, bi = _make_sphere(
            radius=self.BODY_RADIUS,
            color=(0.04, 0.04, 0.05))
        self._body_vao = _upload_pos_color_vao(bv, bc, bi)

        # Build a single cylinder VAO (length 1.0 along +Y) -- reused
        # for all leg segments at different scales / orientations.
        cv, cc, ci = _make_cylinder(
            radius=self.LEG_BASE_RADIUS,
            length=1.0,
            color=(0.02, 0.02, 0.02))
        self._leg_vao = _upload_pos_color_vao(cv, cc, ci)

        # Per-leg joint positions in spider-local coords.  Each leg
        # has 4 points (hip, knee, mid, foot) -- 3 segments between
        # consecutive points.  Hips cluster near the NORTH POLE
        # of the body sphere (Quest design: legs come out the
        # TOP, not the equator).  In sphere coords with the pole
        # at +Y, a hip at angle phi off the pole sits at:
        #     y = R cos(phi)
        #     out_radius = R sin(phi)   (radial in XZ plane)
        phi = math.radians(self.HIP_POLE_OFFSET_DEG)
        hip_y   = self.BODY_RADIUS * math.cos(phi)
        hip_xz  = self.BODY_RADIUS * math.sin(phi)
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

    def step(self, dt):
        """Advance jump animation by `dt` seconds.

        Cycle phases (fraction of `JUMP_CYCLE_SEC`):
            crouch  -- body lowers, knees flex outward
            launch  -- body shoots up, knees extend
            air     -- body follows a ballistic arc apex
            land    -- body settles back to rest height
            rest    -- standing pose until next cycle
        """
        try:
            self._t += float(dt)
        except (TypeError, ValueError):
            return
        cycle = max(float(self.JUMP_CYCLE_SEC), 1e-3)
        phase = (self._t % cycle) / cycle      # 0..1
        f_cr  = self.JUMP_CROUCH_FRAC
        f_la  = f_cr + self.JUMP_LAUNCH_FRAC
        f_ai  = f_la + self.JUMP_AIR_FRAC
        f_ln  = f_ai + self.JUMP_LAND_FRAC
        # `f_rest` is implicit -- whatever's left.

        if phase < f_cr:
            # Crouch: ease-down toward -JUMP_CROUCH_DROP.
            p = phase / f_cr
            y_off = -self.JUMP_CROUCH_DROP * (
                0.5 - 0.5 * math.cos(p * math.pi))
            knee_scale = 1.0 + self.JUMP_KNEE_FLEX * p
        elif phase < f_la:
            # Launch: rapid rise from crouch to apex-bound start.
            p = (phase - f_cr) / max(
                f_la - f_cr, 1e-6)
            y_off = (-self.JUMP_CROUCH_DROP
                     + (self.JUMP_CROUCH_DROP
                        + 0.4 * self.JUMP_PEAK_HEIGHT) * p)
            knee_scale = 1.0 + self.JUMP_KNEE_FLEX * (1.0 - p)
        elif phase < f_ai:
            # Ballistic arc: 0.4 * peak -> peak -> 0 over this span.
            p = (phase - f_la) / max(
                f_ai - f_la, 1e-6)
            # Parabola y_off(p) with y(0)=0.4*peak, y(0.5)=peak, y(1)=0.
            # Fit: y = peak * (1 - (2p - 0.x)^2) ... simpler:
            #   y = peak * (1 - 4*(p - 0.5)^2)    apex at p=0.5
            # Then offset so y(0) lines up with launch end.
            y_off = self.JUMP_PEAK_HEIGHT * (
                1.0 - 4.0 * (p - 0.5) * (p - 0.5))
            knee_scale = 1.0          # legs hang relaxed midair
        elif phase < f_ln:
            # Landing: dip slightly past rest, then settle.
            p = (phase - f_ai) / max(
                f_ln - f_ai, 1e-6)
            y_off = -0.5 * self.JUMP_CROUCH_DROP * math.sin(
                p * math.pi)
            knee_scale = 1.0 + 0.5 * self.JUMP_KNEE_FLEX * math.sin(
                p * math.pi)
        else:
            # Rest: standing.
            y_off = 0.0
            knee_scale = 1.0

        self.world_pos[1] = self._rest_y + float(y_off)
        # Apply knee flex by adjusting each leg's knee point
        # vertically.  Mid + foot stay at rest positions so the
        # knee bends outward (= the leg looks like it springs).
        for i, (hip_r, knee_r, mid_r, foot_r) in enumerate(
                self._legs_rest):
            hip_now  = hip_r
            knee_now = knee_r.copy()
            knee_now[1] = knee_r[1] * float(knee_scale)
            mid_now  = mid_r
            foot_now = foot_r
            self._legs[i] = (hip_now, knee_now, mid_now, foot_now)

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
