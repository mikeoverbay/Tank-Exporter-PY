# Session handoff -- 2026-05-16  (v1.197.0 -> v1.230.5)

Captures the full state of where this long session left off so the
next person picks up without re-deriving anything.

---

## Versions shipped this session (33 bumps)

| ver        | one-liner                                                                 |
|-----------:|----------------------------------------------------------------------------|
| 1.198..200 | aim-cursor / shellhole-decals depth-test churn (eventually settled with one cursor draw AFTER shellholes) |
| 1.201..204 | SS volumetric dome cursor (tried, abandoned, reverted to ball-only on dome) |
| 1.205      | docs + handoff for the dome+cursor work                                   |
| 1.206      | cap zoom-out at dome radius                                                |
| 1.207      | redirect main-loop stdout to `debug_log.txt`                              |
| 1.208      | `cust_tools/scan_all_chassis_tracks.py` + `analyze_chassis_tracks.py`     |
| 1.209      | C-key cycle resets free-cam to 15 m behind tank, 30 deg up                 |
| 1.210      | "Meshes" toolbar button -> "Visible"                                      |
| 1.211      | Persist Track Segments + Track Splines visibility                          |
| 1.212      | Chain jitter fix (EMA on yaw rate) + s_offset cache-rebuild scaling       |
| 1.213/214  | Pad-centre radius substitution (tried, reverted)                          |
| 1.215      | Add `<segmentsInnerThickness>` to wheel R for chain wrap                  |
| 1.216      | Half-pad phase shift (= 0.5*seg_len) for tooth alignment                  |
| 1.217.0    | Tooth phase from chassis-XML `<teethSyncs>` (replaces blind half-pad)     |
| 1.217.1    | Flip tooth-phase sign                                                      |
| 1.218.0    | Wheel-spin uses `R + segmentsInnerThickness` (matches chain pitch circle) |
| 1.219.0    | Drop chassis ACCEL/DECEL ramp -- input goes straight to `cur_forward_mps` |
| 1.219.1    | Mouse-sens slider max 3.0 -> 6.0                                          |
| 1.220.0    | Road/roller wheel push is Y-only (PBD step 2c)                            |
| 1.221.0    | Flat-tangent push variant (reverted)                                       |
| 1.221.1    | Revert flat-tangent (back to v1.220 circle)                               |
| 1.221.2    | Lock push direction by wheel kind (road=down, roller=up)                  |
| 1.221.3    | Penetration-scaled push for road/roller (matches radial branch)            |
| 1.221.4    | PBD wheel_radii inflated by `inner_thickness` (matches homie)             |
| 1.222.0    | Two-piece pad shift: `segment2*` aligns to `segment*` hinge                |
| 1.222.1    | Hide aim cursor while ALT is held                                          |
| 1.222.2    | Two-piece hinge ON the spline (reverted, was worse)                       |
| 1.222.3    | Revert v1.222.2; back to v1.222.0 segment2-only shift                     |
| 1.223.0    | F3 recorder captures `chain_focus` per side                               |
| 1.223.1    | F3 save JSON safety net (`_json_safe`) -- fixed silent "nothing saved"   |
| 1.224.0    | `chain_focus` -> list of per-road-wheel dicts                             |
| 1.224.1    | Cursor hidden through ALT-release capture too                              |
| 1.225.0    | Glancing-touch filter in homie -- broke Archer (reverted)                  |
| 1.226.0    | Ribbon auto-hide gated on `track_segment_models` presence                 |
| 1.227.0    | Revert v1.225.0 glancing-touch filter                                     |
| 1.228.0    | Terrain-floor lift includes `extra_rotating_hubs` (Archer W_L0/L5)        |
| 1.229.0    | Drop OVER_COMP road wheels from chain loop (state-gated, not geometric)   |
| 1.230.0    | Reclassify W_-prefixed singular wheels by radius (Archer normalisation)   |
| 1.230.1    | `_MAT` rubber bands default to unchecked                                  |
| 1.230.2    | Y-lift speed matches pan (no more "gummy" look-at)                        |
| 1.230.3    | `_MAT` check also reads `mesh.identifier`                                 |
| 1.230.4    | Rename "Meshes (visibility)" panel to "Visible" (localised)               |
| 1.230.5    | Add `Mouse` + `Visible` msgids to all 21 locale catalogs                  |

---

## Where we left off (top of next session)

Last user message before "pack it up": **the wheel-spin / pad-rotation
relationship** on Archer.  Two threads were left mid-air:

### 1. F3 recorder wheel-angle lookup (real, easy fix)

`recorder._build_chain_focus` does
`names_road.index(wheel_name)` to look up the spin angle, but
`wheel_name` comes in as bare (`W_L1`) while `tp.wheel_bone_names`
stores the `_BlendBone` suffix form.  Every lookup raises
`ValueError`, ri stays -1, and `spin` defaults to 0.0.  All F3
recordings since v1.223.0 report `wheel_angle_rad = 0.0` for
every wheel.

I drafted a fix (= try both forms with `.endswith('_BlendBone')`
fallback) and reverted it when the user said "i dont think that
is a bug in the recorder.. see if the wheel physics is causing
it".  I then proved via a direct call test that
`advance_wheel_angles` itself works -- 30 frames at v=0.14 m/s
over Archer's R_pitch=0.269 m correctly accumulates -0.2603 rad
per wheel.  The physics is fine; the recorder lookup IS the
reason the JSON shows zeros.

**Next session: apply the lookup fix in `recorder.py:
_build_chain_focus`** (try `wheel_name`, then `wheel_name +
'_BlendBone'`, then strip `_BlendBone` from `wheel_name` and
retry).  Confirmed bug; user just wanted the WHEEL PHYSICS
investigated first.  That's done.

### 2. "There for sure is a bug in the way the pad rotation is set"

User's exact words after the wheel-physics investigation came back
clean.  They want the PAD MESH ROTATION (= the per-pad
`build_oriented_transforms` orientation matrix) investigated.

What I know so far:

* Pad transforms are built from `pad_positions` + `pad_tangents`
  via `track_pads.build_oriented_transforms` (line 306+).
* Per-pad up direction = `pos - centroid` projected to YZ, normalised.
* Per-pad forward = `pad_tangents` (= central-chord tangent from
  the runtime override at `viewer.py:7992-7999`).
* Per-pad right = `cross(fwd, up)`.

