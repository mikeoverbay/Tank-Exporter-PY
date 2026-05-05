"""
Flipbook texture loader + GPU-rendered billboard particle system.

Two classes:

    FlipbookTexture
        Loads a numbered PNG sequence (e.g. 0000.png .. 0090.png) from a
        folder and uploads it as a GL_TEXTURE_2D_ARRAY -- one layer per
        frame.  Mip-mapped, alpha-aware, ready for a sampler2DArray in
        a fragment shader.

    ParticleSystem
        Camera-facing billboard particles spawned from a list of emitters
        (each a {pos, fwd} dict).  Per-particle state lives in numpy
        arrays for vectorised updates; alive-particle data is uploaded
        to a single dynamic VBO each frame and drawn with one
        glDrawArrays(GL_TRIANGLES) call.  Velocity decays exponentially
        each frame so particles slow down as they drift away from the
        emitter (mimics the pressure drop of an exhaust plume).
"""

import os
import ctypes
import numpy as np

try:
    from PIL import Image
except ImportError:           # gracefully degrade if Pillow unavailable
    Image = None

from OpenGL.GL import *


# ---------------------------------------------------------------------------
# Flipbook texture (GL_TEXTURE_2D_ARRAY)
# ---------------------------------------------------------------------------

class FlipbookTexture:
    """Load a numbered PNG sequence into a GL_TEXTURE_2D_ARRAY.

    Args:
        folder (str): directory containing numbered PNG files
                      (e.g. 0000.png ... 0090.png).
        ext    (str): file extension to filter on (default '.png').
        max_frames (int|None): cap to first N files (None = take all).
    """

    def __init__(self, folder, ext='.png', max_frames=None):
        if Image is None:
            raise RuntimeError(
                "Pillow (PIL) is required to load flipbook textures")

        files = sorted(f for f in os.listdir(folder)
                       if f.lower().endswith(ext))
        if max_frames:
            files = files[:max_frames]
        if not files:
            raise RuntimeError(
                f"FlipbookTexture: no '{ext}' files in {folder}")

        # Determine dimensions from first frame
        first = Image.open(os.path.join(folder, files[0])).convert('RGBA')
        w, h  = first.size
        n     = len(files)

        self.tex_id = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D_ARRAY, self.tex_id)
        # Allocate the array texture (no data yet)
        glTexImage3D(GL_TEXTURE_2D_ARRAY, 0, GL_RGBA8,
                     w, h, n, 0,
                     GL_RGBA, GL_UNSIGNED_BYTE, None)

        # Upload frames one layer at a time
        for i, fname in enumerate(files):
            img = Image.open(os.path.join(folder, fname)).convert('RGBA')
            # OpenGL textures use bottom-left origin; PNGs are top-left
            img = img.transpose(Image.FLIP_TOP_BOTTOM)
            glTexSubImage3D(GL_TEXTURE_2D_ARRAY, 0,
                            0, 0, i, w, h, 1,
                            GL_RGBA, GL_UNSIGNED_BYTE, img.tobytes())

        glGenerateMipmap(GL_TEXTURE_2D_ARRAY)
        glTexParameteri(GL_TEXTURE_2D_ARRAY, GL_TEXTURE_MIN_FILTER,
                        GL_LINEAR_MIPMAP_LINEAR)
        glTexParameteri(GL_TEXTURE_2D_ARRAY, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D_ARRAY, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D_ARRAY, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        glBindTexture(GL_TEXTURE_2D_ARRAY, 0)

        self.width       = w
        self.height      = h
        self.frame_count = n
        print(f"[FlipbookTexture] {os.path.basename(folder)}: "
              f"{n} frames @ {w}x{h}  tex_id={self.tex_id}")

    def cleanup(self):
        if self.tex_id:
            glDeleteTextures(1, [self.tex_id])
            self.tex_id = None


# ---------------------------------------------------------------------------
# Particle system
# ---------------------------------------------------------------------------

# Static per-vertex layout for one particle's 6 verts (2 triangles,
# unindexed).  The shader expands each vertex along the camera's
# right/up axes by `offset * size`.
_CORNER_OFFSETS = np.array([
    [-0.5, -0.5], [ 0.5, -0.5], [ 0.5,  0.5],
    [-0.5, -0.5], [ 0.5,  0.5], [-0.5,  0.5],
], dtype=np.float32)
_CORNER_UVS = np.array([
    [0.0, 0.0], [1.0, 0.0], [1.0, 1.0],
    [0.0, 0.0], [1.0, 1.0], [0.0, 1.0],
], dtype=np.float32)


class ParticleSystem:
    """Camera-facing billboard particles with flipbook animation.

    Attributes (tunables -- safe to mutate at runtime):
        start_size  (float): billboard side length at t=0
        end_size    (float): billboard side length at t=lifetime
        speed       (float): initial velocity magnitude along the
                             emitter's forward vector
        drag        (float): exponential velocity decay rate (1/s);
                             higher = faster slow-down with distance
        lifetime    (float): seconds before a particle is recycled
        spawn_rate  (float): particles spawned per second per emitter
                             (rounded down each frame -- accumulator
                             absorbs the fractional remainder).
    """

    def __init__(self, flipbook, max_particles=512):
        self.flipbook = flipbook
        self.max      = max_particles

        # ---- Per-particle CPU state (numpy for vectorised updates) ----------
        self.pos   = np.zeros((max_particles, 3), dtype=np.float32)
        self.vel   = np.zeros((max_particles, 3), dtype=np.float32)
        self.age   = np.zeros(max_particles,      dtype=np.float32)
        self.alive = np.zeros(max_particles,      dtype=bool)

        # ---- Emitters: list of {'pos': vec3, 'fwd': vec3} ------------------
        self.emitters = []

        # ---- Tunables (also exposed via UI sliders) ------------------------
        self.start_size = 0.10     # m
        self.end_size   = 0.25     # m
        self.speed      = 2.0      # m/s along fwd
        self.drag       = 1.5      # 1/s -- velocity *= exp(-drag * dt)
        self.lifetime   = 2.0      # s
        self.spawn_rate = 60.0     # particles/s/emitter (doubled from 30)
        # Frame-based alpha fade-out (sm flipbook is 91 frames, indices 0..90).
        # Default ramps down between frame 75 and frame 91 so the smoke
        # dissipates over the last ~17 frames of the flipbook.  Persisted
        # in config -- once a value is dialled in, it survives the slider
        # being removed because the shader reads from these attributes.
        self.fade_start_frame = 75.0
        self.fade_end_frame   = 91.0

        self._spawn_accum   = 0.0  # fractional spawn carry-over
        self._emitter_index = 0    # round-robin emitter selection

        # ---- GPU buffer (sized for max alive particles) --------------------
        # 6 verts/particle, 8 floats/vert (pos3 + offset2 + uv2 + age1)
        self._FLOATS_PER_PARTICLE = 6 * 8
        self._VERTS_PER_PARTICLE  = 6
        buf_bytes = max_particles * self._FLOATS_PER_PARTICLE * 4

        self.vao = glGenVertexArrays(1)
        self.vbo = glGenBuffers(1)
        glBindVertexArray(self.vao)
        glBindBuffer(GL_ARRAY_BUFFER, self.vbo)
        glBufferData(GL_ARRAY_BUFFER, buf_bytes, None, GL_DYNAMIC_DRAW)

        stride = 8 * 4   # 8 floats * 4 bytes
        # location 0 -- a_pos (vec3) at byte offset 0
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, stride,
                              ctypes.c_void_p(0))
        glEnableVertexAttribArray(0)
        # location 1 -- a_offset (vec2) at byte offset 12
        glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, stride,
                              ctypes.c_void_p(12))
        glEnableVertexAttribArray(1)
        # location 2 -- a_uv (vec2) at byte offset 20
        glVertexAttribPointer(2, 2, GL_FLOAT, GL_FALSE, stride,
                              ctypes.c_void_p(20))
        glEnableVertexAttribArray(2)
        # location 3 -- a_age (float) at byte offset 28
        glVertexAttribPointer(3, 1, GL_FLOAT, GL_FALSE, stride,
                              ctypes.c_void_p(28))
        glEnableVertexAttribArray(3)
        glBindVertexArray(0)

    # ------------------------------------------------------------------
    def set_emitters(self, emitter_list):
        """Replace the active emitter list.

        Args:
            emitter_list: iterable of dicts {'pos': (3,), 'fwd': (3,)}.
                          Empty list pauses spawning (existing alive
                          particles continue to drift to lifetime).
        """
        self.emitters     = [
            {'pos': np.asarray(e['pos'], dtype=np.float32),
             'fwd': np.asarray(e['fwd'], dtype=np.float32)}
            for e in emitter_list
        ]
        self._spawn_accum   = 0.0
        self._emitter_index = 0

    # ------------------------------------------------------------------
    def reset(self):
        """Kill every alive particle immediately."""
        self.alive[:]      = False
        self._spawn_accum  = 0.0

    # ------------------------------------------------------------------
    def _spawn_one(self, idx, emitter):
        """Initialise particle slot `idx` from `emitter`."""
        # Slight position jitter so the plume doesn't look like a single
        # column of overlapping cards
        pos_jitter = np.random.uniform(-0.02, 0.02, size=3).astype(np.float32)
        # Cone spread on the velocity direction (~10% off-axis)
        spread = np.random.uniform(-0.10, 0.10, size=3).astype(np.float32)
        vel_dir = emitter['fwd'] + spread
        n = float(np.linalg.norm(vel_dir))
        if n > 1e-6:
            vel_dir /= n
        else:
            vel_dir = emitter['fwd']

        self.pos[idx]   = emitter['pos'] + pos_jitter
        self.vel[idx]   = vel_dir * self.speed
        self.age[idx]   = 0.0
        self.alive[idx] = True

    # ------------------------------------------------------------------
    def update(self, dt):
        """Advance the simulation by `dt` seconds: spawn, integrate, cull.

        Vectorised over the alive-mask so per-frame cost is O(n_alive).
        """
        if dt <= 0.0:
            return

        # Age + cull
        self.age[self.alive] += dt
        self.alive &= (self.age < self.lifetime)

        # Apply drag (exponential decay) and integrate position
        if np.any(self.alive):
            decay = float(np.exp(-self.drag * dt))
            self.vel[self.alive] *= decay
            self.pos[self.alive] += self.vel[self.alive] * dt

        # Spawn from emitters (round-robin so each emitter gets a fair share)
        if not self.emitters or self.spawn_rate <= 0.0:
            return
        self._spawn_accum += self.spawn_rate * len(self.emitters) * dt
        n_to_spawn = int(self._spawn_accum)
        self._spawn_accum -= n_to_spawn
        if n_to_spawn <= 0:
            return

        dead_slots = np.where(~self.alive)[0]
        n_spawn    = min(n_to_spawn, len(dead_slots))
        for k in range(n_spawn):
            slot    = dead_slots[k]
            emitter = self.emitters[self._emitter_index % len(self.emitters)]
            self._emitter_index += 1
            self._spawn_one(slot, emitter)

    # ------------------------------------------------------------------
    def render(self, particle_shader, view, projection):
        """Build and draw the alive particles.

        Args:
            particle_shader (ParticleShader): bound by caller is fine; we
                call .use() ourselves to be explicit.
            view, projection (4x4 row-major float32): same matrices used
                by the rest of the scene.
        """
        alive_idx = np.where(self.alive)[0]
        n_alive   = len(alive_idx)
        if n_alive == 0:
            return

        # Build vertex buffer for alive particles (vectorised broadcast)
        # shape: (n_alive, 6, 8) -- 6 verts per particle, 8 floats per vert
        verts = np.empty((n_alive, self._VERTS_PER_PARTICLE, 8),
                         dtype=np.float32)
        verts[:, :, 0:3] = self.pos[alive_idx, np.newaxis, :]
        verts[:, :, 3:5] = _CORNER_OFFSETS
        verts[:, :, 5:7] = _CORNER_UVS
        verts[:, :, 7]   = self.age[alive_idx, np.newaxis]
        flat = verts.reshape(-1)

        # Camera right / up axes in world space (row-major view matrix)
        cam_right = view[0, :3]
        cam_up    = view[1, :3]

        # Upload (sub-fill rather than re-allocating)
        glBindBuffer(GL_ARRAY_BUFFER, self.vbo)
        glBufferSubData(GL_ARRAY_BUFFER, 0, flat.nbytes, flat)

        # ---- Render state for transparent particles -----------------------
        # Standard alpha blend, depth test on but write off (so particles
        # don't occlude each other in z-fight order), backface cull off.
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glDepthMask(GL_FALSE)
        glDisable(GL_CULL_FACE)

        particle_shader.use()
        particle_shader.set_mat4 ('u_view',       view)
        particle_shader.set_mat4 ('u_proj',       projection)
        particle_shader.set_vec3 ('u_cam_right',  float(cam_right[0]),
                                                  float(cam_right[1]),
                                                  float(cam_right[2]))
        particle_shader.set_vec3 ('u_cam_up',     float(cam_up[0]),
                                                  float(cam_up[1]),
                                                  float(cam_up[2]))
        particle_shader.set_float('u_start_size',       self.start_size)
        particle_shader.set_float('u_end_size',         self.end_size)
        particle_shader.set_float('u_lifetime',         self.lifetime)
        particle_shader.set_float('u_num_frames',       float(self.flipbook.frame_count))
        particle_shader.set_float('u_fade_start_frame', self.fade_start_frame)
        particle_shader.set_float('u_fade_end_frame',   self.fade_end_frame)

        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D_ARRAY, self.flipbook.tex_id)
        particle_shader.set_int('u_flipbook', 0)

        glBindVertexArray(self.vao)
        glDrawArrays(GL_TRIANGLES, 0, n_alive * self._VERTS_PER_PARTICLE)
        glBindVertexArray(0)

        # ---- Restore state ------------------------------------------------
        glDepthMask(GL_TRUE)
        glEnable(GL_CULL_FACE)
        glDisable(GL_BLEND)

    # ------------------------------------------------------------------
    def cleanup(self):
        if self.vbo:
            glDeleteBuffers(1, [self.vbo])
            self.vbo = None
        if self.vao:
            glDeleteVertexArrays(1, [self.vao])
            self.vao = None
