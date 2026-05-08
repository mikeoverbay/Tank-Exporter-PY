# Tank physics — wheel suspension + chassis solver

> Lives in: `tankExporterPy/tank_physics.py` (~1700 lines).  Hooked
> into the render loop in `tankExporterPy/viewer.py` around line
> 9134-9165.  Drive-input handler at line 8842 onward.

A per-wheel-on-terrain rigid-body solver.  Each frame samples the
terrain Y under every road wheel, classifies wheels as
CONTACT / HANGING / OVER_COMPRESSED, fits a plane through the
contacting set, and emits a chassis pose (4×4 matrix) plus a
per-wheel Y residual that drives the GPU skinning shader so each
wheel visually deflects independently.

---

## Per-frame cycle (`TankPhysics.update(terrain, dt)`)

Lines 836-1300 of `tank_physics.py`.

```
1. Mesh-local wheels → world XZ via current chassis_matrix()
2. terrain.sample_heights(world_x, world_z)  per wheel
3. Target wheel-centre Y = terrain_y + radius + track_thickness
4. Classify each wheel:
       CONTACT          (target Y inside [min_offset, max_offset])
       HANGING          (target Y above max_offset -- in the air)
       OVER_COMPRESSED  (target Y below min_offset -- bump-stop)
5. lstsq plane fit through CONTACT wheels only.
   < 3 contact wheels -> no plane; chassis falls under gravity.
6. Per-wheel residual Y = (plane Y at wheel x,z) - (bind-pose Y).
   HANGING wheels droop to droop cap; OVER_COMPRESSED clamp to
   compression cap; CONTACT wheels take their lstsq residual.
7. Asymmetric damping: residual_y -> smoothed_residual_y.
   Compression is fast (real shock fluid), extension is slow
   (real spring re-extends slower under gravity-only load).
   Drives the GPU skinning bone matrix array next frame.
```

Iteration converges in ~10 sub-steps from cold-start (chassis spawned
in the air); steady state is a per-frame correction.

---

## `bone_matrix_array(palette, max_bones=64)` — three passes

Line 1501 of `tank_physics.py`.  Builds the 64-slot bone array the
skinning shader expects.  Called once per draw call by
`viewer._upload_skinning(mesh)`.

| Pass | Bone pattern | What it does |
|------|-------------|--------------|
| 1 | `W_<L\|R>\d+_BlendBone` (and Hotchkiss-EBR-style `W_F_<L\|R>` / `W_R\d+_<L\|R>`) | Pure Y-translation by `smoothed_residual_y[wheel_idx]`.  Identity rotation.  Direct passthrough of the +up residual (no sign flip — see "Sign convention" below). |
| 2 | `Track_<L\|R>\d+_BlendBone` | Look up the corresponding `W_<side><i>` via `_track_to_wheel_name()`; copy its Y-translation so the track segment stays glued to the wheel underneath. |

Anything else in the palette (decorative bones, V_BlendBone,
`WD_*` drive sprocket / idler / return rollers, etc.) stays at
identity.  The Pass-3 50%-partial-follow for `WD_*` bones from
v1.99.0 was reverted because (a) the rubber-band track ribbon is
gated off behind `RENDER_RUBBER_BAND_TRACK = False` while the
NURB track work proceeds, and (b) the "drop WD_ when W_ exists"
filter in `from_chassis_meshes` now removes WD_ bones from the
wheel rig altogether on tracked tanks, so there's no wheel-index
to inherit from in Pass 3 anyway.

### Sign convention

`last_residual_y[i] > 0` means the wheel needs to RISE (UP, into
the hull, compression direction).  `< 0` means the wheel needs to
DROP (DOWN, away from the hull, extension direction).  Matches
mechanical intuition AND the GL skinning math (`shaders/mesh.vert`
does plain `world = model * skin * position`, so bone Y > 0 moves
the visible vertex up).

Pass 1 / Pass 2 write `out[pi, 1, 3] = +ry` directly — no sign
flip anywhere in the chain.  An earlier docstring claimed there
was a "shader-side sign quirk" requiring `out[pi, 1, 3] = -ry`;
that was a misdiagnosis that propagated for years and produced
the "wheels sinking up to their rims" symptom.  Fixed 2026-05-08.

### WD_ filter

