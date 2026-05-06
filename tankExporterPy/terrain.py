"""Procedural ground for the viewer -- so the tank stops floating in space.

Generates a square heightmap via Perlin fractal Brownian motion
(fBm), builds an indexed triangle mesh with smooth-shaded normals,
and renders it through the bundled `shaders/terrain.{vert,frag}`
program.  Pure numpy + Pillow + PyOpenGL -- no external noise libs.

Why Perlin fBm and not diamond-square: diamond-square has visible
diagonal creases at quad boundaries (an artifact of its midpoint-
displacement structure) and is tied to a 2^n+1 grid.  fBm Perlin
has none of that, samples at any resolution, and is the algorithm
every modern terrain generator (Skyrim, Elite Dangerous's planet
engine, No Man's Sky, World Machine, Gaea, ...) is built on.

Public API
----------
    Terrain(seed=0, size=257, world_size=40.0, height_scale=3.0,
            octaves=5, persistence=0.5, lacunarity=2.0)
    .render(shader, view, proj, light_dir)  -- one draw call
    .cleanup()                              -- free the VAO + textures

The default 257-vertex grid (66049 verts, ~131K triangles) over 40
metres of world is plenty for the splash / tank-view scene without
chewing into per-frame budgets.  Bigger meshes would only make
sense once we wanted to drive around in there.

Generation runs once at construction (a couple hundred ms for the
default size) and the result is uploaded straight to a static VBO.
The shader does the lighting + height-banded colour blend on the
GPU at render time.
"""

import os
import struct

import numpy as np
from OpenGL.GL import *


# ---------------------------------------------------------------------------
# Perlin fBm noise (classic 1985 / improved 2002 gradient noise)
# ---------------------------------------------------------------------------

# Classic 8-direction gradient set.  Picked because the lower 3
# bits of a hash index into them directly without a lookup table.
# All vectors have unit-ish magnitude so the noise output stays in
# roughly [-1, 1] per octave.
_GRAD2D = np.array([
    ( 1.0,  0.0), (-1.0,  0.0), ( 0.0,  1.0), ( 0.0, -1.0),
    ( 0.7071,  0.7071), (-0.7071,  0.7071),
    ( 0.7071, -0.7071), (-0.7071, -0.7071),
], dtype=np.float32)


def _fade(t):
    """Ken Perlin's improved smoothstep -- 6t^5 - 15t^4 + 10t^3.

    Smoother than the classic 3t^2 - 2t^3 (continuous up to the
    second derivative), which kills the directional artefacts you
    get with the older recipe.
    """
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)


def _perlin2d_grid(perm, size, freq, offset=(0.0, 0.0)):
    """Sample one octave of 2-D Perlin noise on a `size x size` grid.

    Args:
        perm   (np.ndarray): 512-entry permutation table (uint8;
                             the standard 256-entry table tiled
                             twice for wrap-free index math).
        size   (int)        : grid edge length in samples
        freq   (float)      : samples-per-unit -- higher = finer
                              detail.  fBm sums many octaves at
                              progressively higher freqs.
        offset (tuple)      : world-space origin of the sample
                              region; lets the heightmap re-tile
                              in any direction without recompute.

    Returns:
        np.ndarray (size, size) float32, range roughly [-1, 1].
    """
    # Sample positions in noise space.  ij indexing keeps Y=row,
    # X=col which matches numpy's default array layout.
    xs = (np.arange(size, dtype=np.float32) * freq) + offset[0]
    ys = (np.arange(size, dtype=np.float32) * freq) + offset[1]
    X, Y = np.meshgrid(xs, ys, indexing='xy')

    # Cell-space integer corner + within-cell fractional offset.
    xi = np.floor(X).astype(np.int32) & 255
    yi = np.floor(Y).astype(np.int32) & 255
    xf = X - np.floor(X)
    yf = Y - np.floor(Y)

    # Hash the four lattice corners through the permutation table.
    aa = perm[(perm[xi    ] + yi    ) & 255]
    ba = perm[(perm[xi + 1] + yi    ) & 255]
    ab = perm[(perm[xi    ] + yi + 1) & 255]
    bb = perm[(perm[xi + 1] + yi + 1) & 255]

    # Each corner's gradient vector dotted with the offset from
    # that corner to the sample point.
    def _grad_dot(h, dx, dy):
        idx = h & 7
        gx  = _GRAD2D[idx, 0]
        gy  = _GRAD2D[idx, 1]
        return gx * dx + gy * dy

    n00 = _grad_dot(aa, xf,        yf)
    n10 = _grad_dot(ba, xf - 1.0,  yf)
    n01 = _grad_dot(ab, xf,        yf - 1.0)
    n11 = _grad_dot(bb, xf - 1.0,  yf - 1.0)

    # Smoothstep along each axis, then bilinear blend.
    u  = _fade(xf)
    v  = _fade(yf)
    nx0 = n00 + u * (n10 - n00)
    nx1 = n01 + u * (n11 - n01)
    return (nx0 + v * (nx1 - nx0)).astype(np.float32)


