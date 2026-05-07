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

    # Floor the heights at y=0 then scale to the requested vertical
    # band.  h is in roughly [-1, 1] before this step; subtracting
    # h.min() lands the lowest sample at 0, then 0.5x of height_scale
    # keeps most of the surface in a sensible range -- the rare peaks
    # reach near the full band.  Anchoring at 0 (vs. the previous
    # mean-centred convention) matches the image-loaded path so a
    # tank rendered on either source sits on the ground plane the
    # same way.
    h = h - h.min()
    return (h * 0.5 * height_scale).astype(np.float32)


# ---------------------------------------------------------------------------
# Sand texture loader  (anisotropic, mip-mapped, repeating)
# ---------------------------------------------------------------------------

# Anisotropic-filter extension constants.  These are part of core
# 4.6 but available since the late 2000s as EXT_texture_filter_-
# anisotropic on basically every desktop GPU.  Hard-coded so we
# don't depend on a specific PyOpenGL build exposing the symbol.
_GL_TEXTURE_MAX_ANISOTROPY     = 0x84FE
_GL_MAX_TEXTURE_MAX_ANISOTROPY = 0x84FF


def _load_terrain_diffuse(path):
    """Load a tileable diffuse texture for the terrain shader.

    Pipeline:
      1. Pillow open + RGB convert + Y-flip (OpenGL bottom-left
         origin convention -- matches loaders.TextureLoader).
      2. glTexImage2D upload, GL_RGB internal format.
      3. glGenerateMipmap so the trilinear chain is ready before
         the first frame draws -- avoids the GPU-driver-side JIT
         stall we used to see on first-tank-load.
      4. Trilinear filtering (GL_LINEAR_MIPMAP_LINEAR for min, plus
         GL_LINEAR for mag) so distant samples blur smoothly across
         mip levels instead of crawling.
      5. GL_REPEAT wrap on both axes for tiling.
      6. Anisotropic filtering at the GPU's max -- typically 16x.
         Turns mid-distance grazing-angle samples (the long stretch
         of terrain receding into the horizon) from a smudgy mush
         into clean texel rows.  Falls back to no-anisotropy if
         the extension isn't supported.

    Returns the GL texture name (uint), or 0 on failure.  Caller
    must `glDeleteTextures` when finished.
    """
    try:
        from PIL import Image
    except ImportError:
        print("[terrain] Pillow unavailable; sand texture disabled.")
        return 0

    if not os.path.isfile(path):
        return 0

    try:
        img = Image.open(path).convert('RGB')
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
        data = img.tobytes('raw', 'RGB')
        tex = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, tex)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB,
                     img.width, img.height, 0,
                     GL_RGB, GL_UNSIGNED_BYTE, data)
        glGenerateMipmap(GL_TEXTURE_2D)

        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER,
                        GL_LINEAR_MIPMAP_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER,
                        GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_REPEAT)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_REPEAT)

        # Probe the driver's max anisotropy and crank to it -- the
        # cost is negligible on modern hardware and the visual win
        # at grazing angles is huge for ground-plane texturing.
        try:
            max_aniso = glGetFloatv(_GL_MAX_TEXTURE_MAX_ANISOTROPY)
            try:
                max_aniso = float(max_aniso)
            except (TypeError, ValueError):
                max_aniso = 0.0
            if max_aniso >= 1.0:
                glTexParameterf(GL_TEXTURE_2D,
                                _GL_TEXTURE_MAX_ANISOTROPY,
                                float(max_aniso))
                print(f"[terrain] sand texture: aniso={max_aniso:.0f}x "
                      f"({img.width}x{img.height} mip-chained)")
        except Exception:
            # Anisotropic ext absent -- still works, just smudgier
            # at distance.
            pass

        glBindTexture(GL_TEXTURE_2D, 0)
        return int(tex)
    except Exception as exc:
        print(f"[terrain] sand texture load failed: {exc}")
        return 0


