"""
Per-impact pool for projectile-hit visual effects.

Mirror of `shot_pool.py`, but on the receiving end: every time a
fired round terminates on terrain / hull / scenery, an `Impact` is
allocated from the pool and stamped with the hit data future
ground-effect renders need:

    * `pos`     -- world-space hit point.
    * `dir_in`  -- unit-length INCOMING direction of the round at
                   the moment of impact (= the projectile's
                   velocity vector, normalised).  Lets decals
                   orient along the strike, and lets dust ejecta
                   spray in the right opposite-of-incoming cone.
    * `normal`  -- unit-length SURFACE NORMAL at the hit point
                   (terrain normal for ground hits, mesh normal
                   for body hits).  Drives decal-quad orientation
                   and dust-cone axis.
    * Phases for each authored effect (dust, crater, scorch).

The pool is INDEPENDENT of `ShotPool`.  Coffee 2026-05-13: "they
are not linked by index.. they are only a pool of effect triggers".
A single shot may produce 0, 1, or N impacts (penetration / spall
/ ricochet); the pool just collects every hit event so the
renderer can iterate one flat list.

Default capacity 50 to match the shot pool's spec.  No GL state
lives here -- this file ships per-impact STATE; rendering is the
caller's job and is deliberately deferred until the structure is
verified.

Author: Coffee + Claude, 2026-05-13.
"""
from __future__ import annotations

import numpy as np


# ----------------------------------------------------------------------------
# Tuning -- per-effect lifetimes in seconds.  Slot auto-frees once the
# LONGEST component (scorch decal) has expired.
#
# FLASH_LIFETIME_S  : sub-second impact flash (sparks / ignition).
# DUST_LIFETIME_S   : ejecta cloud lingers ~1.5 s like the smoke puff.
# SCORCH_LIFETIME_S : crater / scorch decal stays visible for several
#                     seconds before fading.  Caller can override
#                     per-instance for slow-fade or permanent decals.

FLASH_LIFETIME_S  = 0.12
DUST_LIFETIME_S   = 1.50
SCORCH_LIFETIME_S = 6.00


class Impact:
    """One impact event.  Mirrors `Shot`'s shape so the call sites
    feel symmetric, but the payload is hit-side: surface normal +
    incoming direction instead of muzzle direction.

    Public state (only valid while `active` is True):

        pos           : (3,) np.float32 -- hit world position.
        dir_in        : (3,) np.float32 -- unit incoming ray dir
                        (= projectile velocity at impact).
        normal        : (3,) np.float32 -- unit surface normal at
                        the hit point.  Used to orient ground
                        decals so they lay flat on the terrain
                        slope, and to flip the dust cone away from
                        the surface.
        age           : float -- seconds since the hit.
        flash_phase   : 0..1 over FLASH_LIFETIME_S.
        dust_phase    : 0..1 over DUST_LIFETIME_S.
        scorch_phase  : 0..1 over SCORCH_LIFETIME_S.

    `Impact` is a plain data carrier; no GL state, no rendering.
    """

    __slots__ = ("active", "age", "pos", "dir_in", "normal",
                 "flash_phase", "dust_phase", "scorch_phase",
                 "skip_decal")

    # `is_alive` is a read-only alias for `active` so consumer code
    # can self-document early-out checks.  Per Coffee 2026-05-13
    # ("all should early rejection (is alive) logic").
    @property
    def is_alive(self):
        return self.active

    def __init__(self):
        self.active        = False
        self.age           = 0.0
        self.pos           = np.zeros(3, dtype=np.float32)
        # Default incoming direction = +Y (straight down) so an
        # uninitialised slot reads as "from the sky" rather than zero.
        # Renderers shouldn't see this; active==False is the gate.
        self.dir_in        = np.array([0.0, -1.0, 0.0],
                                       dtype=np.float32)
        # Default normal = +Y (flat ground).
        self.normal        = np.array([0.0,  1.0, 0.0],
                                       dtype=np.float32)
        self.flash_phase   = 1.0    # done by default
        self.dust_phase    = 1.0
        self.scorch_phase  = 1.0
        # Per Coffee 2026-05-15 ("remove shellhole birth if we
        # are on the dome when a shell hits"): when True, the
        # shellhole-decal pass skips this impact (still gets
        # the fire / dust explosion billboards via the
        # impact-billboard system; only the persistent ground
        # decal is suppressed).  Default False = decal
        # rendered like every prior impact.
        self.skip_decal    = False

    # ------------------------------------------------------------------
    def hit(self, pos, dir_in, normal, skip_decal=False):
        """Arm this slot with a fresh impact.  Normalises `dir_in`
        and `normal` so the renderer can treat them as unit vectors.

        Args:
            pos:    (3,) world-space hit point.
            dir_in: (3,) projectile velocity at impact (= incoming
                    ray direction).  Length doesn't matter; it gets
                    normalised in place.
            normal: (3,) surface normal at the hit point.  Same
                    normalisation policy.
            skip_decal: when True, the shellhole-decal pass
                    skips this impact entirely (no persistent
                    ground decal).  Fire / dust explosion
                    billboards still fire from the same slot --
                    they're rendered separately off `flash_phase`
                    / `dust_phase`.  Set to True for dome
                    impacts (no ground to paint a shellhole on).
        """
        self.active       = True
        self.age          = 0.0
        self.pos[:]       = np.asarray(pos, dtype=np.float32)
        d = np.asarray(dir_in, dtype=np.float32)
        dn = float(np.linalg.norm(d))
        if dn > 1e-9:
            d = d / dn
        self.dir_in[:]    = d
        n = np.asarray(normal, dtype=np.float32)
        nn = float(np.linalg.norm(n))
        if nn > 1e-9:
            n = n / nn
        self.normal[:]    = n
        self.flash_phase  = 0.0
        self.dust_phase   = 0.0
        self.scorch_phase = 0.0
        self.skip_decal   = bool(skip_decal)

    # ------------------------------------------------------------------
    def update(self, dt):
        """Advance phases by `dt` seconds.  Auto-deactivates when the
        longest-lived component (scorch decal) finishes.  No-op when
        inactive."""
        if not self.active:
            return
        self.age += float(dt)
        self.flash_phase  = min(
            1.0, self.age / max(FLASH_LIFETIME_S, 1e-6))
        self.dust_phase   = min(
            1.0, self.age / max(DUST_LIFETIME_S, 1e-6))
        self.scorch_phase = min(
            1.0, self.age / max(SCORCH_LIFETIME_S, 1e-6))
        # Slot reusable once the scorch decal fades.  Adjust if the
        # caller wants permanent decals (skip the auto-free entirely
        # and harvest them on a different signal).
        if self.scorch_phase >= 1.0:
            self.active = False


