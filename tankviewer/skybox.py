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
