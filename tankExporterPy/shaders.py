"""
Shader programs - compile and provide uniform setters for the three shader sets.

Classes:
    ShaderProgram      : main mesh shader (loads shaders/mesh.{vert,frag})
                         Uniforms: model, view, projection, light_pos, view_pos,
                                   diffuse_map, normal_map, ao_map,
                                   use_normal_map, is_GA_normal,
                                   alpha_test_enable, alpha_ref, alpha_in_normal_red,
                                   ao_in_diffuse_alpha, has_ao_map.
    SimpleColorShader  : lines / vertex-colored geometry (grid, axes, sphere).
                         Uniforms: model, view, projection.
    UIShader           : 2D textured/solid quads for the menu bar.
                         Uniforms: projection, u_color, u_tex, u_use_tex.

Each shader exposes: use(), set_mat4(name, mat), set_vec3/vec4/int/float as appropriate.
"""

from OpenGL.GL import *

from .common import load_shader_file


def _compile_program(vsrc, fsrc, label='shader'):
    """Compile and link a vertex+fragment shader pair. Raises on error."""
    vs = glCreateShader(GL_VERTEX_SHADER)
    glShaderSource(vs, vsrc)
    glCompileShader(vs)
    if not glGetShaderiv(vs, GL_COMPILE_STATUS):
        print(f"{label} vertex shader error:", glGetShaderInfoLog(vs).decode())
        raise RuntimeError(f"{label} vertex shader compilation failed")

    fs = glCreateShader(GL_FRAGMENT_SHADER)
    glShaderSource(fs, fsrc)
    glCompileShader(fs)
    if not glGetShaderiv(fs, GL_COMPILE_STATUS):
        print(f"{label} fragment shader error:", glGetShaderInfoLog(fs).decode())
        raise RuntimeError(f"{label} fragment shader compilation failed")

    program = glCreateProgram()
    glAttachShader(program, vs)
    glAttachShader(program, fs)
    glLinkProgram(program)
    if not glGetProgramiv(program, GL_LINK_STATUS):
        print(f"{label} link error:", glGetProgramInfoLog(program).decode())
        raise RuntimeError(f"{label} link failed")

    glDeleteShader(vs)
    glDeleteShader(fs)
    return program


def _compile_program_vgf(vsrc, gsrc, fsrc, label='shader'):
    """Compile and link a vertex + GEOMETRY + fragment shader trio.

    Same shape as `_compile_program` but with a geometry stage in the
    middle.  Raises on any compile / link failure.  Used by
    NormalsShader for the surface-normal debug-line feature -- the
    geometry shader expands each vertex into a 2-point line strip.
    """
    vs = glCreateShader(GL_VERTEX_SHADER)
    glShaderSource(vs, vsrc)
    glCompileShader(vs)
    if not glGetShaderiv(vs, GL_COMPILE_STATUS):
        print(f"{label} vertex shader error:",
              glGetShaderInfoLog(vs).decode())
        raise RuntimeError(f"{label} vertex shader compilation failed")

    gs = glCreateShader(GL_GEOMETRY_SHADER)
    glShaderSource(gs, gsrc)
    glCompileShader(gs)
    if not glGetShaderiv(gs, GL_COMPILE_STATUS):
        print(f"{label} geometry shader error:",
              glGetShaderInfoLog(gs).decode())
        raise RuntimeError(f"{label} geometry shader compilation failed")

    fs = glCreateShader(GL_FRAGMENT_SHADER)
    glShaderSource(fs, fsrc)
    glCompileShader(fs)
    if not glGetShaderiv(fs, GL_COMPILE_STATUS):
        print(f"{label} fragment shader error:",
              glGetShaderInfoLog(fs).decode())
        raise RuntimeError(f"{label} fragment shader compilation failed")

    program = glCreateProgram()
    glAttachShader(program, vs)
    glAttachShader(program, gs)
    glAttachShader(program, fs)
    glLinkProgram(program)
    if not glGetProgramiv(program, GL_LINK_STATUS):
        print(f"{label} link error:",
              glGetProgramInfoLog(program).decode())
        raise RuntimeError(f"{label} link failed")

    glDeleteShader(vs)
    glDeleteShader(gs)
    glDeleteShader(fs)
    return program


