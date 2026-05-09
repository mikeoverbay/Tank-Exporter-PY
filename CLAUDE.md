# Project Orientation — Tank Exporter PY (TEPY)

You are working on **Tank Exporter PY**, a Python/PyOpenGL rewrite of the
original VB.NET TankExporter for *World of Tanks*.  This file orients you
on conventions and known pitfalls so you don't re-derive what previous
sessions already worked out.

The user (mikeoverbay / "coffee") owns the original VB project and is
porting it.  Public repo: `mikeoverbay/Tank-Exporter-PY` on GitHub
(default branch `master`).

---

## Where we left off (handoff 2026-05-08, v1.106.0)

**Active workstream: Track NURB rendering.**  Phases A + B are
DONE -- the per-side spline runs every frame, deforms with wheel
suspension residuals, F8 toggles the line-strip overlay, LEFT /
RIGHT arrows inject manual test deflections (BACKSPACE resets).
Topology audited (no Z-flip needed).  See `docs/TRACK_PHYSICS.md`
"Status snapshot at handoff" + "Next session pickup".

**Top of next session's todo: Phase C -- per-pad mesh + instance
render** to replace the line-strip with actual track segment
geometry.  Discovery starts at the gameplay XML's
`<chassis>...<tracks><trackPair>` block; pad meshes likely live at
`vehicles/<n>/<tank>/normal/lod0/Track*.primitives_processed`.
Use `cust_tools/analyze_track_spline.py <tank>` to verify the
binding still works on whatever tank you start with.

Also pending Phase A2 generalisation: binding has only been
validated on T30; need a sweep across one tank from each major
nation (T110E4, Object 268, Maus, AMX 50B, plus the WD-only
Hotchkiss EBR edge case) before declaring it tank-agnostic.

Recent tank-physics work (v1.100.0 -> 1.106.0): two-pose
solver/render split + inertia damping, per-tank yaw cap from
`<rotationSpeed>` XML, F3 1-Turn / F4 Zig-Zag automated test
recordings to `test_runs/`, C-key 3rd-press crash fixed.  See
`docs/PHYSICS.md` and `CHANGELOG.md` for details -- those
landed cleanly and are stable.

---

## Where to look first

**Before any `Grep` over source code, run the doc search:**

```
python cust_tools/arch.py search "<topic>"      # grep docs, group by file + section
python cust_tools/arch.py list                  # enumerate every doc heading
python cust_tools/arch.py stale                 # flag docs older than the .py they describe
```

The docs folder is split per-domain and is the FIRST stop.  Picking
the right doc shortcuts most "where does X live" questions before
they turn into a code-grep round trip.

| Question | Read |
|----------|------|
| Topic → doc map | `docs/INDEX.md` |
| Wheel suspension, contact classification, plane fit, drive controls, settling math, total tank weight | `docs/PHYSICS.md` |
| Kinematic-bone-driven NURB track rewrite (in progress) | `docs/TRACK_PHYSICS.md` |
| Render passes, draw order, mesh-skip gates (solid / wireframe / normals / picker), camera modes | `docs/RENDERING.md` |
| Per-module file-by-file API breakdown, WoT-specific notes table | `ARCHITECTURE.md` |
| User-facing features / controls | `README_TANK_VIEWER.md` |
| Recent changes / why things are the way they are | `CHANGELOG.md` (newest entries first) |
| Coordinate-system conventions | `COORDINATE_SYSTEMS.md` |
| `.primitives_processed` byte format (read **and** write) | `VISUAL_PROCESSED_FORMAT.md` |

When you change code, update the matching doc IN THE SAME COMMIT.
`arch.py stale` will flag drift; don't let it accumulate.

---

## Tech stack reminders

* **PyOpenGL 3.3 core** + GLSL 330; geometry shaders for the
  surface-normal debug pass.
* **Pygame** for the window / input / UI rendering.
* **Tkinter** for modal file dialogs and the format / component pickers.
* **Blender headless** is the bridge for FBX / GLB / GLTF / OBJ I/O.
  Two runner scripts (`tankExporterPy/exporters/_blender_runner.py` and
  `tankExporterPy/importers/_blender_importer.py`) execute inside Blender's
  Python (`bpy` available, `tankExporterPy` package NOT) -- imports there
  must stay minimal.

---

## WoT format gotchas to keep in mind