# ---------------------------------------------------------------------------
# Bilinear height sampler -- shared by Terrain.sample_height /
# sample_heights AND any standalone test code that builds a height
# grid without going through the GL-touching `Terrain.__init__`.
# ---------------------------------------------------------------------------

def bilinear_sample_height(heightmap, world_size, x, z, base_y=0.0):
    """Bilinear-sample a (size, size) heightmap at world (x, z).

    Args:
        heightmap  (np.ndarray): (size, size) float32 grid spanning
            world `[-world_size/2, +world_size/2]` on both X and Z
            (matches the Terrain class's `_heightmap` layout).  Row
            index = Z, column index = X.
        world_size (float): physical extent of the grid in metres.
        x, z       (float): world coords to sample at.
        base_y     (float): added to the interpolated value so the
            caller can pass a terrain `base_y` offset through.

    Returns:
        float: world Y at (x, z), or `base_y` if the point is
        outside the grid.
    """
    half = float(world_size) * 0.5
    if not (-half <= x <= half) or not (-half <= z <= half):
        return float(base_y)
    n  = heightmap.shape[0] - 1
    u  = (x + half) / float(world_size) * n   # column (X)
    v  = (z + half) / float(world_size) * n   # row    (Z)
    i0 = int(np.floor(v))
    j0 = int(np.floor(u))
    i1 = min(i0 + 1, n)
    j1 = min(j0 + 1, n)
    fy = float(v - i0)
    fx = float(u - j0)
    h00 = float(heightmap[i0, j0])
    h01 = float(heightmap[i0, j1])
    h10 = float(heightmap[i1, j0])
    h11 = float(heightmap[i1, j1])
    top = h00 * (1.0 - fx) + h01 * fx
    bot = h10 * (1.0 - fx) + h11 * fx
    return float(top * (1.0 - fy) + bot * fy + base_y)


def bilinear_sample_heights(heightmap, world_size, xs, zs, base_y=0.0):
    """Vectorised counterpart of `bilinear_sample_height`.

    `xs` / `zs` can be any matching shape; returns a numpy array
    of the same shape with float32 Y values, defaulting to
    `base_y` outside the grid.
    """
    xs   = np.asarray(xs, dtype=np.float32)
    zs   = np.asarray(zs, dtype=np.float32)
    out  = np.full_like(xs, float(base_y), dtype=np.float32)
    half = float(world_size) * 0.5
    in_b = ((xs >= -half) & (xs <= half)
            & (zs >= -half) & (zs <= half))
    if not np.any(in_b):
        return out
    n   = heightmap.shape[0] - 1
    ws  = float(world_size)
    u   = (xs[in_b] + half) / ws * n
    v   = (zs[in_b] + half) / ws * n
    i0  = np.floor(v).astype(np.int32)
    j0  = np.floor(u).astype(np.int32)
    i1  = np.minimum(i0 + 1, n)
    j1  = np.minimum(j0 + 1, n)
    fy  = (v - i0).astype(np.float32)
    fx  = (u - j0).astype(np.float32)
    h00 = heightmap[i0, j0]
    h01 = heightmap[i0, j1]
    h10 = heightmap[i1, j0]
    h11 = heightmap[i1, j1]
    top = h00 * (1.0 - fx) + h01 * fx
    bot = h10 * (1.0 - fx) + h11 * fx
    out[in_b] = top * (1.0 - fy) + bot * fy + float(base_y)
    return out


# ---------------------------------------------------------------------------
# Tiled detail-displacement sampler
# ---------------------------------------------------------------------------