class NormalsShader:
    """Surface-normal debug-line shader (vert + geom + frag).

    Renders short debug lines along surface normals.  Two modes
    driven by `u_mode`:

      0 (by-face, default)  -- one cyan line per triangle, drawn
        from the triangle's centroid along the AVERAGE of the
        three vertex normals.  Cleaner per-face overview.

      1 (by-vertex)         -- three lines per triangle, one
        starting at each vertex going in that vertex's own normal.
        Coloured by abs(normal) so axis-aligned faces show pure
        R/G/B and off-axis blends are natural mixes.

    Driven by the right-panel "Normals" slider (length, 0 = off)
    plus the "PerVtx" checkbox (mode toggle).  The viewer re-binds
    each mesh's existing VAO and draws GL_TRIANGLES via its EBO,
    so no extra geometry gets uploaded for the debug pass.

    Uniforms:
        model            (mat4)  -- per-mesh world placement
        view             (mat4)  -- camera view
        projection       (mat4)  -- camera projection
        u_normal_length  (float) -- line length in world units; 0 = off
        u_mode           (int)   -- 0 = by-face, 1 = by-vertex
    """

    def __init__(self):
        vs = load_shader_file('shaders/normals.vert')
        gs = load_shader_file('shaders/normals.geom')
        fs = load_shader_file('shaders/normals.frag')
        self.program = _compile_program_vgf(vs, gs, fs, 'Normals shader')

    def use(self):
        glUseProgram(self.program)

    def set_mat4(self, name, matrix):
        glUniformMatrix4fv(glGetUniformLocation(self.program, name),
                           1, GL_TRUE, matrix)

    def set_float(self, name, value):
        glUniform1f(glGetUniformLocation(self.program, name), float(value))

    def set_int(self, name, value):
        glUniform1i(glGetUniformLocation(self.program, name), int(value))


class SimpleColorShader:
    """Per-vertex colored shader for lines and simple meshes (grid, axes, sphere)."""

    def __init__(self):
        vs = load_shader_file('shaders/color.vert')
        fs = load_shader_file('shaders/color.frag')
        self.program = _compile_program(vs, fs, 'Color shader')

    def use(self):
        glUseProgram(self.program)

    def set_mat4(self, name, matrix):
        glUniformMatrix4fv(glGetUniformLocation(self.program, name), 1, GL_TRUE, matrix)


class TerrainShader:
    """Procedural-ground shader.  Three-band height-then-slope colour
    blend (grass / dirt / rock) under a single directional light --
    cheap fixed-cost pipeline for the Terrain class.

    Uniforms set per-frame by `Terrain.render`:
        u_view, u_proj  (mat4)
        u_light_dir     (vec3, world space; shader normalises)
        u_height_min,
        u_height_max    (float, world Y range of the heightmap)
    """

    def __init__(self):
        vs = load_shader_file('shaders/terrain.vert')
        fs = load_shader_file('shaders/terrain.frag')
        self.program = _compile_program(vs, fs, 'Terrain shader')

    def use(self):
        glUseProgram(self.program)

    def set_mat4(self, name, matrix):
        glUniformMatrix4fv(
            glGetUniformLocation(self.program, name), 1, GL_TRUE, matrix)

    def set_vec3(self, name, vec):
        v = (float(vec[0]), float(vec[1]), float(vec[2]))
        glUniform3f(glGetUniformLocation(self.program, name), *v)

    def set_float(self, name, value):
        glUniform1f(glGetUniformLocation(self.program, name), float(value))

    def set_int(self, name, value):
        glUniform1i(glGetUniformLocation(self.program, name), int(value))