`from_chassis_meshes` filters out `WD_<L|R>\d+_BlendBone` (drive
sprocket / idler / return rollers) from a side IF that side has at
least one `W_<L|R>\d+_BlendBone` road-wheel.  T30's `WD_L0` (drive
sprocket) sits within the 15 cm Y-band around road-wheel height
and was leaking through the band post-pass before this filter was
added, producing 10 wheel positions per side where T30 actually
has 9.  Hotchkiss-EBR-style rigs that ONLY have WD_ bones (no W_)
are unaffected — the `has_w` check is False there, so the WD_
entries stay in as the real road-wheel bones.

---

## Per-tank parameters from gameplay XML

`VehicleXMLLoader.parse_info()` in `loaders.py` extracts the
selected chassis's physics inputs from the deeply nested
`<chassis>...<tracks><trackPair><trackDebris><physicalParams>`
block (descendant search via `.//tag`, NOT direct child):

| XML field | TankPhysics ctor arg | T30 | T110E4 |
|-----------|----------------------|-----|--------|
| `<wheelGroups><groupRadius_road>` (matched on bone name regex `^W_[LR]\d+$`) | `radius` | 0.345 | 0.330 |
| `<groundNodes><group><minOffset>` | `min_offset` | -0.06 | -0.04 |
| `<groundNodes><group><maxOffset>` | `max_offset` | +0.06 | +0.08 |
| `<trackThickness>` (preferred) or `<renderModelOffset>` | `track_thickness` | +0.029 | +0.016 |
| `<rotationSpeed>` (chassis section, dps) | `max_yaw_rate_dps` | ~22 | 26 |

Note: T30's `<renderModelOffset>` is published as `-0.029` in
the gameplay XML.  The loader takes `abs()` at parse time --
"track thickness" is a strictly positive geometric quantity, but
different tanks publish the field with different sign
conventions.  The abs() at the parse site keeps downstream code
sign-clean (formula `target_centre = ty + radius +
track_thickness` would otherwise sink T30's wheels 2.9 cm into
the dirt).

Total tank weight = sum of every component's `<weight>` (hull +
chassis + turret + gun + engine + radio + fueltank).  Surfaced as
`info['total_weight_kg']`.  T30 = 61 t.

These are wired in `viewer.py` at line 7805-7841 — `_pending_chassis_info`
flows from the load path into the `from_chassis_meshes` call.

---

## Wheel auto-extract (`from_chassis_meshes`)

Line 554 of `tank_physics.py`.  Walks every loaded chassis mesh,
clusters verts by their dominant `iii` byte (= which bone they're
mostly weighted to), takes each cluster's vertex CENTROID as the
wheel's mesh-local position.

Filters by bone-name regex (`W_<side>\d+`, `WD_<side>\d+`, etc.) plus
a Y-band post-pass that rejects bones outside the typical road-wheel
height range.  This is what lets the same `WD_L0_BlendBone` name be
ACCEPTED as a wheel on a Hotchkiss EBR (8-wheel armoured car) and
REJECTED as a drive sprocket on every tracked tank.

The accepted wheel bone names are saved in `self.wheel_bone_names`
so `bone_matrix_array` doesn't have to re-run the filter every
frame — it just consults the set.

**Bone-palette layout per mesh** matters here.  T30 example:

| Mesh (in Chassis.primitives_processed) | road-wheel (W) | drive/idler (WD) | track-segment (Track_*) | total |
|---|---:|---:|---:|---:|
| `chassis_RShape21_split_1` | 0 | 0 | 4 | 5 |
| `chassis_RShape21_split_0` | 9 | 9 | 5 | 24 |
| `chassis_LShape21_split_1` | 0 | 0 | 4 | 5 |
| `chassis_LShape21_split_0` | 9 | 9 | 5 | 24 |
| `track_LShape12` | 0 | 0 | 12 | 13 |
| `track_RShape12` | 0 | 0 | 12 | 13 |

Read carefully: road-wheel + sprocket + idler bones are ONLY in
the `*Shape21_split_0` meshes (the smaller ones, ~336 verts each).
The bigger split_1 meshes (~10 K verts) and the track ribbon meshes
have NO road-wheel bones at all — they're skinned only to
`Track_*` bones.

---

## Drive controls

`viewer.handle_input()` line 8842 onward.

