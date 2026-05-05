"""
Scene helpers -- camera and simple OpenGL geometry for spatial reference.

Classes:
    Camera : trackball orbit camera; produces view/projection matrices.
             Methods: get_view_matrix(), get_projection_matrix(),
                      get_model_matrix(), fit_to_bounds(bbox_min, bbox_max).
    Grid   : flat XZ grid of lines; rendered with SimpleColorShader.
             Constructor: Grid(cell_size=0.25, grid_cells=50)
             Methods: render(color_shader, view, projection), cleanup().
    Axes   : XYZ axis lines (R=X, G=Y, B=Z).
             Constructor: Axes(scale=1.0)
             Methods: render(color_shader, view, projection), cleanup().
    Sphere : UV sphere for visualizing the light source.
             Constructor: Sphere(radius=0.2, sectors=32, stacks=16)
             Methods: render(color_shader, model_matrix, view, projection),
                      cleanup().
"""

import ctypes

import numpy as np
from OpenGL.GL import *
from OpenGL.GLU import gluPerspective, gluLookAt


# ============================================================================
# Camera
# ============================================================================

class Camera:
    """Simple trackball orbit camera.

    Public state (set directly by Viewer):
        distance  (float) : distance from center
        yaw       (float) : horizontal angle in degrees
        pitch     (float) : vertical angle in degrees (clamped ±89deg)
        center    (vec3)  : look-at target point
        fov       (float) : vertical field of view in degrees
        width/height (int): viewport dimensions (for aspect ratio)
    """

    def __init__(self):
        self.distance = 5.0
        # Default yaw 225 puts the camera at the -X / -Z front-left
        # quadrant -- 45 degrees to the right of pure front view --
        # so the user gets a 3/4 perspective on the tank instead of
        # a flat head-on shot.  Pitch +30 keeps the camera above the
        # ground plane (smoke spawning at rear-side exhaust hardpoints
        # would z-occlude under the hull from a negative pitch).  R-key
        # reset lands here too.
        self.yaw      = 225.0
        self.pitch    = 30.0
        self.center   = np.array([0.0, 0.0, 0.0], dtype=np.float32)
        self.fov      = 45.0
        self.width    = 1280
        self.height   = 720

    # ------------------------------------------------------------------
    def get_view_matrix(self):
        """Return view matrix via gluLookAt (numpy float32, row-major)."""
        rad_yaw   = np.radians(self.yaw)
        rad_pitch = np.radians(self.pitch)

        x = self.center[0] + self.distance * np.cos(rad_pitch) * np.sin(rad_yaw)
        y = self.center[1] + self.distance * np.sin(rad_pitch)
        z = self.center[2] + self.distance * np.cos(rad_pitch) * np.cos(rad_yaw)

        glMatrixMode(GL_MODELVIEW)
        glLoadIdentity()
        gluLookAt(x, y, z,
                  self.center[0], self.center[1], self.center[2],
                  0.0, 1.0, 0.0)
        # GL returns column-major; .T gives row-major for GL_TRUE in set_mat4
        return np.array(glGetFloatv(GL_MODELVIEW_MATRIX), dtype=np.float32).T

    def get_projection_matrix(self):
        """Return projection matrix via gluPerspective (numpy float32, row-major)."""
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        gluPerspective(self.fov, self.width / max(1, self.height), 0.1, 500.0)
        m = np.array(glGetFloatv(GL_PROJECTION_MATRIX), dtype=np.float32).T
        glMatrixMode(GL_MODELVIEW)
        return m

    def get_model_matrix(self):
        """Return identity matrix (model stays at origin)."""
        return np.eye(4, dtype=np.float32)

    # ------------------------------------------------------------------
    def fit_to_bounds(self, bbox_min, bbox_max):
        """Auto-position camera so the whole mesh is visible."""
        center      = (bbox_min + bbox_max) / 2.0
        self.center = center.astype(np.float32)

        extents       = (bbox_max - bbox_min) / 2.0
        radius        = np.linalg.norm(extents)
        fov_rad       = np.radians(self.fov / 2.0)
        self.distance = (radius / np.sin(fov_rad)) * 1.2

        print(f"Camera fit: center={center}, distance={self.distance:.2f}, radius={radius:.2f}")


# ============================================================================
# Helpers - shared VAO construction
# ============================================================================

def _make_pos_color_vao(vertices, colors, indices):
    """Upload position+color geometry and return (vao, vbo_pos, vbo_col, ebo).

    Args:
        vertices (N,3 float32): vertex positions
        colors   (N,3 float32): per-vertex RGB
        indices  (M,  uint32) : element indices
    """
    vao = glGenVertexArrays(1)
    glBindVertexArray(vao)

    vbo_pos = glGenBuffers(1)
    glBindBuffer(GL_ARRAY_BUFFER, vbo_pos)
    glBufferData(GL_ARRAY_BUFFER, vertices.nbytes, vertices, GL_STATIC_DRAW)
    glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 12, ctypes.c_void_p(0))
    glEnableVertexAttribArray(0)

    vbo_col = glGenBuffers(1)
    glBindBuffer(GL_ARRAY_BUFFER, vbo_col)
    glBufferData(GL_ARRAY_BUFFER, colors.nbytes, colors, GL_STATIC_DRAW)
    glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 12, ctypes.c_void_p(0))
    glEnableVertexAttribArray(1)

    ebo = glGenBuffers(1)
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, ebo)
    glBufferData(GL_ELEMENT_ARRAY_BUFFER, indices.nbytes, indices, GL_STATIC_DRAW)

    glBindVertexArray(0)
    return vao, vbo_pos, vbo_col, ebo


