"""
Skybox -- loads six face images into an OpenGL cubemap, renders a background
cube, and performs GPU-based IBL prefiltering at startup.

GPU prefilter output (all generated once during __init__, discarded after):
    irradiance_id   -- 32×32 Lambertian diffuse irradiance cubemap
    prefiltered_id  -- 128×128 GGX prefiltered specular cubemap (5 mip levels)
    brdf_lut_id     -- 512×512 BRDF split-sum LUT  (R=scale, G=bias, linear)

Classes:
    SkyboxShader : compiles shaders/skybox.{vert,frag}.
                   Uniforms: view, projection, skybox (samplerCube).
                   Methods: use(), set_mat4(name, m), set_int(name, v).
    Skybox       : loads cube geometry and 6 PNG face images, runs GPU prefilter.
                   Constructor: Skybox(x_file, image_dir)
                   Methods: render(view, projection), cleanup().
                   Public attributes:
                       cubemap_id    (GLuint) -- raw env cubemap (for skybox draw)
                       irradiance_id (GLuint) -- diffuse IBL cubemap
                       prefiltered_id(GLuint) -- specular IBL cubemap (mips)
                       brdf_lut_id   (GLuint) -- split-sum BRDF LUT (2D)

Face → texture mapping (WoT Tank-Exporter convention):
    cubemap_m00_c00.png  →  GL_TEXTURE_CUBE_MAP_POSITIVE_X  (+X)
    cubemap_m00_c01.png  →  GL_TEXTURE_CUBE_MAP_NEGATIVE_X  (-X)
    cubemap_m00_c02.png  →  GL_TEXTURE_CUBE_MAP_POSITIVE_Y  (+Y)
    cubemap_m00_c03.png  →  GL_TEXTURE_CUBE_MAP_NEGATIVE_Y  (-Y)
    cubemap_m00_c04.png  →  GL_TEXTURE_CUBE_MAP_POSITIVE_Z  (+Z)
    cubemap_m00_c05.png  →  GL_TEXTURE_CUBE_MAP_NEGATIVE_Z  (-Z)
"""

import ctypes
import math
import os

import numpy as np
from OpenGL.GL import *
from PIL import Image

from .common   import load_shader_file
from .shaders  import _compile_program
from .xloader  import load_x


# ---------------------------------------------------------------------------
# Skybox background shader
# ---------------------------------------------------------------------------

class SkyboxShader:
    """Minimal shader for drawing the background cubemap."""

    def __init__(self):
        vs = load_shader_file('shaders/skybox.vert')
        fs = load_shader_file('shaders/skybox.frag')
        self.program = _compile_program(vs, fs, 'Skybox shader')

    def use(self):
        glUseProgram(self.program)

    def set_mat4(self, name, m):
        glUniformMatrix4fv(glGetUniformLocation(self.program, name), 1, GL_TRUE, m)

    def set_int(self, name, v):
        glUniform1i(glGetUniformLocation(self.program, name), v)


# ---------------------------------------------------------------------------
# Raw cubemap loader
# ---------------------------------------------------------------------------

def _load_cubemap(image_dir):
    """Upload 6 face images from image_dir into a GL_TEXTURE_CUBE_MAP.

    Files: cubemap_m00_c00.png … cubemap_m00_c05.png (WoT convention).
    Index i → GL_TEXTURE_CUBE_MAP_POSITIVE_X + i.

    Returns (texture_id, face_width).
    """
    tex = glGenTextures(1)
    glBindTexture(GL_TEXTURE_CUBE_MAP, tex)

    src_width = 1
    face_labels = ['+X', '-X', '+Y', '-Y', '+Z', '-Z']
    for i in range(6):
        fname = f'cubemap_m00_c0{i}.png'
        path  = os.path.join(image_dir, fname)
        img   = Image.open(path).convert('RGB')
        data  = img.tobytes()
        w, h  = img.size
        if i == 0:
            src_width = w
        face_enum = GL_TEXTURE_CUBE_MAP_POSITIVE_X + i
        glTexImage2D(face_enum, 0, GL_RGB, w, h, 0, GL_RGB, GL_UNSIGNED_BYTE, data)
        print(f'  [skybox] c0{i} ({face_labels[i]}) → {fname}  ({w}×{h})')

    # Mipmaps are needed for textureLod() in the prefilter shader
    glGenerateMipmap(GL_TEXTURE_CUBE_MAP)
    glTexParameteri(GL_TEXTURE_CUBE_MAP, GL_TEXTURE_MIN_FILTER, GL_LINEAR_MIPMAP_LINEAR)
    glTexParameteri(GL_TEXTURE_CUBE_MAP, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
    glTexParameteri(GL_TEXTURE_CUBE_MAP, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_CUBE_MAP, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_CUBE_MAP, GL_TEXTURE_WRAP_R, GL_CLAMP_TO_EDGE)
    glBindTexture(GL_TEXTURE_CUBE_MAP, 0)
    return tex, src_width


# ---------------------------------------------------------------------------
# IBL prefilter helpers
# ---------------------------------------------------------------------------

def _make_fullscreen_quad():
    """Return (vao, vbo) for a two-triangle fullscreen quad (positions only)."""
    verts = np.array([
        -1.0, -1.0,
         1.0, -1.0,
         1.0,  1.0,
        -1.0, -1.0,
         1.0,  1.0,
        -1.0,  1.0,
    ], dtype=np.float32)
    vao = glGenVertexArrays(1)
    vbo = glGenBuffers(1)
    glBindVertexArray(vao)
    glBindBuffer(GL_ARRAY_BUFFER, vbo)
    glBufferData(GL_ARRAY_BUFFER, verts.nbytes, verts, GL_STATIC_DRAW)
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 8, ctypes.c_void_p(0))
    glEnableVertexAttribArray(0)
    glBindVertexArray(0)
    return vao, vbo


