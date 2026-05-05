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
