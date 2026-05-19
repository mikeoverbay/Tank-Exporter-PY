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
    LEG_HIP_HEIGHT      = 0.0   # hips at body equator
    LEG_OUT_DIST        = 0.8   # how far OUT the first joint sits
    LEG_KNEE_HEIGHT     = 1.6   # how HIGH above body the knee is
    LEG_MID_OUT         = 2.0   # mid-joint outward distance
    LEG_MID_HEIGHT      = -0.2  # mid joint slightly below body
    LEG_FOOT_OUT        = 2.2   # foot outward distance
    LEG_FOOT_HEIGHT     = -1.5  # foot below body
    LEG_SEG_LEN         = 1.5   # cylinder pre-length (scaled per segment)
    LEG_COUNT           = 4

    def __init__(self, world_pos=(0.0, 0.0, 0.0)):
        self.world_pos = np.asarray(world_pos, dtype=np.float32)
        # Body orientation -- only yaw matters for now; identity to start.
        self.yaw_rad   = 0.0

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
        # consecutive points.
        self._legs = []
        for i in range(self.LEG_COUNT):
            theta = 2.0 * math.pi * i / self.LEG_COUNT + math.pi / 4.0
            cx = math.cos(theta)
            cz = math.sin(theta)
            hip  = np.asarray([
                self.BODY_RADIUS * cx,
                self.LEG_HIP_HEIGHT,
                self.BODY_RADIUS * cz,
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
            self._legs.append((hip, knee, mid, foot))

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