class ShaderProgram:
    """Main textured mesh shader (Phong + normal map + alpha test + AO)."""

    def __init__(self):
        vs = load_shader_file('shaders/mesh.vert')
        fs = load_shader_file('shaders/mesh.frag')
        self.program = _compile_program(vs, fs, 'Mesh shader')

    def use(self):
        glUseProgram(self.program)

    def set_mat4(self, name, matrix):
        glUniformMatrix4fv(glGetUniformLocation(self.program, name), 1, GL_TRUE, matrix)

    def set_int_array(self, name, values):
        """Upload a list of ints to a `uniform int name[N]` array.

        Uses `glUniform1iv` against the base array name (no `[0]`
        suffix); some drivers don't expose per-element locations
        for active uniform arrays, so the only portable path is
        one call against the base location.
        """
        import numpy as _np
        loc = glGetUniformLocation(self.program, name)
        if loc < 0:
            return
        arr = _np.asarray(values, dtype=_np.int32)
        glUniform1iv(loc, arr.size, arr)

    def set_mat4_array(self, name, matrices):
        """Upload a list of mat4s to a `uniform mat4 name[N]` array.

        Source matrices arrive row-major (numpy / TankPhysics convention);
        we forward `transpose=GL_TRUE` to match the per-matrix `set_mat4`
        path so the shader sees its `u_bones[i] * vec4(p, 1.0)` resolve
        the same way for arrays as for single uniforms.

        Args:
            name (str): uniform name (just the array name, no `[0]`).
            matrices (np.ndarray): shape (N, 4, 4) float32, OR any
                array-like that reshapes to (N, 4, 4).
        """
        import numpy as _np
        loc = glGetUniformLocation(self.program, name)
        if loc < 0:
            return
        arr = _np.ascontiguousarray(matrices, dtype=_np.float32)
        n   = arr.size // 16
        glUniformMatrix4fv(loc, n, GL_TRUE, arr)

    def set_vec3(self, name, x, y, z):
        glUniform3f(glGetUniformLocation(self.program, name), x, y, z)

    def set_vec3_array(self, name, positions):
        """Upload a list of vec3s to a `uniform vec3 name[N]` array.

        Args:
            name (str): uniform name (just the array name, no [0] suffix).
            positions (sequence of (x, y, z) tuples or 3-floats).
        """
        import numpy as _np
        loc = glGetUniformLocation(self.program, name)
        if loc < 0:
            return
        flat = _np.asarray(positions, dtype=_np.float32).reshape(-1)
        glUniform3fv(loc, len(positions), flat)

    def set_vec2(self, name, x, y):
        glUniform2f(glGetUniformLocation(self.program, name), x, y)

    def set_int(self, name, value):
        glUniform1i(glGetUniformLocation(self.program, name), value)

    def set_float(self, name, value):
        glUniform1f(glGetUniformLocation(self.program, name), value)

    def get_uniform(self, name):
        return glGetUniformLocation(self.program, name)