def _make_empty_cubemap(size, internal_fmt=None):
    """Create an all-black cubemap with a single mip level.

    GL_RGBA16F is used by default -- GL_RGB16F is NOT required to be
    color-renderable in OpenGL 3.3 and causes silent FBO failure on many drivers.
    """
    if internal_fmt is None:
        internal_fmt = GL_RGBA16F
    tex = glGenTextures(1)
    glBindTexture(GL_TEXTURE_CUBE_MAP, tex)
    for face in range(6):
        glTexImage2D(GL_TEXTURE_CUBE_MAP_POSITIVE_X + face, 0,
                     internal_fmt, size, size, 0,
                     GL_RGBA, GL_FLOAT, None)
    glTexParameteri(GL_TEXTURE_CUBE_MAP, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
    glTexParameteri(GL_TEXTURE_CUBE_MAP, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
    glTexParameteri(GL_TEXTURE_CUBE_MAP, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_CUBE_MAP, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_CUBE_MAP, GL_TEXTURE_WRAP_R, GL_CLAMP_TO_EDGE)
    glBindTexture(GL_TEXTURE_CUBE_MAP, 0)
    return tex


def _make_cubemap_with_mips(base_size, num_mips, internal_fmt=None):
    """Create an all-black cubemap with num_mips pre-allocated mip levels.

    GL_RGBA16F is used by default (see _make_empty_cubemap note).
    """
    if internal_fmt is None:
        internal_fmt = GL_RGBA16F
    tex = glGenTextures(1)
    glBindTexture(GL_TEXTURE_CUBE_MAP, tex)
    for mip in range(num_mips):
        mip_size = max(1, base_size >> mip)
        for face in range(6):
            glTexImage2D(GL_TEXTURE_CUBE_MAP_POSITIVE_X + face, mip,
                         internal_fmt, mip_size, mip_size, 0,
                         GL_RGBA, GL_FLOAT, None)
    glTexParameteri(GL_TEXTURE_CUBE_MAP, GL_TEXTURE_MIN_FILTER, GL_LINEAR_MIPMAP_LINEAR)
    glTexParameteri(GL_TEXTURE_CUBE_MAP, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
    glTexParameteri(GL_TEXTURE_CUBE_MAP, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_CUBE_MAP, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_CUBE_MAP, GL_TEXTURE_WRAP_R, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_CUBE_MAP, GL_TEXTURE_MAX_LEVEL, num_mips - 1)
    glBindTexture(GL_TEXTURE_CUBE_MAP, 0)
    return tex


def _set_uniform_i(prog, name, v):
    loc = glGetUniformLocation(prog, name)
    if loc >= 0:
        glUniform1i(loc, v)


def _set_uniform_f(prog, name, v):
    loc = glGetUniformLocation(prog, name)
    if loc >= 0:
        glUniform1f(loc, v)


def _prefilter_ibl(raw_cubemap_id, src_width, prog, quad_vao,
                   irr_size=32,  irr_samples=128,
                   pf_size=128,  pf_mips=5, pf_samples=512,
                   lut_size=512, lut_samples=512):
    """Run the GPU IBL prefilter and return (irradiance_id, prefiltered_id, brdf_lut_id).

    Renders into offscreen FBO surfaces.  All three maps are stored as
    GL_RGB16F (float) textures.

    Args:
        raw_cubemap_id  GL cubemap with mipmaps (source environment).
        src_width       Width of the source cubemap face (for LOD formula).
        prog            Compiled IBL prefilter shader program.
        quad_vao        VAO for a fullscreen quad.
        irr_size        Irradiance cubemap face size (default 32).
        irr_samples     QMC samples for Lambertian pass (default 128).
        pf_size         Base face size for prefiltered cubemap (default 128).
        pf_mips         Number of roughness mip levels (default 5).
        pf_samples      QMC samples per roughness level (default 512).
        lut_size        BRDF LUT texture size (default 512).
        lut_samples     QMC samples for BRDF LUT (default 512).
    """

    # ---- Save GL state -------------------------------------------------------
    # glGetIntegerv returns numpy arrays in PyOpenGL -- flatten before int()
    prev_vp   = [int(x) for x in glGetIntegerv(GL_VIEWPORT)]
    prev_prog = int(np.asarray(glGetIntegerv(GL_CURRENT_PROGRAM)).flat[0])
    prev_fbo  = int(np.asarray(glGetIntegerv(GL_FRAMEBUFFER_BINDING)).flat[0])

    glDisable(GL_DEPTH_TEST)
    glDisable(GL_CULL_FACE)
    glDisable(GL_BLEND)

    fbo = glGenFramebuffers(1)
    glBindFramebuffer(GL_FRAMEBUFFER, fbo)

    glUseProgram(prog)
    glBindVertexArray(quad_vao)

    # Bind source cubemap (unit 0)
    glActiveTexture(GL_TEXTURE0)
    glBindTexture(GL_TEXTURE_CUBE_MAP, raw_cubemap_id)
    _set_uniform_i(prog, 'u_cubemapTexture', 0)

    # Common uniforms
    _set_uniform_i(prog, 'u_isGeneratingLUT',  0)
    _set_uniform_i(prog, 'u_floatTexture',      1)
    _set_uniform_f(prog, 'u_intensityScale',    1.0)
    _set_uniform_f(prog, 'u_lodBias',           0.0)
    _set_uniform_i(prog, 'u_width',             src_width)

    # ---- 1. Lambertian irradiance map ----------------------------------------
    print(f'  [ibl] Rendering irradiance map ({irr_size}×{irr_size}, '
          f'{irr_samples} samples)...')
    irr_id = _make_empty_cubemap(irr_size)
    _set_uniform_i(prog, 'u_distribution', 0)        # cLambertian
    _set_uniform_f(prog, 'u_roughness',    0.0)
    _set_uniform_i(prog, 'u_sampleCount',  irr_samples)
    glViewport(0, 0, irr_size, irr_size)

    for face in range(6):
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                               GL_TEXTURE_CUBE_MAP_POSITIVE_X + face,
                               irr_id, 0)
        if face == 0:
            status = glCheckFramebufferStatus(GL_FRAMEBUFFER)
            if status != GL_FRAMEBUFFER_COMPLETE:
                glBindFramebuffer(GL_FRAMEBUFFER, prev_fbo)
                glDeleteFramebuffers(1, [fbo])
                raise RuntimeError(
                    f'[ibl] FBO not complete (status={status:#010x}). '
                    'GL_RGBA16F may not be color-renderable on this driver.')
        _set_uniform_i(prog, 'u_currentFace', face)
        glDrawArrays(GL_TRIANGLES, 0, 6)
    print('  [ibl]   irradiance done.')

    # ---- 2. GGX prefiltered specular map (per roughness mip) -----------------
    print(f'  [ibl] Rendering prefiltered specular map ({pf_size}×{pf_size}, '
          f'{pf_mips} mips, {pf_samples} samples/mip)...')
    pf_id = _make_cubemap_with_mips(pf_size, pf_mips)
    _set_uniform_i(prog, 'u_distribution', 1)        # cGGX
    _set_uniform_i(prog, 'u_sampleCount',  pf_samples)

    for mip in range(pf_mips):
        mip_size  = max(1, pf_size >> mip)
        roughness = float(mip) / float(pf_mips - 1)
        glViewport(0, 0, mip_size, mip_size)
        _set_uniform_f(prog, 'u_roughness', roughness)

        for face in range(6):
            glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                                   GL_TEXTURE_CUBE_MAP_POSITIVE_X + face,
                                   pf_id, mip)
            _set_uniform_i(prog, 'u_currentFace', face)
            glDrawArrays(GL_TRIANGLES, 0, 6)

        print(f'  [ibl]   mip {mip}: {mip_size}×{mip_size}  roughness={roughness:.2f}')

    # ---- 3. BRDF integration LUT --------------------------------------------
    print(f'  [ibl] Rendering BRDF LUT ({lut_size}×{lut_size}, '
          f'{lut_samples} samples)...')
    lut_id = glGenTextures(1)
    glBindTexture(GL_TEXTURE_2D, lut_id)
    # GL_RGBA16F: required color-renderable in GL 3.0+; GL_RGB16F is not guaranteed
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA16F,
                 lut_size, lut_size, 0,
                 GL_RGBA, GL_FLOAT, None)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
    glBindTexture(GL_TEXTURE_2D, 0)

    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                           GL_TEXTURE_2D, lut_id, 0)
    glViewport(0, 0, lut_size, lut_size)
    _set_uniform_i(prog, 'u_isGeneratingLUT', 1)
    _set_uniform_i(prog, 'u_distribution',    1)     # cGGX
    _set_uniform_i(prog, 'u_sampleCount',     lut_samples)
    glDrawArrays(GL_TRIANGLES, 0, 6)
    print('  [ibl]   BRDF LUT done.')

    # ---- Restore GL state ----------------------------------------------------
    glBindVertexArray(0)
    glBindFramebuffer(GL_FRAMEBUFFER, prev_fbo)
    glDeleteFramebuffers(1, [fbo])
    glViewport(prev_vp[0], prev_vp[1], prev_vp[2], prev_vp[3])
    glUseProgram(prev_prog)
    glEnable(GL_DEPTH_TEST)
    glEnable(GL_CULL_FACE)
    # Note: GL_BLEND deliberately left disabled -- viewer init handles it

    return irr_id, pf_id, lut_id


