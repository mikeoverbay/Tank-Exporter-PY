"""Position-based-dynamics (PBD) track chain.

Coffee 2026-05-11 ("we need a radial resolver for for the force
on the chain..  think of it a directional lock on the wheel..
ZY .. never X").

The chain is N pads riding in 2D chassis-local (Y, Z).  X is
fixed at +/- gauge / 2 per side, never solved -- the chain stays
on its track plane.  Each frame:

  1. Predict positions: `pos += vel * dt + gravity * dt^2`
     (Verlet-style integration; vel implied by `pos - prev_pos`).
  2. Iterate constraint relaxation (4 passes by default):
     a. Distance constraints between adjacent pads -- chain link
        length = `segmentLength`.  Stiff (full correction split
        half-and-half between the two pads each iteration).
     b. Wheel constraints (the "radial resolvers"): for every
        wheel hub at `hub` with radius `R`, any pad with
        `|pos - hub| < R` gets pushed out radially to
        `hub + R * (pos - hub) / |pos - hub|`.  Direction is
        the unit radial vector at the pad -- exactly the
        "directional lock" you described, the wheel only allows
        tangential motion at its rim.
  3. Velocity = `(pos - prev_pos) / dt` implicit.

Uniform wheel treatment: drive sprockets, idlers, road wheels,
return rollers all feed the constraint loop the same way --
they are all "circles the chain can't penetrate".  The solver
doesn't distinguish.

Cold-start protection: caller seeds positions from the homie
geometric chain on the first step so the relaxation doesn't
start from zeros.
"""
import numpy as np


GRAVITY_Y = -9.81   # m/s^2 (chassis-local; chain falls along -Y)


