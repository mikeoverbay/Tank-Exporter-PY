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

# Horizontally-mirrored UVs (u' = 1 - u).  Used by AnimatedBillboard
# layers tagged flip_x=True so half the stacked flames at each
# emitter sample the source texture left-flipped, breaking the
# "all four are the same image" symmetry.
_CORNER_UVS_MIRROR_X = np.array([
    [1.0, 0.0], [0.0, 0.0], [0.0, 1.0],
    [1.0, 0.0], [0.0, 1.0], [1.0, 1.0],
], dtype=np.float32)

# Bottom-anchored offsets used by AnimatedBillboard for the burning-
# tank fire sprites.  Y runs 0.0 -> 1.0 instead of -0.5 -> 0.5 so the
# bottom edge of the flame quad sits AT the emitter position
# (HP_Fire_*) and the flame rises upward.  Without this, the centered
# offsets sink the lower half of every flame into the hull.  X stays
# at +/-0.5 so the flame is centered horizontally over the hardpoint.
_CORNER_OFFSETS_BOTTOM = np.array([
    [-0.5, 0.0], [ 0.5, 0.0], [ 0.5, 1.0],
    [-0.5, 0.0], [ 0.5, 1.0], [-0.5, 1.0],
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

    def __init__(self, flipbook, max_particles=512, one_shot_pool=False):
        self.flipbook = flipbook
        self.max      = max_particles
        # Per Coffee 2026-05-14, one_shot_pool=True enables the
        # per-projectile-trail behaviour:
        #
        #   rule 1: `max_particles` IS the single per-shot limit.
        #           Once that many particles have spawned, the emitter
        #           is done -- no time limit, no spawn-rate cutoff,
        #           nothing else; just the budget.  Particles already
        #           alive keep updating + rendering until they all die.
        #   rule 2: never reuse a dead slot.  A monotonic `_next_slot`
        #           cursor walks 0 -> max and never reverses.  Once a
        #           slot has held a particle and that particle dies,
        #           the slot stays dead.  Only `reset_pool` brings
        #           slots back -- called by the viewer when a new
        #           shot rents this system's slot.
        #   rule 3: a particle dies at alpha == 0, not at `lifetime`.
        #           See `_alpha_zero_time`.
        #
        # one_shot_pool=False (default) preserves the legacy slot-reuse
        # path for engine-exhaust / smoke / fire emitters that run
        # continuously.
        self.one_shot_pool = bool(one_shot_pool)
        self._next_slot    = 0   # monotonic spawn cursor (one-shot mode)

        # ---- Per-particle CPU state (numpy for vectorised updates) ----------
        self.pos   = np.zeros((max_particles, 3), dtype=np.float32)
        self.vel   = np.zeros((max_particles, 3), dtype=np.float32)
        self.age   = np.zeros(max_particles,      dtype=np.float32)
        self.alive = np.zeros(max_particles,      dtype=bool)
        # Per Coffee 2026-05-14 ("do one for the emitters location
        # for every birth"): record the emitter's `pos` field at
        # the moment each particle was spawned.  For tracer
        # sweep-spawn this is the bullet's CURRENT position (=
        # end of the swept segment for that frame), shared by all
        # particles spawned in the same frame.  Diagnostic only --
        # not consulted by update / render.
        self.emit_pos = np.zeros((max_particles, 3), dtype=np.float32)
        # Screen-plane rotation (in radians) chosen at spawn time, held
        # constant for the particle's life.  Random uniform 0..2pi by
        # default in _spawn_one -- gives every smoke puff a different
        # starting orientation so the eye doesn't lock onto a single
        # rotation aligning with the camera.  Set rotation_jitter to 0
        # to disable (legacy axis-aligned billboards).
        self.rot   = np.zeros(max_particles,      dtype=np.float32)
        self.rotation_jitter = float(2.0 * np.pi)   # full 0..2pi spread
        # Per-particle world-space position jitter applied on top of
        # the spawn anchor.  Default 0.02 m (= 2 cm cube) softens
        # the plume-as-column look on smoke / engine systems.  Set
        # to 0.0 for tracer trails where each particle should sit
        # exactly on the lerped path point (Coffee 2026-05-14
        # "echo of the shot" diagnosis -- the 2 cm spread was
        # widening the trail into a visible band).
        self.pos_jitter = 0.02

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
        # Per-particle fade-in length, in flipbook frames.  Default
        # 5 matches the legacy spawn-fade smoke systems use to soften
        # particle pop-in.  Set to 0 to disable (= particle is at
        # full alpha from birth) -- correct for tracer trails per
        # Coffee 2026-05-14 ("its aging alpha backwards"): a real
        # tracer is BRIGHTEST at ignition and dims as the burn
        # consumes its material, so the youngest particle (at the
        # bullet's current position) must be brightest and the
        # oldest (at the muzzle end) must be dim.  fade_in inverts
        # that.
        self.fade_in_frames   = 5.0
        # Distance-based spawning (per Coffee 2026-05-14 "we stop
        # following the bullet").  When > 0 AND `one_shot_pool` is
        # True AND the emitter carries a `prev_pos`, the per-frame
        # spawn count is `spawn_per_meter * |cur_pos - prev_pos|`
        # instead of the time-rate.  That makes trail density a
        # property of the bullet's PATH rather than the wall clock,
        # so the trail follows the bullet at any speed and stops
        # only when the slot budget runs out (= trail length cap,
        # not trail duration cap).  Default 0 disables -- legacy
        # smoke / fire / engine emitters keep their time-rate.
        self.spawn_per_meter = 0.0

        self._spawn_accum   = 0.0  # fractional spawn carry-over
        self._emitter_index = 0    # round-robin emitter selection

        # ---- GPU buffer (sized for max alive particles) --------------------
        # 6 verts/particle, 9 floats/vert
        # (pos3 + offset2 + uv2 + age1 + rotation1 = 9).
        self._FLOATS_PER_PARTICLE = 6 * 9
        self._VERTS_PER_PARTICLE  = 6
        buf_bytes = max_particles * self._FLOATS_PER_PARTICLE * 4

        self.vao = glGenVertexArrays(1)
        self.vbo = glGenBuffers(1)
        glBindVertexArray(self.vao)
        glBindBuffer(GL_ARRAY_BUFFER, self.vbo)
        glBufferData(GL_ARRAY_BUFFER, buf_bytes, None, GL_DYNAMIC_DRAW)

        stride = 9 * 4   # 9 floats * 4 bytes
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
        # location 4 -- a_rotation (float) at byte offset 32
        glVertexAttribPointer(4, 1, GL_FLOAT, GL_FALSE, stride,
                              ctypes.c_void_p(32))
        glEnableVertexAttribArray(4)
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
    def update_emitter_positions(self, emitter_list):
        """Refresh emitter pos / fwd in place WITHOUT resetting the
        spawn accumulator or round-robin index.

        Why this exists: `set_emitters` is the canonical "new
        scene / new tank" entry point and resets `_spawn_accum =
        0.0`.  That's correct on tank load.  But the chassis-pose
        follow-the-tank code (`Viewer._update_emitters_for_chassis_
        pose`) calls into the particle system every frame to
        refresh world positions -- using `set_emitters` there
        meant the spawn accumulator got zeroed every frame, so
        the fractional remainder that bridges multiple frames
        was thrown away.  Result: at typical spawn rates (15-30
        emitters/sec, 2 HPs, 60 fps) the per-frame contribution
        is < 1.0, so `floor(accum)` was always 0 -- particles
        spawned sparsely or not at all.  Different frame jitter
        on each emitter made one look "alive" while the other
        looked dead.

        Falls back to `set_emitters` if the count changed (e.g.
        tank reload added / removed an HP) -- in that case we
        DO want a clean reset.
        """
        if len(emitter_list) != len(self.emitters):
            self.set_emitters(emitter_list)
            return
        for em, e in zip(self.emitters, emitter_list):
            em['pos'] = np.asarray(e['pos'], dtype=np.float32)
            em['fwd'] = np.asarray(e['fwd'], dtype=np.float32)

    # ------------------------------------------------------------------
    def reset(self):
        """Kill every alive particle immediately."""
        self.alive[:]      = False
        self._spawn_accum  = 0.0

    # ------------------------------------------------------------------
    def reset_pool(self):
        """Fully repristinate the pool for a new firing cycle.

        Clears every per-particle field (alive flag, age, pos, vel,
        rot), the spawn accumulator, the round-robin emitter index,
        the monotonic `_next_slot` cursor, AND the emitter list.
        After this call the system is indistinguishable from a
        freshly-constructed one and the next `update` starts spawning
        at slot 0 again.

        Per Coffee 2026-05-14 ("reset all particles in the pool.
        remove dead flag for each particle.  reset data to zero.
        age and such"): this is the ONLY way a one-shot pool brings
        dead slots back to life.  The viewer calls it the instant a
        ShotPool slot is rented for a new round.
        """
        self.alive[:]       = False
        self.age[:]         = 0.0
        self.pos[:]         = 0.0
        self.vel[:]         = 0.0
        self.rot[:]         = 0.0
        self.emit_pos[:]    = 0.0
        self._spawn_accum   = 0.0
        self._emitter_index = 0
        self._next_slot     = 0
        self.emitters       = []

    # ------------------------------------------------------------------
    def _alpha_zero_time(self):
        """Age in seconds at which a particle's alpha hits 0.

        Per Coffee 2026-05-14 (rule 3 -- "once it has faded to 0
        alpha. it is now dead.  don't wait for age to trigger it").

        The shader maps `age/lifetime` linearly onto frame index
        `[0, num_frames)`, and `fade_end_frame` is the frame where
        alpha == 0.  Inverting that:

            age_alpha0 = (fade_end_frame / num_frames) * lifetime

        Clamped to `[0, lifetime]` so a misconfigured fade_end_frame
        past `num_frames` can't extend culling past the legacy
        lifetime cap.
        """
        n_frames = max(1.0, float(self.flipbook.frame_count))
        fe = max(0.0, float(self.fade_end_frame))
        t = (fe / n_frames) * float(self.lifetime)
        return min(t, float(self.lifetime))

    # ------------------------------------------------------------------
    def _spawn_one(self, idx, emitter, pos_override=None):
        """Initialise particle slot `idx` from `emitter`.

        `pos_override`, when not None, replaces `emitter['pos']` as
        the spawn anchor.  Used by tracer sweep-spawn to drop the K
        per-frame particles ALONG the segment the round flew this
        frame instead of all at the round's current point.
        """
        # Slight position jitter so the plume doesn't look like a single
        # column of overlapping cards.  Disabled (=0) for tracer
        # trails -- see `self.pos_jitter` docstring.
        if self.pos_jitter > 0.0:
            pos_jitter = np.random.uniform(
                -self.pos_jitter, self.pos_jitter,
                size=3).astype(np.float32)
        else:
            pos_jitter = np.zeros(3, dtype=np.float32)
        # Cone spread on the velocity direction (~10% off-axis)
        spread = np.random.uniform(-0.10, 0.10, size=3).astype(np.float32)
        vel_dir = emitter['fwd'] + spread
        n = float(np.linalg.norm(vel_dir))
        if n > 1e-6:
            vel_dir /= n
        else:
            vel_dir = emitter['fwd']

        anchor = (emitter['pos'] if pos_override is None
                  else pos_override)
        self.pos[idx]   = anchor + pos_jitter
        self.vel[idx]   = vel_dir * self.speed
        self.age[idx]   = 0.0
        self.alive[idx] = True
        # Record the emitter's pos at birth time for diagnostic
        # dumps.  We snapshot emitter['pos'] (NOT the lerp anchor)
        # so a sweep-spawned batch all share the same emit_pos --
        # the bullet position that frame.
        self.emit_pos[idx] = np.asarray(
            emitter['pos'], dtype=np.float32)
        # Random screen-plane rotation chosen ONCE at spawn and held
        # constant for the particle's lifetime.  Each smoke puff
        # therefore appears at a different starting orientation,
        # which breaks up the "every billboard is the same sprite"
        # giveaway without making the particles spin (which would
        # look unnatural for slow-moving smoke).
        if self.rotation_jitter > 0.0:
            self.rot[idx] = float(np.random.uniform(0.0,
                                                     self.rotation_jitter))
        else:
            self.rot[idx] = 0.0

    # ------------------------------------------------------------------
    def update(self, dt):
        """Advance the simulation by `dt` seconds: spawn, integrate, cull.

        Vectorised over the alive-mask so per-frame cost is O(n_alive).
        """
        if dt <= 0.0:
            return

        # Age + cull.  Use alpha-zero rather than `lifetime` so the
        # slot dies the instant the particle is no longer visible
        # (rule 3 -- "don't wait for age to trigger it").  For legacy
        # configs where fade_end_frame == num_frames this is identical
        # to the old `age < lifetime` cull.
        self.age[self.alive] += dt
        self.alive &= (self.age < self._alpha_zero_time())

        # Apply drag (exponential decay) and integrate position
        if np.any(self.alive):
            decay = float(np.exp(-self.drag * dt))
            self.vel[self.alive] *= decay
            self.pos[self.alive] += self.vel[self.alive] * dt

        # Spawn from emitters (round-robin so each emitter gets a fair share)
        if not self.emitters or self.spawn_rate <= 0.0:
            return

        # In one-shot mode the spawn cursor is monotonic; once it
        # reaches `max` the emitter is done (rule 1).  No new spawns
        # ever; the pool just drains via the cull above.  Rule 2 is
        # enforced by the cursor itself never going backwards.
        if self.one_shot_pool and self._next_slot >= self.max:
            return

        # Distance-based spawn (per Coffee 2026-05-14 "we stop
        # following the bullet"): when the trail system has both a
        # `spawn_per_meter` density AND an emitter with `prev_pos`,
        # the per-frame spawn count comes from the segment length
        # rather than wall-clock dt.  Spreads particles uniformly
        # along the swept path regardless of bullet speed, and
        # makes the slot budget a TRAIL LENGTH cap instead of a
        # trail-duration cap.  Falls through to the time-rate when
        # the density is zero or the emitter has no prev_pos (= the
        # legacy smoke / engine emitters).
        em0 = self.emitters[0]
        use_distance = (
            self.one_shot_pool
            and self.spawn_per_meter > 0.0
            and em0.get('prev_pos') is not None
        )
        if use_distance:
            seg = (np.asarray(em0['pos'], dtype=np.float32)
                   - np.asarray(em0['prev_pos'], dtype=np.float32))
            seg_len = float(np.linalg.norm(seg))
            self._spawn_accum += (self.spawn_per_meter * seg_len
                                  * len(self.emitters))
        else:
            self._spawn_accum += (self.spawn_rate * len(self.emitters)
                                  * dt)
        n_to_spawn = int(self._spawn_accum)
        self._spawn_accum -= n_to_spawn
        if n_to_spawn <= 0:
            return

        if self.one_shot_pool:
            remaining = self.max - self._next_slot
            n_spawn   = min(n_to_spawn, remaining)
            # Per Coffee 2026-05-14 ("we need to emit much faster.
            # we may need to calculate time loop and fill in spots
            # in our vector that were missed by the render loop
            # time"): a high-velocity round at 60 FPS jumps several
            # metres per frame, so spawning every per-frame particle
            # at the round's CURRENT position drops a fence of
            # discrete puffs.  Real tracers burn continuously along
            # the trajectory, so we sweep each spawn linearly
            # between `prev_pos` and `pos` -- the segment the round
            # flew through this frame -- filling in the gap.
            # Falls back to the plain emitter['pos'] when prev_pos
            # is missing (single-point emitter, e.g. muzzle).
            for k in range(n_spawn):
                slot    = self._next_slot
                emitter = self.emitters[
                    self._emitter_index % len(self.emitters)]
                self._emitter_index += 1
                prev = emitter.get('prev_pos')
                if prev is not None and n_spawn > 1:
                    # Lerp by (k + 0.5) / n_spawn so the K spawn
                    # points sit at the centres of K equal sub-
                    # intervals along [prev_pos, pos] -- no point
                    # collapses to either endpoint exactly.
                    t = (k + 0.5) / float(n_spawn)
                    pos_override = (
                        prev * (1.0 - t) + emitter['pos'] * t
                    ).astype(np.float32)
                    self._spawn_one(slot, emitter,
                                     pos_override=pos_override)
                else:
                    self._spawn_one(slot, emitter)
                self._next_slot += 1
        else:
            dead_slots = np.where(~self.alive)[0]
            n_spawn    = min(n_to_spawn, len(dead_slots))
            for k in range(n_spawn):
                slot    = dead_slots[k]
                emitter = self.emitters[
                    self._emitter_index % len(self.emitters)]
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
        # shape: (n_alive, 6, 9) -- 6 verts per particle, 9 floats per vert
        # (pos3 + offset2 + uv2 + age1 + rotation1 = 9)
        verts = np.empty((n_alive, self._VERTS_PER_PARTICLE, 9),
                         dtype=np.float32)
        verts[:, :, 0:3] = self.pos[alive_idx, np.newaxis, :]
        verts[:, :, 3:5] = _CORNER_OFFSETS
        verts[:, :, 5:7] = _CORNER_UVS
        verts[:, :, 7]   = self.age[alive_idx, np.newaxis]
        verts[:, :, 8]   = self.rot[alive_idx, np.newaxis]
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
        # Fade-in is configurable per-system.  Default 5 frames
        # softens spawn pop-in for smoke / engine emitters; tracer
        # trails set this to 0 so each particle is at full alpha
        # from birth (a tracer is brightest at ignition).
        particle_shader.set_float('u_fade_in_frames',
                                  float(self.fade_in_frames))

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


# ---------------------------------------------------------------------------
# Animated billboard (one looping flipbook quad per emitter)
# ---------------------------------------------------------------------------

class AnimatedBillboard:
    """Camera-facing animated billboard, ONE quad per emitter.

    Different beast from `ParticleSystem` -- no spawn rate, no drift,
    no per-particle lifecycle.  Each registered emitter gets a
    single permanent quad that loops the flipbook continuously
    while the emitter list is non-empty.  Right tool for things
    that are anchored to a position and just animate in place
    (think: a torch, an idle flame on a knocked-out tank, a
    glowing engine vent).

    Reuses the existing ParticleShader -- the shader's age->frame
    mapping happily covers the looping case.  We feed it a per-
    emitter `age = (current_time + offset) % lifetime` and set
    `lifetime = num_frames / fps` so one full lifetime span equals
    one full flipbook playthrough.

    Per-emitter time offset is randomised at set_emitters() time
    so two adjacent flames don't play perfectly synchronised --
    that visual sync is the giveaway that flames are flipbook
    sprites, not actual fire.

    Tunables (all live; safe to mutate at runtime):
        size       (float): billboard side length in world units
        fps        (float): flipbook playback rate, frames/sec
        loop       (bool):  True (default) keeps the cycle running
                            forever; False stops at the last frame.
        sync_jitter (float): seconds of per-emitter random offset
                             applied at set_emitters time.  0.0 =
                             every emitter starts at frame 0
                             together; large value = fully
                             desynchronised.

    Note: forward / direction vector on each emitter is IGNORED.
    Billboards always face the camera; for a flame, that's the
    right call ("fire goes up and outwards in 2D", we don't try to
    orient the flipbook in 3D).
    """

    def __init__(self, flipbook):
        self.flipbook = flipbook

        # Tunables
        self.size       = 1.5      # m -- single value, no start/end interp
        self.fps        = 30.0     # flipbook playback rate
        self.loop       = True
        self.sync_jitter = 0.7     # seconds of per-emitter offset

        # Each registered HP_Fire emitter is rendered as `layers_per_emitter`
        # overlapping camera-facing quads at the SAME position, each one
        # phase-staggered to a different point in the flipbook loop.
        # Half of them are horizontally mirrored.  This breaks up the
        # tell-tale "this is one repeating sprite" look that single-
        # billboard fire suffers from -- the eye sees four uncorrelated
        # flame shapes blending together instead.
        #
        # `phase_frames`: per-layer starting frame offset.  Default
        # (0, 8, 16, 24) on a 32-frame flipbook = quarter-period
        # staggers.
        # `flip_pattern`: per-layer horizontal mirror flag.  Alternates
        # so adjacent layers don't look identical.
        # `speed_pattern`: per-layer INITIAL playback-rate multiplier
        # on the nominal `fps`.  Used only for the FIRST cycle; each
        # subsequent cycle re-rolls a random fps in [min_fps, max_fps].
        # `min_fps` / `max_fps`: bounds for the per-cycle random
        # playback rate.  At every loop wrap each layer picks a fresh
        # `fps_now = uniform(min_fps, max_fps)` so over time the four
        # layers wander out of step with each other instead of
        # locking into a recognisable rhythm.  Independent of render
        # frame rate -- if the GPU runs at 60 or 144 Hz the per-layer
        # cycle still takes (frame_count / fps_now) real seconds.
        self.layers_per_emitter = 4
        self.phase_frames       = (0, 8, 16, 24)
        self.flip_pattern       = (False, True, False, True)
        self.speed_pattern      = (0.92, 1.05, 0.97, 1.11)
        self.min_fps            = 24.0
        self.max_fps            = 36.0

        # Emitters: list of dicts
        # {'pos': vec3, 'phase': float in [0, 1),
        #  'fps_now': float, 'flip_x': bool}.
        # We don't need 'fwd' (camera-facing) but keep set_emitters
        # API-compatible with ParticleSystem.set_emitters.
        self.emitters = []
        self._rng = None     # lazy-init in set_emitters / update

        # GPU buffer: 6 verts per emitter, same 8-float vertex layout
        # ParticleSystem uses (pos3 + offset2 + uv2 + age1).  Sized
        # to a generous default so resizing is rare; grows on demand
        # in set_emitters if a tank somehow has more HP_Fire points
        # than this cap.
        self._FLOATS_PER_QUAD = 6 * 8
        self._VERTS_PER_QUAD  = 6
        # Capacity counts INTERNAL emitter slots (4 per HP_Fire by
        # default), so 64 slots = 16 HP_Fire emitters at the default
        # 4 layers each.  Real tanks have 1-4 HP_Fire points so this
        # is comfortably oversized; set_emitters resizes if needed.
        self._capacity        = 64
        buf_bytes = self._capacity * self._FLOATS_PER_QUAD * 4

        self.vao = glGenVertexArrays(1)
        self.vbo = glGenBuffers(1)
        glBindVertexArray(self.vao)
        glBindBuffer(GL_ARRAY_BUFFER, self.vbo)
        glBufferData(GL_ARRAY_BUFFER, buf_bytes, None, GL_DYNAMIC_DRAW)

        stride = 8 * 4
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, stride,
                              ctypes.c_void_p(0))
        glEnableVertexAttribArray(0)
        glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, stride,
                              ctypes.c_void_p(12))
        glEnableVertexAttribArray(1)
        glVertexAttribPointer(2, 2, GL_FLOAT, GL_FALSE, stride,
                              ctypes.c_void_p(20))
        glEnableVertexAttribArray(2)
        glVertexAttribPointer(3, 1, GL_FLOAT, GL_FALSE, stride,
                              ctypes.c_void_p(28))
        glEnableVertexAttribArray(3)
        glBindVertexArray(0)

    # ------------------------------------------------------------------
    @property
    def lifetime(self):
        """One flipbook playthrough takes (frames / fps) seconds.
        Computed from `self.fps` so changing fps live updates the
        loop period without further bookkeeping."""
        n = max(1.0, float(self.flipbook.frame_count))
        return n / max(0.1, float(self.fps))

    # ------------------------------------------------------------------
    def set_emitters(self, emitter_list):
        """Replace the active emitter list.  Each entry must be a
        dict with at least a 'pos' key (3-tuple or array).  'fwd'
        is accepted for API symmetry with ParticleSystem.set_emitters
        but ignored (billboards face the camera).

        Each input HP_Fire emitter is EXPANDED into
        `self.layers_per_emitter` overlapping internal billboards
        at the same position, each with:
          - a phase offset = (phase_frame_N / fps)  -- so layer 0
            starts at flipbook frame 0, layer 1 at frame 8, etc.
          - a horizontal-flip flag from `flip_pattern`.
        Plus a small RANDOM jitter on top of the phase offset
        (capped by `sync_jitter`) so two HP_Fire points that share
        the same layer index don't lockstep.
        """
        self._rng = np.random.default_rng()
        new_emitters = []

        n_layers = max(1, int(self.layers_per_emitter))
        fps      = max(0.1, float(self.fps))
        n_frames = max(1, int(self.flipbook.frame_count))
        phases   = list(self.phase_frames) or [0]
        flips    = list(self.flip_pattern) or [False]
        speeds   = list(self.speed_pattern) or [1.0]

        # Per-layer phase is normalised to [0, 1) -- the fraction of
        # the flipbook played so far.  Each frame we advance phase by
        # (dt * fps_now / frame_count); on wrap we roll a new
        # fps_now in [min_fps, max_fps].
        for e in emitter_list:
            pos = np.asarray(e['pos'], dtype=np.float32)
            # ONE random base phase per HP_Fire emitter so two HPs on
            # the same tank don't lockstep.  Their per-layer offsets
            # then add the planned 0/8/16/24-frame stagger on top.
            # Expressed as a fraction of the flipbook (i.e., a phase
            # in [0, 1)).
            hp_phase_base = float(self._rng.uniform(0.0, 1.0))
            for k in range(n_layers):
                phase_frame = phases[k % len(phases)]
                flip_x      = bool(flips[k % len(flips)])
                speed       = float(speeds[k % len(speeds)])
                # Convert frame-index stagger to phase fraction.
                phase_offset = (hp_phase_base
                                + float(phase_frame) / n_frames) % 1.0
                # First-cycle fps from the speed_pattern; subsequent
                # cycles roll in update().
                fps_now = fps * speed
                new_emitters.append({
                    'pos':     pos,
                    'phase':   phase_offset,
                    'fps_now': fps_now,
                    'flip_x':  flip_x,
                })

        self.emitters = new_emitters

        # Grow the GPU buffer if we hit the soft cap.  Rare in
        # practice -- 4 layers x 4 HP_Fire = 16 internal slots,
        # comfortably under the 64-slot default.
        if len(new_emitters) > self._capacity:
            new_cap = max(self._capacity * 2, len(new_emitters) + 4)
            glBindBuffer(GL_ARRAY_BUFFER, self.vbo)
            glBufferData(GL_ARRAY_BUFFER,
                         new_cap * self._FLOATS_PER_QUAD * 4,
                         None, GL_DYNAMIC_DRAW)
            self._capacity = new_cap

    # ------------------------------------------------------------------
    def reset(self):
        """Stop animating and clear the emitter list."""
        self.emitters = []

    # ------------------------------------------------------------------
    def update_emitter_positions(self, emitter_list):
        """Refresh per-emitter pos in place WITHOUT re-rolling the
        per-layer RNG phases.

        Same purpose as `ParticleSystem.update_emitter_positions`:
        the per-frame chassis-pose tracker calls into the
        billboard system to refresh world coordinates, but the
        full `set_emitters` path RNG-rolls a fresh phase for
        every internal layer on every call.  Calling that per
        frame meant the flipbook never advanced coherently --
        each frame restarted the animation at a random point.

        Each input emitter expanded to `layers_per_emitter`
        internal billboards (matching the layout in
        `set_emitters`).  We update those internal billboards'
        positions in lockstep -- N input emitters * K layers
        each maps to indices `[i*K : (i+1)*K]` in the internal
        list.  Falls back to `set_emitters` if the count or
        layer geometry has changed.
        """
        n_layers = max(1, int(self.layers_per_emitter))
        if len(self.emitters) != len(emitter_list) * n_layers:
            self.set_emitters(emitter_list)
            return
        for i, e in enumerate(emitter_list):
            pos = np.asarray(e['pos'], dtype=np.float32)
            for k in range(n_layers):
                self.emitters[i * n_layers + k]['pos'] = pos

    # ------------------------------------------------------------------
    def update(self, dt):
        """Advance each layer's normalised phase ∈ [0, 1).

        Per layer:
          * phase advances by `dt * fps_now / frame_count` so the
            layer plays at its own real-time fps regardless of
            render rate.
          * On wrap (phase >= 1), subtract 1 and roll a fresh
            `fps_now` in [min_fps, max_fps].  Each cycle gets a
            new random playback speed -- the four overlapping
            layers continually drift out of phase / out of rhythm.

        `_time` is no longer used; phase is the single source of
        truth and is bounded to [0, 1) so we never accumulate
        floating-point error.  `loop=False` mode caps phase at
        just-below-1 instead of wrapping.
        """
        if dt <= 0.0 or not self.emitters:
            return
        n_frames = max(1, int(self.flipbook.frame_count))
        if self._rng is None:
            self._rng = np.random.default_rng()

        for em in self.emitters:
            # phase is fraction of one flipbook playthrough
            inc = dt * em['fps_now'] / n_frames
            new_phase = em['phase'] + inc
            if self.loop:
                while new_phase >= 1.0:
                    new_phase -= 1.0
                    # Roll a fresh playback rate for the next cycle.
                    em['fps_now'] = float(self._rng.uniform(
                        self.min_fps, self.max_fps))
            else:
                if new_phase >= 1.0:
                    new_phase = 0.999999    # park at last frame
            em['phase'] = new_phase

    # ------------------------------------------------------------------
    def render(self, particle_shader, view, projection):
        """Build and draw one quad per emitter.  Reuses the same
        ParticleShader the smoke system uses; differences live in
        the uniforms and the vertex stream we feed in."""
        if not self.emitters:
            return

        n = len(self.emitters)
        verts = np.empty((n, self._VERTS_PER_QUAD, 8), dtype=np.float32)

        # Per-layer "age" passed to the shader is `phase * lifetime`
        # (where lifetime is the NOMINAL fps's frame_count/fps).
        # The shader does t = age/lifetime, frame = floor(t *
        # num_frames) -- so passing phase * nominal_lifetime makes
        # t == phase, picking the correct frame for any per-layer
        # playback speed without the shader needing to know about it.
        L = self.lifetime
        for i, em in enumerate(self.emitters):
            age = em['phase'] * L
            verts[i, :, 0:3] = em['pos']
            # Bottom-anchored offsets: emitter pos = bottom-center of
            # the flame quad rather than its centroid.  See
            # _CORNER_OFFSETS_BOTTOM for why.
            verts[i, :, 3:5] = _CORNER_OFFSETS_BOTTOM
            verts[i, :, 5:7] = (_CORNER_UVS_MIRROR_X
                                if em.get('flip_x') else _CORNER_UVS)
            verts[i, :, 7]   = age

        flat = verts.reshape(-1)

        # Camera right / up axes in world space (row-major view matrix)
        cam_right = view[0, :3]
        cam_up    = view[1, :3]

        # Upload + draw
        glBindBuffer(GL_ARRAY_BUFFER, self.vbo)
        glBufferSubData(GL_ARRAY_BUFFER, 0, flat.nbytes, flat)

        # Same render state ParticleSystem uses: alpha blend, depth
        # test on but write off, no backface cull.
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
        # Same size on both ends -- billboards don't grow over a
        # lifetime the way particles do.
        particle_shader.set_float('u_start_size',       self.size)
        particle_shader.set_float('u_end_size',         self.size)
        particle_shader.set_float('u_lifetime',         L)
        particle_shader.set_float('u_num_frames',
                                   float(self.flipbook.frame_count))
        # No fade for a continuous flame -- pin both fade endpoints
        # past the last frame so the shader's fade_out term stays at
        # 1.0 across the full loop.  (The fade-in over the first
        # ~5 frames in the shader is harmless: it only matters if
        # you happen to land near frame 0 of the loop, and even
        # there the smoothstep mostly resolves to 1.0 quickly.)
        n_frames = float(self.flipbook.frame_count)
        particle_shader.set_float('u_fade_start_frame', n_frames + 1.0)
        particle_shader.set_float('u_fade_end_frame',   n_frames + 2.0)
        # No fade-in for billboard mode -- this is a continuous movie
        # clip, every loop should snap straight back to frame 0 at
        # full opacity.  Fade-in over the first 5 frames would make
        # the flame disappear briefly at every loop boundary.
        particle_shader.set_float('u_fade_in_frames',   0.0)

        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D_ARRAY, self.flipbook.tex_id)
        particle_shader.set_int('u_flipbook', 0)

        glBindVertexArray(self.vao)
        glDrawArrays(GL_TRIANGLES, 0, n * self._VERTS_PER_QUAD)
        glBindVertexArray(0)

        # Restore state
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
