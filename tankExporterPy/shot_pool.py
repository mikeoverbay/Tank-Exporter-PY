"""
Per-shot pool for gun-fire visual effects.

Each `Shot` is a single firing event that owns three short-lived
visual components:

    1. Muzzle-flash billboard burst   (= a one-shot copy of the
                                        fire-billboard flipbook;
                                        fires once, expires, then
                                        the slot is reusable).
    2. Muzzle-smoke puff               (= a one-shot copy of the
                                        smoke-particle emitter,
                                        same one-shot semantics).
    3. Point light source              (= a fading light dropped at
                                        the muzzle for the
                                        sub-second flash period).

`ShotPool` holds a fixed array of N (default 50) Shot slots.  When
the gun fires, `fire(pos, fwd)` grabs the first inactive slot and
arms it.  Every frame `update(dt)` advances each active shot's age;
the slot auto-frees when its longest component (the smoke puff)
finishes.

Rendering is NOT wired here.  This file ships the per-frame STATE
the renderer will read -- positions, ages, normalised phases, light
intensities.  Caller draws the flash + smoke + light each frame
from this state, then re-uses the slot once `active` flips False.

Per Coffee 2026-05-13 ("copy the fire emitter and rename it...
don't repeat.. fire and done.. reusable in an array.  I want up
to 50 shots in a row").

Author: Coffee + Claude, 2026-05-13.
"""
from __future__ import annotations

import numpy as np


# ----------------------------------------------------------------------------
# Tuning -- per-component lifetimes in seconds.  Total slot lifetime is the
# MAX of these three because the slot can only be reused after the longest
# component has fully expired.  Smoke usually wins.
#
# FLASH_LIFETIME_S       : muzzle-flash billboard playback (one flipbook cycle).
# SMOKE_LIFETIME_S       : how long the smoke puff lingers after firing.
# LIGHT_LIFETIME_S       : how long the point-light source stays bright.
# LIGHT_INITIAL_INTENSITY: peak light strength at t=0; falls linearly to 0.
# PROJECTILE_SPEED_MPS_DEFAULT : fallback speed when the gun XML
#                                didn't ship a `<speed>` for its shell.
#                                Real WoT shells are 700-1500 m/s; we
#                                fall back well below that so a missing
#                                XML doesn't fire a sniper round.

FLASH_LIFETIME_S            = 0.20     # ~200 ms muzzle flash
SMOKE_LIFETIME_S            = 1.50     # 1.5 s smoke fade
LIGHT_LIFETIME_S            = 0.15     # very brief flash light
LIGHT_INITIAL_INTENSITY     = 1.0      # 0..1; renderer scales as needed
PROJECTILE_SPEED_MPS_DEFAULT = 600.0   # used when caller didn't pass a speed

# Per-shot maximum particles the trail emitter is allowed to spawn.
# Coffee 2026-05-14: "emitter is lock to rounds position and emits
# until max particle count is used".  Once a shot has been counted as
# the source of `TRAIL_MAX_PARTICLES_PER_SHOT` births, the renderer
# stops including its position in the emitter list.  Existing
# particles continue to live their own lifetimes; only NEW spawns
# from this shot stop.
TRAIL_MAX_PARTICLES_PER_SHOT = 40