def _make_heightmap(seed, size, world_size, octaves, persistence,
                    lacunarity, height_scale, base_freq=1.0):
    """Generate a `size x size` heightmap via Perlin fBm.

    Sums `octaves` Perlin grids at progressively higher frequency
    and lower amplitude (`persistence^i`) -- the classic fractal
    Brownian motion recipe.  Output is normalised so the per-pixel
    range covers roughly the full [-height_scale/2, +height_scale/2]
    band.

    Returns:
        np.ndarray (size, size) float32  -- height in world metres.
    """
    rng  = np.random.default_rng(seed)
    perm = rng.permutation(256).astype(np.uint16)
    perm = np.concatenate([perm, perm])     # 512 entries -> wrap-free

    h     = np.zeros((size, size), dtype=np.float32)
    amp   = 1.0
    freq  = base_freq * (size / world_size) * 0.04
    norm  = 0.0
    for _ in range(octaves):
        h    += _perlin2d_grid(perm, size, freq) * amp
        norm += amp
        amp  *= persistence
        freq *= lacunarity
    if norm > 0.0:
        h /= norm

    # Roughly recentre at 0 and scale to the requested vertical
    # band.  We don't want a literal min/max stretch because that
    # over-amplifies single outlier peaks; multiplying by half the
    # height scale gives most of the surface a sensible range and
    # the rare peaks just stick out a bit further.
    return (h * 0.5 * height_scale).astype(np.float32)


# ---------------------------------------------------------------------------
# Mesh build helpers
# ---------------------------------------------------------------------------

def _build_grid_indices(size):
    """Build a triangle-list index array for a `size x size` grid.

    Two triangles per quad, (size-1) * (size-1) quads total.  Index
    layout per quad (looking down at +Y, X right, Z forward):
        i00 -- i10
         |   /|
         |  / |
         | /  |
        i01 -- i11
    Triangles: (i00, i01, i10) and (i10, i01, i11) -- CCW so the
    normal points up.
    """
    n_quads = (size - 1) * (size - 1)
    idx     = np.empty(n_quads * 6, dtype=np.uint32)
    out     = 0
    # Build via numpy operations so we don't pay a Python loop on
    # every quad.  i00 indices for every quad form their own grid.
    rows, cols = np.indices((size - 1, size - 1))
    i00 = (rows     * size + cols    ).astype(np.uint32).ravel()
    i10 = (rows     * size + cols + 1).astype(np.uint32).ravel()
    i01 = ((rows+1) * size + cols    ).astype(np.uint32).ravel()
    i11 = ((rows+1) * size + cols + 1).astype(np.uint32).ravel()
    # Two triangles per quad, CCW winding looking from +Y down.
    tri = np.stack([i00, i01, i10,  i10, i01, i11], axis=-1).ravel()
    return tri.astype(np.uint32, copy=False)


def _vertex_normals(positions, indices):
    """Compute smooth per-vertex normals via face-normal averaging.

    Args:
        positions (np.ndarray): (N, 3) float32 vertex positions
        indices   (np.ndarray): (3M,)  uint32 triangle indices

    Returns:
        np.ndarray (N, 3) float32 unit normals.
    """
    tris = indices.reshape(-1, 3)
    p0   = positions[tris[:, 0]]
    p1   = positions[tris[:, 1]]
    p2   = positions[tris[:, 2]]
    face_n = np.cross(p1 - p0, p2 - p0)
    # Don't normalise face normals -- weighting by triangle area
    # gives better averages on irregular tessellations.
    n = np.zeros_like(positions)
    for i in range(3):
        np.add.at(n, tris[:, i], face_n)
    # Now normalise the per-vertex sums.
    lens = np.linalg.norm(n, axis=1)
    lens[lens == 0.0] = 1.0
    return (n / lens[:, None]).astype(np.float32)


# ---------------------------------------------------------------------------
# Terrain class
# ---------------------------------------------------------------------------

