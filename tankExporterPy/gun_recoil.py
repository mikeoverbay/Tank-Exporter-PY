"""
Gun recoil — per-frame skinning offset for the gun's recoil bone.

Standalone module per the 1.119.0 lock on `tank_physics.py`: recoil
state lives here, not in TankPhysics.  Same uniform interface as
`TankPhysics.bone_matrix_array(palette)` so the viewer's existing
`_upload_skinning` path can swap which provider it queries when
the mesh's `component == 'gun'`.

Triggered by SPACE in `Viewer.handle_input`.  One firing cycle:

    t=0          : SPACE pressed -> phase = OUT, offset starts at 0
    t = OUT_S    : barrel fully back (offset = MAX_TRAVEL_M)
    +DWELL_S     : held at full travel
    +RETURN_S    : critically-damped return to 0
    return phase : IDLE (offset = 0, awaiting next SPACE)

Coordinate convention: the recoil bone slides BACKWARD along its
mesh-local +Z axis (= the chassis convention where the gun
points along -Z in chassis-local space, so the recoil moves the
barrel slot +Z back into the turret).  If a specific tank's gun
authors recoil along a different axis, swap the index in
`_recoil_translation` (one site, single integer change).

Author: Coffee + Claude, 2026-05-13.
"""
from __future__ import annotations

import math
import re

import numpy as np


# --- Tuning ---------------------------------------------------------------
# Values rounded to "feels right for a tank gun" defaults.  Real 152mm
# guns recoil ~0.6-0.9m with ~80ms out + ~250-400ms return.  We pick
# 0.40m / 60ms / 300ms which reads as a snappy recoil at 60 fps without
# the dwell making the gun look stuck.
MAX_TRAVEL_M = 0.40       # how far back the barrel slides
OUT_TIME_S   = 0.06       # back-stroke duration
DWELL_S      = 0.04       # held at full travel
RETURN_TIME_S = 0.30      # forward return (critically-damped easing)

# Which mesh-local axis the recoil slides along.  WoT chassis convention
# is gun-points-along-(-Z), so +Z is backward.  Flip to 0 (X) or 1 (Y) if
# the gun on this tank is authored differently and SPACE doesn't visibly
# move the barrel in the expected direction.
RECOIL_AXIS = 2           # 0=X, 1=Y, 2=Z


# Phase codes used by the small state machine.
PHASE_IDLE   = 0
PHASE_OUT    = 1
PHASE_DWELL  = 2
PHASE_RETURN = 3


# Heuristics for picking the recoil bone out of a gun palette.
#
# Per Coffee 2026-05-13 ("parts that animate backwards.. if of the
# III values, bone weight's that are = t0 3 recoil"): the earlier
# `G_*` fallback was matching `G_BlendBone`, which is the gun ROOT
# (every gun vertex is weighted to it).  Triggering recoil on
# G_BlendBone slid the whole gun back -- including mantlet, breech,
# anything bolted to the chassis-side of the recoil cylinder.
# Wrong: a real recoil bone is a CHILD bone that only the barrel
# verts are weighted to.
#
# Patterns now only fire on bones whose name semantically claims to
# be a recoil / barrel mover.  When NONE of these match, the picker
# returns None and the entire gun stays rigid -- safer than picking
# the root.  In that case we want the user to tell us the correct
# bone name (printed by Viewer at tank load so it's visible).
_BONE_NAME_SCORES = (
    (re.compile(r'recoil',          re.I), 100),  # explicit recoil bone
    (re.compile(r'barrel',          re.I),  80),
    (re.compile(r'gun_?recoil',     re.I), 100),
    (re.compile(r'gun_?barrel',     re.I),  90),
    (re.compile(r'^G_?Barrel',      re.I),  85),
    (re.compile(r'^G_?Recoil',      re.I),  95),
    # BigWorld / WoT chassis-style naming: the recoil-driven bone is
    # `Gun_BlendBone` (capital-G-u-n underscore), distinct from the
    # gun ROOT bone which is `G_BlendBone` (one letter only).
    # Confirmed on Tiger I (G04_PzVI) -- palette
    # ['G_BlendBone', 'Gun_BlendBone'].  Score above the generic
    # 'gun' word to win against false positives in turret meshes
    # (which can carry `gunMantlet` or similar in their palettes).
    (re.compile(r'^Gun_',                ),  95),
    (re.compile(r'_GunFire',        re.I),  60),  # last-resort fire-marker bone
)


# Override hatch.  Coffee can hardcode the exact bone name for a
# specific gun here when the heuristic doesn't catch it; the picker
# tries this first and short-circuits on a match.  Empty by default.
# Example for a hypothetical:
#     RECOIL_BONE_OVERRIDE = 'G_Recoil_BlendBone'
RECOIL_BONE_OVERRIDE = ''