| Key(s)   | Action |
|----------|--------|
| `W`, `S` | Forward (hold).  S used to be cruise toggle; flipped to hold-to-drive at user request. |
| `Z`, `X` | Backward (hold).  X is duplicate of Z. |
| `A`, `Q` | Yaw left.  Rate = chassis XML `<rotationSpeed>` in dps (T30 ~22, T110E4 26, M60 ~48; 60 default).  No ramp -- tracked vehicles spool yaw fast because both tracks contribute torque immediately. |
| `D`, `E` | Yaw right. |
| `0`-`9`  | Speed step.  0 = stopped (default).  1 = full top speed.  9 = creep (0.1 kph).  2-8 linearly interpolated. |
| `O`      | Toggle auto-circle drive.  Tank ignores manual keys, pulls a steady arc of radius `_auto_circle_radius` at the current ramped speed.  Yaw rate `omega = v / R` is clamped to `max_yaw_rate_dps`; when a tight radius + high speed exceeds the cap, the tank visibly widens the arc rather than out-spinning its chassis. |
| `C`      | Cycle camera mode (orbit / chase / commander).  Always starts on chase (1) per recent change. |
| `F2`     | Wireframe overlay. |

Forward/backward is **ramped** through `self._current_forward`:

```
DRIVE_ACCEL = 5.0 m/s^2     spool-up
DRIVE_DECEL = 10.0 m/s^2    braking + reversal
```

Tank reaches full step-speed in ~2 s on accel, brakes in ~1 s
(matches real heavy tanks where braking force exceeds drive force).

Top speed is per-tank from gameplay XML's `<speedLimits><forward>`
in **kph** (NOT m/s — was multiplied by 3.6 previously, fixed at
v1.97).  T30 = 35 kph; T110E4 = 50 kph.  Conversion to scene
units uses the heightmap's 1 unit = 1 yard scale.

---

## Static settling math

For seeding `Track_L<i>` bones on flat ground (used by the upcoming
track-physics pass, see [`TRACK_PHYSICS.md`](TRACK_PHYSICS.md)):

```
F_per_wheel = M·g / N                       Newtons each (M = total mass, N = total wheels)
k           = 2 · F_per_wheel / |maxOffset| Hooke's k under the "static load = 50% of compression
                                            travel" engineering rule (Model A in our derivation)
δ_static    = |maxOffset| / 2               settled deflection (≡ F/k by definition)

y_wheel_settled = y_wheel_neutral - δ_static
```

T30: M = 61 000 kg, N = 18, maxOffset = 0.06 m → F ≈ 33 kN/wheel,
k ≈ 1.11 MN/m, δ_static ≈ 30 mm.

The actual per-frame chassis solver is more general (couples chassis
6 DOF with N suspension DOF via lstsq plane fit, runs to convergence
each frame).  Static math is just for "where do wheels rest under
gravity on flat ground" — useful as a seed and for the kinematic
bone-driven NURB track displacement.

---

## Cached debug state (read by overlays)

After each `update()` call:

| Field | Shape | Meaning |
|-------|-------|---------|
| `last_wheel_world` | (N, 3) | World-space wheel centres (post-chassis-pose).  Used by contact-overlay pink-star markers. |
| `last_terrain_y` | (N,) | Sampled terrain Y under each wheel. |
| `last_target_y` | (N,) | Wheel-centre target Y (terrain + radius + track_thickness). |
| `last_wheel_state` | (N,) | int code: CONTACT / HANGING / OVER_COMPRESSED.  Drives the wheel-highlight shader path. |
| `last_residual_y` | (N,) | Pre-damping plane-fit residual.  For diagnostics. |
| `smoothed_residual_y` | (N,) | Asymmetric-damped Y, fed to the bone matrix array each frame. |

The Debug + Susp checkbox combo on the right panel surfaces these
visually (contact stars, droop / compression colouring, residual
length).

---

## Reverted experiments (NOT in scope)

A v1.93.0 attempt added angular inertia (`_step_angle_inertia`,
`omega_pitch_deg`, etc.) and integrated chassis pitch / roll over
time.  Reverted in v1.93.2: the integrator-lagged pose values fed
back into the contact classifier created a positive feedback loop
during the iterative refinement loop -- chassis wobbled wildly.  If
we want angular damping later, refactor so the classifier reads a
SETTLED chassis pose, not the in-progress integrator state.
