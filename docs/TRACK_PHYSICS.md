# Track physics roadmap (in progress, started 2026-05-08)

> Lives in: `tankExporterPy/tank_physics.py` (will gain a per-frame
> V_loc → pad pass), `tankExporterPy/loaders.py` (gains
> `TrackSplineLoader`), `tankExporterPy/viewer.py` (NURB debug
> overlay), and a new `tankExporterPy/track_spline.py` module.

The current track render is a static "rubber-band" ribbon: a single
welded mesh (`track_LShape12` / `track_RShape12` inside
`Chassis.primitives_processed`) that's UV-scrolled to fake
movement.  It does not deform with wheel deflection, has no per-
pad geometry, and cannot be exported as a real animatable track.

The replacement is a **kinematic-bone-driven NURB** with arc-length-
uniform pad resampling.  We chose this over a spring-mass / per-pad
rigid-body chain after looking at the data: every chassis already
ships the bone scaffolding the in-game runtime drives the track
with, and `segmentsCount × segmentLength` naturally exceeds the
spline length, which is the slack budget — not a spring.

---

## Source data already in pkg

| Asset | Path | What it carries |
|-------|------|-----------------|
| Per-side NURB | `vehicles/<n>/<tank>/track/{left,right}.track` | Collada XML; 17 V_loc nodes / side on T30 (control / sample points along the loop), encoded as 4×4 row-major matrices, units **centimetres** |
| Track-pair gameplay block | `vehicles/<n>/<tank>/<tank>.xml` `<chassis>...<tracks><trackPair>` | `segmentLength`, `segmentsCount`, pad mesh references |
| Bone hierarchy | `Chassis.visual_processed` | `Track_L<i>` (one per ground-contact, Y=0 under each road wheel), `Track_VT_L<i>` (top-run sag between rollers), `Track_VD_L<i>` (drive-sprocket / idler wraparound), `WD_L<i>` (rollers; lowest idx = drive sprocket, highest = idler) |
| Wheel groups | `<chassis>...<wheels><wheelGroups>` | `groupRadius_road`, group membership; `WD` group radii give us the true sprocket / idler radii (T30: drive r=0.339 m, idler r=0.324 m) |

---

## Coordinate-frame conversion

The `.track` Collada matrices need a **Z-negate + units
conversion** to match the runtime chassis-local frame (the
frame `parse_chassis_bone_world_positions` returns):

```python
chassis_pos = (raw[0] / 100, raw[1] / 100, -raw[2] / 100)   # cm -> m, Z negated
```

`track_spline.to_chassis_frame()` applies this by default
(`flip_z=True`).

### Why the Z-negate (1.111.0+)

Pre-1.111 the conversion left Z unsigned.  The spline LOOKED
right because the V_loc set is Z-symmetric (front cluster <->
rear cluster), so a Z-rotated spline still draped correctly
over the wheels.  But it was actually rotated 180 deg about Y --
the spline's "rear" was being rendered at the chassis's front,
and lean-pitch (squat / dive) was applied with the wrong sign
at each end.  That's the symptom Coffee reported as "spline
sinks at rear under accel, front under brake".

Verified by the gameplay-XML `<teethSyncs>` data
(`cust_tools/plot_t30_spline_math.py`):

  * Each drive wheel has a `<startAngle>` + `<teethCount>` in
    the chassis XML.  Tooth k=0 sits at
    `(wheel_Y - R*sin(angle), wheel_Z - R*cos(angle))` on the
    outer track-surface circle.
  * With Z **unflipped** the worst tooth-to-V_loc match was
    ~4.7 cm and the assumed front-tangent V_loc landed on the
    wrong end of the tank.
  * With Z **negated** the matches drop to **machine
    precision**: WD_L0 tooth k=2 -> V_loc31 at 0.001 m;
    WD_L9 tooth k=12 -> V_loc19 at 0.014 m.  Per-tooth angular
    spacing (24 deg on a 15-tooth wheel) lines up with adjacent
    V_locs across the entire wraparound -- impossible by
    coincidence.

The standalone probes (`_plot_t30_*.py`) negate Y for plot-axis
convention; that's separate from the runtime Z-negate fixed
here.