class TrackChainPBD:
    """One side's PBD chain (= 80 pads on T110E4, 102 on Tiger).

    State:
        pos        (N, 2)  current positions in chassis-local (Y, Z).
        prev_pos   (N, 2)  previous-frame positions (Verlet vel).
        n_pads     int     pad count.
        seg_len    float   rest distance between adjacent pads.
        side_x     float   chassis-local X for this side (constant).
        hubs       (W, 2)  wheel hub centres in (Y, Z).
        radii      (W,)    wheel radii in metres.
        wheel_names list[str] for diagnostic logging.

    Tuning constants live as class attributes; subclass / override
    to adjust without forking.  Defaults are tank-agnostic and
    match the geometry scale of WoT tanks (~ 0.1-0.5 m wheels,
    ~ 0.15 m chain pitch).
    """

    N_RELAX_ITERS    = 14
    # Per Coffee 2026-05-11 ("chain_runtime is not smooth ffs"):
    # gravity dropped to a small fraction.  Real-world `9.81`
    # over 60 settle steps was producing 5-10 cm sag between
    # consecutive Tiger road wheels (which sit ~0.5 m apart)
    # -- too soft for a tank chain.  The seed already carries
    # authored sag from the skinned-track ribbon; PBD now runs
    # mostly as a constraint smoother, with a small gravity
    # bias so wheel binds still SLIDE the bound pads to the
    # lowest tangent point on each wheel.
    GRAVITY_SCALE    = 0.10
    DISTANCE_STIFF   = 1.0   # 1.0 = full correction; <1.0 softer
    WHEEL_PUSH_MULT  = 1.0   # 1.0 = exact radial; lower = soft contact
    # Per Coffee 2026-05-11 ("you are applying to the spline and
    # not the segments"): bending stiffness on FREE pads (not
    # bound to a wheel) -- each pad gets pulled toward the
    # midpoint of its two neighbours per iteration.  Resists
    # zig-zag chain shape; consecutive pads then rotate as a
    # smooth chain, not as free 2D particles.  0.0 = no
    # smoothing; 1.0 = flatten to line every iter (over-stiff).
    # 0.20 gives a visibly straight free run without
    # over-constraining sag.
    BEND_STIFF       = 0.50
    # Per Coffee 2026-05-11 ("can we apply tensioners between
    # segments?  max dist?  sag cant cause each one to fall to
    # far..  hanging bridge deck sorta thing?"): long-range
    # distance constraints between non-adjacent pads.  Each
    # pad i is tied to pad i+SKIP_K at the SEED distance --
    # captured at seed time so wheel wraps (chord shorter than
    # K*seg_len) and straight runs (= K*seg_len exactly) both
    # respect their as-authored geometry.  Limits how far ANY
    # section of K consecutive pads can sag: gravity can only
    # pull the chain down until each K-span hits its rest
    # length, then the tensioner pulls back like a bridge
    # cable.  K=4 covers the typical inter-wheel free run
    # without over-stiffening individual links.
    SKIP_K           = 8
    SKIP_STIFF       = 0.30
    MAX_DT           = 0.040 # clamp huge frame deltas (40 ms)

    def __init__(self, side_x, seg_len, n_pads,
                 hubs_yz, wheel_radii, wheel_names=None,
                 wheel_kinds=None):
        self.side_x   = float(side_x)
        self.seg_len  = float(seg_len)
        self.n_pads   = int(n_pads)
        self.hubs     = np.asarray(hubs_yz,   dtype=np.float32
                                    ).reshape(-1, 2)
        self.radii    = np.asarray(wheel_radii, dtype=np.float32
                                    ).reshape(-1)
        self.wheel_names = (list(wheel_names)
                             if wheel_names is not None
                             else [f'wheel_{i}'
                                   for i in range(len(self.hubs))])
        # Per Coffee 2026-05-13 ("Wheels are not planets with
        # gravity. they are objects.  This law only applies to
        # ground wheels and the lower 1/2 of the drive and end
        # idlers"): per-hub kind, parallel to self.hubs.  Used
        # by `seed_from_homie` and the bilateral-bind step to
        # gate the wheel's "pull to rim" by gravity direction.
        #   sprocket / idler : bind only when pad sits on the
        #                      LOWER half of the rim (along the
        #                      current gravity vector).
        #   road / roller    : never bind -- pure solid contact
        #                      (the unilateral push-out keeps the
        #                      chain from penetrating; nothing
        #                      sucks the chain to the rim).
        # Default to 'sprocket' when unspecified so legacy
        # callers (no kinds arg) keep the binding-everywhere
        # behaviour they had before.
        if wheel_kinds is None:
            wheel_kinds = ['sprocket'] * len(self.hubs)
        self.wheel_kinds = list(wheel_kinds)
        # Positions seeded by `seed_from_homie` before first step.
        self.pos      = np.zeros((self.n_pads, 2), dtype=np.float32)
        self.prev_pos = np.zeros((self.n_pads, 2), dtype=np.float32)
        # Per-pad BILATERAL wheel binding -- index into hubs/radii
        # of the wheel this pad is bound to, or -1 for "free"
        # (between-wheel chain segments).  Set at seed time: any
        # pad within (R + BIND_TOL) of a wheel hub at seed time
        # is considered "on the wheel" and gets locked to its
        # rim every step.  Free pads only get the unilateral
        # push-out, so they can sag under gravity until the
        # distance constraints from the bound pads pull them
        # taut.
        self._bound_wheel = np.full(self.n_pads, -1,
                                      dtype=np.int32)
        # Skip-K rest lengths -- the seed distance from pad i to
        # pad i+SKIP_K.  Computed once at `seed_from_homie`
        # time.  Straight runs land at K*seg_len; wheel wraps
        # land at the chord (less).  PBD step pulls each
        # (i, i+K) pair back to this rest length.
        self._skip_rest_len = np.zeros(self.n_pads,
                                         dtype=np.float32)
        self.seeded   = False
        self._prev_dt = 1.0 / 60.0   # default frame time
        # Per Coffee 2026-05-13 ("PBD won't [work] as is.  It's
        # not using the gravity vector"): chassis-local gravity
        # in m/s^2 as a (Y, Z) vector.  Caller updates each step
        # via `set_chassis_gravity` so the chain hangs in WORLD
        # -down regardless of how the chassis is pitched / rolled
        # in the world.  Defaults to the hardcoded chassis-local
        # -Y so existing call sites that never call set_*() keep
        # the old behaviour.
        self._gravity_yz = np.array(
            [GRAVITY_Y, 0.0], dtype=np.float32)

    # ------------------------------------------------------------------
    def seed_from_homie(self, homie_pos_3d):
        """Initialise `pos` and `prev_pos` from the geometric homie
        chain.  Caller passes the homie-chain (N, 3) array; we drop
        the X column (= side_x verbatim) and keep (Y, Z).
        """
        p2 = np.asarray(homie_pos_3d, dtype=np.float32
                          ).reshape(-1, 3)[:, 1:3]
        if len(p2) != self.n_pads:
            # Pad count drift -- accept whatever the caller gave us.
            self.n_pads = len(p2)
            self.pos      = np.zeros((self.n_pads, 2),
                                      dtype=np.float32)
            self.prev_pos = np.zeros((self.n_pads, 2),
                                      dtype=np.float32)
        self.pos[:]      = p2
        self.prev_pos[:] = p2
        # Per Coffee 2026-05-11 ("chain_runtime is not smooth"):
        # bind only pads SQUARELY on a wheel rim, not pads on
        # the inter-wheel tangent line that happen to sit near
        # the rim by geometry.  Tightened to 3 mm.  Homie
        # places ARC pads at distance R exactly; LINE pads
        # adjacent to a wheel sit at sqrt(delta_z^2 + R^2)
        # which is at least a couple cm OUTSIDE the rim for
        # any meaningful delta_z, so 3 mm is generous for arc
        # pads and tight enough to exclude tangent pads.
        BIND_TOL = 0.003
        if len(self.hubs) > 0:
            diff = (self.pos[:, None, :]
                     - self.hubs[None, :, :])         # (N, W, 2)
            r    = np.linalg.norm(diff, axis=2)       # (N, W)
            slack = r - self.radii[None, :]           # >0 = outside
            # Closest wheel per pad, and its slack.
            w_idx = np.argmin(slack, axis=1)          # (N,)
            min_slack = slack[np.arange(self.n_pads), w_idx]
            # Per Coffee 2026-05-13: only sprocket / idler wheels
            # can BIND the chain.  Road wheels + return rollers
            # are pure solid contact -- chain rests against them
            # via the unilateral push-out, never attracted to
            # the rim.  Build a per-wheel mask up-front and
            # AND it into the seed-bind decision.
            kind_allowed = np.array(
                [k in ('sprocket', 'idler')
                 for k in self.wheel_kinds],
                dtype=bool)
            kind_ok = kind_allowed[w_idx]              # (N,)
            self._bound_wheel = np.where(
                (min_slack < BIND_TOL) & kind_ok,
                w_idx, -1).astype(np.int32)
        else:
            self._bound_wheel = np.full(self.n_pads, -1,
                                          dtype=np.int32)
        # Capture skip-K rest lengths (closed loop).
        nxt_k = np.roll(self.pos, -self.SKIP_K, axis=0)
        self._skip_rest_len = np.linalg.norm(
            nxt_k - self.pos, axis=1).astype(np.float32)
        self.seeded      = True

    # ------------------------------------------------------------------
    def set_chassis_gravity(self, gy, gz):
        """Per-frame update of the chassis-local gravity vector
        in m/s^2.  Caller projects world `(0, -9.81, 0)` through
        the inverse of the chassis 3x3 rotation and passes the
        Y / Z components here -- so PBD always pulls the chain
        in the actual world-down direction, even when the
        chassis is pitched / rolled.  `GRAVITY_SCALE` is still
        applied inside `step()`.
        """
        self._gravity_yz = np.array([float(gy), float(gz)],
                                      dtype=np.float32)

    # ------------------------------------------------------------------
    def update_hubs(self, hubs_yz, wheel_radii=None):
        """Refresh the wheel constraint list (suspension moved the
        bones, so the hubs and radii need a new snapshot).  Radii
        usually constant per tank, so the kwarg is optional."""
        self.hubs = np.asarray(hubs_yz, dtype=np.float32
                                ).reshape(-1, 2)
        if wheel_radii is not None:
            self.radii = np.asarray(wheel_radii,
                                      dtype=np.float32
                                      ).reshape(-1)

    # ------------------------------------------------------------------
    def step(self, dt):
        """Advance the chain one physics tick.

        Args:
            dt  : seconds since previous step.  Clamped to
                  `MAX_DT` so a frame stutter doesn't explode the
                  relaxation.

        Returns:
            tuple `(pos_3d, tan_3d)` of `(N, 3)` float32 arrays.
            `pos_3d` is chassis-local (X = side_x, Y, Z).
            `tan_3d` is the chord tangent at each pad (central diff
            with the closed-loop wraparound).  Same shape +
            convention as `track_homie.compute_homie_chain`.
        """
        if not self.seeded:
            # Caller hasn't seeded; bail out with zeros + flat tan.
            n = self.n_pads
            return (np.zeros((n, 3), dtype=np.float32),
                    np.zeros((n, 3), dtype=np.float32))

        dt = float(max(min(dt, self.MAX_DT), 1e-4))

        # ---- 1. Verlet predict ---------------------------------------
        # Implicit velocity from position delta.
        #
        # Per Coffee 2026-05-13 ("remove the gravity component from
        # the equations"): gravity bias dropped from the predict.
        # The chain is now a pure constraint relaxation -- it
        # holds the seed shape and only deforms in response to
        # wheel motion (suspension residuals shift the hubs,
        # bilateral bind drags the bound pads, distance / bending
        # / skip-K propagate the change through the rest of the
        # chain).  The `_gravity_yz` field and
        # `set_chassis_gravity` plumbing are kept intact so we
        # can re-enable the gravity term without touching the
        # call site.
        damping = 0.995  # tiny per-step velocity dissipation so
                         # the chain settles when at rest
        vel = (self.pos - self.prev_pos) * damping
        self.prev_pos[:] = self.pos
        self.pos[:, 0] += vel[:, 0]      # Y axis  (no gravity bias)
        self.pos[:, 1] += vel[:, 1]      # Z axis  (no gravity bias)

        # ---- 2. Constraint relaxation ---------------------------------
        for _ in range(self.N_RELAX_ITERS):
            # Per Coffee 2026-05-13 ("remove wheel attractors"):
            # the bilateral wheel BIND step is gone.  Wheels are
            # solid objects -- they only resist penetration via
            # the unilateral push-out below.  Nothing PULLS the
            # chain onto a rim.  Chain shape is held by the seed
            # positions + distance + skip-K + bending + push-out.
            # `_bound_wheel` and `set_chassis_gravity` are still
            # computed / accepted so re-enabling either is a
            # localised edit.
            active_bound = np.zeros(self.n_pads, dtype=bool)
            free = ~active_bound

            # 2a-bis. Bending: pull each free pad toward the
            # midpoint of its prev / next neighbours.  Actively-
            # bound pads skip this -- their wheel rim already
            # determines their position.  Without bending, the
            # distance constraint alone lets consecutive pads
            # zig-zag freely.
            if self.BEND_STIFF > 0.0:
                prev_p = np.roll(self.pos, +1, axis=0)
                next_p = np.roll(self.pos, -1, axis=0)
                mid    = 0.5 * (prev_p + next_p)
                delta  = mid - self.pos
                self.pos[free] += (
                    self.BEND_STIFF * delta[free]).astype(
                    self.pos.dtype)

            # 2b. Distance: adjacent pads at rest-length seg_len.
            # Roll the array so each pair (i, i+1) is paired with
            # its successor; the same correction goes onto i and
            # opposite onto i+1.  Closed loop: pad N-1 paired with 0.
            nxt = np.roll(self.pos, -1, axis=0)
            d   = nxt - self.pos
            L   = np.linalg.norm(d, axis=1, keepdims=True)
            L   = np.maximum(L, 1e-6)
            err = (L - self.seg_len) * (0.5 * self.DISTANCE_STIFF)
            corr = err * (d / L)
            self.pos += corr
            # Subtract the SAME correction from the next pad by
            # rolling the corr back, so both ends of each spring
            # share the fix half-and-half.
            self.pos -= np.roll(corr, +1, axis=0)

            # 2b-bis. SKIP-K tensioner: each pad i tied to pad
            # i+SKIP_K at its seed distance.  Acts like a bridge
            # cable -- limits how far the chain can sag in any
            # K-span without flattening it (since the rest
            # length IS the authored shape).
            if self.SKIP_K > 0 and self.SKIP_STIFF > 0.0:
                nxt_k = np.roll(self.pos, -self.SKIP_K, axis=0)
                dk    = nxt_k - self.pos
                Lk    = np.linalg.norm(dk, axis=1, keepdims=True)
                Lk    = np.maximum(Lk, 1e-6)
                rest  = self._skip_rest_len[:, None]
                err_k = (Lk - rest) * (0.5 * self.SKIP_STIFF)
                corr_k = err_k * (dk / Lk)
                self.pos += corr_k
                self.pos -= np.roll(corr_k, +self.SKIP_K, axis=0)

            # 2c. Wheel radial UNILATERAL: pads that drift INTO
            # a wheel rim get pushed out.  Applies to FREE pads
            # (= not actively bound this iteration) -- actively-
            # bound pads were already snapped to the rim by 2a.
            # Road / roller pads are NEVER bound (per Coffee
            # 2026-05-13), so this push-out is the ONLY thing
            # that keeps the chain from passing through them;
            # those wheels are solid objects.
            diff = (self.pos[:, None, :]
                     - self.hubs[None, :, :])    # (N, W, 2)
            r    = np.linalg.norm(diff, axis=2)  # (N, W)
            r    = np.maximum(r, 1e-6)
            penetration = self.radii[None, :] - r       # (N, W)
            inside = penetration > 0.0                  # (N, W)
            if inside.any():
                worst_w = np.argmax(penetration * inside,
                                     axis=1)              # (N,)
                pad_idx = np.arange(self.n_pads)
                w_idx   = worst_w
                pen     = penetration[pad_idx, w_idx]
                mask    = (pen > 0.0) & free
                if mask.any():
                    push_dir = (diff[pad_idx, w_idx]
                                / r[pad_idx, w_idx, None])
                    self.pos[mask] += (
                        push_dir[mask]
                        * pen[mask, None]
                        * self.WHEEL_PUSH_MULT)

        # ---- 3. Build output ----------------------------------------
        self._prev_dt = dt
        n = self.n_pads
        pos_3d = np.zeros((n, 3), dtype=np.float32)
        pos_3d[:, 0] = self.side_x
        pos_3d[:, 1] = self.pos[:, 0]
        pos_3d[:, 2] = self.pos[:, 1]

        # Central-chord tangent (same convention the rest of the
        # pipeline expects -- pad-local +Z = -fwd).
        chord = np.roll(pos_3d, -1, axis=0) - np.roll(pos_3d, +1, axis=0)
        cn    = np.linalg.norm(chord, axis=1, keepdims=True)
        tan_3d = chord / np.maximum(cn, 1e-9)
        return pos_3d.astype(np.float32), tan_3d.astype(np.float32)