These bit me at least once.  Don't let them bite you again.

### `.primitives_processed` has TWO section layouts

Detected on the writer side from `mesh.name` (the source section base
name preserved by the loader):

* **Bare-shared** -- one global `indices` + `vertices` (+ `uv2`)
  section pair, group_count in the indices header reports `N` primitive
  groups.  Visual references by index.  Used by hull / turret on
  G102_Pz_III.
* **Named-per-mesh** -- one `<base>.indices` + `<base>.vertices`
  (+ `<base>.uv2`) per mesh, each with `group_count = 1`.  Visual
  references by name (`<primitive>exportChassL1_Shape</primitive>`).
  Used by chassis / gun on G102.

The encoder picks the layout from `mesh.name`.  **Never** substitute
`mesh.identifier` (the visual-side material label) for the section
name -- they are different fields and the engine will fail the lookup.

### UV2 BPVT preamble is 136 bytes, not 132

Mirrors the `.vertices` preamble: 68-byte primary format string
(`BPVSuv2`) + 64-byte secondary (`set3/uv2pc`) + uint32 count + body.
A 132-byte probe matches by integer-division coincidence and silently
shifts the UV stream forward by 4 bytes -- this was a real bug we
fixed; see CHANGELOG 2026-05-05.  When probing, also require
`(size - body_offset) % 8 == 0` to kill the false positive.

### Custom split normals (FBX round-trip)

The Blender runner (export side) sets per-loop split normals via
`me.normals_split_custom_set_from_vertices`; the importer reads them
via `me.corner_normals[li].vector` (Blender 4.1+) or
`me.calc_normals_split() + me.loops[li].normal` (3.x / 4.0).  FBX
import passes `use_custom_normals=True` so split normals survive the
round trip without Blender re-computing them.

### WoT* color attributes carry the full skin / TBN data

Tangents, binormals, bone indices, and bone weights ride through FBX
/ glTF as named `FLOAT_COLOR` attributes
(`WoTTangent`, `WoTBinormal`, `WoTBoneIdx`, `WoTBoneWeight`) so the
round trip is loss-less.  Decoders are documented in
`tankExporterPy/importers/_blender_importer.py`.

### `.fbm` folders on FBX export are bad

If you set `path_mode='COPY'` on `bpy.ops.export_scene.fbx`, Blender
clones every referenced texture into a `<basename>.fbm/` folder next
to the FBX -- duplicating the files we already wrote to
`<basename>_textures/`.  Use `path_mode='AUTO'`.

### Pre-warming pkg ZipFile handles is the perf win

`PkgExtractor.__init__` opens every kept pkg via `_get_zip()` once at
startup and caches the handle.  That avoids per-load central-directory
re-parsing.  Skip map pkgs (regex `^\d+_`) -- they don't carry tank
assets.

### Persisted-entries cache is batched, not per-write

When PkgExtractor finds an entry via scan-fallback, it queues the
discovery.  `flush_persisted_entries()` writes them all at once at the
end of `load_vehicle`.  We previously rewrote the 15 MB
`xmlitemslist.xml` per discovery (20-30 times per fresh tank load);
batching dropped a 7.7 s first-chassis-load to ~70 ms.

### Never ship Wargaming pixels

Every PNG that used to live under `resources/fire/` and
`resources/smoke/` was a slice of WoT's `eff_tex.dds` particle
atlas.  We can't redistribute those.  They're now gitignored, and
`Viewer.__init__` calls
`cust_tools.extract_wot_fire_atlas.ensure_runtime_flipbooks()` at
startup -- which checks if those folders are empty and, if so,
re-extracts the atlas from the user's local `particles.pkg` and
slices the two grids the runtime actually consumes (`fire_BIG` ->
`resources/fire/`, `smoke_white` -> `resources/smoke/`).  The
Wargaming bytes never enter the repo.  Same rule applies to any
future texture pulled from a pkg: gitignore the destination,
trigger an extract from the user's install on demand.

### Tileable textures must be UNIFORM at far mip levels

Two separate problems show up when you tile a procedural
texture across a large terrain via `GL_REPEAT`:

1. **Seam discontinuity at the edge.**  Fix: every layer the
   painter emits must wrap.  The canonical recipe in
   `cust_tools/paint_sand_desert.py` is:
   * Sin waves at integer cycles per tile (`freq * tile_meters`
     must land exactly on a whole number).
   * Noise / fBm built via FFT-domain low-pass filtering -- the
     DFT inherently treats input as periodic so anything filtered
     in frequency space comes out spatial-periodic for free.
   * Gradient passes (e.g. fake-shading) via `np.roll` central
     differences, NOT `np.gradient` (which has open boundaries
     and leaves a thin frame around each tile).
   * Rotating a sin wave's direction breaks integer-cycles -- the
     warp-the-coordinates approach gives directional variety
     without losing periodicity.

2. **Per-tile colour shift visible at distance.**  Even with
   perfectly tileable seams, low-frequency content within the
   tile shows up at far mip levels as a "rectangle grid" because
   GL averages each tile down to a pixel.  Fix: FFT high-pass
   the surface BEFORE colorizing; suppresses anything below ~12
   cycles per tile so block-mean variation at any mip level
   stays under 1 % of dynamic range.  Without this, tonal-drift
   layers (low-freq noise contributing to colour) become
   wallpaper at distance.

### SC_UBYTE4_REVERSE_PADDED bone bytes -> `byte / 3` is the index

Bigworld vertex skinning stores `iii` (bone index triplets) and
`ww` (weight triplets) in the SC_UBYTE4_REVERSE_PADDED format
(per `shaders/formats/*.xml` in the engine).  The byte values
are NOT direct indices into the renderSet bone palette -- they
are `palette_idx * 3`, because the bone matrix uniform array
is uploaded as `vec4 bones[N*3]` (each bone = three vec4s for
a 3x4 affine matrix).  The shader vertex stage reads each
byte + offset directly into the flat-vec4 array.

So when the picker reports `bone bytes [27 0 3 0]` for a vertex,
the actual palette indices are `[9, 0, 1, 0]`.  Look those up
in the renderSet `<node>` list (parsed from
`Chassis.visual_processed`) to recover the real bone names.

`cust_tools/dump_track_skinning.py` and the per-tank vertex-group
dumps in `hand_off/TRACK_SKINNING_*.md` always do the divide-by-3
before looking up the palette.

### Track skinning rig: per-wheel Z windows + 2-bone blends

Every WoT chassis I've inspected (T110E4, T92) follows the same
track-skinning architecture:

* Bottom run is segmented into N contiguous Z windows, one per
  main road wheel; each window's verts are dominantly bound to
  that wheel's `Track_<side><i>_BlendBone`.  Wheel ordering
  is rear-to-front (`Track_L0` is rearmost).
* ~138 verts per wheel zone (give or take, with the front /
  drive-sprocket wheel slightly denser).
* Top run + nose + tail bind 100% to `V_BlendBone` (chassis
  root) -- never sag.
* Transition zones between adjacent wheels use 2-bone blends at
  discrete byte-quantised weights: `0.502 / 0.6 / 0.7 / 0.8`
  (bytes 128 / 154 / 179 / 204) -- no continuous gradient.
* Per-vertex 3- or 4-bone blends are NOT used by the track mesh
  on any tank we've examined.  Only V_BlendBone (1 slot) +
  pairs of Track_<side><i> (2 slots).

Reference write-up: `hand_off/TRACK_SKINNING_T110E4.md`.

### Tank-physics: pivot is V_BlendBone at chassis-local origin

Chassis pose composition in `tank_physics.py` is `T(pos) @ Ry(yaw) @
Rx(pitch) @ Rz(roll)`.  All three rotations apply about chassis-local
`(0, 0, 0)` -- which coincides with `V_BlendBone`'s bind position
(V_BlendBone's parent V is at +1.139 Y, V_BlendBone has a -1.139
local offset that cancels it).  V_BlendBone in bind pose sits at the
**ground-plane center** of the tank, so when the chassis tilts, the
front rises / rear drops symmetrically about that point.  This is
intentional and matches the WoT convention.

Suspension travel happens at the `Track_<L|R><i>` ground nodes,
NOT V_BlendBone.  Each Track_L\<i\> is a parent of W_L\<i\> (the
visible wheel) which sits +0.421 m above the ground node.  When
the engine deflects a wheel, it moves the GROUND NODE vertically;
the wheel rides along as a rigid child.  V_BlendBone is rigid-with-
hull -- everything under it (decorative WD_ wheels, virtual track
nodes) follows the chassis pose only, no per-wheel deflection.