# ---------------------------------------------------------------------------
# Skybox class
# ---------------------------------------------------------------------------

class Skybox:
    """Environment cube rendered as a background, plus GPU IBL prefilter maps.

    Loads the .x cube model for geometry and the 6 PNG face images from
    image_dir into a GL_TEXTURE_CUBE_MAP.  Immediately after loading, runs
    the IBL prefilter pass to produce irradiance_id, prefiltered_id, and
    brdf_lut_id for PBR IBL in the mesh shader.

    Args:
        x_file    (str): path to cube_model.x
        image_dir (str): directory containing cubemap_m00_c0{0..5}.png
    """

    def __init__(self, x_file, image_dir):
        self.shader = SkyboxShader()

        # ---- Cube geometry (position only for cubemap sampling) --------------
        print("[skybox] Loading cube geometry...")
        mesh      = load_x(x_file)
        positions = mesh['positions'].astype(np.float32)
        indices   = mesh['indices'].astype(np.uint32)

        self.vao = glGenVertexArrays(1)
        glBindVertexArray(self.vao)

        self.vbo = glGenBuffers(1)
        glBindBuffer(GL_ARRAY_BUFFER, self.vbo)
        glBufferData(GL_ARRAY_BUFFER, positions.nbytes, positions, GL_STATIC_DRAW)
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 12, ctypes.c_void_p(0))
        glEnableVertexAttribArray(0)

        self.ebo = glGenBuffers(1)
        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, self.ebo)
        glBufferData(GL_ELEMENT_ARRAY_BUFFER, indices.nbytes, indices, GL_STATIC_DRAW)

        self.index_count = len(indices)
        glBindVertexArray(0)

        # ---- Raw environment cubemap -----------------------------------------
        print("[skybox] Loading cubemap faces...")
        self.cubemap_id, src_width = _load_cubemap(image_dir)

        # ---- GPU IBL prefilter -----------------------------------------------
        print("[skybox] Running GPU IBL prefilter...")
        _vs = load_shader_file('shaders/ibl_prefilter.vert')
        _fs = load_shader_file('shaders/ibl_prefilter.frag')
        _prog  = _compile_program(_vs, _fs, 'IBL prefilter shader')
        _quad_vao, _quad_vbo = _make_fullscreen_quad()

        self.irradiance_id, self.prefiltered_id, self.brdf_lut_id = _prefilter_ibl(
            raw_cubemap_id = self.cubemap_id,
            src_width      = src_width,
            prog           = _prog,
            quad_vao       = _quad_vao,
        )

        # Prefilter shader and quad are only needed at init time
        glDeleteProgram(_prog)
        glDeleteBuffers(1, [_quad_vbo])
        glDeleteVertexArrays(1, [_quad_vao])

        print(f"[skybox] Done.  cubemap_id={self.cubemap_id}  "
              f"irradiance_id={self.irradiance_id}  "
              f"prefiltered_id={self.prefiltered_id}  "
              f"brdf_lut_id={self.brdf_lut_id}  "
              f"triangles={self.index_count // 3}")

    # ------------------------------------------------------------------
    def render(self, view, projection):
        """Draw the skybox behind all scene geometry.

        Call AFTER opaque geometry with depth func GL_LEQUAL, or before with
        GL_LESS — the vertex shader outputs w=z so depth == 1.0 everywhere.

        Args:
            view       (4×4 float32): camera view matrix
            projection (4×4 float32): perspective projection matrix
        """
        glDepthFunc(GL_LEQUAL)
        glDisable(GL_CULL_FACE)

        self.shader.use()
        self.shader.set_mat4('view',       view)
        self.shader.set_mat4('projection', projection)

        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_CUBE_MAP, self.cubemap_id)
        self.shader.set_int('skybox', 0)

        glBindVertexArray(self.vao)
        glDrawElements(GL_TRIANGLES, self.index_count, GL_UNSIGNED_INT, None)
        glBindVertexArray(0)

        glDepthFunc(GL_LESS)
        glEnable(GL_CULL_FACE)

    # ------------------------------------------------------------------
    def cleanup(self):
        """Delete all GPU resources."""
        glDeleteBuffers(1, [self.vbo])
        glDeleteBuffers(1, [self.ebo])
        glDeleteVertexArrays(1, [self.vao])
        glDeleteTextures(1, [self.cubemap_id])
        glDeleteTextures(1, [self.irradiance_id])
        glDeleteTextures(1, [self.prefiltered_id])
        glDeleteTextures(1, [self.brdf_lut_id])