class ImportedShader:
    """Simple diffuse + bump shader used when source_type == 'fbx'.

    Source: shaders/mesh.vert + shaders/imported.frag.
    No PBR, no IBL, no GMM/AO routing, no alpha test, no nation tint --
    just diffuse + optional normal map + Lambertian + Phong from the
    3 scene lights.  Same vertex shader as the PBR pipeline so the
    existing Mesh VAO layout works without changes.
    """

    def __init__(self):
        vs = load_shader_file('shaders/mesh.vert')
        fs = load_shader_file('shaders/imported.frag')
        self.program = _compile_program(vs, fs, 'Imported shader')

    def use(self):
        glUseProgram(self.program)

    def set_mat4(self, name, matrix):
        glUniformMatrix4fv(glGetUniformLocation(self.program, name),
                           1, GL_TRUE, matrix)

    def set_vec3(self, name, x, y, z):
        glUniform3f(glGetUniformLocation(self.program, name), x, y, z)

    def set_vec3_array(self, name, positions):
        import numpy as _np
        loc = glGetUniformLocation(self.program, name)
        if loc < 0:
            return
        flat = _np.asarray(positions, dtype=_np.float32).reshape(-1)
        glUniform3fv(loc, len(positions), flat)

    def set_int(self, name, value):
        glUniform1i(glGetUniformLocation(self.program, name), value)

    def set_int_array(self, name, values):
        # Mirrored from ShaderProgram.set_int_array so the viewer
        # can upload `u_contact_bones` polymorphically regardless
        # of whether ImportedShader or ShaderProgram is active.
        import numpy as _np
        loc = glGetUniformLocation(self.program, name)
        if loc < 0:
            return
        arr = _np.asarray(values, dtype=_np.int32)
        glUniform1iv(loc, arr.size, arr)

    def set_mat4_array(self, name, matrices):
        # Mirrored from ShaderProgram.set_mat4_array (skinning
        # bones).  ImportedShader uses the same mesh.vert as
        # ShaderProgram, so the GPU skinning path needs the same
        # uniform-array upload regardless of which fragment shader
        # is paired with it.
        import numpy as _np
        loc = glGetUniformLocation(self.program, name)
        if loc < 0:
            return
        arr = _np.ascontiguousarray(matrices, dtype=_np.float32)
        n   = arr.size // 16
        glUniformMatrix4fv(loc, n, GL_TRUE, arr)

    def set_float(self, name, value):
        glUniform1f(glGetUniformLocation(self.program, name), value)

    def get_uniform(self, name):
        return glGetUniformLocation(self.program, name)


class ParticleShader:
    """Shader for camera-facing billboard particles with flipbook texture.

    Source: shaders/particle.vert + shaders/particle.frag.
    Uniforms used by the renderer (ParticleSystem.render):
        u_view, u_proj          - 4x4 row-major
        u_cam_right, u_cam_up   - vec3, world-space camera axes
        u_start_size, u_end_size, u_lifetime - floats
        u_flipbook              - sampler2DArray, bound to a texture unit
        u_num_frames            - float (== flipbook.frame_count)
    """

    def __init__(self):
        vs = load_shader_file('shaders/particle.vert')
        fs = load_shader_file('shaders/particle.frag')
        self.program = _compile_program(vs, fs, 'Particle shader')

    def use(self):
        glUseProgram(self.program)

    def set_mat4(self, name, matrix):
        glUniformMatrix4fv(glGetUniformLocation(self.program, name),
                           1, GL_TRUE, matrix)

    def set_vec3(self, name, x, y, z):
        glUniform3f(glGetUniformLocation(self.program, name), x, y, z)

    def set_int(self, name, value):
        glUniform1i(glGetUniformLocation(self.program, name), value)

    def set_float(self, name, value):
        glUniform1f(glGetUniformLocation(self.program, name), value)


class ScreenSpaceDecalShader:
    """Volumetric screen-space decal projector shader.

    Per Coffee 2026-05-15 ("look here at my decal projector frag
    and vert  C:\\nuTerra\\nuTerra\\shaders\\Terrain_shaders"):
    ported from nuTerra's `DecalProject.{vert,frag}` and re-
    targeted to GL 3.30 / TEPY's forward pipeline.

    The decal is rendered as a UNIT CUBE in local [-0.5, +0.5]^3
    space.  The frag stage reads the scene depth at every cube
    fragment, reconstructs the underlying world surface point,
    transforms it into decal-local space via a pre-multiplied
    invMVP, and clips anything outside the unit cube.  Survivors
    sample the albedo texture at `(local.xy + 0.5)`.

    Source: shaders/decal_project.vert + shaders/decal_project.frag.

    Uniforms:
        u_view, u_proj            - 4x4 row-major camera matrices
        u_decal_matrix            - 4x4, decal-cube -> world
        u_inv_view_proj_decal     - 4x4, inverse(proj*view*decal)
                                     pre-multiplied CPU-side so the
                                     frag stage's reconstruction is
                                     a single mat-vec product.
        u_albedo                  - sampler2D, decal RGBA
        u_depth_tex               - sampler2D, scene depth snapshot
        u_resolution              - vec2, viewport pixels (w, h)
        u_global_alpha            - float, master alpha multiplier
        u_fade_start              - float, age-fade onset (0..1).
                                     Pin past 1.0 to disable fade.
        u_age_frac                - float, decal age in [0, 1].
                                     Pin to 0.0 for single-frame
                                     decals (e.g. aim crosshair).
    """

    def __init__(self):
        vs = load_shader_file('shaders/decal_project.vert')
        fs = load_shader_file('shaders/decal_project.frag')
        self.program = _compile_program(vs, fs,
                                         'ScreenSpaceDecal shader')

    def use(self):
        glUseProgram(self.program)

    def set_mat4(self, name, matrix):
        glUniformMatrix4fv(glGetUniformLocation(self.program, name),
                           1, GL_TRUE, matrix)

    def set_int(self, name, value):
        glUniform1i(glGetUniformLocation(self.program, name), value)

    def set_float(self, name, value):
        glUniform1f(glGetUniformLocation(self.program, name), value)

    def set_vec2(self, name, x, y):
        glUniform2f(glGetUniformLocation(self.program, name),
                    float(x), float(y))