### Tank-physics: classification uses hysteresis to kill oscillation

`update()` classifies each wheel as CONTACT / HANGING / OVER_COMP
each frame.  Without hysteresis, a borderline wheel flapped between
states every frame, dragging plane-fit pitch / roll with it -- the
chassis visibly rang.  Solution: 2 cm hysteresis margin at the
envelope edges.  A CONTACT wheel only flips to HANGING when its
delta clears `ext_cap - HYST`; a HANGING wheel only flips back when
delta climbs above `ext_cap + HYST`.  Same for OVER_COMP.  Don't
remove the hysteresis without putting something else in to
break the feedback loop.

### Tank-physics: avoid mass-inertia inside iterative refinement

A v1.93.0 attempt to add critically-damped angular acceleration on
pitch / roll caused wild jumps.  Cause: the iterative refinement
loop calls `chassis_matrix()` repeatedly, each iteration reading
`self.pitch_deg` / `self.roll_deg` -- but those were the LAGGED
integrator outputs, not the target ones, so refinement saw stale
poses, classified wheels wrong, plane fit jumped wildly, integrator
chased -- positive feedback runaway.  Reverted in v1.93.2.  If we
revisit, compute the converged target OUTSIDE the refinement loop,
then ramp pos / pitch / roll once at the very end of `update()`.

### Picker / overlay shaders MUST consume `u_model`

The off-screen triangle picker and its overlay shader take the
same `mat4 u_model` uniform the main mesh shader uses.  Skip it
and the picker FBO renders every mesh from MODEL space while
the visible scene transforms each mesh by its per-frame
`mesh.model_matrix` (turret rotation, gun pitch, hardpoint
offsets).  Symptom: hover the (correctly-positioned) tank, get
hits on empty space below where the model-space geometry
happened to land in screen.  Both `picking.vert` and
`overlay_solid.vert` set `gl_Position = u_proj * u_view *
u_model * vec4(a_position, 1.0)`; `picker.update_pass` uploads
each mesh's matrix per draw and `picker.draw_overlay` re-applies
the picked mesh's matrix when painting the highlight.

### Spine-collapse override classifies by `group_id`, not x

The spine-collapse override at the end of `Viewer._on_resize`
hides every left-panel widget when `info_collapsed` is True.
Earlier versions discriminated "left vs right panel" by checking
`widget.x < INFO_PANEL_W` -- which mis-classified right-panel
widgets that hadn't been positioned yet (sliders default
`track_x = 0`, checkboxes default to `SLIDER_CB_X = 278`, both
< INFO_PANEL_W = 280) as left-panel and force-set
`visible = True`.  The result: when the right-panel Debug
section was pre-collapsed at startup, its sliders + checkboxes
appeared at (0, 0), painted over the top of the left pane.

The override now uses `widget.group_id` instead.  Right-panel
widgets carry `'smoke'` / `'fire'` / `'normals'` group tags at
`add_slider` / `add_checkbox` time; the override skips any
widget whose `group_id` is in that set, regardless of where it
happens to be positioned.  Robust against any future
"layout-skipped-on-startup" case.

### Wireframe polygon offset

The wireframe-over-solid recipe uses
`glPolygonOffset(4.0, 4.0)` + `GL_POLYGON_OFFSET_FILL` on the
solid pass; the line pass renders at natural Z with the offset
flag disabled and the value reset to `(0, 0)`.  We tried
`(1, 1)` (z-fought on track segments), then a stacked `+2 / -2`
recipe with `GL_POLYGON_OFFSET_LINE` (still flickering at
grazing angles); `(4, 4)` push-back on fills only is the
landing place.  Don't bump higher without testing -- pushing
too far produces visible gaps where adjacent tris meet.

### First-tank-load slowness was Pillow + GL warm-up

A separate, later 6-second first-load stall turned out to NOT be
pkg I/O (PkgExtractor was 55 ms / 7582 ms = 0.7%).  It was Pillow's
DDS codec + the GL driver's BC-format / mipmap-gen pipeline JIT-ing
on the first batch of texture uploads.  After the first ~48
textures, every subsequent upload was fast for the rest of the
session.  Fix lives in `Viewer._prewarm_first_load_caches`: push
ONE real DDS (`resources/Details_map.dds`) through
`TextureLoader.load_texture` at splash so all the lazy init happens
during the already-expected startup wait.  Same routine also warms
`ArmorColorLoader` (`base_paints.xml`) and
`VehicleXMLLoader._shared_xml_cache` (5 component XMLs × 11
nations).  Result: first tank load is now indistinguishable from
the second.