# ---------------------------------------------------------------------------
# Finite-radius procedural skydome
# ---------------------------------------------------------------------------

class SkyDomeShader:
    """Shader pair for the procedural skydome -- a real-world-space
    sphere at the origin, NOT the camera-locked infinite skybox.

    Per Coffee 2026-05-15 (`maps\\skyboxes\\01_Karelia_sky\\
    skydome\\sky_karelia_forward.dds`): WoT skydomes ship as
    equirectangular panoramas not cubemaps.  The `mode` arg
    selects which fragment shader to compile:

        'cubemap'   -- sample a samplerCube via `u_cubemap`.
                       Original variant, paired with the
                       existing env cubemap when no panorama
                       is available.
        'equirect'  -- sample a sampler2D via `u_panorama`,
                       computing (u, v) from the direction
                       vector via atan2 / acos.  Used when
                       the runtime loaded a WoT skydome panorama
                       (e.g. sky_karelia_forward.png).

    Source: shaders/skydome.vert + shaders/skydome[ _equirect].frag.
    """

    def __init__(self, mode='cubemap'):
        vs = load_shader_file('shaders/skydome.vert')
        if mode == 'equirect':
            fs_file = 'shaders/skydome_equirect.frag'
            label   = 'SkyDome shader (equirect)'
        else:
            fs_file = 'shaders/skydome.frag'
            label   = 'SkyDome shader (cubemap)'
        fs = load_shader_file(fs_file)
        self.program = _compile_program(vs, fs, label)
        self.mode    = mode

    def use(self):
        glUseProgram(self.program)

    def set_mat4(self, name, m):
        glUniformMatrix4fv(glGetUniformLocation(self.program, name),
                           1, GL_TRUE, m)

    def set_int(self, name, v):
        glUniform1i(glGetUniformLocation(self.program, name), v)

    def set_float(self, name, v):
        glUniform1f(glGetUniformLocation(self.program, name), v)

    def set_vec3(self, name, x, y, z):
        glUniform3f(glGetUniformLocation(self.program, name), x, y, z)