class Terrain:
    """Procedural ground mesh + renderer.

    Generates the heightmap, mesh, and GL buffers in `__init__`;
    holds them static for the rest of the session.  `render` is one
    draw call.

    Args:
        seed         (int)   : Perlin permutation seed; same seed +
                               same parameters = identical ground.
        size         (int)   : vertex count per side (default 257).
        world_size   (float) : terrain extent in world metres
                               (centred on the origin).
        height_scale (float) : peak-to-trough vertical range.
        octaves      (int)   : fBm octave count.  More = more detail
                               at high frequencies, slower to gen.
        persistence  (float) : amplitude falloff per octave (0..1).
                               0.5 is the classic value -- lower
                               makes terrain rounder, higher rougher.
        lacunarity   (float) : frequency growth per octave.  2.0 is
                               canonical; rarely needs changing.
        base_y       (float) : Y offset applied to every vertex
                               after height generation.  Lets the
                               caller park the surface a little
                               below the tank's tracks so meshes
                               sit on it instead of through it.
    """

    def __init__(self, seed=0, size=257, world_size=40.0,
                 height_scale=3.0, octaves=5, persistence=0.5,
                 lacunarity=2.0, base_y=0.0):
        self.seed         = int(seed)
        self.size         = int(size)
        self.world_size   = float(world_size)
        self.height_scale = float(height_scale)
        self.base_y       = float(base_y)

        # ---- Heightmap generation ------------------------------------
        heights = _make_heightmap(
            seed=self.seed,
            size=self.size,
            world_size=self.world_size,
            octaves=int(octaves),
            persistence=float(persistence),
            lacunarity=float(lacunarity),
            height_scale=self.height_scale)
        self._heightmap = heights   # kept for debug / future use

        # ---- Vertex grid ---------------------------------------------
        # World coords: span [-world_size/2, +world_size/2] on X/Z;
        # Y comes from the heightmap.  Indexing 'xy' so the
        # heightmap's [row, col] aligns with [Z, X].
        half = self.world_size * 0.5
        xs   = np.linspace(-half, +half, self.size, dtype=np.float32)
        zs   = np.linspace(-half, +half, self.size, dtype=np.float32)
        X, Z = np.meshgrid(xs, zs, indexing='xy')
        Y    = heights + self.base_y

        positions = np.stack(
            [X.ravel(), Y.ravel(), Z.ravel()], axis=-1
        ).astype(np.float32)

        indices = _build_grid_indices(self.size)
        normals = _vertex_normals(positions, indices)

        self._n_indices = int(len(indices))
        self._min_y     = float(Y.min())
        self._max_y     = float(Y.max())

        # ---- Upload to GPU -------------------------------------------
        self.vao         = glGenVertexArrays(1)
        self._vbo_pos    = glGenBuffers(1)
        self._vbo_normal = glGenBuffers(1)
        self._ebo        = glGenBuffers(1)

        glBindVertexArray(self.vao)

        glBindBuffer(GL_ARRAY_BUFFER, self._vbo_pos)
        glBufferData(GL_ARRAY_BUFFER, positions.nbytes,
                     positions, GL_STATIC_DRAW)
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 0, None)
        glEnableVertexAttribArray(0)

        glBindBuffer(GL_ARRAY_BUFFER, self._vbo_normal)
        glBufferData(GL_ARRAY_BUFFER, normals.nbytes,
                     normals, GL_STATIC_DRAW)
        glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 0, None)
        glEnableVertexAttribArray(1)

        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, self._ebo)
        glBufferData(GL_ELEMENT_ARRAY_BUFFER, indices.nbytes,
                     indices, GL_STATIC_DRAW)

        glBindVertexArray(0)

    # ------------------------------------------------------------------
    @property
    def height_range(self):
        """(min_y, max_y) of the generated heightmap in world units."""
        return (self._min_y, self._max_y)

    # ------------------------------------------------------------------
    def render(self, shader, view, proj, light_dir):
        """One draw call.  Caller supplies the shader + view/proj
        matrices; we bind the VAO and dispatch the indexed draw.

        Args:
            shader   (TerrainShader): the bundled terrain program
            view     (np.ndarray)   : 4x4 view matrix
            proj     (np.ndarray)   : 4x4 projection matrix
            light_dir (np.ndarray)  : world-space directional-light
                                       direction (NOT normalised
                                       here; shader does that).
        """
        shader.use()
        shader.set_mat4('u_view',  view)
        shader.set_mat4('u_proj',  proj)
        shader.set_vec3('u_light_dir', light_dir)
        shader.set_float('u_height_min', self._min_y)
        shader.set_float('u_height_max', self._max_y)

        glBindVertexArray(self.vao)
        glDrawElements(GL_TRIANGLES, self._n_indices,
                       GL_UNSIGNED_INT, None)
        glBindVertexArray(0)

    # ------------------------------------------------------------------
    def cleanup(self):
        """Free the VAO / VBOs.  Idempotent."""
        if self.vao:
            try:
                glDeleteBuffers(3, [self._vbo_pos,
                                     self._vbo_normal,
                                     self._ebo])
                glDeleteVertexArrays(1, [self.vao])
            except Exception:
                pass
            self.vao = 0
            self._vbo_pos = self._vbo_normal = self._ebo = 0