class DecalShader:
    """Shader for impact decal projection -- shell-hole albedos on
    terrain.

    Source: shaders/decal.vert + shaders/decal.frag.  Each active
    `Impact` is uploaded as 6 verts (2 triangles) in world space
    by the `Decals` projector.  The quad already sits at
    `impact.pos + impact.normal * bias` with corners pushed out
    along an orthonormal basis on the surface normal, so this
    stage is just `proj * view * a_position` -- the projection
    math lives on the CPU side.

    Per Coffee 2026-05-14 ("time to add a decal projector. to
    where the shells hit"): rendered AFTER opaque terrain in the
    alpha pass, before any other particle / billboard layers,
    so the decals tint the terrain underneath everything else.

    Uniforms:
        u_view, u_proj    - 4x4 row-major view + projection
        u_albedo          - sampler2D, shellhole PNG (RGBA)
        u_fade_start      - float, age fraction where fade-out
                            begins (0..1).  0.85 -> hold full
                            for 85 % of lifetime, fade out the
                            last 15 %.
        u_global_alpha    - float, master alpha multiplier
                            (renderer can dim/disable without
                            touching per-decal data)
    """

    def __init__(self):
        vs = load_shader_file('shaders/decal.vert')
        fs = load_shader_file('shaders/decal.frag')
        self.program = _compile_program(vs, fs, 'Decal shader')

    def use(self):
        glUseProgram(self.program)

    def set_mat4(self, name, matrix):
        glUniformMatrix4fv(glGetUniformLocation(self.program, name),
                           1, GL_TRUE, matrix)

    def set_int(self, name, value):
        glUniform1i(glGetUniformLocation(self.program, name), value)

    def set_float(self, name, value):
        glUniform1f(glGetUniformLocation(self.program, name), value)


class FlashShader:
    """Shader for the muzzle-flash 3-quad fan.

    Source: shaders/flash.vert + shaders/flash.frag.  Each shot's
    flash is rendered as 3 world-aligned quads (not camera-facing)
    fanning around the gun-forward axis at the muzzle position.

    Uniforms (Coffee 2026-05-14 WoT shimmer pipeline):

        u_view, u_proj          - 4x4 row-major
        u_muzzle_pos            - vec3, world pivot (= bottom-center)
        u_fwd                   - vec3, gun forward direction (unit)
        u_up                    - vec3, perpendicular-to-fwd up axis
        u_length                - float, plume length in world m
        u_thickness             - float, plume thickness in world m

        u_flipbook              - sampler2DArray, gun_flash color
        u_frame                 - int, current color frame index
        u_alpha                 - float, fade-out modulator [0, 1]
        u_tint                  - vec3, WoT keyframe color
        u_intensity             - float, WoT keyframe alpha scalar

        u_distortion            - sampler2DArray, distortion flipbook
        u_dist_frame            - int, distortion frame index
        u_dist_strength         - float, refraction magnitude in
                                  screen-uv units (~0.05 = subtle)
        u_refraction_tex        - sampler2D, back-buffer copy
        u_viewport_size         - vec2, (width, height) in pixels
    """

    def __init__(self):
        vs = load_shader_file('shaders/flash.vert')
        fs = load_shader_file('shaders/flash.frag')
        self.program = _compile_program(vs, fs, 'Flash shader')

    def use(self):
        glUseProgram(self.program)

    def set_mat4(self, name, matrix):
        glUniformMatrix4fv(glGetUniformLocation(self.program, name),
                           1, GL_TRUE, matrix)

    def set_vec3(self, name, x, y, z):
        glUniform3f(glGetUniformLocation(self.program, name),
                    x, y, z)

    def set_vec2(self, name, x, y):
        glUniform2f(glGetUniformLocation(self.program, name), x, y)

    def set_int(self, name, value):
        glUniform1i(glGetUniformLocation(self.program, name), value)

    def set_float(self, name, value):
        glUniform1f(glGetUniformLocation(self.program, name), value)