def _cleanup_pos_color_vao(vao, vbo_pos, vbo_col, ebo):
    if vbo_pos: glDeleteBuffers(1, [vbo_pos])
    if vbo_col: glDeleteBuffers(1, [vbo_col])
    if ebo:     glDeleteBuffers(1, [ebo])
    if vao:     glDeleteVertexArrays(1, [vao])


# ============================================================================
# Grid
# ============================================================================

class Grid:
    """Flat XZ grid of lines for spatial reference.

    Args:
        cell_size  (float): size of each grid cell in world units
        grid_cells (int)  : number of cells along each axis
    """

    def __init__(self, cell_size=0.25, grid_cells=50):
        self.cell_size  = cell_size
        self.grid_cells = grid_cells
        half            = (cell_size * grid_cells) / 2.0

        verts   = []
        indices = []
        idx     = 0

        # Lines parallel to X axis
        for i in range(grid_cells + 1):
            z = -half + i * cell_size
            verts += [[-half, 0.0, z], [half, 0.0, z]]
            indices += [idx, idx + 1];  idx += 2

        # Lines parallel to Z axis
        for i in range(grid_cells + 1):
            x = -half + i * cell_size
            verts += [[x, 0.0, -half], [x, 0.0, half]]
            indices += [idx, idx + 1];  idx += 2

        verts   = np.array(verts,   dtype=np.float32)
        indices = np.array(indices, dtype=np.uint32)
        dark_yellow = np.array([0.8, 0.7, 0.0], dtype=np.float32)
        colors  = np.tile(dark_yellow, (len(verts), 1)).astype(np.float32)

        self.vao, self.vbo_pos, self.vbo_col, self.ebo = \
            _make_pos_color_vao(verts, colors, indices)
        self.index_count = len(indices)

    def render(self, color_shader, view, projection):
        """Draw grid lines.

        Args:
            color_shader (SimpleColorShader): per-vertex color shader
            view         (4x4 float32)      : view matrix
            projection   (4x4 float32)      : projection matrix
        """
        color_shader.use()
        color_shader.set_mat4('model',      np.eye(4, dtype=np.float32))
        color_shader.set_mat4('view',       view)
        color_shader.set_mat4('projection', projection)

        glBindVertexArray(self.vao)
        glLineWidth(1.0)
        glDrawElements(GL_LINES, self.index_count, GL_UNSIGNED_INT, None)
        glBindVertexArray(0)

    def cleanup(self):
        _cleanup_pos_color_vao(self.vao, self.vbo_pos, self.vbo_col, self.ebo)


# ============================================================================
# Axes
# ============================================================================

class Axes:
    """XYZ axis lines: X=red, Y=green, Z=blue.

    Args:
        scale (float): length of each axis in world units
    """

    def __init__(self, scale=1.0):
        verts = np.array([
            [0, 0, 0], [scale, 0, 0],   # X
            [0, 0, 0], [0, scale, 0],   # Y
            [0, 0, 0], [0, 0, scale],   # Z
        ], dtype=np.float32)

        colors = np.array([
            [1, 0, 0], [1, 0, 0],
            [0, 1, 0], [0, 1, 0],
            [0, 0, 1], [0, 0, 1],
        ], dtype=np.float32)

        indices = np.arange(6, dtype=np.uint32)

        self.vao, self.vbo_pos, self.vbo_col, self.ebo = \
            _make_pos_color_vao(verts, colors, indices)
        self.index_count = 6

    def render(self, color_shader, view, projection):
        """Draw axis lines.

        Args:
            color_shader (SimpleColorShader): per-vertex color shader
            view         (4x4 float32)      : view matrix
            projection   (4x4 float32)      : projection matrix
        """
        color_shader.use()
        color_shader.set_mat4('model',      np.eye(4, dtype=np.float32))
        color_shader.set_mat4('view',       view)
        color_shader.set_mat4('projection', projection)

        glDisable(GL_TEXTURE_2D)
        glBindVertexArray(self.vao)
        glLineWidth(3.0)
        glDrawElements(GL_LINES, self.index_count, GL_UNSIGNED_INT, None)
        glLineWidth(1.0)
        glBindVertexArray(0)

    def cleanup(self):
        _cleanup_pos_color_vao(self.vao, self.vbo_pos, self.vbo_col, self.ebo)