def _build_uv_sphere(radius, lat_segments=24, lon_segments=48):
    """Generate a UV-sphere mesh -- the canonical lat/long
    subdivision.  Returns `(vertices_flat, indices)` ready to
    push into a GL buffer.

    Standard recipe:
      * `lat_segments` rings between the +Y pole and -Y pole.
      * `lon_segments` slices around the Y axis.
      * Two triangles per quad face; pole rings reuse the pole
        vertex (degenerate quad collapses to a triangle).

    Output verts are float32 in CCW winding when viewed from
    OUTSIDE the sphere.  The skydome draw flips culling so we
    see the INSIDE; this winding makes the swap a one-flag
    change instead of a remesh.

    Args:
        radius        (float): world-units sphere radius.
        lat_segments  (int)  : number of latitude rings (>=4).
        lon_segments  (int)  : number of longitude slices (>=6).

    Returns:
        (np.ndarray float32 verts shape (V, 3),
         np.ndarray uint32 indices shape (F * 3,))
    """
    lat_segments = max(4, int(lat_segments))
    lon_segments = max(6, int(lon_segments))
    verts = []
    for i in range(lat_segments + 1):
        lat = np.pi * (i / lat_segments)        # 0 .. pi
        sin_lat = np.sin(lat)
        cos_lat = np.cos(lat)                   # +1 at top, -1 at bot
        for j in range(lon_segments + 1):
            lon = 2.0 * np.pi * (j / lon_segments)
            sin_lon = np.sin(lon)
            cos_lon = np.cos(lon)
            x = radius * sin_lat * cos_lon
            y = radius * cos_lat
            z = radius * sin_lat * sin_lon
            verts.append((x, y, z))
    indices = []
    for i in range(lat_segments):
        for j in range(lon_segments):
            a = i * (lon_segments + 1) + j
            b = a + 1
            c = a + (lon_segments + 1)
            d = c + 1
            # CCW from outside: (a, c, b) + (b, c, d).  When we
            # flip culling at draw time the inside view sees CCW
            # triangles too -- no winding-direction issues.
            indices.extend((a, c, b, b, c, d))
    return (np.asarray(verts, dtype=np.float32),
            np.asarray(indices, dtype=np.uint32))