class PadShader:
    """Instanced track-pad shader (Phase C step 3 of the
    track-physics roadmap).

    Source: shaders/pad.vert + shaders/pad.frag.  The vert reads
    a per-instance mat4 transform from attribute locations 9..12
    (a mat4 split across 4 vec4 attributes; locations 5/6 stay
    free for the existing `iii` / `ww` skin attribs even though
    pads don't skin).

    Uniforms:
        u_chassis_pose : mat4 -- the tank's render-pose chassis
                         matrix.  Same matrix the standard mesh
                         shader's `model` is built from at the
                         tank-load-applied bind level.  Fed once
                         per frame.
        u_view, u_proj : standard view/proj.
        u_color        : vec4 -- flat tint applied per-pad.
                         Commit 1 keeps this constant; later
                         commits will sample diffuse / normal
                         maps and replace this knob.
    """

    def __init__(self):
        vs = load_shader_file('shaders/pad.vert')
        fs = load_shader_file('shaders/pad.frag')
        self.program = _compile_program(vs, fs, 'Pad shader')

    def use(self):
        glUseProgram(self.program)

    def set_mat4(self, name, m):
        glUniformMatrix4fv(glGetUniformLocation(self.program, name),
                           1, GL_TRUE, m)

    def set_vec4(self, name, r, g, b, a):
        glUniform4f(glGetUniformLocation(self.program, name),
                    r, g, b, a)

    def set_vec3(self, name, x, y, z):
        glUniform3f(glGetUniformLocation(self.program, name),
                    float(x), float(y), float(z))

    def set_vec3_array(self, name, positions):
        """Upload a list of vec3s to a `uniform vec3 name[N]` array.

        Used by the per-frame global-state bind in
        `_render_track_pad_body` to push the three scene lights
        into the pad's PBR fragment shader.
        """
        import numpy as _np
        loc = glGetUniformLocation(self.program, name)
        if loc < 0:
            return
        flat = _np.asarray(positions, dtype=_np.float32).reshape(-1)
        glUniform3fv(loc, len(positions), flat)

    def set_float(self, name, v):
        glUniform1f(glGetUniformLocation(self.program, name), float(v))

    def set_int(self, name, v):
        glUniform1i(glGetUniformLocation(self.program, name), int(v))

    def get_uniform(self, name):
        return glGetUniformLocation(self.program, name)


class UIShader:
    """2D shader for menu bar: solid color or sampled text texture."""

    def __init__(self):
        vs = load_shader_file('shaders/ui.vert')
        fs = load_shader_file('shaders/ui.frag')
        self.program = _compile_program(vs, fs, 'UI shader')

    def use(self):
        glUseProgram(self.program)

    def set_mat4(self, name, m):
        glUniformMatrix4fv(glGetUniformLocation(self.program, name), 1, GL_TRUE, m)

    def set_vec4(self, name, r, g, b, a):
        glUniform4f(glGetUniformLocation(self.program, name), r, g, b, a)

    def set_int(self, name, v):
        glUniform1i(glGetUniformLocation(self.program, name), v)