# ============================================================================
# LineBatch -- debug line segments (exhaust vectors, hardpoint normals, etc.)
# ============================================================================

class LineBatch:
    """Mutable batch of colored line segments rendered as GL_LINES.

    Use update(segments) to (re)upload a new set; call render() per frame.
    Each segment is ((x0,y0,z0), (x1,y1,z1), (r,g,b)) -- color is per
    segment (both endpoints get the same colour).
    """

    def __init__(self, line_width=2.0):
        self.line_width = float(line_width)
        self.vao         = None
        self.vbo_pos     = None
        self.vbo_col     = None
        self.ebo         = None
        self.index_count = 0

    # ------------------------------------------------------------------
    def update(self, segments):
        """Replace the buffer contents with the given list of segments.

        segments : iterable of ((sx,sy,sz), (ex,ey,ez), (r,g,b)).
        Empty list is fine -- subsequent render() draws nothing.
        """
        self.cleanup()
        if not segments:
            self.index_count = 0
            return

        verts  = []
        colors = []
        for (s, e, c) in segments:
            verts.append(s); verts.append(e)
            colors.append(c); colors.append(c)
        verts   = np.asarray(verts,   dtype=np.float32)
        colors  = np.asarray(colors,  dtype=np.float32)
        indices = np.arange(len(verts), dtype=np.uint32)

        self.vao, self.vbo_pos, self.vbo_col, self.ebo = \
            _make_pos_color_vao(verts, colors, indices)
        self.index_count = len(indices)

    # ------------------------------------------------------------------
    def render(self, color_shader, view, projection):
        if not self.index_count:
            return
        color_shader.use()
        color_shader.set_mat4('model',      np.eye(4, dtype=np.float32))
        color_shader.set_mat4('view',       view)
        color_shader.set_mat4('projection', projection)
        glBindVertexArray(self.vao)
        glLineWidth(self.line_width)
        glDrawElements(GL_LINES, self.index_count, GL_UNSIGNED_INT, None)
        glBindVertexArray(0)

    # ------------------------------------------------------------------
    def cleanup(self):
        if self.vao:
            _cleanup_pos_color_vao(self.vao, self.vbo_pos,
                                   self.vbo_col, self.ebo)
            self.vao = self.vbo_pos = self.vbo_col = self.ebo = None
            self.index_count = 0


# ============================================================================
# Sphere
# ============================================================================

class Sphere:
    """UV sphere for visualizing the orbiting light source.

    Rendered yellow with SimpleColorShader.

    Args:
        radius  (float): sphere radius
        sectors (int)  : longitudinal subdivisions
        stacks  (int)  : latitudinal subdivisions
    """

    def __init__(self, radius=0.2, sectors=32, stacks=16,
                 color=(1.0, 1.0, 0.0)):
        """Args:
            radius  (float)        : sphere radius (world units)
            sectors (int)          : longitudinal subdivisions
            stacks  (int)          : latitudinal subdivisions
            color   ((r, g, b))    : per-vertex colour, baked into the VBO.
                                     Default = light yellow (matches the
                                     existing 3-light markers).
        """
        verts   = []
        indices = []

        for i in range(stacks + 1):
            angle_v = np.pi / 2 - i * np.pi / stacks
            xy      = radius * np.cos(angle_v)
            z       = radius * np.sin(angle_v)
            for j in range(sectors + 1):
                angle_h = j * 2 * np.pi / sectors
                verts.append([xy * np.cos(angle_h), xy * np.sin(angle_h), z])

        for i in range(stacks):
            k1 = i * (sectors + 1)
            k2 = k1 + sectors + 1
            for j in range(sectors):
                if i != 0:
                    indices += [k1, k2, k1 + 1]
                if i != stacks - 1:
                    indices += [k1 + 1, k2, k2 + 1]
                k1 += 1;  k2 += 1

        verts   = np.array(verts,   dtype=np.float32)
        indices = np.array(indices, dtype=np.uint32)
        col_vec = np.array(color, dtype=np.float32)
        colors  = np.tile(col_vec, (len(verts), 1)).astype(np.float32)

        self.vao, self.vbo_pos, self.vbo_col, self.ebo = \
            _make_pos_color_vao(verts, colors, indices)
        self.index_count = len(indices)

    def render(self, color_shader, model_matrix, view, projection):
        """Draw the sphere at the given model transform.

        Args:
            color_shader (SimpleColorShader): per-vertex color shader
            model_matrix (4x4 float32)      : pre-built translation matrix
            view         (4x4 float32)      : view matrix
            projection   (4x4 float32)      : projection matrix
        """
        color_shader.use()
        color_shader.set_mat4('model',      model_matrix)
        color_shader.set_mat4('view',       view)
        color_shader.set_mat4('projection', projection)

        glBindVertexArray(self.vao)
        glDrawElements(GL_TRIANGLES, self.index_count, GL_UNSIGNED_INT, None)
        glBindVertexArray(0)

    def cleanup(self):
        _cleanup_pos_color_vao(self.vao, self.vbo_pos, self.vbo_col, self.ebo)