### Drive wheels (WD_) and pitch radius

`<wheelGroup><groupRadius>` is the **outer track-surface
radius**, not the pitch radius.  V_locs and the teethSync tooth
positions all sit on this outer circle.

If you need the pitch radius (for engineering-correct per-pad
spacing on the wraparound, e.g. Phase C instanced pad meshes),
derive it from the inscribed-chord formula on a regular
N-tooth polygon:

```
R_pitch = segmentLength / (2 * sin(pi / teethCount))
```

T30 example: `0.133 / (2 * sin(12 deg)) = 0.3198 m` for both
drive wheels.  Outer-vs-pitch error: WD_L0 (rear) = 5.9 %
(19 mm tooth height); WD_L9 (front) = 1.3 % (4 mm).  The 1.3 %
front isn't an idler -- both ends are drive wheels (per the
WD_ asset-naming convention); WD_L0 just has more pronounced
sprocket teeth because it transmits engine torque.

---

## Resampling pipeline

Implemented in the standalone probe `_plot_t30_smooth.py` (root
of the experiment tree, NOT shipped); will move to
`tankExporterPy/loaders.py` as `TrackSplineLoader` under Phase A.

1. Read 17 V_loc points per side, apply DX→GL conversion.
2. **Closed centripetal Catmull-Rom (α = 0.5)** through the V_locs.
   Uniform CR (α = 0) overshoots the sprocket / idler bends by
   ~6.4 % on T30, producing self-intersecting loops; centripetal
   kills the bulge while still passing through every control
   point.
3. Dense sample (256 pts × 17 segments = 4352 samples) →
   cumulative arc-length table.
4. Resample at uniform arc-length intervals to produce
   `segmentsCount / 2` pad transforms `(position, tangent)` per
   side.

T30 numbers (sanity check):

| Curve | Length | Δ vs 117 × 0.133 |
|-------|-------:|-----------------:|
| Chord polyline (V_loc straight lines)            | 15.062 m | -0.499 m (-3.21 %) |
| Uniform CR (α = 0)                               | 16.558 m | +0.997 m (+6.40 %) |
| Centripetal CR through 17 V_locs only (Phase A1) | 15.126 m | -0.435 m (-2.79 %) |
| **Centripetal CR through V_locs + Track_L<i> (A2)** | **15.510 m** | **-0.051 m (-0.33 %)** |
| Target = `segmentsCount × segmentLength / 2`     | 15.561 m | — |

The Phase A1 number was originally interpreted as "track slack
budget"; Phase A2 measurement made clear that most of it was the
missing bottom-run.  Once the 9 Track_L\<i\> bone positions are
spliced into the V_loc loop in arc-length order between V_loc2
and V_loc15, the residual drops 8× and the spline path now
reaches the ground (pad Y minimum -0.038 m vs Track_L\<i\> bind
Y = 0, matching to numerical precision).  The remaining 5 cm
shortfall (-0.33 %) is the actual physical slack — pads can
hang loose between the rear roller and the drive sprocket
without bottoming out the spline path.

Resampled-pad spacing on T30, **Phase A2** (V_locs + Track_L<i>,
26 control points, 117 pads): mean 0.1325 m, std 0.3 mm —
uniformity is essentially machine-precision once the cumulative-
arc-length table is in (and the augmented control loop didn't
hurt it).

---

## What's in the .track file vs what's NOT

Discovered while wiring Phase A1 (2026-05-08): T30's `left.track`
contains exactly **17 V_locs covering ONLY the top run + the
sprocket / idler wraparounds**.  There are NO V_locs along the
bottom run.  The 17 V_locs trace, in source order:

* `V_loc29 / V_loc30 / V_loc31` — top → front-wrap-down (Y from
  +1.19 sliding to +0.93 as Z goes -3.15 → -3.50)
* `V_loc1 / V_loc2`             — front-wrap-bottom (Y +0.71, +0.52)
* gap → `V_loc15`               — rear-wrap-bottom (Y +0.62 at
  Z = +3.45) — this is a **6.76 m chord** at Y ≈ +0.55 spanning
  the entire bottom run with no intermediate control points