class SkyDome:
    """Procedural skydome -- a UV sphere at the world origin
    sampled with the existing env cubemap.

    Per Coffee 2026-05-14 ("we have skydomes in the game.. make
    a sphere. rad = map size / 2"): sized so the radius matches
    half the heightmap's world_size, the sphere fully encloses
    the play area.  Drawn INSIDE-OUT (front-face cull) so the
    camera sees the inside surface.  Sampled with the same
    cubemap the Skybox uses, so the dome and skybox carry
    matching imagery.

    Unlike `Skybox` -- which uses a z=w trick to sit at infinity
    + follows the camera -- the SkyDome is a real mesh in world
    space.  Move the camera near the dome's edge and you'll see
    the back curve (intentional: the dome marks the play area).

    Args:
        cubemap_id (int): GL cubemap texture to sample.  Pass
                          the Skybox's `cubemap_id` so the dome
                          and skybox stay visually in sync.
        radius     (float): sphere radius in world units.
        lat / lon  (int): subdivision counts.  24 x 48 = 2304
                          triangles by default -- cheap.

    The render() call assumes the caller has already drawn opaque
    geometry; the dome's depth test is GL_LEQUAL on the existing
    depth buffer + depth-write OFF so it doesn't poison later
    transparent layers.  Front-face cull keeps the OUTSIDE of
    the sphere invisible (no need for a back-face dome separately
    for outside views).
    """

    def __init__(self, cubemap_id=None, radius=512.0,
                 panorama_png=None,
                 lat_segments=24, lon_segments=48):
        """Build the dome geometry + load the chosen sky source.

        Per Coffee 2026-05-15 (`sky_karelia_forward.dds`): when
        `panorama_png` is given, we load it as a 2-D
        equirectangular texture and bind the
        skydome_equirect.frag variant.  Otherwise we fall back
        to the cubemap path (sampling the env cubemap that
        powers Skybox + IBL).  At least ONE of cubemap_id /
        panorama_png must resolve; otherwise __init__ raises.

        Args:
            cubemap_id  (int|None) : GL cubemap texture id (e.g.
                                     Skybox.cubemap_id).  Used
                                     when no panorama is supplied.
            radius      (float)    : sphere radius (world units).
            panorama_png (str|None): path to an equirectangular
                                     PNG.  Selected first if
                                     present.
            lat / lon   (int)      : UV-sphere subdivision counts.
        """
        self.radius = float(radius)
        # Source-selection: panorama wins, cubemap is fallback.
        self.cubemap_id    = 0
        self.panorama_id   = 0
        self.mode          = 'cubemap'
        if panorama_png and os.path.isfile(panorama_png):
            self.panorama_id = _load_panorama_2d(panorama_png)
            self.mode        = 'equirect'
        elif cubemap_id:
            self.cubemap_id  = int(cubemap_id)
            self.mode        = 'cubemap'
        else:
            raise RuntimeError(
                "SkyDome: need either cubemap_id or panorama_png")

        self.shader = SkyDomeShader(mode=self.mode)
        # Default fog tint = a warm-grey haze.  Override via
        # `set_fog_color` from the caller (Viewer keeps a sky
        # palette and can colour-match terrain fog).
        self.fog_color    = (0.66, 0.71, 0.78)
        # Horizon band: fade between y=t0 (above) -> y=t1 (below)
        # in normalised sphere-direction space.  Hits a clean
        # mid-ground transition.
        self.horizon_t0   = 0.10   # full sky at +y above this
        self.horizon_t1   = -0.15  # full fog at +y below this

        verts, indices = _build_uv_sphere(
            self.radius, lat_segments=lat_segments,
            lon_segments=lon_segments)
        self.index_count = int(indices.size)

        self.vao = glGenVertexArrays(1)
        self.vbo = glGenBuffers(1)
        self.ebo = glGenBuffers(1)
        glBindVertexArray(self.vao)

        glBindBuffer(GL_ARRAY_BUFFER, self.vbo)
        glBufferData(GL_ARRAY_BUFFER, verts.nbytes, verts,
                     GL_STATIC_DRAW)
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE,
                              3 * 4, ctypes.c_void_p(0))
        glEnableVertexAttribArray(0)

        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, self.ebo)
        glBufferData(GL_ELEMENT_ARRAY_BUFFER, indices.nbytes,
                     indices, GL_STATIC_DRAW)
        glBindVertexArray(0)

        print(f"[SkyDome] radius={self.radius:.0f}m  "
              f"lat={lat_segments} lon={lon_segments}  "
              f"tris={self.index_count // 3}")

    # ------------------------------------------------------------------
    def set_fog_color(self, rgb):
        """Update the ground-band tint.  Live; reads on next render."""
        r, g, b = (float(rgb[0]), float(rgb[1]), float(rgb[2]))
        self.fog_color = (r, g, b)

    # ------------------------------------------------------------------
    # Class-level tunables for the two-pass rotated-blend trick.
    # `rotate_pass_deg`  -- Y-axis rotation applied between the
    #                       opaque first pass + the alpha second
    #                       pass.  Tiny number on purpose: 0.01
    #                       deg shifts the second pass by ~1.5 cm
    #                       at a 1 km radius, just enough to
    #                       soften the wrap seam.
    # `rotate_pass_alpha` -- alpha of the second pass.  0.5 = a
    #                       50/50 average of the two seam
    #                       positions.
    rotate_pass_deg   = 0.01
    rotate_pass_alpha = 0.5

    def render(self, view, projection):
        """Draw the dome twice with a tiny azimuth rotation between
        passes, smearing the equirect-wrap seam into a soft blend.

        Per Coffee 2026-05-15 ("i always turn depth testing off
        when rendering it.. I write no depth info" + "double
        render map with depth write off. draw, rotate _ .01 degree
        and draw skydome again"):

          * Depth test OFF, depth write OFF on both passes -- the
            dome is a pure backdrop.  Caller must render BEFORE
            any opaque geometry so terrain / tank / etc. overdraw
            it where they sit closer.
          * Pass 1: u_alpha = 1.0, view = caller-supplied.
          * Pass 2: u_alpha = 0.5, view = view * Ry(0.01 deg).
            The rotation is applied to world coordinates (pre-
            multiplied INTO the view matrix), so the dome
            geometry shifts by 0.01 deg of azimuth between the
            two draws.  Combined with the alpha blend, the wrap
            seam from the first pass averages with a slightly
            offset seam from the second pass -- visible line
            softens into a faint band.
          * Cull face stays at GL_BACK so the dome is visible
            from INSIDE the sphere and hidden from outside.
        """
        glEnable(GL_CULL_FACE)
        glCullFace(GL_BACK)
        glDisable(GL_DEPTH_TEST)
        glDepthMask(GL_FALSE)
        # Alpha blend so pass 2 layers over pass 1.
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

        self.shader.use()
        self.shader.set_mat4 ('projection',   projection)
        self.shader.set_vec3 ('u_fog_color',
                              *self.fog_color)
        self.shader.set_float('u_horizon_t0', self.horizon_t0)
        self.shader.set_float('u_horizon_t1', self.horizon_t1)

        glActiveTexture(GL_TEXTURE0)
        if self.mode == 'equirect':
            glBindTexture(GL_TEXTURE_2D, self.panorama_id)
            self.shader.set_int('u_panorama', 0)
        else:
            glBindTexture(GL_TEXTURE_CUBE_MAP, self.cubemap_id)
            self.shader.set_int('u_cubemap', 0)

        # ---- Pass 1: opaque, original view ---------------------
        self.shader.set_mat4 ('view',    view)
        self.shader.set_float('u_alpha', 1.0)
        glBindVertexArray(self.vao)
        glDrawElements(GL_TRIANGLES, self.index_count,
                       GL_UNSIGNED_INT, None)

        # ---- Pass 2: alpha-blended, world rotated 0.01 deg Y ----
        # Pre-multiply `view` by a Y-axis rotation so the dome
        # geometry effectively spins under a stationary camera.
        # Tiny enough (0.01 deg) that the visual content is the
        # same image -- the only useful effect is the seam shift.
        #
        # Per Coffee 2026-05-15 ("turn depth write on for 2nd
        # dome edge hiding draw and convert cursor to draw on
        # it using the decal projection shader"): enable depth
        # WRITE on this second pass so the dome's depth ends up
        # in the buffer.  The downstream aim-cursor block uses
        # the SS volumetric decal projector for dome hits (same
        # path as terrain hits) and the projector's frag stage
        # reconstructs the surface from a scene-depth snapshot
        # -- without dome depth in the buffer the reconstruction
        # falls into the cleared far value and discards.
        #
        # IMPORTANT (OpenGL spec gotcha caught 2026-05-15):
        # `glDepthMask(GL_TRUE)` alone is NOT enough -- the
        # depth buffer is bypassed entirely when depth-test is
        # disabled, REGARDLESS of the mask.  We have to enable
        # the depth test with `GL_ALWAYS` (every fragment
        # passes) for the writes to actually land.  Confirmed
        # with glReadPixels: with depth-test off + mask on,
        # the framebuffer's depth at dome pixels stays at the
        # cleared far value (1.0); with depth-test on +
        # `GL_ALWAYS` + mask on it correctly holds the dome's
        # clip-space depth.  GL_LESS is restored in the
        # cleanup block below.
        glEnable(GL_DEPTH_TEST)
        glDepthFunc(GL_ALWAYS)
        glDepthMask(GL_TRUE)
        try:
            angle = math.radians(float(self.rotate_pass_deg))
            c = math.cos(angle)
            s = math.sin(angle)
            Ry = np.array([
                [ c, 0.0,  s, 0.0],
                [0.0, 1.0, 0.0, 0.0],
                [-s, 0.0,  c, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ], dtype=np.float32)
            view_rot = (np.asarray(view, dtype=np.float32) @ Ry
                        ).astype(np.float32)
            self.shader.set_mat4 ('view',    view_rot)
            self.shader.set_float('u_alpha',
                                   float(self.rotate_pass_alpha))
            glDrawElements(GL_TRIANGLES, self.index_count,
                           GL_UNSIGNED_INT, None)
        except Exception as exc:
            print(f"[SkyDome] second pass skipped: {exc}")
        glBindVertexArray(0)

        # Restore depth + blend state for downstream passes.
        glDisable(GL_BLEND)
        glEnable(GL_DEPTH_TEST)
        glDepthFunc(GL_LESS)
        glDepthMask(GL_TRUE)

    # ------------------------------------------------------------------
    def cleanup(self):
        glDeleteBuffers(1, [self.vbo])
        glDeleteBuffers(1, [self.ebo])
        glDeleteVertexArrays(1, [self.vao])
        # Only delete the panorama -- the cubemap belongs to the
        # Skybox and is freed by its cleanup.  Owning the same
        # GL handle in two places would lead to double-free.
        if self.panorama_id:
            glDeleteTextures(1, [self.panorama_id])
            self.panorama_id = 0


def _load_panorama_2d(png_path):
    """Upload an equirectangular RGBA PNG as a GL_TEXTURE_2D.

    Per Coffee 2026-05-15 (`sky_karelia_forward.dds`): the WoT
    skydome panorama is 4096 x 1024 + needs to wrap around the
    azimuth (so u-wrap = GL_REPEAT) and clamp at the poles
    (v-clamp = GL_CLAMP_TO_EDGE so a sphere vertex at d.y = +/-1
    samples the texture's top/bottom row instead of bleeding
    past the edge).  Returns the GL texture id.
    """
    img = Image.open(png_path).convert('RGBA')
    w, h = img.size
    data = img.tobytes()
    tex = glGenTextures(1)
    glBindTexture(GL_TEXTURE_2D, tex)
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8,
                 w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, data)
    glGenerateMipmap(GL_TEXTURE_2D)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER,
                    GL_LINEAR_MIPMAP_LINEAR)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_REPEAT)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T,
                    GL_CLAMP_TO_EDGE)
    glBindTexture(GL_TEXTURE_2D, 0)
    print(f"[SkyDome] panorama {os.path.basename(png_path)}: "
          f"{w}x{h}  tex_id={tex}")
    return int(tex)