class Shot:
    """One firing event.  Allocated from the pool, armed by `fire`,
    advanced by `update`, auto-frees when every component is done.

    Public state the renderer reads each frame (only meaningful
    when `active` is True):

        pos              : (3,) np.float32 -- muzzle world position
                           the moment the shot was armed.  Static for
                           the shot's lifetime -- the gun moves on,
                           but the flash + smoke stay at the muzzle
                           that produced them.
        fwd              : (3,) np.float32 -- gun forward direction
                           at fire time (used to billboard / orient
                           the flash and to give smoke an initial
                           ejection direction).
        age              : float -- seconds since fire().
        flash_phase      : 0..1 -- normalised flipbook position for
                           the muzzle flash; >= 1.0 means done.
        smoke_phase      : 0..1 -- same for the smoke puff.
        light_intensity  : 0..LIGHT_INITIAL_INTENSITY -- linear decay
                           over LIGHT_LIFETIME_S.

    `Shot` is a plain data carrier; no GL state, no rendering.
    """

    __slots__ = ("active", "age", "pos", "fwd",
                 "flash_phase", "smoke_phase", "light_intensity",
                 # Projectile state (Coffee 2026-05-13 "fire red
                 # spheres at the terrain ... test we can shoot and
                 # stop a round"):
                 #   cur_pos          -- ball position right now in world.
                 #   target_pos       -- where the shot is heading
                 #                       (terrain hit captured at fire time).
                 #   projectile_alive -- True while the round is in flight;
                 #                       flips False on impact, hit pos
                 #                       stashes into `impact_pos`.
                 #   impact_pos       -- world coords of the hit.  None
                 #                       until projectile_alive flips.
                 "cur_pos", "target_pos", "projectile_alive",
                 "impact_pos", "velocity_mps",
                 # Per Coffee 2026-05-14 (tracer sweep-spawn):
                 # position the round occupied at the START of the
                 # current physics step.  `cur_pos - prev_pos` is the
                 # segment the round flew through this frame -- the
                 # render loop only saw a single point at cur_pos,
                 # so the trail spawner distributes its per-frame
                 # particle quota along [prev_pos, cur_pos] to fill
                 # in the gap a real tracer's burn would have made
                 # luminous.
                 "prev_pos",
                 # Per Coffee 2026-05-14 ("if a bullet hits something
                 # it stops emitting but the slot isn't open again
                 # until all tracer smoke has died"): bool flag set
                 # by the viewer's trail loop each frame from its
                 # per-slot ParticleSystem `alive.any()`.  Shot.update
                 # refuses to deactivate the slot while this is True
                 # so the slot stays rented past the smoke-puff
                 # lifetime until every tracer particle has aged
                 # past alpha-zero.
                 "trail_alive",
                 # Per Coffee 2026-05-14 ("add to output total active
                 # particles at impact of round"): one-shot guard so
                 # the impact-frame stats line is emitted exactly
                 # once per shot.  Set True by the viewer when the
                 # round transitions from in-flight to impacted and
                 # the log line is written.  Reset on each fire.
                 "impact_logged",
                 # Per-shot particle emission counter.  Bumped by the
                 # renderer once per frame while the shot is in the
                 # trail emitter list; capped at
                 # TRAIL_MAX_PARTICLES_PER_SHOT.  Coffee 2026-05-14
                 # "emits until max particle count is used".
                 "trail_particles_emitted")

    # `is_alive` is a read-only alias for `active` so consumer code
    # (renderer, debug overlay, etc.) can self-document early-out
    # checks: `if shot.is_alive: render(shot)`.  Per Coffee
    # 2026-05-13 ("all should early rejection (is alive) logic").
    @property
    def is_alive(self):
        return self.active

    def __init__(self):
        self.active           = False
        self.age              = 0.0
        self.pos              = np.zeros(3, dtype=np.float32)
        self.fwd              = np.array([0.0, 0.0, -1.0],
                                          dtype=np.float32)
        self.flash_phase      = 1.0    # done by default
        self.smoke_phase      = 1.0
        self.light_intensity  = 0.0
        # Projectile state.
        self.cur_pos          = np.zeros(3, dtype=np.float32)
        self.prev_pos         = np.zeros(3, dtype=np.float32)
        self.target_pos       = np.zeros(3, dtype=np.float32)
        self.projectile_alive = False
        self.impact_pos       = None
        self.velocity_mps     = PROJECTILE_SPEED_MPS_DEFAULT
        self.trail_particles_emitted = 0
        self.trail_alive      = False
        self.impact_logged    = False

    # ------------------------------------------------------------------
    def fire(self, pos, fwd, target_pos=None,
             velocity_mps=PROJECTILE_SPEED_MPS_DEFAULT):
        """Arm this shot from an inactive state.

        Args:
            pos:           (3,) world-space muzzle position.
            fwd:           (3,) gun forward direction at fire time.
                           Normalised internally.
            target_pos:    (3,) world-space hit point captured at
                           fire time (= ball position).  When None
                           the projectile flies along `fwd` to
                           infinity; when finite the round halts
                           there and stamps `impact_pos`.
            velocity_mps:  flight speed in m/s.  Per Coffee
                           2026-05-13 ("get info on rounds velocity
                           and wire in"), the caller pulls this from
                           the gun's shell XML and may scale it for
                           visualisation.  Defaults to the module's
                           PROJECTILE_SPEED_MPS_DEFAULT.
        """
        self.active         = True
        self.age            = 0.0
        self.pos[:]         = np.asarray(pos, dtype=np.float32)
        f                   = np.asarray(fwd, dtype=np.float32)
        n                   = float(np.linalg.norm(f))
        if n > 1e-9:
            f = f / n
        self.fwd[:]         = f
        self.flash_phase    = 0.0
        self.smoke_phase    = 0.0
        self.light_intensity = LIGHT_INITIAL_INTENSITY
        # Projectile state -- start at the muzzle, head toward target.
        # prev_pos starts at the muzzle too; the first frame's
        # sweep-spawn segment [prev_pos, cur_pos] collapses to a
        # single point at the muzzle, which is the right look
        # (initial particles cluster at the gun, then spread as
        # the round leaves).
        self.cur_pos[:]     = self.pos
        self.prev_pos[:]    = self.pos
        if target_pos is not None:
            self.target_pos[:]    = np.asarray(target_pos,
                                                dtype=np.float32)
            self.projectile_alive = True
        else:
            self.target_pos[:]    = self.pos + self.fwd * 1e6
            self.projectile_alive = True
        self.impact_pos       = None
        self.velocity_mps     = max(0.1, float(velocity_mps))
        # Reset trail emission counter at fire time so the slot can
        # be re-used and start fresh.
        self.trail_particles_emitted = 0
        # Trail starts empty; the viewer raises this within a frame
        # or two once the dedicated ParticleSystem starts spawning.
        self.trail_alive   = False
        # Re-arm the one-shot impact log so this fresh round gets
        # its own at-impact stats line.
        self.impact_logged = False

    # ------------------------------------------------------------------
    def update(self, dt):
        """Advance phases AND projectile position by `dt` seconds.
        Auto-deactivates when the longest-lived component (smoke)
        finishes.  No-op when inactive."""
        if not self.active:
            return
        dt = float(dt)
        self.age += dt
        # Phase 0..1 across each component's own lifetime.  Clamped
        # at 1.0 so the renderer can read the last frame indefinitely
        # without overrunning the flipbook count.
        self.flash_phase = min(
            1.0, self.age / max(FLASH_LIFETIME_S, 1e-6))
        self.smoke_phase = min(
            1.0, self.age / max(SMOKE_LIFETIME_S, 1e-6))
        # Light decays linearly over LIGHT_LIFETIME_S then stays 0.
        if self.age <= LIGHT_LIFETIME_S:
            t = self.age / max(LIGHT_LIFETIME_S, 1e-6)
            self.light_intensity = (
                LIGHT_INITIAL_INTENSITY * (1.0 - t))
        else:
            self.light_intensity = 0.0

        # Projectile advance.  Walk along fwd at PROJECTILE_SPEED_MPS,
        # stop when the cumulative travel exceeds the muzzle->target
        # distance.  Linear motion (no ballistic arc) -- this is the
        # debug-sphere test; ballistic comes later if needed.
        if self.projectile_alive:
            # Stash the pre-step position so the renderer's tracer
            # spawner can sweep particles across [prev, cur] this
            # frame.  Done BEFORE the integration step so prev_pos
            # is genuinely the start-of-step location.
            self.prev_pos[:] = self.cur_pos
            step = dt * self.velocity_mps
            self.cur_pos[:] = self.cur_pos + self.fwd * step
            # Project the cur->target vector onto fwd to see if we've
            # passed the target along the firing direction.  Robust
            # to the "target was below muzzle and we overshot" case
            # better than a plain distance compare.
            to_target = self.target_pos - self.cur_pos
            if float(np.dot(to_target, self.fwd)) <= 0.0:
                # Snap to target on the impact frame so the visible
                # sphere doesn't sit a fraction past the hit point.
                self.cur_pos[:]      = self.target_pos
                self.projectile_alive = False
                self.impact_pos       = self.target_pos.copy()

        # Slot is reusable once every visual component is done.
        # Smoke-puff phase is one gate; the tracer-trail particle
        # system is the other (Coffee 2026-05-14 "the slot isn't
        # open again until all tracer smoke has died").  The slot
        # only frees when BOTH the original muzzle-smoke phase has
        # completed AND the dedicated trail PS has fully drained
        # (trail_alive flipped False by the viewer).
        if self.smoke_phase >= 1.0 and not self.trail_alive:
            self.active = False
            self.projectile_alive = False