* `V_loc15..19`                 — rear-wrap-up to top (Y +0.62 → +1.26)
* `V_loc21..27`                 — top run (Y +1.24, evenly Z-spaced)

The bottom run is reconstructed at runtime by **inserting
Track_L\<i\> bone positions** (one per road wheel) between
`V_loc2` and `V_loc15` in arc-length order along the loop.
Each Track_L\<i\> bone sits at `(W_L<i>.x, ground_y, W_L<i>.z)` —
the wheel's hub X/Z but ground-contact Y, deflecting with
suspension as the wheel-rest changes.  See Phase A3 below.

## Why kinematic, not elastic

Per-bone V_loc binding (refined version, post-Phase-A1 discovery):

| V_loc neighbourhood | Source bone | Updated each frame from |
|---------------------|-------------|--------------------------|
| Top-run V_locs                    | `Track_VT_L<i>` | Bind position from `.visual_processed`, optionally sagging slightly between rollers (catenary; future polish) |
| Drive-sprocket / idler wraparound | `Track_VD_L<i>` | Rigid offset from chassis (sprocket / idler centre + radius) — these don't deflect |
| **Bottom-run synthetic points**   | `Track_L<i>`    | Inserted between `V_loc2` and `V_loc15`, X/Z from the bind W_L\<i\>, Y = wheel hub Y - radius - track_thickness, after `tank_physics.py` resolves the suspension residual |

When a wheel deflects on a bump → its `Track_L<i>` bone moves →
the bound V_loc rides it → the centripetal-CR through the V_locs
deforms → the 117 resampled pad transforms are recomputed →
render.  No springs, no integrator, no settle-in delay, no
replay divergence.  The "elastic feel" comes free from CR's
smooth interpolation across neighbouring V_locs.

True spring-mass NURB physics is a possible v3 upgrade for
wobble-after-jolt and track-throw under extreme deformation.
Not in scope yet.

---

## Phased plan

**Phase A — Physics + spline (no visuals).**

1. **DONE (1.103.0)** -- `tankExporterPy/track_spline.py` shipped:
   `parse_track_vlocs`, `to_chassis_frame`, centripetal CR,
   uniform arc-length resample, `TrackSplineLoader.from_pkg`,
   `TrackSplineSide`.  T30 smoke test passes.
2. **DONE (1.104.0)** -- bone-binding map.  Same module gained
   `parse_chassis_bone_world_positions` (walks .visual_processed
   for `{name -> world_xyz}`) and `TrackBoneBinding.build` /
   `TrackSplineSide.attach_binding`.  Algorithm: filter bones to
   the requested side, V_loc -> nearest bone by 2-D (Y, Z)
   distance, capture rigid offset.  Bottom-run gap detected as
   the largest source-order chord (T30: V_loc2 -> V_loc15 at
   6.76 m); Track_L\<i\> bones ordered by Z for arc-length
   traversal.  T30 binding result: V_loc29..V_loc1 -> WD_L9
   (front idler), V_loc15..V_loc19 -> WD_L0 (rear sprocket),
   V_loc21..V_loc27 -> WD_L1..WD_L7 (return rollers, 1:1 in
   order).
3. **DONE (1.105.0)** -- per-frame deform.  Implemented in
   `viewer.py` rather than `tank_physics.py` (kept the physics
   layer untouched; the spline runs in the render pass alongside
   the other debug overlays).  See Phase B below for the wiring.

**Phase B — Debug visualisation.  DONE (1.105.0 + 1.106.0).**