Then `viewer.py:~6373` runs an orientation OVERRIDE for arc pads:
overrides `up` with `pos - hub` (= exact radial outward at the
arc pad's own wheel).  Line pads keep chord-perp up with sign
inherited from the nearest arc pad.

I built a table of per-frame pad angles around each L-side wheel
for Archer's recording -- pad angles fluctuate by ~10 deg
around -90 deg (= wheel-bottom tangent) on the road wheels.  The
chain's `s_offset` advances by 2.76 m over the 2.39 s
recording, equivalent to ~10.26 rad of "wheel spin"
(s_offset / R_pitch).  Pads do move around each wheel as the
chain flows -- their MEAN-of-in-band angle stays near -90 deg
because as one pad leaves the band, another enters.

User hadn't said specifically what's wrong yet.  Three candidate
diagnostics for next session:

* **A.** Fix the recorder lookup (#1 above) and re-record so the
  raw `wheel_angle_rad` is in the JSON.  Compare directly
  against the computed-from-s_offset spin.  If they don't
  match, advance_wheel_angles isn't being called or is being
  reset.  If they DO match, look elsewhere.
* **B.** Add per-PAD tracking to chain_focus (= follow one
  specific pad index over time as it enters / wraps / exits a
  wheel, not band-mean).  Lets us see the pad's angle around
  each hub as a continuous function, which can be compared
  directly to the wheel's spin rate.
* **C.** Inspect the `build_oriented_transforms` + override
  rotation matrix per pad in a couple of frames -- look for
  pads whose `right` column != `cross(fwd, up)` or whose
  rotation doesn't smoothly advance with `s_offset`.

---

## Open items carried into next session

1. **F3 recorder wheel-angle lookup fix** -- 5-min surgical edit in
   `tankExporterPy/recorder.py` `_build_chain_focus`.  User
   acknowledged the lookup is the symptom; just wanted physics
   investigated first.
2. **Pad rotation bug (= the user's "for sure")** -- investigate
   per-pad rotation behaviour.  Probably in
   `build_oriented_transforms` or the orientation override.
3. **OVER_COMP fold (= the v1.225 reverted attempt)** -- v1.229.0
   addresses it for the SINGLE-bump case via state-gated drop.
   Could go further with a contact-state-aware tangent fitting,
   but no current visual complaint.
4. **Recording sweep across more tanks** -- v1.230.0 reclassifies
   W_-prefixed singular wheels.  Smoke-tested on Archer, T30,
   T92, Pudel, T110E4.  Wider sweep (via
   `cust_tools/scan_all_chassis_tracks.py` re-run) would catch
   any tank where my 0.7x-median radius threshold misclassifies.

---

## New tooling added this session (cust_tools/)

| script                                | purpose                                                     |
|---------------------------------------|-------------------------------------------------------------|
| `add_locale_entries.py`              | idempotent: insert/update msgids across all 21 `.po` files + recompile `.mo` via `build_locale_mo`.  Re-run any time new `_()` strings appear in code. |
| `diag_pbd_pileup.py`                 | load real tank, run production homie + PBD pipeline, perturb one road wheel, capture per-iteration pad snapshots, flag pile-ups. |
| `diag_pad_mesh_overlap.py`           | rectangle-SAT overlap detector for adjacent pad meshes.  Use to verify chain "spline-looks-better-pads-not-so-much" type complaints geometrically. |
| `plot_f3_frame.py`                   | render one frame of an F3 recording as a top-down side-view PNG.  Wheels coloured by state (CONTACT / HANGING / OVER_COMP / focus), chain pads in blue + orange polyline. |
| `plot_tank_chain.py`                 | per-tank homie chain plot with wheels labelled + chain pads.  Contact-point markers in green/red.  Helpful for "is the chain wrapping the right wheels?" diagnostics on a tank you've never opened. |
| `scan_f3_chain_flips.py`             | scan an F3 recording for angle-flip events around the focus wheels.  Reports per-wheel flip counts, frame-by-frame state correlation, timeline PNG. |

---

## Key architectural changes recap

### Loader (`tankExporterPy/loaders.py`)

* `wheel_roles` now post-processes singular `<wheel>` entries (= non-
  leading) based on radius.  W_-prefixed wheels with radius >= 0.7
  * median(group_road_R) get reclassified to `road_wheels_<side>`;
  smaller W_-prefixed wheels (= tensioners like T30's W_L8) stay
  in `idlers_<side>`.  Fixes Archer where W_L0/L5 were
  XML-classified as idlers but visually + physically road wheels.
* `<teethSyncs><sync>` now parsed into
  `info['chassis']['tooth_syncs']` per drive wheel
  (`{'startAngle': float, 'teethCount': int}`).
  `info['chassis']['segmentSyncPivotOffset']` also captured.

### Chain math (`tankExporterPy/track_homie.py`)

* `build_chain_segments` -- gained `inner_thickness=0.0` kwarg
  (v1.215.0) that inflates wheel wrap radii by
  `segmentsInnerThickness` so the chain rides the pitch circle.
* `compute_tooth_phase_offset` -- new helper that turns a drive
  sprocket's `<startAngle>` + `<teethCount>` into an
  arc-length phase shift, applied at pad-placement time so a
  pad always lands on tooth k=0.
* `compute_chain_total` -- new helper exposing the loop's natural
  length, used by viewer to scale `s_offset` across chain
  rebuilds so pads don't jump.
* `_filter_glancing_touches` -- written, then disabled.
  Geometric heuristic was wrong for Archer; the call site in
  `_order_loop` no longer routes through it.  Function stays in
  the file for the contact-state-aware future fix.

### PBD chain (`tankExporterPy/track_chain_pbd.py`)

* Step 2c (unilateral wheel push) rewritten:
  road / roller wheels use **penetration-scaled Y-only push**
  with direction locked by wheel kind (road = down, roller =
  up).  Sprocket / idler keeps full radial push.

NOTE: PBD remains GATED OFF in production (`if False and
self._use_chain_pbd ...` in viewer.py:8075).  None of the PBD
changes affect what the user sees.  Production = pure homie
output.

### Tank physics (`tankExporterPy/tank_physics.py`)

* `advance_wheel_angles` -- new `inner_thickness=0.0` kwarg.
  Wheel-spin omega = `v / (R + inner_thickness)` (= pitch
  circle) so wheels track the chain pitch circle, not the
  bare rim.  Sign of delta unchanged.
* `_step_pose_integrator` terrain-floor lift -- now ALSO tests
  every `extra_rotating_hubs` entry (= sprockets / idlers /
  return rollers) with `comp_cap = 0` (no suspension travel).
  Catches Archer's W_L0/L5 corner wheels when they were
  classified as idlers; harmless no-op on tanks where extras
  are high above terrain (sprockets at hub Y > 0.6).

### Viewer (`tankExporterPy/viewer.py`)

* `_compute_homie_chain_for_frame` -- builds a per-frame
  `chain_roles` filtered copy of `roles` that drops any wheel
  whose `tp.last_wheel_state == OVER_COMP` from the road-
  wheels list.  Replaces v1.225.0's geometric drop with a
  contact-state-aware version.
* `_render_aim_cursor` block (~line 18517) -- gated on
  `pygame.key.get_mods() & KMOD_ALT` OR
  `_pending_alt_capture is not None`.  Cursor hidden through
  the entire ALT screenshot lifecycle, including the post-
  release capture frame.
* `_chain_for_side` (inside `_compute_homie_chain_for_frame`)
  -- `s_offset -= compute_tooth_phase_offset(...)` per-side
  phase shift, replacing the v1.216 blind `0.5 * seg_len`.
* Mesh-window populate -- ribbon auto-hide is now gated on
  `track_segment_models['segmentModelLeft']` being set.  `_MAT`
  named meshes (BOTH `mesh.name` and `mesh.identifier`) default
  to unchecked unconditionally.

### Recorder (`tankExporterPy/recorder.py`)

* `chain_focus` field -- list of per-road-wheel dicts per side,
  each carrying:
  * `wheel_name`, `hub_chassis_yz`, `hub_R_m`, `inner_t_m`
  * `wheel_angle_rad` (= spin from `tp.wheel_angles_rad[i]` --
    **BROKEN by name-suffix mismatch, fix next session**)
  * `s_offset_m` (= chain flow accumulator for the side)
  * `pads`: list of `{idx, y, z, d_hub}` for pads within
    `1.5 * (R + inner_t)` of the hub.
* `_json_safe` -- recursive numpy/tuple/bytes -> JSON converter.
  Wraps `chassis_info_xml` and is registered as `default=` on
  `json.dump`.  Fixes the silent "nothing saved" caused by
  `track_sag_L/R` numpy-array tuples in
  `_pending_chassis_info`.

### UI (`tankExporterPy/ui.py`)

* `UIMeshWindow.populate` title now reads `_("Visible")`
  (renamed from "Meshes (visibility)").  `_` imported from
  `tankExporterPy.localization` at top of file.

### Localization (`tankExporterPy/locale/`)

* 21 catalogs updated with `Mouse` + `Visible` msgids.
* Tool path: `python cust_tools/add_locale_entries.py`
  whenever new `_()` strings get added to code.

---

## State snapshot for next session pickup

* **Production chain path** = homie (geometric) ONLY.  PBD is
  gated off.  All chain shape comes from
  `track_homie.compute_homie_chain` + the per-frame Z-flip /
  central-chord-tangent re-run in
  `_compute_homie_chain_for_frame`.
* **Production wheel spin** = `tp.wheel_angles_rad` updated by
  `advance_wheel_angles(v_L, v_R, dt, inner_thickness=...)`.
  Verified working via direct call test (30 frames @ 0.14 m/s
  -> -0.2603 rad).
* **F3 recordings save successfully** since v1.223.1.
  `chain_focus` data captured per-wheel since v1.224.0.
  `wheel_angle_rad` field STORES ZEROS due to lookup bug ->
  use `s_offset_m / -(R + inner_t_m)` as a workaround.
* **Archer specifically** -- after v1.227.0 + v1.228.0 +
  v1.230.0, its 6 W_L wheels all behave as road wheels with
  suspension + chain wrap + terrain check.  W_L0 / W_L5 no
  longer sink below terrain.  Visual "spline deform" still
  TBD per the open-pad-rotation thread.
* **`_MAT` rubber bands** hidden by default everywhere.  Track
  ribbon (`track_*Shape*`) hidden only on tanks that ALSO have
  `<trackPair><segmentModelLeft>`.
* **Cursor** hidden cleanly during ALT screenshot drag + release.

---

## Pickup checklist

1. Restart viewer.  Confirm `__version__` shows `1.230.5` in
   the title bar.
2. Load GB44_Archer.  Verify:
   * Chain visible.
   * W_L0 / W_L5 sit on / above terrain.
   * Mesh-window panel header reads "Visible".
   * `track_mat_L` / `track_mat_R` rows are pre-unchecked.
   * `Mouse` slider label is the active language's translation.
3. Re-take an F3 recording.  Note: `wheel_angle_rad` field
   still reports 0.0 until the recorder lookup is fixed.
4. Apply the recorder lookup fix (= drop the name-suffix
   mismatch).  Take another recording; verify field is
   populated.
5. With populated data, run the pad-rotation comparison again
   (see "open item 2" above) -- this is where the user wants
   to land next.