def _detail_displacement(detail_path, world_size, mesh_size,
                         tile_meters, height_scale):
    """Sample a tileable grayscale heightmap to produce per-vertex
    displacement on a `mesh_size x mesh_size` terrain grid.

    The detail texture is treated as a `tile_meters` x `tile_meters`
    repeating pattern and sampled via world-(x,z) modulo tile_meters
    -- so a 50 m tile of detail repeats over and over across the
    160 m terrain, giving the same physical ripple wavelength
    regardless of the macro terrain extent.

    Args:
        detail_path  (str)   : on-disk grayscale image (PNG / JPG).
        world_size   (float) : extent of the macro terrain in metres
                                (the same value passed to Terrain).
        mesh_size    (int)   : vertex count per side on the macro
                                terrain grid.
        tile_meters  (float) : metres per repeat of the detail
                                tile.  Should match the colour
                                texture's tile size so geometry +
                                colour ripples line up.
        height_scale (float) : peak displacement amplitude in
                                metres.  Sand-ripple scale is
                                ~5-10 cm at most.

    Returns:
        np.ndarray (mesh_size, mesh_size) float32, displacement in
        world metres (always >= 0; min lifted to 0 so the detail
        only adds height, never digs negative).

    Returns a zero array if Pillow isn't available or the file
    can't be opened -- terrain still builds, just without detail.
    """
    try:
        from PIL import Image
    except ImportError:
        return np.zeros((mesh_size, mesh_size), dtype=np.float32)

    if not os.path.isfile(detail_path):
        return np.zeros((mesh_size, mesh_size), dtype=np.float32)

    try:
        img = Image.open(detail_path).convert('L')
    except Exception as exc:
        print(f"[terrain] detail heightmap load failed: {exc}")
        return np.zeros((mesh_size, mesh_size), dtype=np.float32)

    # Resample the detail map to a fixed working resolution.  Using
    # the source resolution directly is fine but can be huge (8K^2)
    # which is overkill for vertex-level displacement -- a 1024^2
    # working buffer captures every cycle of a 50-m ripple tile at
    # 5-cm fidelity, plenty for the mesh densities we're dealing
    # with.
    work_n = 1024
    img    = img.resize((work_n, work_n), resample=Image.LANCZOS)
    detail = np.asarray(img, dtype=np.float32) / 255.0

    # Vertex (world x, z) coords for the macro terrain.  Same
    # convention used in Terrain.__init__: span [-half, +half].
    half = float(world_size) * 0.5
    xs   = np.linspace(-half, +half, int(mesh_size), dtype=np.float32)
    zs   = np.linspace(-half, +half, int(mesh_size), dtype=np.float32)
    X, Z = np.meshgrid(xs, zs, indexing='xy')

    # Modulo into [0, tile_meters), then into [0, work_n) detail-
    # texture pixel space.  np.mod handles negatives cleanly so
    # the second-quadrant of the macro terrain wraps the same way.
    tm   = max(float(tile_meters), 1e-3)
    u    = (np.mod(X, tm) / tm) * float(work_n)
    v    = (np.mod(Z, tm) / tm) * float(work_n)
    # Bilinear sample.  Cheap, smooth enough for displacement.
    iu   = np.clip(u.astype(np.int32),     0, work_n - 1)
    iv   = np.clip(v.astype(np.int32),     0, work_n - 1)
    iu1  = np.clip(iu + 1,                 0, work_n - 1)
    iv1  = np.clip(iv + 1,                 0, work_n - 1)
    fu   = u - iu.astype(np.float32)
    fv   = v - iv.astype(np.float32)
    s00 = detail[iv,  iu ]
    s10 = detail[iv,  iu1]
    s01 = detail[iv1, iu ]
    s11 = detail[iv1, iu1]
    sampled = ((s00 * (1.0 - fu) + s10 * fu) * (1.0 - fv) +
               (s01 * (1.0 - fu) + s11 * fu) * fv)

    # Anchor at zero, scale to displacement amplitude.  Min-lift
    # rather than mean-centre so the detail only adds upward --
    # the macro heightmap already defines where ground is, and we
    # don't want the detail biasing the tank into a trough.
    sampled = sampled - float(sampled.min())
    return (sampled * float(height_scale)).astype(np.float32)