4. NURB debug overlay in `viewer.py`:
   * `Viewer.__init__`: `track_lines = LineBatch(line_width=1.0)`,
     `_track_left / _track_right`, `_track_chassis_bones_bind`,
     `_show_track_spline` (persisted in cfg).
   * Vehicle-load hook: derives `vehicles/<nation>/<tank>` from
     `mesh.primitives_zip`, calls `TrackSplineLoader.from_pkg`,
     parses chassis bones via
     `parse_chassis_bone_world_positions`, attaches per-side
     binding.  Soft-fails with detailed `[track_spline]`
     console diagnostics on every early-out path.
   * `_track_current_bone_positions(tp)`: returns
     `{bone_name: chassis_local_xyz}` -- bind dict copy with
     `Track_<side><i>` Y values overridden by
     `(hub_y_local - radius - track_thickness +
       smoothed_residual_y[wheel_idx] + _track_test_dy)`.  The
     `hub_y - radius - thickness` lift is the **Y-lift fix**
     (1.106.0) that put the spline on top of the terrain
     instead of 5 cm below it.
   * Render block in the post-tank overlay pass: per side, build
     augmented control loop, run centripetal CR + uniform
     resample to 64 pads, transform chassis-local pad positions
     to world via `chassis_matrix().T` (column-vector
     convention; `world = chassis_matrix @ vert`), draw closed
     line strip via `LineBatch`.  Yellow = LEFT, cyan = RIGHT.
   * **Toggles**:
     * `F8` -- show / hide overlay.  Persists to cfg.
     * `RIGHT arrow` (held) -- drop all wheels by 0.5 m/s
       (droop test).  Adds to a uniform `_track_test_dy` bias
       on the spline overlay only -- does NOT enter the
       physics solver.
     * `LEFT arrow`  (held) -- lift all wheels (compression test).
     * `BACKSPACE` -- reset bias to 0.
     * Bias clamped to +- 30 cm; on-screen log shows
       `track test dy = +-X.XXX m` while held.
     * The `_show_lookat_lines` flag (look-at crosshair) is
       raised while either arrow is held so the user has a
       consistent visual anchor for "is the spline reacting?".

**Phase C — Per-pad meshes + textures.**

5. Discover per-pad mesh assets.  They are **not** in
   `Chassis.primitives_processed` (that holds the rubber-band
   mesh).  Probe target: `vehicles/<n>/<tank>/normal/lod0/Track*`
   plus the `<trackPair>` element in the gameplay XML for
   explicit model refs.  Some tanks are single-piece pads,
   others are shoe + connector; loader needs a
   `[(model, length, count)]` pad-pattern abstraction.
6. Textures: ride along the visual_processed material section
   (existing texture loader path; no new code).
7. Render via instanced draw — 234 instances per tank is
   trivial, do not submit one draw per pad.

**Phase D — Park the rubber-band path.**

8. Move the current welded-track ribbon code into
   `tankExporterPy/legacy_rubber_track.py` behind a
   `--legacy-tracks` viewer flag.  Don't delete — kept for A/B
   and for tanks where the segmented version misbehaves until
   coverage is verified.

**Phase E — Export / import.**

9. Bake per-pad transforms into FBX as 234 separate empty
   objects (or a skinned mesh with one bone per pad) so DCC
   tools see a real animatable track.  Round-trippable.
10. Mirror import.

---

## Investigation tooling (1.107.0)

Coffee reported "oscillating and not settling after spline was
added" -- visible chassis ringing on uneven terrain that didn't
exist pre-1.105.  Source-of-truth `tank_physics.py` is
unchanged between 1.103 and 1.106 (the spline is purely a
viewer-side render pass), so the suspect chain is:

1. **dt-spike from spline overhead** -- centripetal-CR through
   17 segments x 64 samples x 2 sides + uniform resample x 64
   pads runs every frame.  An explicit-Euler integrator over
   a critically-damped second-order spring (omega_n ~ 14
   rad/s on the Y axis) is numerically stable up to dt ~
   143 ms; below that it just damps slower.  But if dt jitter
   blows past that ceiling, the integrator goes unstable.
2. **Hysteresis chattering** -- a wheel sitting at
   `|d - (env_edge +/- HYST)| < 0.005 m` flips CONTACT <->
   HANGING <-> OVER_COMP every frame, dragging plane fit with
   it.
3. **Integrator error not decaying** -- the second-order
   spring fighting an input that keeps changing under it
   (e.g. solver target drifting because the contact set
   keeps changing).

Rather than guess, 1.107 ships a manual recorder that
captures every signal feeding the loop:

* `F3` toggles a manual recording.  Output is
  `test_runs/manual_<tank>_<ts>.json` (one frame per render).
* Per-frame timers (`frame_dt_in`, `physics_update`,
  `spline_overlay`, etc.) localise dt spikes to specific
  passes.
* Per-wheel `hyst_dist_to_flip_m` exposes hysteresis-edge
  proximity (small sustained values = chattering).
* Integrator `e_pitch_deg`, `e_roll_deg`, `e_y_m` exposes
  spring error decay.
* `state_changes_count` per frame surfaces classifier
  flip-flopping at the top level for fast triage.

See `docs/PHYSICS.md` "F3 manual recorder" for the full
field list.  No physics-math changes in this rev -- only
field assignments (`last_delta_y`, `_last_target_*`,
`_last_e_*`, `_last_lift_needed_m`) for diagnostic readout.

---

## Status snapshot at handoff (1.106.0, 2026-05-08)

| Phase | Status | Where it lives |
|---|---|---|
| A1 -- spline math + parsers | DONE 1.103.0 | `tankExporterPy/track_spline.py` |
| A2 -- V_loc <-> bone binding | DONE 1.104.0 | same module (`TrackBoneBinding`) |
| A3 -- per-frame deform     | DONE 1.105.0 | `viewer.py` (in render pass; physics layer untouched) |
| B  -- debug overlay        | DONE 1.105.0 | `viewer.py` -- F8 toggle + LEFT/RIGHT manual deflection |
| Y-lift correction          | DONE 1.106.0 | `Viewer._track_current_bone_positions` |
| Topology Z-flip audit      | VERIFIED 1.106.0 | augmented loop is Z-monotone through bottom run; no flip needed |
| C  -- per-pad mesh + texture | NOT STARTED | next |
| D  -- park rubber-band ribbon | NOT STARTED | gated behind `RENDER_RUBBER_BAND_TRACK = False` flag still |
| E  -- FBX export / import   | NOT STARTED | |

The line-strip overlay deforms correctly under wheel residuals
on T30 in static + driving tests.  Manual deflection bias
(LEFT/RIGHT arrows, BACKSPACE reset) confirms the bottom run
moves with `_track_test_dy` across the full +/-30 cm clamp range.
Topology check via `cust_tools/analyze_track_spline.py` passes
on T30 (front idler at -Z, rear sprocket at +Z, bottom run
Z-monotone ascending, no front/back mixup).

## Next session pickup

**Top priority: Phase C -- per-pad mesh + texture.**  We have
the pad TRANSFORMS (position + tangent) for free already; what's
missing is the actual pad geometry to instance at each pad.

Concrete first steps:

1. Probe the gameplay XML's `<chassis>...<tracks><trackPair>`
   block on T30 to find pad mesh references.  Likely fields:
   `<modelLeft>`, `<modelRight>`, `<segmentLength>`,
   `<segmentsCount>`, possibly a `<linkLeft>` / `<linkRight>`
   for two-piece pads.  See `cust_tools/analyze_track_spline.py`
   for the gameplay-XML-target-length code already pulling
   `<segmentLength>` and `<segmentsCount>` -- extend that to
   harvest the model paths.
2. Where do those model paths point?  Probable target:
   `vehicles/<n>/<tank>/normal/lod0/Track*.primitives_processed`,
   but confirm by extracting and listing the contents with
   `cust_tools/dump_sections.py`.
3. Loader: register a per-tank pad-mesh table.  Cache in
   `MeshSet.tank_info['track_pads'] = {'left': [(model, length,
   count), ...], 'right': [...]}`.
4. Renderer: instanced draw of pad geometry.  234 instances
   total (117/side on T30) is well within "submit one draw per
   side" territory.  Each pad's instance transform =
   `chassis_matrix @ pad_transform_chassis_local`.  Pad-local
   orientation: tangent + chassis-up cross product gives the
   pad's binormal; build a 3x3 orthonormal frame from
   tangent + binormal + up.
5. Texture: ride along the pad mesh's `.visual_processed`
   material section; existing `TextureLoader` path handles it.

