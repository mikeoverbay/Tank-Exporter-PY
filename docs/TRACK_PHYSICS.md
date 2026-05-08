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

The `.track` Collada matrices land in the SAME chassis-local
frame as the chassis-mesh vertices (and the bone hierarchy in
`.visual_processed`).  At load time we apply only a units
conversion:

```python
chassis_pos = (raw[0] / 100, raw[1] / 100, raw[2] / 100)   # cm -> m
```

**No Y flip, no Z flip.**  Verified on T30: raw V_loc Y values
land in [+0.52, +1.26] with the top run at Y = +1.24 m sitting
above the W_L<i> wheel hubs at Y = +0.448 m, which is exactly
the geometric arrangement (track wraps over the top of the
hull above the wheels).  The chassis is no longer Z-flipped on
load (`from_chassis_meshes` has used `flip_z = False` since
v1.93.2), so we don't need to undo a flip we no longer apply.

The standalone probes (`_plot_t30_*.py`) negate Y in their
extraction, but that's a PLOT-axis convention for the side-view
images — they project to YZ with Y-down as the natural image
axis.  Don't carry that into the runtime; `track_spline.to_chassis_frame()`
defaults to `flip_y=False` and warns against opting in.

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
| Chord polyline (V_loc straight lines)        | 15.062 m | -0.499 m (-3.21 %) |
| Uniform CR (α = 0)                           | 16.558 m | +0.997 m (+6.40 %) |
| **Centripetal CR (α = 0.5)**                 | **15.126 m** | **-0.435 m (-2.79 %)** |
| Target = `segmentsCount × segmentLength / 2` | 15.561 m | — |

The 2.79 % shortfall is the **track slack budget** — pad rest-
length sum naturally exceeds the rigid spline path so the track
can hang loose between the rear roller and the drive sprocket.
At runtime we either let the top run sag (visually correct) or
stretch pad pitch to 0.1292 m if we want pads tight on the
spline.

Resampled-pad spacing on T30 with centripetal CR: mean
0.1292 m, **std 0.3 mm** across all 117 pads — uniformity is
essentially machine-precision once the cumulative-arc-length
table is in.

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

1. `TrackSplineLoader` in `loaders.py`: per-side V_loc parse,
   DX→GL conversion + traversal reversal, cache on the loaded
   vehicle.
2. Bone-binding map: walk `.visual_processed` once, build
   `{V_loc_i → bone_name}` by nearest-neighbour with type
   preference (`Track_L*` for Y≈0, `Track_VD*` near
   sprocket/idler centres, `Track_VT*` for top run).
3. Per-frame deform in `tank_physics.py`: pull each V_loc's bone
   world position, run centripetal CR, resample
   `segmentsCount/2` pad transforms per side.  Reuses the wheel
   rest-Y already settled by the existing physics.

**Phase B — Debug visualisation.**

4. NURB debug overlay in `viewer.py` — draws the V_loc dots, the
   smooth CR polyline, and the resampled pad tangent ticks as
   3-D line strips.  Toggled from an existing Debug checkbox.
   Built *before* any pad mesh work — debugging mesh placement
   without the spline overlay is masochism.

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

## Open probes (do before writing code)

* Dump the contents of `vehicles/american/A14_T30/normal/lod0/`
  and any other directories the gameplay-XML `<trackPair>` block
  points at.  Confirm where per-pad mesh assets actually live.
* Run the V_loc → nearest-bone mapping on T30 + one Russian +
  one German tank.  Confirm the binding rule generalises before
  we bake it into `TrackSplineLoader`.

---

## Files that will land

```
tankExporterPy/track_spline.py         (new) TrackSplineLoader, centripetal CR
tankExporterPy/tank_physics.py         per-frame V_loc → resampled pads pass
tankExporterPy/viewer.py               NURB debug overlay, --legacy-tracks flag
tankExporterPy/legacy_rubber_track.py  (new) old rubber-band code, parked
tankExporterPy/loaders.py              <trackPair> pad-pattern parsing extension
```

The standalone probes that proved this out and produced the
plots referenced above live at the experiment-tree root and are
NOT shipped:

```
_plot_t30_track.py    (V_loc + wheels + bend-bone YZ overview)
_plot_t30_segs.py     (V_loc loop + 117 pads + drive sprocket + idler)
_plot_t30_smooth.py   (centripetal-CR resample + length / spacing report)
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