class ImpactPool:
    """Fixed-capacity pool of `Impact` slots.

    Coffee 2026-05-13 ("on impact with an object, we want to copy
    their location data and impact angle for future ground
    effects.  we will want the same size I guess.. they are not
    linked by index..  they are only a pool of effect triggers"):

      * Default capacity 50, matching `ShotPool`.  The pool exists
        independently of any particular shot -- one shot can
        produce zero or many impacts (overpenetration, spall,
        ricochet) so the index-linkage of shot-to-impact is not
        useful.
      * `hit(pos, dir_in, normal)` arms the first free slot and
        returns it.  When the pool is full, returns None and
        increments `dropped_count`.
      * `update(dt)` ticks every slot.  Per-shot `active_impacts`
        / `active_count` for renderer / diagnostics.
    """

    def __init__(self, capacity=50):
        self.capacity      = int(capacity)
        self.impacts       = [Impact() for _ in range(self.capacity)]
        self.dropped_count = 0
        # Maintained alive-counter for early rejection per Coffee
        # 2026-05-13 ("all should early rejection (is alive) logic").
        # Bumped in `hit()`, recomputed at the END of `update()`.
        self._alive_count  = 0

    # ------------------------------------------------------------------
    @property
    def has_alive(self):
        """True iff at least one slot is currently active.  Renderer
        / debug overlay early-out on this so idle frames pay just an
        attribute load + a compare."""
        return self._alive_count > 0

    # ------------------------------------------------------------------
    def hit(self, pos, dir_in, normal, skip_decal=False):
        """Arm the first inactive slot with a new impact.  Returns
        the armed `Impact` or None when the pool is full.

        Args:
            pos, dir_in, normal: see `Impact.hit`.
            skip_decal: pass-through to `Impact.skip_decal`; when
                True the shellhole-decal renderer skips this
                impact (used for dome / no-ground impacts where
                the explosion fire / dust effects still fire
                but a persistent decal doesn't make sense).
        """
        for im in self.impacts:
            if not im.active:
                im.hit(pos, dir_in, normal, skip_decal=skip_decal)
                self._alive_count += 1
                return im
        self.dropped_count += 1
        return None

    # ------------------------------------------------------------------
    def update(self, dt):
        """Advance every active slot by `dt` seconds.  Early-rejects
        on the maintained alive-count so idle frames pay just one
        attribute load + a compare."""
        if self._alive_count <= 0:
            return
        n_alive = 0
        for im in self.impacts:
            if not im.active:
                continue   # per-slot is_alive short-circuit
            im.update(dt)
            if im.active:    # may have expired inside `update`
                n_alive += 1
        self._alive_count = n_alive

    # ------------------------------------------------------------------
    @property
    def active_impacts(self):
        """List of active Impact instances.  Skip via `has_alive`
        when there's nothing to render."""
        if self._alive_count <= 0:
            return []
        return [im for im in self.impacts if im.active]

    # ------------------------------------------------------------------
    @property
    def active_count(self):
        """Cheap count -- maintained, no scan."""
        return self._alive_count