class ShotPool:
    """Fixed-capacity pool of Shot slots.

    Capacity defaults to 50 per Coffee's spec ("I want up to 50
    shots in a row").  All slots are pre-allocated in `__init__`
    so steady-state firing produces no Python-level allocs --
    `fire()` finds an inactive slot and re-uses it.

    When every slot is busy, `fire()` returns None and the caller
    can decide whether to drop the shot, oldest-recycle, or warn.
    Default behaviour: drop silently and increment `dropped_count`
    so the user can see how many shots their pool size missed.
    """

    def __init__(self, capacity=50):
        self.capacity      = int(capacity)
        self.shots         = [Shot() for _ in range(self.capacity)]
        self.dropped_count = 0
        # Maintained-counter early-reject per Coffee 2026-05-13
        # ("all should early rejection (is alive) logic").  Bumped
        # in `fire()`, recomputed once at the END of `update()`.
        # Consumers check `has_alive` before iterating to skip GL
        # state setup + per-effect work entirely on idle frames.
        self._alive_count  = 0

    # ------------------------------------------------------------------
    @property
    def has_alive(self):
        """True iff at least one slot is currently active.
        Renderer / debug overlay early-out on this -- skip the whole
        pass when the pool is idle."""
        return self._alive_count > 0

    # ------------------------------------------------------------------
    def fire(self, pos, fwd, target_pos=None,
             velocity_mps=PROJECTILE_SPEED_MPS_DEFAULT):
        """Arm the first inactive slot.  Returns the armed Shot or
        None when the pool is full.

        `target_pos` is forwarded to `Shot.fire`; when finite the
        projectile stops there and stamps `impact_pos`.
        `velocity_mps` drives the per-frame travel distance.
        """
        for s in self.shots:
            if not s.active:
                s.fire(pos, fwd, target_pos=target_pos,
                       velocity_mps=velocity_mps)
                self._alive_count += 1
                return s
        self.dropped_count += 1
        return None

    # ------------------------------------------------------------------
    def update(self, dt):
        """Advance every active slot by `dt` seconds.  Early-rejects
        on the maintained alive-count so idle frames pay just one
        attribute load + a compare.
        """
        if self._alive_count <= 0:
            return
        n_alive = 0
        for s in self.shots:
            if not s.active:
                continue   # per-slot is_alive short-circuit
            s.update(dt)
            if s.active:    # may have expired inside `update`
                n_alive += 1
        self._alive_count = n_alive

    # ------------------------------------------------------------------
    @property
    def active_shots(self):
        """List of active Shot instances.  Builds a fresh list each
        call -- skip via `has_alive` when there's nothing to render."""
        if self._alive_count <= 0:
            return []
        return [s for s in self.shots if s.active]

    # ------------------------------------------------------------------
    @property
    def active_count(self):
        """Cheap count -- maintained, no scan."""
        return self._alive_count