---

## Build / version conventions

* Version lives in `tankExporterPy/__init__.py` (`__version__`).
* Bump it via `python cust_tools/bump_version.py {minor|major|patch}`
  after every meaningful change (the user wants this discipline kept).
* The "minor" digit climbs for "we added something or fixed a real
  bug"; "patch" for tiny touch-ups.  Major is reserved for the user's
  call.

---

## Diagnostic tools (under `cust_tools/`)

* `bump_version.py` -- CLI version bumper.
* `font_preview.py` -- pygame window cycling all installed fonts;
  used to pick the splash-banner cursive (`Gabriola`).
* `dump_sections.py <local_file>` or `<pkg> <internal>` -- prints the
  section table of any `.primitives_processed`.  First stop when
  debugging "why does the engine reject our file".
* `compare_sections.py <res_mods_tank_dir> <wot_packages_root>` --
  diffs every `.primitives_processed` under a res_mods tank against
  its pkg original at the section-table level.  How we verify
  round-trip correctness.
* `paint_sand_desert.py [--size 8192] [--seed 42]` -- paints a
  tileable procedural sand-desert texture (FFT-built, all layers
  guaranteed to wrap under `GL_REPEAT`) plus a grayscale height
  companion (`*_height.png`) the Terrain class auto-pairs as a
  detail-displacement layer.  Output defaults to
  `resources/sand_painted.png`; the terrain auto-loader picks
  that up over `resources/sand.png` when both exist.
* `dump_track_skinning.py <tag> [--side L|R]` -- chassis
  track-skinning analysis tool.  Walks `Chassis.primitives_processed`
  + `Chassis.visual_processed` to dump the renderSet bone palette,
  group every track vert by its dominant `iii` byte, and render a
  side-view PNG colouring verts by their dominant bone.  Built
  during the bone-blending investigation; canonical reference
  output for T110E4 lives at `hand_off/TRACK_SKINNING_T110E4.md`.
* `demo_terrain_corners.py [--x ...] [--z ...] [--yaw ...]` --
  sanity demo for `Terrain.sample_height` / `sample_heights`.
  Builds the heightmap headlessly (re-uses the same helpers
  `Terrain.__init__` calls without the VAO upload), samples Y at
  known points, drops a virtual T110E4 chassis on the terrain
  and fits a plane through its four corner wheel centres ->
  pitch + roll.  Plane-fit math (`fit_plane`,
  `normal_to_pitch_roll`) lives in the same file for easy lifting
  into a real physics pass later.

---

## What's NOT yet round-trip-safe

Don't claim these work without testing:

* **Vertex `.colour` write-back** -- parsed read-only.  Not yet emitted
  by the encoder, not yet round-tripped through Blender.
* **Visual-file rewrite for added meshes** -- adding new objects to a
  tank requires splicing fresh `<PG_ID>` blocks into
  `.visual_processed`.  Not implemented.
* **Building atlas rendering** -- atlas algorithm decoded from the
  PBS_tiled_atlas shader (see CHANGELOG 2026-05-04), but
  `BuildingLoader` not yet wired up.
* **Skinned bone-byte reverse-padding** -- `iii` / `ww` carry
  `SC_UBYTE4_*_REVERSE_PADDED_*` data per the official
  `shaders/formats/*.xml`.  We currently treat them as plain
  `4×uint8` because all we do is round-trip through Blender's
  `WoTBoneIdx` / `WoTBoneWeight` color attributes; the reverse padding
  matters only if we ever drive a real Blender skin cluster.

---

## When in doubt

* The user prefers **bumping a new version + writing real comments**
  over leaving a TODO.
* "Don't rename the parts" was a real complaint -- never invent or
  modify section / mesh names that came from a pkg.  Pass them through
  verbatim.
* The legacy VB writer at `reference/modPrimWriter.vb` is the
  ground-truth reference for the encoder logic when ours and the engine
  disagree.
* If a file format question can be answered by **looking at bytes from
  a real pkg**, do that first instead of theorising.
  `cust_tools/dump_sections.py` and ad-hoc hex dumps of pkg slices
  have ended several debugging sessions in minutes.