**Secondary -- generalisation.**  Phase A2 binding has only
been validated on T30.  The binding heuristic
(nearest-neighbour by Y/Z, gap = largest source-order chord,
Track_L\<i\> sorted by Z) should generalise but verify on at
least:

* T110E4 (American, 7-wheel)
* IS-7 / Object 268 (Russian)
* Maus / E100 (German, 8-wheel)
* AMX 50B (French autoloader)
* Hotchkiss EBR (the WD-only edge case noted in PHYSICS.md)

Run `cust_tools/analyze_track_spline.py <tank>` against each;
the topology-check section flags any non-monotonic bottom run
or front/back swap.

**Tertiary -- polish.**  None blocking, but on the radar:

* Top-run catenary sag between return rollers (currently the
  V_locs are at fixed bind position; real tracks droop slightly).
* Track texture UV scrolling driven by chassis forward speed
  (the rubber-band ribbon already does this; pad meshes need
  the same treatment if we want visible movement at ribbon
  speed).
* Gameplay XML `<segmentsCount>` parse into
  `VehicleXMLLoader.parse_info` so the analyzer's
  `target_per_side` length comparison populates by default.

---

## Reference files

```
tankExporterPy/track_spline.py         shipped (Phase A1+A2 + augmented loop builder)
tankExporterPy/viewer.py               shipped (Phase A3+B overlay + manual deflection)
tankExporterPy/tank_physics.py         untouched -- the spline doesn't enter the physics loop
tankExporterPy/loaders.py              untouched (chassis-info parse already covers what we use)
cust_tools/analyze_track_spline.py     CLI analyser; parameter discovery + PNG plot
```

Standalone probes that proved this out (root of experiment
tree, NOT shipped):

```
_plot_t30_track.py    V_loc + wheels + bend-bone YZ overview
_plot_t30_segs.py     V_loc loop + 117 pads + drive sprocket + idler
_plot_t30_smooth.py   centripetal-CR resample + length / spacing report
```

Files that WILL land in Phase C:

```
tankExporterPy/track_pads.py           (new) pad mesh loader + instance render
tankExporterPy/legacy_rubber_track.py  (new) old rubber-band code, parked behind --legacy-tracks
tankExporterPy/loaders.py              <trackPair> pad-pattern + segmentLength parse extension
```

---

## Known issues caught while wiring this up

### Skipping `track_*Shape*` mesh hides road wheels on T30

When `track_LShape12` / `track_RShape12` are skipped from rendering
(via the `RENDER_RUBBER_BAND_TRACK = False` gate in `viewer.py`),
the **road wheels also disappear visually** on T30.

Why: the road wheel geometry isn't where you'd expect.  Bone
palette dump from T30's `Chassis.primitives_processed`:

| Mesh | road-wheel bones (W) | drive/idler (WD) | Track bones | total palette |
|------|---:|---:|---:|---:|
| `chassis_RShape21_split_1` | 0 | 0 | 4 | 5  |
| `chassis_RShape21_split_0` | 9 | 9 | 5 | 24 |
| `chassis_LShape21_split_1` | 0 | 0 | 4 | 5  |
| `chassis_LShape21_split_0` | 9 | 9 | 5 | 24 |
| `track_LShape12` | 0 | 0 | 12 | 13 |
| `track_RShape12` | 0 | 0 | 12 | 13 |

Read carefully: the SHAPE_0 splits carry the W_L*/W_R* + WD_L*/WD_R*
palette entries (the road wheel + sprocket / idler bones), and the
SHAPE_1 splits + the track meshes only carry Track_* (track-segment)
bones.  Wheel bones are NOT in the track meshes — so skipping the
track meshes shouldn't drop wheel deflection at all.

Open question: the user observed wheels stop deflecting visually
when track meshes are skipped.  Hypothesis: physics state is fine
(bone matrix array is computed regardless), but something in the
wheel-mesh draw is gated on something else that the track-skip
inadvertently disables.  Investigation pending; the rubber-band
gate in `viewer.py` is currently `False` and the wheel rig is
actively broken on screen.  Flip the gate to `True` to confirm
the physics rig still works (it does), then narrow down what the
real linkage is.