def pick_recoil_bone(palette):
    """Return the index of the recoil bone in `palette`.

    Coffee 2026-05-14 (cust_tools/diff_gun_data.py diff of G78 vs
    A38): the palette ORDER varies by nation.

        G78 (German, recoils):  ['G_BlendBone', 'Gun_BlendBone']
        A38 (USA, didn't):      ['Gun_BlendBone', 'G_BlendBone']

    So neither "always idx 1" nor "always idx 0" is right --
    must locate the recoil bone BY NAME so the per-tank palette
    order doesn't matter.  Pattern set focuses on:

      * `Gun_*` (capital G-u-n underscore -- BigWorld convention
        for the recoil-driven bone, distinct from the gun ROOT
        `G_BlendBone` which is just `G_` one letter).
      * Explicit `recoil` / `barrel` substrings as belt-and-
        suspenders for any tank whose authors named the bone
        differently.

    Returns None on:
      * Empty palette.
      * No name matches AND single-bone palette (= un-recoilable
        rigid mantlet).
      * Override-name not found in palette (caller can set
        `RECOIL_BONE_OVERRIDE = 'XYZ'` to force).
    """
    if not palette:
        return None
    if RECOIL_BONE_OVERRIDE:
        try:
            return list(palette).index(RECOIL_BONE_OVERRIDE)
        except ValueError:
            pass  # override doesn't exist; fall through

    # Name-based pick.  Higher score wins; ties broken by declaration
    # order.  `^Gun_` is the canonical WoT recoil-bone name and
    # scores high enough to win against the root `G_BlendBone`
    # (which doesn't match this pattern).
    best_idx   = None
    best_score = 0
    for i, name in enumerate(palette):
        if not name:
            continue
        score = 0
        for pat, w in _BONE_NAME_SCORES:
            if pat.search(name):
                score = max(score, w)
        if score > best_score:
            best_score = score
            best_idx   = i
    return best_idx


class GunRecoil:
    """Per-tank recoil state machine + bone-matrix-array provider.

    Lifecycle:
        * One instance per Viewer (built once at viewer __init__).
        * `bind_palette(palette)` is implicit -- we don't cache the
          palette; each `bone_matrix_array(palette)` call re-resolves
          the recoil bone for the supplied palette.  Cheap, and means
          a tank load can change the gun (different palette) without
          us needing a teardown hook.
        * `trigger()` starts a new cycle (no-op while one is already
          in flight; idempotent at the SPACE-bar level).
        * `update(dt)` advances the state machine.  Called once per
          frame from the same site that calls TankPhysics.update.

    Visual offset is `self.offset_m`: 0 at idle / start of cycle,
    grows to MAX_TRAVEL_M at end of OUT, holds through DWELL,
    eases back to 0 over RETURN.
    """

    def __init__(self):
        self.phase      = PHASE_IDLE
        self.t_in_phase = 0.0
        self.offset_m   = 0.0

    # -------------------------------------------------------------- API

    def trigger(self):
        """Begin a new recoil cycle.  No-op when one is already
        running -- a held SPACE bar fires once per actual press,
        not every frame.
        """
        if self.phase == PHASE_IDLE:
            self.phase      = PHASE_OUT
            self.t_in_phase = 0.0
            self.offset_m   = 0.0

    def update(self, dt):
        """Advance the state machine by `dt` seconds and recompute
        `self.offset_m`.  Safe to call every frame regardless of
        whether SPACE was pressed.
        """
        if self.phase == PHASE_IDLE:
            self.offset_m = 0.0
            return

        self.t_in_phase += float(dt)

        if self.phase == PHASE_OUT:
            # Linear back-stroke -- guns slam back hard.  Real
            # physics is a brief impulse + decel, but linear at
            # 60ms is visually indistinguishable.
            u = min(1.0, self.t_in_phase / max(OUT_TIME_S, 1e-6))
            self.offset_m = MAX_TRAVEL_M * u
            if u >= 1.0:
                self.phase      = PHASE_DWELL
                self.t_in_phase = 0.0

        elif self.phase == PHASE_DWELL:
            self.offset_m = MAX_TRAVEL_M
            if self.t_in_phase >= DWELL_S:
                self.phase      = PHASE_RETURN
                self.t_in_phase = 0.0

        elif self.phase == PHASE_RETURN:
            # Critically-damped 2nd-order-ish return: ease-out cubic.
            # The hydraulic / spring recuperator on a real gun
            # produces this shape much better than a linear ramp
            # would; the recoil "settles" rather than abruptly
            # stopping at zero.
            u = min(1.0, self.t_in_phase / max(RETURN_TIME_S, 1e-6))
            # 1 - (1 - u)^3  starts fast, slows near the end.
            ease = 1.0 - (1.0 - u) * (1.0 - u) * (1.0 - u)
            self.offset_m = MAX_TRAVEL_M * (1.0 - ease)
            if u >= 1.0:
                self.phase      = PHASE_IDLE
                self.t_in_phase = 0.0
                self.offset_m   = 0.0

    # ---------------------------------------------------- skinning glue

    def bone_matrix_array(self, palette):
        """Build an `(N, 4, 4)` float32 row-major bone-matrix array
        for the given gun palette.  Every bone gets identity except
        the recoil bone (picked via `pick_recoil_bone`), which gets
        a +Z translation of `self.offset_m`.

        Args:
            palette (sequence[str] | None): the gun mesh's bone
                palette in declaration order (same convention as
                TankPhysics).

        Returns:
            np.ndarray  shape (N, 4, 4), dtype float32.  When the
            palette is empty or no recoil bone can be picked,
            every matrix is identity -- the GPU skinning path
            then collapses to bind pose for the whole mesh.
        """
        if not palette:
            return np.zeros((0, 4, 4), dtype=np.float32)
        n = len(palette)
        out = np.tile(np.eye(4, dtype=np.float32), (n, 1, 1))
        if self.offset_m == 0.0:
            return out
        idx = pick_recoil_bone(palette)
        if idx is None:
            return out
        # Recoil direction: barrel slides BACKWARD along the
        # mesh-local axis selected by RECOIL_AXIS (default +Z, the
        # WoT chassis convention).  Single-axis translation; the
        # bone's bind orientation is preserved.
        axis = max(0, min(2, int(RECOIL_AXIS)))
        out[idx, axis, 3] = float(self.offset_m)
        return out