# ---------------------------------------------------------------------------
# Image-source heightmap loader
# ---------------------------------------------------------------------------

def _heightmap_from_image(path, size, height_scale,
                          smooth_sigma=1.0, edge_fade=0.10,
                          curve_gamma=1.0):
    """Load a grayscale image and convert it into a height grid suitable
    for the Terrain mesh.

    Args:
        path         (str)   : on-disk path to a PNG / JPG / TIFF /
                                whatever Pillow can open.  Expected to
                                be (roughly) square; non-square images
                                are letterboxed-resampled to `size`.
        size         (int)   : target grid edge length.  The Terrain
                                mesh always builds on a `size x size`
                                grid, so we resample the image to match
                                regardless of source resolution.
        height_scale (float) : peak-to-trough vertical range in world
                                metres (the same knob the procedural
                                Perlin generator takes; the image's
                                grayscale span maps onto this band).
        smooth_sigma (float) : Gaussian smoothing radius in pixels
                                applied AFTER resample.  Kills JPEG
                                blockiness and bilinear stair-stepping
                                so the lit terrain reads as smooth
                                rolling hills instead of pixel-stepped.
                                0.0 disables.  Default 1.0 is a gentle
                                touch -- raise toward 2.0 for low-res
                                source images.
        edge_fade    (float) : fraction of the grid (0..0.5) over which
                                the heights ramp down to 0 at the
                                world-edge boundary.  Prevents a
                                vertical cliff where the terrain meets
                                the world's edge.  0.0 disables; 0.10
                                gives a 10 %-of-side falloff which is
                                usually enough to read as a "hills end
                                here" cue without losing too much of
                                the source image.
        curve_gamma  (float) : power-curve remap on the normalised
                                heights before scaling to world units.
                                <1 lifts mid-tones (more "valley with a
                                few peaks"), >1 darkens mid-tones (more
                                "plateau with cliffs").  1.0 = linear,
                                which is usually what you want.

    Returns:
        np.ndarray (size, size) float32  -- heights in world metres,
        roughly centred near 0 with peak-to-trough close to
        `height_scale`.

    Resampling uses Pillow's LANCZOS for the highest-fidelity
    downsample.  Smoothing uses a separable Gaussian (numpy convolve
    twice) so we don't pull in scipy.

    Raises FileNotFoundError if `path` doesn't exist; ValueError if
    Pillow can't decode the file.
    """
    from PIL import Image     # local import: terrain module shouldn't
                               # cost Pillow load when caller only uses
                               # the procedural path.

    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"heightmap image not found: {path!r}")

    img = Image.open(path).convert('L')   # 'L' = 8-bit grayscale
    img = img.resize((int(size), int(size)),
                     resample=Image.LANCZOS)
    h = np.asarray(img, dtype=np.float32) / 255.0   # [0..1]

    # ---- Normalise the actual luminance span to fill [0..1].
    # Many heightmaps are exported with the brightest pixel well
    # below 255 (PNG dynamic range used "tastefully"); without this
    # rescale the mesh would only use a fraction of height_scale
    # even though the user authored full-range terrain.
    lo, hi = float(h.min()), float(h.max())
    if hi - lo > 1e-6:
        h = (h - lo) / (hi - lo)

    # ---- Power-curve remap: <1 favours valleys, >1 favours plateaux.
    if abs(curve_gamma - 1.0) > 1e-6:
        h = np.power(h, float(curve_gamma), dtype=np.float32)

    # ---- Gaussian smooth (separable) ----------------------------------
    if smooth_sigma > 0.0:
        # Build a 1-D Gaussian kernel covering +/- 3 sigma -- captures
        # 99.7 % of the curve, which is plenty for height-shading
        # smoothness purposes.
        radius = max(1, int(round(3.0 * smooth_sigma)))
        xs     = np.arange(-radius, radius + 1, dtype=np.float32)
        kern   = np.exp(-(xs * xs) / (2.0 * smooth_sigma * smooth_sigma))
        kern  /= kern.sum()
        # Two passes: rows then columns.  np.apply_along_axis is
        # convenient and plenty fast at 257-edge resolutions.
        h = np.apply_along_axis(
            lambda row: np.convolve(row, kern, mode='same'),
            axis=1, arr=h)
        h = np.apply_along_axis(
            lambda col: np.convolve(col, kern, mode='same'),
            axis=0, arr=h)

    # ---- Edge fade: ramp heights down to 0 within `edge_fade` of
    # the boundary so the mesh doesn't end in a vertical cliff at
    # the world's edge.  Computed via a separable Hann-style ramp
    # multiplied across rows and columns -- smooth, monotonic, hits
    # exactly 0 at the corner pixel.
    if edge_fade > 0.0:
        n     = int(size)
        edge  = max(1, int(round(edge_fade * n)))
        ramp1 = np.ones(n, dtype=np.float32)
        # Smooth half-cosine on [0, edge): 0 at index 0, 1 at index edge.
        i        = np.arange(edge, dtype=np.float32)
        ramp1[:edge]      = 0.5 - 0.5 * np.cos(np.pi * i / edge)
        ramp1[n - edge:]  = ramp1[:edge][::-1]
        # Outer product gives a 2-D mask peaking at the centre.
        mask  = np.outer(ramp1, ramp1)
        # Anchor point is the lowest bound of the source heights;
        # the fade interpolates everything down to that value rather
        # than to absolute 0 so a "valley" image doesn't suddenly
        # rise at the borders.
        anchor = float(h.min())
        h = anchor + (h - anchor) * mask

    # ---- Anchor at zero and scale to world units -----------------------
    # Floor the heights at y=0 (lowest pixel of the image lands flat
    # on the ground plane) and scale up to `height_scale`.  After the
    # earlier min-max normalise step h is already in [0..1], so the
    # multiply gives a clean [0..height_scale] band -- valleys at 0,
    # peaks at the top of the band -- which is what the user expects
    # when an image-defined terrain is read against an "up axis = Y"
    # convention.
    h = h - h.min()
    return (h * height_scale).astype(np.float32)


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
                 lacunarity=2.0, base_y=0.0,
                 image_path=None,
                 image_smooth_sigma=1.0,
                 image_edge_fade=0.10,
                 image_curve_gamma=1.0,
                 sand_path=None,
                 sand_tile_size=50.0,
                 detail_image_path=None,
                 detail_tile_size=50.0,
                 detail_height_scale=0.05):
        self.seed                = int(seed)
        self.size                = int(size)
        self.world_size          = float(world_size)
        self.height_scale        = float(height_scale)
        self.base_y              = float(base_y)
        self.image_path          = (str(image_path) if image_path else None)
        self.sand_path           = (str(sand_path)  if sand_path  else None)
        self.sand_tile_size      = float(sand_tile_size)
        self.detail_image_path   = (str(detail_image_path)
                                     if detail_image_path else None)
        self.detail_tile_size    = float(detail_tile_size)
        self.detail_height_scale = float(detail_height_scale)
        self._sand_tex           = 0  # GL texture name; 0 = no texture loaded

        # ---- Heightmap generation ------------------------------------
        # When `image_path` is given, load the image and convert it
        # to a height grid; otherwise fall through to the procedural
        # Perlin-fBm path.  Either way we end up with a (size, size)
        # float32 array `heights` in world units that the rest of
        # __init__ treats identically.
        if self.image_path:
            heights = _heightmap_from_image(
                path=self.image_path,
                size=self.size,
                height_scale=self.height_scale,
                smooth_sigma=float(image_smooth_sigma),
                edge_fade=float(image_edge_fade),
                curve_gamma=float(image_curve_gamma))
        else:
            heights = _make_heightmap(
                seed=self.seed,
                size=self.size,
                world_size=self.world_size,
                octaves=int(octaves),
                persistence=float(persistence),
                lacunarity=float(lacunarity),
                height_scale=self.height_scale)

        # Optional detail-displacement layer: a SECOND grayscale
        # heightmap tiled at `detail_tile_size` metres per repeat,
        # added on top of the macro heights.  Designed for a sand-
        # ripple companion of the colour-tile texture so the
        # geometry's ripple troughs line up with the colour
        # texture's ripple troughs.  Skipped silently if no path
        # is provided or the file is missing.
        if self.detail_image_path:
            detail = _detail_displacement(
                detail_path=self.detail_image_path,
                world_size=self.world_size,
                mesh_size=self.size,
                tile_meters=self.detail_tile_size,
                height_scale=self.detail_height_scale)
            heights = heights + detail

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

        # ---- Diffuse "sand" texture (optional) -----------------------
        # When `sand_path` is provided, the terrain shader samples
        # this texture as the surface diffuse instead of using the
        # built-in cosine-palette procedural colour.  Mip-mapped +
        # anisotropic at load time; tiled across world (xz) at
        # `sand_tile_size`-metre intervals.
        if self.sand_path:
            self._sand_tex = _load_terrain_diffuse(self.sand_path)

    # ------------------------------------------------------------------
    @property
    def height_range(self):
        """(min_y, max_y) of the generated heightmap in world units."""
        return (self._min_y, self._max_y)

    # ------------------------------------------------------------------
    def sample_height(self, x, z):
        """Return the world-Y of the terrain surface at world (x, z).

        Includes the macro heightmap AND any tiled sand-detail
        displacement -- both are baked into `self._heightmap` at
        construction time, so this is a single bilinear
        interpolation against that 2-D grid plus the `base_y`
        offset (matching exactly what the GPU mesh renders at
        the requested point).  Out-of-bounds queries return
        `base_y` so downstream physics can sample edge-of-world
        without special-casing.
        """
        return bilinear_sample_height(
            self._heightmap, self.world_size, x, z,
            base_y=self.base_y)

    # ------------------------------------------------------------------
    def sample_heights(self, xs, zs):
        """Vectorised counterpart of `sample_height`.

        `xs` / `zs` can be any matching shape; returns a numpy
        array of the same shape with the terrain Y at each point.
        Designed for hot-path use (12 wheels x 60 FPS is microseconds
        when batched).
        """
        return bilinear_sample_heights(
            self._heightmap, self.world_size, xs, zs,
            base_y=self.base_y)

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

        # World-space eye position derived from the view matrix --
        # needed by terrain.frag's distance-fog blend.  Same
        # math as the mesh shader (eye = -R^T * t where view = R|t).
        # Done here rather than at the call site so every render path
        # gets a correct u_eye for free.
        R   = view[:3, :3]
        t   = view[:3,  3]
        eye = -R.T @ t
        shader.set_vec3('u_eye', eye)

        # Sand diffuse: bound to texture unit 0.  Shader checks
        # `u_has_sand_tex` to decide whether to sample the texture
        # or fall back to the procedural cosine palette -- so a
        # missing / un-loaded sand texture still renders cleanly.
        shader.set_float('u_sand_tile_size', self.sand_tile_size)
        if self._sand_tex:
            glActiveTexture(GL_TEXTURE0)
            glBindTexture(GL_TEXTURE_2D, self._sand_tex)
            shader.set_int('u_sand_tex',     0)
            shader.set_int('u_has_sand_tex', 1)
        else:
            shader.set_int('u_has_sand_tex', 0)

        glBindVertexArray(self.vao)
        glDrawElements(GL_TRIANGLES, self._n_indices,
                       GL_UNSIGNED_INT, None)
        glBindVertexArray(0)

    # ------------------------------------------------------------------
    def cleanup(self):
        """Free the VAO / VBOs + sand texture.  Idempotent."""
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
        if self._sand_tex:
            try:
                glDeleteTextures([int(self._sand_tex)])
            except Exception:
                pass
            self._sand_tex = 0
