# Changelog — Tank Exporter PY

A Python/PyOpenGL rewrite of the original VB.NET Tank Exporter for World of Tanks.
Entries are ordered newest-first.  Dates are best-effort: today's session is exact;
earlier entries are grouped by the milestone they belong to (no git history was
available at the time this file was written).

---

## 2026-05-15 (early morning)

### Cap zoom-out at dome radius (1.206.0)

Per Coffee 2026-05-15 ("stop allowing the zoom radius to
keep moving past dome radius"): `camera.distance` (orbit /
free cameras) and `_chase_distance` (chase cam) are now
hard-capped at `camera.max_eye_radius` at zoom-input time,
not just at view-matrix-output time.

The view-matrix clamp in `Camera.get_view_matrix` already
prevented the EYE from punching through the dome -- but the
underlying distance state kept growing each wheel-out tick.
Result: after extended zooming-out, the first several
wheel-IN ticks felt dead because they were just unwinding
accumulated overage; the visible zoom didn't start moving
until the distance dropped back below the cap.

Fix in `viewer.py` MOUSEWHEEL handler:

```
_max_r = camera.max_eye_radius
if chase:  _chase_distance = clamp(_chase * zoom, 0.5, _max_r)
elif orbit: camera.distance = clamp(camera.distance * zoom, 0.5, _max_r)
```

Ortho mode unchanged -- it doesn't use camera.distance.
Commander mode (camera_mode == 2) still ignores wheel zoom.

### Docs update for the 1.201.0 - 1.204.0 dome-cursor arc (1.205.0)

Per Coffee 2026-05-15 ("update docs bump and push"): docs
updated to reflect the post-1.204.0 state (dome cursor
decal removed, rotation-fix infrastructure reverted,
impact billboards depth-test off).

* `docs/RENDERING.md`:
  - Per-frame pass order updated: aim-marker pass now
    notes TERRAIN-only reticle render with DOME-skip;
    impact billboard particles call out the new
    depth-test-off behaviour.
  - "Aim cursor + shellhole decal subsystem" section
    dispatch logic updated to show `pass` for the dome
    branch + clarifying comment about the abandoned
    1.201.0 - 1.203.0 rotation work.
  - AimCrosshair section: clarified the class is now only
    used as the SS-compile-failed fallback on terrain
    (dome-via-flat-quad path removed).
  - New "Impact billboards" section describing
    depth-test off + the `y_offset` rationale.
  - New "Skipping the shellhole decal on dome impacts"
    section describing the `skip_decal` plumbing.
  - "Common failure modes" expanded with the new
    dome-specific entries (no reticle, explosions
    invisible without depth-test off, shellhole on dome,
    etc.).

* `hand_off/STATE_SNAPSHOT.md`: bumped to 1.205.0, recent
  series timeline now includes 1.201 - 1.204.

* `hand_off/HANDOFF_2026-05-15_dome_and_cursor.md`:
  appended a new "Update: 1.201.0 -> 1.204.0 dome cursor
  rotation arc" section walking through the three
  iterations + the final abandonment.  Two new "Lessons
  (continued)" entries: don't chase aesthetic fixes
  through render-state archaeology, and OpenGL depth
  writes are bypassed when depth-test is disabled.

No code changes in this commit.

### Drop dome cursor decal, revert rotation-fix code, fix dome explosions (1.204.0)

Per Coffee 2026-05-15 ("just remove the decal from the
dome, keep the ball" + "explosions are not rendered right
now on the dome" + "find when we tried to fix the rotation
of the cursor on the dome [and revert]"):

**(1) Cursor on the dome: ball only, no decal.**  Earlier
(1.201.0 - 1.203.0) we tried three iterations of getting
the SS volumetric decal projector to produce a correctly-
oriented reticle on the dome: surface-aligned cube via
frisvad basis (1.201.0), bitangent flip for Y orientation
(1.202.0), stable +Y seed for X-flip-on-pan (1.203.0).
The user's verdict ("just remove the decal from the dome"):
the reticle on a sky backdrop adds nothing useful, and
the rotation iteration was never going to converge.

`viewer.py` cursor block: the dome branch now does
`pass` (no decal render).  The blue ball at
`_aim_hit_world` still draws (it's outside the dome /
terrain branching), so the user still has an exact
"aiming here" cue on the dome.

**(2) Revert the rotation-fix infrastructure.**  With the
dome decal gone, the supporting code is dead weight:

* `tankExporterPy/skybox.py SkyDome.render`: removed the
  `glEnable(GL_DEPTH_TEST); glDepthFunc(GL_ALWAYS);
  glDepthMask(GL_TRUE);` block before Pass 2 (1.201.0 +
  1.202.0 depth-write enable).  Pass 2 goes back to
  depth-test OFF, depth-write OFF -- the dome's depth
  values no longer need to land in the buffer because
  nothing downstream uses them.
* `tankExporterPy/particles.py`: removed the
  `_build_decal_matrix_surface` function (frisvad-rotated
  variant of the decal-cube matrix, added 1.201.0,
  iterated 1.202.0 + 1.203.0).  Removed the
  `surface_aligned=False` flag from
  `ScreenSpaceDecals._draw_one` and `render_single` --
  callers don't need to know about a non-existent code
  path anymore.

The world-axis-aligned `_build_decal_matrix` (terrain
decals, shellhole decals, terrain cursor) is unchanged.

**(3) Impact billboards now render on dome.**  The
explosion fire + dust billboards were depth-test rejected
at dome impacts because `y_offset` shifts the billboard
in world +Y, which puts the quad in FRONT of camera for
terrain impacts (camera typically above the tank looking
down) but TANGENTIAL to the dome surface for horizon dome
hits -- the billboard's depth ends up equal to or
slightly farther than the dome surface, and `GL_LESS`
rejected every fragment.

`tankExporterPy/particles.py ImpactBillboards.render`:
added `glDisable(GL_DEPTH_TEST)` before the draw + a
matching `glEnable(GL_DEPTH_TEST)` in the cleanup.
Billboards now render unconditionally on top of whatever
was drawn before, regardless of depth state.  Trade-off:
a tank between camera and a terrain impact no longer
occludes the explosion, but for a debug viewer that's
acceptable.

**Net code impact**: viewer.py +40 / -64, particles.py
+much less / -184, skybox.py -26.  Reverted code lives in
the git history at 1.201.0 - 1.203.0 if a future session
wants to revisit a different dome-cursor approach.

### Stable dome cursor basis + skip shellhole on dome (1.203.0)

Two follow-ups to 1.202.0:

**(1) Stable dome cursor tangent (no more X flip on pan).**
1.202.0's `_build_decal_matrix_surface` picked the frisvad
seed by smallest `|n.axis|`.  For dome horizon hits the
typical seed was +Y (since `|n.y| ≈ 0`), but when panning
across the world XZ cardinal lines (where `n.x` or `n.z`
crosses through 0), the seed switched to +X or +Z and the
tangent rotated by ~90 deg discontinuously -- the cursor
texture flipped its X axis visibly mid-pan.

Fix: force seed = world +Y for dome cursors (handled
inline in `_build_decal_matrix_surface`).  Falls back to
+X only when `|n.y| > 0.99` (zenith / nadir, where
`cross(Y, n)` degenerates).  Tangent now varies smoothly
as the hit sweeps around the horizon ring.

**(2) Skip shellhole decal on dome impacts.**  Coffee
("remove shellhole birth if we are on the dome when a
shell hits"): the persistent shellhole decal doesn't make
sense on the dome -- the dome is a backdrop, not a
surface that bullets dig into.  The fire / dust explosion
billboards still fire (visually consistent with terrain
hits).

Plumbing:
* `tankExporterPy/impact_pool.py` Impact + ImpactPool.hit
  take a new `skip_decal=False` kwarg.  Stored on the
  Impact instance.
* `tankExporterPy/particles.py` `ScreenSpaceDecals.
  render_impacts` and the legacy flat-quad `Decals.render`
  filter out impacts where `skip_decal=True`.
* `tankExporterPy/viewer.py` `_fire_round` impact handler
  computes `skip_decal` by comparing `|impact_pos|` to
  `self.skydome.radius` (1 m slop to account for the snap
  to `target_pos` and float precision).  Hits within slop
  of the dome radius get `skip_decal=True`.

### Dome cursor fixes: depth-test on Pass 2 + bitangent flip (1.202.0)

Two bugs in 1.201.0 caught + fixed:

**(1) Dome Pass 2 depth write was a no-op.**  OpenGL spec
gotcha: when `GL_DEPTH_TEST` is DISABLED, depth-buffer
writes are bypassed entirely, REGARDLESS of `glDepthMask`.
1.201.0's `glDepthMask(GL_TRUE)` on Pass 2 didn't actually
land any depth into the framebuffer.  Confirmed empirically
with `glReadPixels(... GL_DEPTH_COMPONENT ...)` at the
projected cursor pixel: the depth buffer at dome pixels
stayed at the cleared far value (1.0) even though the
mask was on.

Fix in `SkyDome.render`: enable depth test with
`glDepthFunc(GL_ALWAYS)` (every fragment passes) alongside
the mask, so the writes actually go through.  Cleanup
block already restores `GL_LESS` for downstream passes.

After the fix, the cursor pixel's framebuffer depth
matches the CPU-projected expected depth to within
float precision -- the SS reconstruction in
`decal_project.frag` now correctly lands on the dome
surface.

**(2) Dome cursor reticle was Y-flipped vs terrain.**
With the surface-aligned cube building its basis as
`b = cross(n, t)`, the bitangent at a horizon dome hit
ended up pointing in world +Y.  Combined with the SS
shader's `tex_uv = local.xz + 0.5` mapping and the
two-flip texture upload chain (`make_cursor.py` flip +
`_load_decal_tex_2d` flip + GL's bottom-left-origin
upload), the result was that upper-screen cube fragments
sampled the upper half of the GL texture -- which holds
the S area of the reticle (N is near V=0.1, S near
V=0.9).  Cursor appeared with S at the top of the
reticle on the dome.

Fix in `_build_decal_matrix_surface`: `b = cross(t, n)`
instead of `cross(n, t)`.  Negates the bitangent so
upper-screen cube fragments now sample the lower half
of the texture (N area).  Handedness of the local basis
no longer matches a strict right-handed (T, B, N) frame
but the only shader consumer is `local.xz` for UV --
handedness is irrelevant there.

Terrain unaffected: terrain uses the world-axis-aligned
matrix (no frisvad basis), which doesn't go through
`_build_decal_matrix_surface`.

### Dome cursor via SS projector + dome depth write (1.201.0)

Per Coffee 2026-05-15 ("turn depth write on for 2nd dome
edge hiding draw and convert cursor to draw on it using
the decal projection shader"): the cursor on dome hits now
renders through the SAME screen-space volumetric projector
as terrain hits, with the cube rotated to align with the
inward sphere normal.

Two coordinated changes make this work:

1. **`SkyDome.render` (`tankExporterPy/skybox.py`):** the
   second (edge-hiding) pass now has `glDepthMask(GL_TRUE)`
   enabled.  Pass 1 stays at depth-write OFF for the
   alpha-blended opaque seed; pass 2 writes dome depth into
   the buffer.  Depth TEST stays off on both passes -- the
   dome is still a pure backdrop without self-occlusion,
   only the depth value lands so downstream SS-projector
   passes can read it.

2. **Cursor dome branch (`tankExporterPy/viewer.py`):**
   was `aim_crosshair.render(pos, normal, decal_shader,
   ...)` (flat quad).  Now
   `aim_crosshair_ss.render_single(pos, normal,
   ss_decal_shader, ..., surface_aligned=True)` with the
   SS volumetric projector.  Flat-quad path kept as a
   fallback when the SS shader didn't compile.

Supporting plumbing in `tankExporterPy/particles.py`:

* New `_build_decal_matrix_surface(pos, normal, ...)` --
  frisvad-basis variant of `_build_decal_matrix`.  Builds
  a cube with `local +Y = surface normal`, `local +X / +Z`
  spanning the tangent plane.  Consolidates the same
  frisvad math that `AimCrosshair.render` uses inline.
* `ScreenSpaceDecals._draw_one` accepts `surface_aligned=False`
  (default), routes between the two matrix builders.
* `ScreenSpaceDecals.render_single` exposes the same flag.

Why the dome SS path was blocked before: the SS frag stage
samples scene depth at every cube pixel to reconstruct the
surface point.  When the dome ran with depth-write off, the
depth buffer at dome pixels held the cleared far value
(1.0).  The reconstruction landed at world infinity, fell
outside the cube's `[-0.5, 0.5]^3` volume, and discarded.
Enabling the write on pass 2 puts dome Z into the buffer
so the reconstruction lands on the dome surface where the
cursor's terrain-branch logic already knows how to handle
it.

Why the cube needs surface-alignment for the dome: with the
world-axis-aligned variant, the cube's projection axis is
world -Y (straight down).  On the dome, the surface normal
varies arbitrarily with the hit point (horizon hits have
near-horizontal normals).  A straight-down projection
would shear the cursor reticle along the dome wall.  The
frisvad basis aligns the cube's projection axis to the
inward sphere normal so the texture lays flat on the dome
tangent plane at every hit point.

Compass orientation: the frisvad seed is whichever world
axis has the smallest `|N.dot(axis)|`, so the on-dome
N/E/S/W tags reorient slightly based on the hit point.
The flat-quad path used the same logic, so the visual is
consistent with what shipped before -- only the
reconstruction-vs-flat-quad pipeline changed.

### Cursor draws once after shellhole decals -- final layout (1.200.0)

Per Coffee 2026-05-15 (the multi-message iteration of
"redraw the cursor after the shot decals are drawn" ->
"no change.  Cursor needs to draw before tanks and other
decals are drawn" -> "it should draw the ball again too"
-> "if we dont redraw the cursor, the decals will draw
over it.  can we just drop the first cursor pair draw
now.  It's setup now before its drawn now"): final
landing position of the projected aim marker is ONE draw
of (reticle + ball) right after the shellhole-decal pass,
both placed BEFORE any opaque mesh / tank render.

Final per-frame order around the cursor (`viewer.py:render`):

    terrain.render                          (writes depth)
    shellhole-decal pass                    (alpha, no depth write)
    aim-point marker (reticle + ball)       (one draw, depth-test on)
    background helpers (grid + axes)
    tank meshes (opaque)                    (writes depth)
    transparency layers (smoke / fire / etc.)
    debug overlays + UI

Why this works:

* The shellhole decals come BEFORE the cursor, so the
  cursor naturally renders on top of decal alpha at the
  same world point -- no second pass needed.
* Both the decal pass and the cursor pass operate on
  terrain-only scene depth (neither tank meshes nor any
  other opaque geometry has rendered yet), so the SS
  volumetric projector's world-space reconstruction lands
  on the right surface.  Without this, a tank rendered
  in front of the cursor would push tank Z into the
  scene-depth grab and the cursor's reconstruction would
  snap onto the tank surface or discard against the cube
  -- visually nothing would change after a decal landed.
* The ball's depth write is left at the default ON.  In
  the brief 1.198.0 - 1.199.0 iteration we drew the
  cursor twice and tried various depth-mask tricks; with
  ONE draw positioned AFTER the decal pass, the ball's
  depth no longer contaminates the decal `_grab_scene_depth`
  (decals already drew), so depth-write can stay on like
  any normal opaque mesh.

Why the late draw is safe (no startup-spiral regression):
the 1.190.0 fix that put the cursor right after
terrain.render was needed because the per-frame aim-hit
projection (`_drive_aim_from_aim_state`) ran late in the
render loop -- by the time the renderer reached the
cursor block, `_aim_hit_world` was still stale.  That
projection has since been moved to ~`viewer.py:18073`,
BEFORE any render call, so the cursor's world hit is
valid by the time the render loop reaches the new draw
site.  The right-after-terrain anchor is no longer
required.

Block is NOT gated on tank load.  `_aim_hit_world` is
set by the cursor's own terrain raycast + dome
intersection regardless of whether a tank is loaded, so
the cursor renders whenever the aim ray has a target.

Iteration churn: 1.198.0 added a second redraw of the
reticle after shellholes.  1.199.0 hoisted the shellhole
pass + redraw up out of the transparent-layer sequence
so the SS reconstruction got clean depth.  Both
intermediate states had real bugs (cursor invisible
under decals; ball obscured by decals; depth contamination
from first-pass ball write).  Final state in 1.200.0
collapses the two cursor blocks back into one
positioned where it needs to be -- after the decals --
which the early-projection move made possible.

### Bake cursor flip into the PNG, drop dead CPU UV transform (1.197.0)

Per Coffee 2026-05-15 ("UVs are created in the projection
shader. Don't do that if you are. It isnt working anyway.
Just flip the image you made"): correctly diagnosed the
prior problem -- the cursor on terrain hits goes through
the screen-space `decal_project.frag`, which COMPUTES UVs
from world-space reconstruction:

    vec2 tex_uv = local.xz + 0.5;

So the per-vertex UV transform I'd added in 1.195.x /
1.196.x was completely ignored on the active path -- only
the flat-quad fallback (dome hits) would have honoured it.
The "flip didn't work" symptom was the screen-space path
sampling the unflipped texture.

Fix: bake the up-down flip into the source PNG.

* `cust_tools/make_cursor.py` -- after `build_cursor()`
  draws the reticle in standard compass orientation, call
  `img.transpose(Image.FLIP_TOP_BOTTOM)` before saving.
  Resulting `Cursor.png` has N at the bottom of the image
  and S at the top so the screen-space projector's
  `local.xz + 0.5` mapping reads it correctly: bottom of
  image (N) lands at world +Z (= forward), top (S) at -Z
  (= back), E stays right (+X), W stays left (-X).
* `tankExporterPy/particles.py AimCrosshair` -- removed
  `flip_v` and `rotation_z_deg` instance attrs.  Removed
  the CPU UV transform loop in `render()`.  Per-vertex
  UV list is now the canonical (0,0)..(1,1) quad with no
  manipulation -- the flat-quad path still uses these UVs
  via `DecalShader`'s vertex stream, but the screen-space
  path ignores them in favour of its world-reconstruction
  formula.

### AimCrosshair up-down flip via `flip_v` knob (1.196.3)

Per Coffee 2026-05-15 ("the cursor needs up down flipped"):
re-introduced ONE vertical-flip knob on `AimCrosshair` (no
`flip_x` -- the user only needs the up-down flip).

* `__init__` -- new `self.flip_v = True` (default flipped --
  what the user wants) + `self.rotation_z_deg = 0.0`.
* `render()` UV transform -- when `flip_v` is True, every
  vertex's V is mirrored (`v = 1 - v`) before the optional
  Z-rotation around the (0.5, 0.5) centre.  Computed CPU-side
  before the VBO upload; shader stays a plain
  `texture(albedo, uv)` sample.

After the flat-quad projector's frisvad basis lays the cursor
on a +Y-up surface (terrain or dome), the reticle now reads
right-side-up to the user.  Both knobs are live-mutable for
future tweaks.

### Drop AimCrosshair flip_x / flip_y knobs (1.196.1)

Per Coffee 2026-05-15 ("remove image flips on the curser we
added last this version"): the regenerated `Cursor.png` in
1.196.0 already ships with the correct compass orientation
(N top, E right, S bottom, W left), so the `flip_x` /
`flip_y` attributes added in 1.195.0 were dead weight.

* `tankExporterPy/particles.py AimCrosshair.__init__` --
  `flip_x` and `flip_y` instance attrs removed.
* `render()` UV transform simplified: drop the per-axis
  scale step.  Pipeline is now {centre at 0.5, rotate by
  `rotation_z_deg`, un-centre} -- four multiplies + adds
  per vertex when the rotation is non-zero, identity-fast
  when it's the default 0.0.

`rotation_z_deg` stays exposed for callers that want to spin
the reticle (e.g. align with a chassis heading) -- single Z
knob, no flip axes underneath.

### Regenerate Cursor.png with correct compass orientation (1.196.0)

Per Coffee 2026-05-15 ("can you remake my curser file so the
headings initials are not upside down or backwards?"): the
original `resources/Cursor.png` (sourced from nuTerra) had
S at top, N upside-down at bottom, and E / W rotated 90 deg
to read from outside the ring.  Regenerated the texture so
every letter is in the standard compass position AND
right-side-up:

    N at top    (12 o'clock)
    E at right  ( 3 o'clock)
    S at bottom ( 6 o'clock)
    W at left   ( 9 o'clock)

New `cust_tools/make_cursor.py` script -- regeneratable, ships
with the repo, uses Pillow's `ImageDraw` for a fully-procedural
reticle.  Style preserved: double-ring outer band, central
target circle with centre dot, four horizontal + vertical
crosshair arms with 10 tick marks each (the 5th tick is
longer so the eye can count without straining), green
compass letters.  Transparent background; 512 x 512.

`tankExporterPy/particles.py AimCrosshair` -- reset the
orientation knobs back to no-op defaults:

    flip_x         = False    (was True)
    flip_y         = False    (was True)
    rotation_z_deg = 0.0      (was 180)

The previous flip+180-deg combo was mathematically identity
anyway (flip-X + flip-Y + rot-180 = double 180-deg rotation
= original), so the new file renders correctly without any
UV transform.  The rotation knob stays exposed in case a
future caller wants to spin the reticle (e.g. align with a
chassis heading).

With the WoT-convention frisvad basis on a +Y-up surface
(tangent = +Z forward, bitangent = +X right) the reticle's
labels now align with world axes automatically: N points
along world +Z (forward), E along +X (right), etc.

### AimCrosshair flip-X / flip-Y / rotation-Z, CPU side (1.195.0)

Per Coffee 2026-05-15 ("flip our curser image in x and y and
rotate it 180%. send a rotation on Z to it" + "we can pre
rotate before shader"):

* `tankExporterPy/particles.py AimCrosshair.__init__` -- three
  new instance attrs:
    - `flip_x = True`
    - `flip_y = True`
    - `rotation_z_deg = 180.0`
  All applied to the per-vertex UVs CPU-side before VBO
  upload.  Shader stays a plain `texture(albedo, uv)` -- no
  per-frame uniform plumbing or UV matrix.

* `render()` UV transform pipeline (per vertex):
    1. Centre the canonical UV at (0.5, 0.5).
    2. Negate X / Y axes when their flip flag is set.
    3. Rotate 2-D by `rotation_z_deg` around the centre.
    4. Un-centre back to (0..1).
  Pre-computed cos/sin outside the vertex loop -- six per-frame
  multiplies + adds total for the whole quad.

* `import math` added to the top of `particles.py`.

Tunables are live -- flipping a flag or bumping
`rotation_z_deg` at runtime changes the next frame's reticle
without restarting.  Default 180-deg rotation makes the
reticle read upright once the frisvad basis lays the quad on
a +Y-up surface (terrain) or onto the dome via the inward
sphere normal.

### XML bar -- cursor stays visible over the dropdown (1.194.1)

Per Coffee 2026-05-15 ("can you please fix the 'the matrix
files' dropdown bar and its contents to allow the mouse to be
visible? it needs to deal with that drop down height changing
after tank loads"): `_update_cursor_visibility` consulted
`UIManager.is_pointer_over_ui` to keep the OS cursor visible
over floating UI -- but the XML tab bar at the top of the
central viewport is painted by `_render_xml_bar` (a viewer
method, not a `UIManager` widget), so the UI-pointer check
returned False there and the cursor went dark the moment it
crossed onto the bar.

Fix: when `is_pointer_over_ui` says no, also call
`self._xml_bar_hit(mx, my)`.  Non-None return means the
pointer is in the header row OR (when expanded) inside any
of the expanded-tab rects.  Both rect lists
(`_xml_bar_header_rect` and `_xml_bar_tab_rects`) are
repopulated EVERY frame by `_render_xml_bar` from the live
`_xml_bar_height()` value, so the hit-test automatically
tracks the bar's current size -- collapsed / expanded /
before / after tank load, no special-casing needed.

### Cursor projects onto the dome on sky shots (1.194.0)

Per Coffee 2026-05-15 ("if the curser is outside of the dome, i
want it to be projected on to the dome. it will need to be
front face aligned to the guns vector dome angles"): when the
cursor's terrain raycast misses, the aim system now intersects
the gun ray with the skydome sphere and places the cursor at
the dome surface point, tangent-aligned via the inward sphere
normal.

* `Viewer.__init__` -- new fields `self._aim_hit_normal`
  (precomputed normal for non-terrain hits) and
  `self._aim_hit_kind` (`'terrain'` / `'dome'` / `'sky'`).
* `_drive_aim_from_aim_state` -- terrain raycast miss branch
  now solves the quadratic `|origin + t*fwd|^2 = R^2` against
  `self.skydome.radius`.  Forward root (positive t) gives the
  dome hit point P.  Inward sphere normal `-P/|P|` stored on
  `_aim_hit_normal`; kind stored as `'dome'`.  When no dome
  is available it falls through to the old `'sky'` behaviour
  (`_aim_hit_world = None`, gun gets a synthetic 1500m sky
  endpoint for yaw/pitch targeting).
* `Viewer.render` aim block -- branches on `_aim_hit_kind`:
    - `'dome'` -> flat-quad projector with the stored inward
      normal.  Screen-space path skipped because the dome
      renders with depth-write OFF so the SS reconstruction
      would fall outside the cube and discard.
    - `'terrain'` (default) -> existing behaviour: SS projector
      if depth-grab succeeds, flat-quad fallback otherwise,
      finite-difference terrain normal.

The cursor's textured face now points back along the inward
sphere normal -- that's the direction toward the camera area
-- so the decal reads "stuck to the inside of the dome at the
gun's hit angle".

### Remove red projectile debug sphere (1.193.2)

Per Coffee 2026-05-15 ("remove the red ball that tracks with
the bullets location"): the red `shot_sphere` was a debug
marker drawn at every active shot's current position (in-
flight) or impact position (post-impact) -- holdover from
the 2026-05-13 "fire red spheres at the terrain first" test.
The smoke trail + muzzle flash + impact billboards already
communicate the projectile's flight clearly, so the bare
sphere was just visual noise.

Removed the per-frame loop in `Viewer.render` that walked
`self.shots.active_shots` and called `self.shot_sphere.render`.
The `self.shot_sphere = Sphere(...)` instance stays allocated
in `__init__` (cheap, idle) in case a future debug toggle
wants it back; only the render path is gone.

### Remove redundant 2-D screen-overlay crosshair (1.193.1)

Per Coffee 2026-05-15 ("that curser on load we could not fix
is working because we changed the render order of it"): the
2-D screen-overlay crosshair landed in 1.187.0 to paint
`Cursor.png` at the viewport centre while no tank was
loaded.  Once 1.190.0 moved the world-projected aim cursor
to render right after terrain, that change unblocked the
projected cursor at startup -- so the static 2-D overlay
was now redundant + would render alongside the world-tracked
one (two crosshairs visible at startup).

Removed every reference in `tankExporterPy/viewer.py`:

  * 5 init fields (`_load_cross_tex` / `_program` / `_vao` /
    `_vbo` / `_size_px`).
  * The `_render_load_crosshair` method.
  * The render-loop call site + its try / err-log.
  * The teardown cleanup block (4 GL-object delete clauses).

Net: only the world-projected cursor remains.  Drawn at the
mouse-aimed terrain point both at startup AND after tank
load, courtesy of the post-terrain render-order change.

### Terrain clip past dome radius (1.193.0)

Per Coffee 2026-05-15 ("clip drawing map out side dome
radius"): the heightmap mesh extends to the corners of a
`world_size x world_size` square, but the skydome only covers
a `world_size / 2`-radius circle.  Terrain pixels past that
circle used to paint over the dome's underside near the
horizon.

Fix:

* `shaders/terrain.frag` -- new `u_world_clip_radius` uniform
  + a top-of-`main` discard:
  ```
  float d2 = world_pos.x^2 + world_pos.z^2;
  if (d2 > radius^2) discard;
  ```
  Cheap (no sqrt; one length2 + compare).  Pure clip; no
  alpha falloff because the terrain pass is intentionally
  opaque.
* `tankExporterPy/terrain.py Terrain` -- class-level
  `world_clip_radius = 1e9` default (no-op when the viewer
  hasn't written a real value).  `render()` feeds it as
  `u_world_clip_radius`.
* `tankExporterPy/viewer.py` -- right after the SkyDome is
  built, sets `self.terrain.world_clip_radius =
  skydome.radius` so the heightmap stops drawing at the
  dome's footprint exactly.

Net: the heightmap's square corners are clipped, the terrain
edge follows the dome's horizontal circle, and the sky
underside no longer competes with terrain pixels near the
horizon.

### Tank + camera clamped inside the dome (1.192.0)

Per Coffee 2026-05-15 ("tank and camera can NOT go out side
the dome"): clamps applied in three places so neither the
chassis position nor the camera eye can exit the skydome's
inner sphere.

* `scene.Camera` -- new `self.max_eye_radius` attribute
  (default `inf` = no clamp).  `get_view_matrix` clamps the
  computed eye to that radius: when the orbit distance
  would push the eye past the dome boundary, the eye is
  scaled back along the eye-origin ray.  Look-at direction
  + up vector untouched, so the orbit angle still reads the
  same to the user -- just zoomed in to the dome's edge.

* `Viewer.__init__` -- after the SkyDome is built, sets
  `self.camera.max_eye_radius = skydome.radius - 5.0`.
  Five-metre margin keeps the eye visibly off the sky
  surface (avoids depth-precision squinting near the dome).

* `Viewer.render` -- after `tank_physics.update(...)`,
  reads the tank's XZ horizontal position.  If
  `sqrt(x*x + z*z) > skydome.radius - 3`, scales (x, z) back
  along the origin ray so the tank sits on the inner-dome
  cylinder.  Mutates `tank_physics.pos` in place + re-reads
  `chassis_matrix()` so the rest of the frame sees the
  clamped pose.  Y is driven by terrain, doesn't need
  clamping.

* `Viewer._anchored_view_matrix` -- chase cam (and any
  non-orbit mode) clamps the computed `eye` against
  `self.camera.max_eye_radius` with the same scale-along-
  origin-ray recipe before the gluLookAt call.

`tank_physics.py` itself is NOT edited (per the locked-files
rule); we just mutate the public `pos` attribute, same as
the existing `_saved_tank_pose` restore path does.

### Unlimited shot range -- dome triggers impact (1.191.0)

Per Coffee 2026-05-15 ("remove range limit on gun.. if it hits
the dome, its done and should trigger the explosion emitter"):
removed the fixed range cap on the gun in `Viewer._fire_round`
and added a ray-vs-sphere intersection against the skydome as
the final fallback.

Two stages in the target-resolution:

  1. **Terrain raycast** -- same `_aim_point_on_terrain` call
     as before, but `max_distance_m` widened to `3 * dome_radius`
     (so any plausible terrain hit fits while the 1m march
     still terminates).
  2. **Dome intersection** -- if the terrain raycast misses
     (sky shot, off-map, no terrain in path), solve the
     quadratic
     ```
     | muzzle + t * fwd | ^ 2  =  R ^ 2
     ```
     for the FORWARD intersection (positive t).  R =
     `world_size / 2` matches the `SkyDome` in `Viewer.__init__`.
     Muzzle is inside the dome so the quadratic has exactly one
     positive root.

The resulting `target_world` flows through the existing shot-
pool pipeline -- `Shot.update` stamps `impact_pos = target_pos`
when the projectile arrives, the viewer's per-frame
impact-transition block detects the flip and calls
`self.impacts.hit(impact_pos, fwd, normal)`, and the existing
impact billboards / decals render at the dome-surface point.

Practical effect: a shot aimed at sky now travels until it
hits the inside of the dome -- you'll see the impact
explosion (dust + fireball, smoke) at whatever point on the
dome the shell would punch through.

### Projected cursor renders right after terrain (1.190.0)

Per Coffee 2026-05-15 ("draw projected cursor right after
terrain and before anything else"): moved the aim-point
marker block (screen-space crosshair + flat-quad fallback +
blue debug ball) out of the late site (right before the UI
overlay pass) and inserted it immediately after
`self.terrain.render(...)`.

Effects:

  * The screen-space decal projector now reads scene depth
    that contains ONLY terrain -- no tank or particles in
    the way -- so the projection lands cleanly on the ground
    where the cursor's terrain raycast computed.
  * Tank meshes + particles + impact decals all draw ON TOP
    of the cursor.  The cursor reads as a ground marker the
    rest of the scene paints over.
  * Replaced the old block with a one-line comment pointer
    so future readers can find the new home easily.

`_aim_hit_world` is still updated by the unconditional
`_drive_aim_from_aim_state` call later in the frame -- when
the marker renders right after terrain, it uses the PREVIOUS
frame's hit world.  One-frame lag is invisible at 60+ FPS.

### Chase cam horizon-locked (yaw only) (1.189.0)

Per Coffee 2026-05-15 ("chase camera stays planer to x and y.
i does not follow the tanks x y z rotations"): the chase cam
was transforming `eye_local` and `target_local` through the
FULL chassis matrix -- which includes the chassis pitch + roll
from `tank_physics.update`.  Result: camera rolled / pitched
with the tank on slopes.

Fix in `Viewer._anchored_view_matrix` (chase branch only):
strip pitch + roll from the chassis rotation before the
common `chassis @ eye_local` transform.  Built a yaw-only
chassis matrix by:

  * Extracting the chassis-local +Z direction's world image
    (`chassis[:3, 2]`).
  * Projecting that vector onto world XZ (drop the Y
    component), normalising what remains.
  * Building a fresh 3x3 yaw-only rotation:
      [[ cy, 0, sy],
       [  0, 1,  0],
       [-sy, 0, cy]]
    where `sy / cy` come from the projected forward direction.
  * Reusing the chassis world translation (so the camera
    still tracks the tank's position).

Then `chassis` (local var) is overwritten with the yaw-only
matrix for the rest of the chase-cam branch.  The common
`up_local = +Y` transform below picks up the new rotation's
identity-Y column, so the up vector stays world +Y -- the
camera never rolls.

Commander branch (`camera_mode == 2`) untouched -- still uses
the full chassis rotation since the "driver POV" should tilt
with the tank.

### Chase cam auto-active on tank load (1.188.0)

Per Coffee 2026-05-15 ("when a tank loads the camera should be
set to the back the tank looking ahead and down.  I want the
chase cam to be active after the tank is loaded"):
`Viewer.load_vehicle` now sets:

  * `self.camera_mode      = 1`       (chase, was 0 / orbit)
  * `self._chase_yaw_deg   = 0.0`     (eye DIRECTLY behind tank)
  * `self._chase_pitch_deg = 20.0`    (lifted so view tilts down)
  * `self._chase_distance  = 15.0`    (default zoom)

In chassis-local convention `yaw=0` is +Z (behind, since the
visible-front lives at -Z) and positive pitch lifts the eye --
combined, the camera sits behind + above the chassis looking
ahead and down at the turret hardpoint.

The `C` key still cycles `orbit -> chase -> commander` as before
so the user can drop back to orbit / commander views on demand.
The orbit yaw / pitch state isn't touched so flipping back to
orbit returns to whatever orbit pose was active before the
tank load.

### Load-time screen-overlay crosshair (1.187.0)

Per Coffee 2026-05-15 ("just make the render window a
crosshair at load time" + "hide it when a tank is loaded"):
draw `resources/Cursor.png` as a 2-D textured quad at the
centre of the 3D viewport when no tank is loaded.  Once a
tank is loaded (`self.meshes` non-empty) the overlay
suppresses itself.

Implementation (`tankExporterPy/viewer.py`):

* `Viewer.__init__` -- four lazy-init fields:
  `_load_cross_tex` / `_load_cross_program` /
  `_load_cross_vao` / `_load_cross_vbo`, plus
  `_load_cross_size_px = 128` for the on-screen pixel size.
* `_render_load_crosshair(scene_x, console_h, scene_w,
  scene_h)` -- new method.  Early-out when `self.meshes` is
  non-empty (= a tank is loaded) OR the viewport is
  degenerate.  First call builds resources:
    - Compiles a tiny textured-NDC-quad shader (re-uses the
      Splash module's `_VS` / `_FS` strings since it already
      supports the texture + tint uniforms we want).
    - Loads `Cursor.png` into a `GL_TEXTURE_2D` with mipmaps
      + CLAMP_TO_EDGE.
    - Allocates a 6-vert dynamic VAO/VBO.
  Per-frame builds an NDC quad sized
  `size_px / scene_{w,h}` so the crosshair stays a fixed
  128 px on screen regardless of window size.  Alpha-blended
  draw with depth test off so it composites on top of
  whatever's behind.
* `Viewer.render` -- calls `_render_load_crosshair(...)`
  inside the scene viewport, right before the UI overlay
  pass switches back to full-window.
* Cleanup wired into the viewer teardown path so the
  texture / VAO / VBO / program get freed.

### Mass revert of the cursor-at-startup experiment (1.186.0)

Per Coffee 2026-05-15 ("revert to before we tried to show the
curser at start up"): all of 1.185.4 through 1.185.14 was a
debugging spiral chasing a "cursor invisible at startup" issue
that compounded.  Reverted to functional 1.185.3 state where
the cursor appears once a tank loads:

  * `Viewer.__init__` -- `self._aim_hit_world = None` (was
    seeded to (0,0,0) in 1.185.5).
  * `_drive_aim_from_aim_state` -- restored
    `self._aim_hit_world = None` on sky / off-map raycast
    miss (was preserving last hit in 1.185.6).
  * Removed `_set_default_aim_hit_world()` method + the call
    from `run()` after splash cleanup (1.185.4).
  * Removed the per-frame live-aim-readout block (status
    line, gate diagnostics, cube diagnostics, depth-grab
    status) -- console no longer spammed every frame
    (1.185.7 / .8 / .9 / .13).
  * Removed the always-rendered origin reference sphere
    (1.185.11).
  * Merged blue debug ball back into the same try-block as
    the crosshair render (split in 1.185.10 -- not needed
    in the working state).
  * `DecalShader` instance re-coupled to shellhole PNG
    existence -- compiled only when `PBS_ShellHole_10_AM.png`
    is on disk (was unconditional in 1.185.8).
  * `_grab_scene_depth` restored to one try / one print()
    on failure (was status-string-tracked in 1.185.12 / .13).
  * Removed the hoisted depth-grab call in render's
    live-readout area (1.185.14).
  * Aim crosshair render block: restored simple
    `if SS available else flat-quad` elif chain (the
    `show_terrain`-gated SS-or-fallback expansion in
    1.185.5 / .8 is gone).

Net behaviour: cursor + ball reappear when a tank is loaded
and the user moves the mouse over the viewport, exactly as
before this debugging round.  All extraction / texture
loading paths (shellhole NM/GMM extracts on disk, etc.)
are unchanged on disk; only the runtime gates moved.

### Hoist depth grab to live-readout site (1.185.14)

Per Coffee 2026-05-15 (`depth_grab_status: never called`): the
aim render block (~2000 lines later in `render()`) was never
reached -- something in between (terrain pass, mesh pass,
particles, etc.) throws an exception or returns early, and
the SS branch that calls `_grab_scene_depth` is never hit.

Fix: hoist the depth-grab call up to the live-readout site
right after `_drive_aim_from_aim_state`.  The texture allocates
regardless of what bombs out downstream in render().  The aim
render still uses the cached `_scene_depth_tex` once / if it
runs.

Trade-off: at the new site the depth buffer holds the PREVIOUS
frame's content (most of the opaque scene hasn't drawn yet
this frame).  For a screen-space decal that's a one-frame
visual lag -- invisible at 60 fps and totally fine for the
crosshair which moves with the mouse.

Once you confirm `depth_grab_status: ok`, we can dig into
WHICH downstream pass is throwing.

### Persistent depth-grab status in live readout (1.185.13)

Per Coffee 2026-05-15 ("no scene depth texture was created..
check for errors"): my own per-frame `log_clear()` was wiping
the depth-grab error messages I added in 1.185.12.  They fired
ONCE on frame 0, then the next frame's clear wiped them before
the user could read.

Fix: store the result on `self._depth_grab_status` (string)
and add a fourth line to the per-frame live readout:
`depth_grab_status: <result>` -- green when ok, red on any
failure.  Possible values now visible every frame:

  * `ok` -- grab succeeded
  * `bail w=X h=Y` -- viewport was 0
  * `alloc <ExceptionType>: <msg>` -- texture allocation
    raised (glGenTextures / glTexImage2D / glTexParameteri)
  * `copy <ExceptionType>: <msg>` -- glCopyTexImage2D raised
  * `never called` -- the SS render branch hasn't reached
    `_grab_scene_depth` yet (= one of the upstream gates
    -- show_terrain, aim_crosshair_ss, etc. -- is False
    each frame, OR an earlier exception is bailing out of
    the render block before reaching the SS branch)

### Depth-grab failure now visible in console (1.185.12)

Per Coffee 2026-05-15 (`scene_depth_tex=185  <-- was zero at
load. never created texture needed`): the depth grab was
failing silently at startup -- the texture id stayed at 0 for
the first frame(s), the screen-space crosshair couldn't
project, AND the user got no diagnostic because the failure
message went through `print()` to stdout.

Rewrote `_grab_scene_depth`:

  * Three separate failure paths now route through
    `self.log(..., color=red)` so they're visible in the
    in-app console:
      - `bailing: w=X h=Y` (degenerate viewport)
      - `alloc failed: <type>: <msg>` (glGenTextures /
        glTexImage2D / glTexParameteri raised)
      - `copy failed: <type>: <msg>` (glCopyTexImage2D raised)
  * Success path also logs ONCE at first allocation:
    `allocated tex=<id> (WxH)` in green.

Restart and watch the console -- you'll see exactly which
step bails on the first frame.  Likely candidates:

  1. The first call lands BEFORE the GL context is fully
     valid (rare; the splash already exercised the context).
  2. `w` / `h` are 0 at frame 0 (viewport not yet sized).
  3. `glCopyTexImage2D` returns ENUM error because the
     framebuffer's depth attachment isn't readable at the
     moment we ask.

### Fixed origin reference marker (1.185.11)

Per Coffee 2026-05-15 ("z flipped?"): added an
ALWAYS-RENDERED reference sphere at world (0, 0, 0) so we can
distinguish two failure modes:

  * if the ORIGIN marker is visible but the AIM ball isn't:
    the ball-rendering code path works -- the bug is in
    `_aim_hit_world` (maybe a sign flip in the unprojection,
    or the camera default puts the hit point off-screen).
  * if NEITHER marker is visible: the ball-rendering path
    itself is broken (color_shader uniform, depth state,
    blend state, etc.).
  * if both appear: the system is working; restart your
    session to pick up the latest binaries.

Marker reuses `self.aim_hit_sphere` (blue) at world origin.
Wrapped in its own try / log so any exception lands in the
in-app console with the error type + message.

### Split aim ball + crosshair into separate try blocks (1.185.10)

Per Coffee 2026-05-15 ("I can not see the cursor until I load
a tank"): the aim-marker block in `Viewer.render` had the
crosshair AND the blue ball inside ONE try/except.  When the
crosshair branch (surface-normal sample, screen-space decal,
flat-quad fallback) threw at frame 0 -- before any tank load
warmed up the codepath -- the except handler swallowed the
exception and the BALL never drew either.  Once a tank loaded,
something in the warmup state succeeded and the entire block
ran cleanly.

Fix: render the ball in its OWN try-block BEFORE the crosshair
logic, so an exception in the decal projector only kills the
crosshair render; the ball still draws.  Also route both
exception logs through `self.log(..., color=(255, 110, 110))`
so the error shows up in the in-app console (not just stdout),
visible at runtime without watching a terminal.

### Live console: cube + depth-grab diagnostics (1.185.9)

Per Coffee 2026-05-15 ("is our cube not created either?"):
extended the live aim-status console with the actual cube
geometry + scene-depth state so we can rule those out as
gates:

  `cursor_cube_idx=36  shellhole_cube_idx=36  scene_depth_tex=N`

`cursor_cube_idx == 36` confirms the unit cube
(`build_unit_cube_vbo()` returns 36 indices = 12 tris) lives
inside the screen-space `aim_crosshair_ss` projector.  Same
for the shellhole projector.  `scene_depth_tex > 0` confirms
`_grab_scene_depth()` succeeded at least once -- if it's 0
the SS crosshair has nothing to project against.

The cube ISN'T a separate engine object -- each ScreenSpaceDecals
instance owns its own VAO/VBO/EBO for the cube (via
`build_unit_cube_vbo()` in its `__init__`).  So when
`ss_cursor=True` the cube is already there; this line just
confirms the index count looks right.

### Aim diagnostics + decouple DecalShader from shellhole PNG (1.185.8)

Per Coffee 2026-05-15 ("do we have the texture to project?
maybe that is only happening if we load a tank?"):

Two fixes:

* `Viewer.__init__` -- decoupled the DecalShader compile from
  the shellhole PNG existence.  Previously
  `self.decal_shader = DecalShader()` only happened inside an
  `if os.path.isfile(_decal_png):` block, so without a
  shellhole extract the shader stayed None, which in turn
  prevented `AimCrosshair` (the flat-quad fallback projector)
  from loading.  Now `DecalShader()` is compiled
  unconditionally; only `shellhole_decals` stays conditional
  on the actual shellhole PNG.

* Live console readout extended with the gate state.  Now
  the per-frame line shows:
  `ss_cursor=True  ss_shader=True  flat_cursor=True
   flat_shader=True  ball=True  terrain=True  show=False`
  -- green when every gate is set, red when any is missing.
  Lets the user spot which gate is open / closed without
  ad-hoc print statements.  (`show=False` for `show_terrain`
  is the most common "why isn't the cursor visible" cause --
  the screen-space crosshair needs terrain in the depth
  buffer to project onto.)

### Live console readout of aim world position (1.185.7)

Per Coffee 2026-05-15 ("clear the console and put the world
space postion in there so we can see if we are at least getting
a projectiong" + "live updates"): every frame the render path
now does `log_clear(status='Aim')` + `log("aim world: (x, y, z)")`
right after the per-frame `_drive_aim_from_aim_state` call.
The console shows the current `_aim_hit_world` in cyan when set,
or a red "<none>" when the value is missing.

Diagnostic for confirming the screen->world unprojection +
raycast are landing on a real terrain point.  When the user
sees `aim world: (+1.23, +0.05, -4.56)` they know the
projection is working even if the crosshair / ball isn't
visible for some other reason (depth grab, render gate, etc.).

### Aim cursor: stop wiping _aim_hit_world on sky miss (1.185.6)

Per Coffee 2026-05-15 ("i have no cursor.  is there a gate
stoping it from being seen? Tank loaded? not loaded? ready set
go?"): YES -- inside `_drive_aim_from_aim_state` (which runs
EVERY frame), when the cursor's terrain raycast missed (sky /
above horizon / off-map) the function set
`self._aim_hit_world = None`.

Both the crosshair and the blue ball gate on
`if self._aim_hit_world is not None:` -- so any time the user's
cursor pointed at the sky, both markers disappeared the next
frame.  The 1.185.5 default-init + seed-raycast did set the
value but it got wiped on the very first `_drive_aim_from_aim_
state` call after startup.

Fix: when the per-frame ray misses terrain, just don't touch
`_aim_hit_world` -- leave it at whatever last valid value (or
the (0,0,0) default).  The gun-targeting solve below still gets
the synthetic 1500m sky-fallback point for its yaw/pitch math,
so the turret keeps tracking the cursor direction.

### Default aim ball + crosshair fallback at startup (1.185.5)

Per Coffee 2026-05-15 ("i have no cursor at start" + "i need
the ball too, it looks amazing"): two combined fixes so the
crosshair AND blue ball both show on frame 0:

* `Viewer.__init__` -- `self._aim_hit_world` is now seeded
  with `(0, 0, 0)` instead of `None`.  The blue debug sphere
  + crosshair render blocks both gate on `_aim_hit_world is
  not None`, so a None init left them dark until the user
  moved the mouse.  Origin default keeps them visible from
  frame 0; the `_set_default_aim_hit_world()` raycast in
  `run()` then refines to the actual terrain hit a beat
  later.

* `Viewer.render` -- the screen-space (volumetric) crosshair
  branch is now gated on `self.show_terrain AND terrain is
  not None`.  Without terrain in the depth buffer the
  screen-space cube's reconstruction reads depth=1.0 (far
  plane) at every pixel and the unit-cube clip discards
  everything -> invisible crosshair.  The flat-quad
  `aim_crosshair` legacy projector doesn't have that
  dependency (just draws a textured quad in world space),
  so it now runs as the fallback whenever the screen-space
  path doesn't actually draw -- including when terrain is
  hidden, the depth grab fails, or the ss pipeline didn't
  initialise.

`_set_default_aim_hit_world` also got chatty -- prints the
eye / fwd / hit (or miss-with-reason) so we can debug when
the seed raycast bails.

### Default aim crosshair at app load (1.185.4)

Per Coffee 2026-05-15 ("i want my curser on at end of app
load... where it points can be just a default head position...
same math for finding where point when a tank is loaded"):
seed `self._aim_hit_world` once at startup so the aim crosshair
appears on the very first frame instead of waiting for the
user to move the mouse over the viewport.

New `Viewer._set_default_aim_hit_world()` helper -- pulled
straight from the camera-eye / camera-forward decomposition
the rest of the file uses (eye = -R^T * t, forward = -R[2]).
Feeds those into `self._aim_point_on_terrain(eye, fwd)`, the
SAME raycast `_drive_aim_from_aim_state` uses when a tank is
loaded.  Stash the result on `_aim_hit_world` so the crosshair
renderer picks it up on frame 0.

Called from `Viewer.run()` right after the splash cleanup, so
the camera + terrain are guaranteed initialised by then.
No-op when the terrain isn't loaded.

Logs `[viewer] default aim hit: (x, y, z)` so the seed value
is visible in the console.

### Skydome two-pass rotated seam blend (1.185.3)

Per Coffee 2026-05-15 ("double render map with depth write off.
draw, rotate _ .01 degree and draw skydome again"): the dome
is now drawn TWICE per frame, with a 0.01-degree Y-axis
rotation applied between the passes:

  Pass 1: `u_alpha = 1.0`, view = caller-supplied (opaque).
  Pass 2: `u_alpha = 0.5`, view = view * Ry(0.01 deg) (alpha
          blend over pass 1).

The tiny rotation shifts the equirect wrap seam between the
two draws; combined via 50/50 alpha blend the visible line
softens into a faint smear instead of a hard discontinuity.

Both passes keep depth test OFF + depth write OFF (the
backdrop convention from 1.185.2).  Render order in
`Viewer.render` is still: Skybox -> SkyDome (both passes) ->
Terrain -> meshes -> particles, so the dome reads as a
backdrop the rest of the scene overdraws.

* `shaders/skydome.frag` + `shaders/skydome_equirect.frag` --
  new `u_alpha` uniform feeds `FragColor.a` so the second
  pass can blend at <1.0.
* `tankExporterPy/skybox.py SkyDome` -- new class attrs
  `rotate_pass_deg = 0.01` + `rotate_pass_alpha = 0.5`.
  `render()` runs both passes back-to-back, enabling
  `GL_BLEND` (SRC_ALPHA / ONE_MINUS_SRC_ALPHA) for the pass
  duration and disabling it on exit.  Y-axis rotation
  matrix is pre-multiplied into `view` for the second pass
  (so the world spins under a stationary camera).

### Skydome: no depth test / no depth write (1.185.2)

Per Coffee 2026-05-15 ("i always turn depth testing off when
rendering it.. I write no depth info"): switched the SkyDome
to the classic backdrop recipe -- depth test OFF, depth
write OFF, rendered BEFORE opaque geometry so terrain / tank /
etc. naturally overdraw it wherever they sit closer to the
camera.

* `tankExporterPy/skybox.py SkyDome.render` --
  `glDisable(GL_DEPTH_TEST)` + `glDepthMask(GL_FALSE)`.
  Restores `GL_DEPTH_TEST` + `GL_LESS` + write on at the end
  of the call so downstream passes find the GL state where
  they expect it.
* `tankExporterPy/viewer.py Viewer.render` -- moved the
  `self.skydome.render(...)` call from AFTER terrain (post
  opaque pass) to BEFORE terrain (right after the infinite
  Skybox draw).  The terrain pass then writes depth in front
  of whatever dome pixels it covers; tank / decals / particles
  follow on top.

### Skydome seam fix -- manual LOD from world derivative (1.185.1)

Per Coffee 2026-05-15 ("our dome texture still has a visible
line  error in building the dome?"): the dome MESH is fine;
the seam comes from GL's auto mip selection misbehaving at the
`atan(-d.x, d.z)` discontinuity.  Where `d.z` swings sign at
`d.x=0` (the line behind the camera in world space), the U
coordinate jumps by ~1.0 across a single screen pixel, so
`dFdx(u)` reports a "the texture spans one pixel" rate and GL
snaps to a tiny mip level -- a blurry line shows up across the
back of the dome.

Fix in `shaders/skydome_equirect.frag`: compute LOD manually
from the WORLD-DIRECTION derivative (smooth across the seam,
no atan discontinuity), then sample via `textureLod`:

    du_rate  =  |dd/dscreen|  /  (2*pi * |d.xz|)
    dv_rate  =  |dd/dscreen|  /  pi
    LOD      =  log2(max(du_rate * tex_w, dv_rate * tex_h))

Clamped to `[0, 10]` so pole pixels (where `|d.xz| -> 0`
inflates `du_rate`) don't push us off the mip pyramid.  Proper
mipmap filtering preserved at distance + zero seam at the
wrap-around.

### Mass revert to pre-PBR decal projector state (1.185.0)

Per Coffee 2026-05-15 ("go back to last fix on the terrain
projector. find that and pull it.. pre pbr added"): the
decal-PBR + terrain-debug + camera-far branches between
1.181.0 and 1.184.0 cumulatively broke the render.  Reverted
the whole stack back to functional-1.180.5 behaviour:

* `shaders/decal_project.frag` -- removed the PBR uniforms
  (`u_normal_map`, `u_gmm_map`, `u_has_*`, `u_light_dir`,
  `u_cam_pos`) and the normal-map + Cook-Torrance branch.
  Frag is back to simple albedo + alpha-blend with cube-edge
  fade + age fade.
* `tankExporterPy/particles.py` -- `ScreenSpaceDecals.__init__`
  no longer loads NM / GMM sibling PNGs.  `_begin_pass` no
  longer sets PBR uniforms or binds extra texture units.
  `_end_pass` only unbinds the depth-tex unit.  `nm_path` /
  `gmm_path` / `light_dir` / `cam_pos` kwargs kept for API
  compat but ignored.
* `tankExporterPy/viewer.py` -- decal `render_impacts` /
  `render_single` call sites no longer compute the camera
  eye or pass `light_dir`.
* `shaders/terrain.frag` -- removed the `u_debug_flat` +
  `u_debug_flat_color` uniforms and the flat-shade branch
  (added in 1.181.0 -> 1.183.x).  Frag runs the original
  procedural path unconditionally.
* `tankExporterPy/terrain.py` -- removed the `debug_flat` /
  `debug_fill_color` class attrs and the conditional draw
  paths.  `render()` is back to a single draw call.
* `tankExporterPy/scene.py` -- `Camera.__init__` no longer
  carries `self.near` / `self.far` instance fields;
  `get_projection_matrix` is back to hardcoded
  `gluPerspective(fov, aspect, 0.1, 500.0)`.
* `tankExporterPy/viewer.py` -- removed the post-Terrain
  `camera.far = max(500, world_size)` bump.

The extracted shellhole NM / GMM PNGs stay on disk
(`resources/decals_pbs/`) -- harmless, costs nothing, and
re-enabling PBR in a future iteration will skip the
re-extract.  The extractor regex stays broadened too (no
reason to refuse a Wargaming-provided sibling).

### Camera far plane = world_size (the actual render flip) (1.184.0)

Per Coffee 2026-05-15 ("the entire render flips. when we
added the max view range, we did it wrong.. we should fix the
max view radius. not view range"): the 1.176.0 max-shot-range
patch extended the projectile cap to the map diagonal but
didn't touch the CAMERA's far clip plane, which was still
hardcoded at 500m in `scene.Camera.get_projection_matrix`.
On any map with `world_size > 1km` the skydome (radius =
world_size / 2) + far-side terrain rendered partly OUTSIDE
the view frustum.  Past the far plane:

  * depth values pile up at 1.0 with garbage precision;
  * the screen-space decal projector's `invMVP * (uv, depth,
    1)` reconstruction snaps to the wrong world point for
    those clipped pixels;
  * the decoded local-space normal flips orientation
    (different invMVP region) -- which is the "view z normal
    flip inside a radius" the user saw.

Fix:

* `scene.Camera.__init__` -- new `self.near = 0.1` /
  `self.far = 500.0` defaults.  `get_projection_matrix`
  reads them instead of the old hardcoded literals.
* `tankExporterPy/viewer.py` -- right after `Terrain(...)`
  succeeds, bumps `camera.far = max(500, world_size)` and
  `camera.near = 0.25` so the skydome + far terrain fit
  inside the frustum AND the depth-buffer dynamic range
  stays usable for close-up tank shots.

Logs `[viewer] camera frustum -> near 0.25 / far <N>` on
terrain load so the values are visible.

### Revert terrain to procedural texture + lighting (1.183.2)

Per Coffee 2026-05-15 ("render engine is cooked.. pull from
pre point i removed the generated terrain texture"): flipped
`Terrain.debug_flat` class default from `True` back to `False`
so `Terrain.render()` falls through to the original procedural
rendering path -- palette / sand texture / slope desat /
Lambert / fog / height darken / muzzle flash, all working as
they did before 1.181.0.

The flat-grey debug branch stays in place behind
`if (u_debug_flat == 1)` in `terrain.frag` -- flip
`Terrain.debug_flat = True` (class- or instance-level) to
re-enable it.  No other code paths touched; PBR decals, the
sky dome, the aim crosshair, etc. all keep working.

### Terrain debug Lambert uses precomputed normals (1.183.1)

Per Coffee 2026-05-15 ("there is no shading of the terrain
period.  DO the normals ahead of time.. it takes nothing to do
it and we wont have to be burdened in the shader with slow
functions"):

The IQ-style `dFdx`/`dFdy` derivative-normal trick added in
1.183.0 produced no visible shading on the actual mesh -- the
cross-product direction and screen-space derivative
orientations didn't line up the way I expected.  Switched the
`u_debug_flat` shader path to use the per-vertex
`v_world_normal` attribute instead.

That data is ALREADY computed ahead of time -- `terrain.py`
calls `_vertex_normals(positions, indices)` in `__init__`
(line 750) which builds area-weighted cross-product vertex
normals, uploads them to the `_vbo_normal` buffer at
location 1, and the vert shader forwards them as
`v_world_normal`.  Zero per-fragment derivative work; the
debug path is now one `normalize` + one `dot` + a mad.

Also bumped the direct-light coefficient from 0.78 to 0.95 so
the lit side reads visibly brighter than the 0.25 ambient
floor -- terrain contours pop clearly.

### Terrain drop wireframe + IQ derivative-normal Lambert (1.183.0)

Per Coffee 2026-05-15 ("the terrain has no good lighting.
remove the wireframe overlay.  use the IQ's (render toy) high
speed normal calculator to create the normals if we are not
already"):

* `tankExporterPy/terrain.py` -- removed the wireframe overlay
  draw call + the `debug_wire_color` class attribute + the
  `glPolygonOffset` / `GL_LINE` polygon-mode toggles.  One
  draw call per frame again.
* `shaders/terrain.frag` -- the `u_debug_flat` path now runs
  the IQ-style screen-space derivative normal:
  ```glsl
  vec3 dx = dFdx(v_world_pos);
  vec3 dy = dFdy(v_world_pos);
  vec3 N  = normalize(cross(dx, dy));
  ```
  followed by a wrap-Lambert `0.25 + 0.78 * max(dot(N, L), 0)`
  multiplied onto `u_debug_flat_color`.  No per-vertex normals
  used; cross product of the world-position derivatives gives
  the true per-triangle face normal.  Result: flat-grey terrain
  with per-triangle Lambert shading -- the polygonal landscape
  reads clearly from the lighting alone, no wireframe needed.

The procedural (non-debug) code path in `terrain.frag` is
unchanged -- it still uses the per-vertex `v_world_normal` for
its smooth-shaded lighting model.  Flip
`Terrain.debug_flat = False` to switch back to that path.

### Full-PBR shellhole decals (1.182.0)

Per Coffee 2026-05-15 ("we are using full pbr on our decals we
shoot on the terrain, right?  the texture sets are there I
think.. maybe a bit different channel coding"): honest pre-
state answer was NO -- the decal shader sampled albedo and
alpha-blended only.  This commit upgrades to the full WoT
PBR triplet.

**Channel coding (verified by inspecting the actual DDS bytes):**

  NM (normal map): DXT5nm BigWorld packing --
    R = 0
    G = Y component
    B = 0
    A = X component
    Z reconstructed via sqrt(1 - X^2 - Y^2).

  GMM (gloss/metallic/mask) -- on `PBS_ShellHole_10_GMM.dds`:
    R = ~constant (interpreted as roughness)
    G = sparse non-zero (metallic specks for debris)
    B = ~0 (unused for this asset)
    A = mostly opaque (visibility mask)

**Extractor** (`cust_tools/extract_wot_shellhole_decals.py`):
broadened the regex from `*_AM.dds` to `*_(AM|NM|GMM).dds` so
the auto-pull at startup now grabs all three.  23 PNGs land in
`resources/decals_pbs/` on a vanilla install (9 AM + 7 NM + 7
GMM; some old shellholes like `_114` and `_Iceland` only ship
AM).

**Loader** (`tankExporterPy/particles.py`):

* New `_load_decal_tex_2d(png_path)` helper -- consolidates
  the PNG -> GL_TEXTURE_2D upload (RGBA8, mipmaps,
  CLAMP_TO_EDGE, V-flip).  Shared across all three maps.
* `ScreenSpaceDecals.__init__` now takes optional `nm_path`
  and `gmm_path`.  Auto-discovers `<base>_NM.png` /
  `<base>_GMM.png` siblings when only the albedo path is
  provided.  Stores `nm_tex_id` / `gmm_tex_id` (0 when
  absent).
* `_begin_pass` accepts `light_dir` + `cam_pos` and binds
  NM at texture unit 2, GMM at unit 3.  `u_has_normal_map`
  / `u_has_gmm_map` int uniforms gate the shader path so
  the aim crosshair (no PBR siblings) still renders as
  flat alpha-blended albedo.

**Shader** (`shaders/decal_project.frag`):

* New `u_normal_map` + `u_gmm_map` samplers, `u_has_*`
  gates, `u_light_dir`, `u_cam_pos`.
* Decode tangent-space normal from DXT5nm (G = Y, A = X).
* Build the surface tangent space from the cube's world-axis
  orientation: local +X = world tangent, local +Z = world
  bitangent, local +Y = world surface normal.  Remap the
  decoded TS normal accordingly.
* Cook-Torrance-style specular: Schlick Fresnel (F0 = 0.04
  for dielectrics, mix to albedo for metals), GGX D, Smith
  G_SchlickGGX (separable).  Lambert diffuse scaled by
  (1 - F) * (1 - metallic).  Ambient lift of 25 % so
  shadowed pixels aren't pitch black.
* GMM defaults when NM is bound but GMM isn't: roughness =
  0.85, metallic = 0.0 (a sensible dirt-crater fallback).
* Falls back to flat alpha-blended albedo when both
  `u_has_*` flags are 0 -- aim crosshair / decals without
  PBR siblings still work.

**Viewer** (`tankExporterPy/viewer.py`):
Both call sites now compute the camera eye position from the
view matrix and pass it alongside `light_dir=(0.5, 1.0, 0.3)`
-- same sun direction the terrain shader uses, so decals
catch matching lighting.

Known limitations / future work:

* The view vector in the spec calculation is approximated as
  straight-down (vec3(0, 1, 0)) for horizontal terrain.  A
  proper world-space view dir needs the surface point passed
  through to the shader as a separate uniform (or recovered
  via a second invMVP).  Specular highlights are slightly
  off for grazing camera angles.
* No IBL environment lookup -- the dome cubemap could be
  sampled for environmental specular.  Substantial follow-up.
* GMM's B channel is currently ignored; if WoT uses it for
  AO or a secondary spec mask we should sample it once the
  convention is confirmed.

### Terrain flat-shade + wireframe debug look (1.181.0)

Per Coffee 2026-05-15 ("remove the generated texture mapping of
the terrain. shade it .3.3.3 grey with .3.3.7 wireframe"):
terrain renders as flat grey + blue wireframe instead of the
procedural palette / sand / lighting / fog stack.

* `shaders/terrain.frag` -- new `u_debug_flat` + `u_debug_
  flat_color` uniforms.  When `u_debug_flat == 1` the frag
  short-circuits to `FragColor = vec4(u_debug_flat_color, 1.0)`
  and skips every procedural step (palette / warp / slope
  desat / Lambert / fog / height-darken / muzzle-flash).
* `tankExporterPy/terrain.py` -- new class-level
  `debug_flat = True` (default), plus `debug_fill_color =
  (0.30, 0.30, 0.30)` and `debug_wire_color = (0.30, 0.30,
  0.70)`.  `render()` now does two passes:
  - Fill pass at .3 grey with `glPolygonOffset(2, 2)` so the
    wireframe wins the z-test.
  - Line pass at .3 .3 .7 with `glPolygonMode(GL_LINE)`.
  Both passes reuse the same `TerrainShader` -- only the
  `u_debug_flat_color` uniform changes between them.

Flip `Terrain.debug_flat = False` (instance- or class-level)
to restore the textured + lit rendering.  All the previous
procedural code paths in `terrain.frag` are untouched, just
gated behind the `u_debug_flat == 0` branch.

### Decal viewport-origin fix + smaller crosshair (1.180.5)

Per Coffee 2026-05-15 ("my zoom or anything else should affect
how that decal is placed.  It sits in world space at our look
at point on the terrain"): the decal cube IS positioned at
`_aim_hit_world` in world space, but the FRAG-shader depth
sample was reading from the wrong framebuffer region whenever
the 3D viewport didn't start at window pixel (0, 0) (which is
always -- the info / tree panels push it inward).

Root cause:

  `gl_FragCoord.xy` is in WINDOW pixel coords (not viewport-
  relative).  The depth texture, copied via
  `glCopyTexImage2D(scene_x, console_h, scene_w, scene_h)`,
  only covers the viewport region.  Sampling at
  `gl_FragCoord.xy / u_resolution` reads texture pixels offset
  by the viewport origin -- wrong depth -> wrong world
  reconstruction -> the cube's clip discards arbitrary pixels.
  Result: the decal appeared to drift in world space as the
  camera moved / zoomed.

Fix:

* New `u_viewport_origin` uniform in
  `shaders/decal_project.frag`.  Frag now computes
  `uv = (gl_FragCoord.xy - u_viewport_origin) / u_resolution`
  so the depth texture is sampled correctly regardless of
  viewport offset.
* `ScreenSpaceDecals.render_impacts()` / `.render_single()`
  + `_begin_pass()` thread `viewport_x` / `viewport_y` through
  to the shader uniform.
* `Viewer.render()` passes `viewport_x=scene_x,
  viewport_y=console_h` at both call sites.

Crosshair size: dropped from 12.0 m to 6.0 m so each visible
reticle tick lands at 0.5 m on the ground (was 1 m).  Per
Coffee 2026-05-15 ("drop its size to one tick = .5m").

### Blue debug ball back for crosshair calibration (1.180.4)

Per Coffee 2026-05-15 ("bring the ball back so i can check we
are hitting the target with the projection"): re-enabled the
`aim_hit_sphere` render at `_aim_hit_world` alongside the
screen-space crosshair so the user can eyeball whether the
projection lands on the same world point the raw aim-ray hit
gives.  Both draw with depth-test on so terrain occlusion
agrees between them.

If the ball sits in the centre of the crosshair, the
projector matrix is correctly calibrated.  Any divergence
indicates a depth-grab mismatch or matrix offset bug -- the
ball is the ground truth (`_aim_point_on_terrain` -> raw
world hit), the crosshair is the projector reconstruction.
Remove this block when calibration is complete.

### Decal projector axis-aligned with world (1.180.3)

Per Coffee 2026-05-15 ("something wrong with the matrix stack
for the projector.. it should always be on xy plane and
projecting in to y-"): the `_build_decal_matrix` used to spin
the cube around the surface normal via a frisvad basis -- on
sloped terrain this rotated the texture's compass directions
to arbitrary azimuths (the aim crosshair's N/E/S/W stopped
pointing at world cardinals).

Fix: drop the basis rotation, keep cube WORLD-AXIS-ALIGNED at
all times:

    local +X axis -> world +X  (size_x)
    local +Y axis -> world +Y  (size_z = thickness; projection
                                axis, -Y down)
    local +Z axis -> world +Z  (size_y)

Frag shader (`shaders/decal_project.frag`) samples UV from
`local.xz + 0.5` (the ground-plane axes) instead of
`local.xy`.  Edge-fade now operates on `|local.y|` (the
projection axis) instead of `|local.z|`.  These are the
Y-up equivalents of nuTerra's Z-up `(local.x, local.y) +
local.z` mapping -- same algorithm, swapped depth/UV roles
for our coordinate system.

`_build_decal_matrix(pos, normal, ...)` still accepts a
`normal` argument for call-site API compat but ignores it.
When raycast-vs-mesh impacts on non-ground surfaces land
(future work), the basis-rotation path can come back behind
a surface-type branch.

Verified: a decal at pos=(10, 5, -3) with size=4, thickness=2
produces a cube spanning x ∈ [8, 12], y ∈ [4, 6], z ∈ [-5, -1],
oriented strictly on world axes.

### Public unit-cube helper for projector geometry (1.180.2)

Per Coffee 2026-05-15 ("we need cube for the projection... it is
required for any Y height changes. cude is just 1 x 1 x 1
centered on xyz zero. Forward is z, -X - left, y+ up.  create
it as a list or vbo.. your call"): promoted the screen-space
decal projector's private `_CUBE_POS` / `_CUBE_INDICES` to a
public, documented utility in `tankExporterPy/particles.py`:

* `UNIT_CUBE_POS` -- (8, 3) float32 array of corner positions
  in [-0.5, +0.5]^3.  WoT axis convention: +X right, +Y up,
  +Z forward.
* `UNIT_CUBE_INDICES` -- (36,) uint32 array of 12-triangle
  indexed mesh.  CCW winding from OUTSIDE each face.
* `build_unit_cube_vbo()` -- allocates a fresh
  `(vao, vbo, ebo, index_count)` tuple for any caller that
  wants its own GL geometry.

`ScreenSpaceDecals.__init__` now uses `build_unit_cube_vbo()`
instead of inlining the buffer-build code.  Behaviour identical;
the cube data is interned in one place so future projectors
(impact box, debug volume) share the same recipe.

Underscore aliases `_CUBE_POS` / `_CUBE_INDICES` kept for one
release for any code path that still imports them privately.

### Mute the auto-paired detail-displacement heightmap (1.180.1)

Per Coffee 2026-05-15 ("remove the generated 2nd height map we
are using. mute it for now"): added a `_DETAIL_AUTO_PAIR = False`
guard in `Viewer.__init__` so `resources/sand_painted_height.png`
is no longer pulled in by default as a secondary terrain
displacement.  An EXPLICIT
`cfg['terrain_detail_heightmap']` value still activates the
secondary heightmap; everything else falls through to a single
primary heights array.  Flip the constant back to True to
restore the auto-pair behaviour.

Console now logs `[viewer] Terrain detail: muted (2nd heightmap
auto-pair disabled)` when the secondary map is skipped.

### Screen-space (volumetric) decal projector (1.180.0)

Per Coffee 2026-05-15 ("look here at my decal projector frag
and vert  C:\\nuTerra\\nuTerra\\shaders\\Terrain_shaders"):
upgraded the decal pipeline from flat oriented quads to the
proper screen-space (volumetric) projector technique used by
nuTerra's `DecalProject.{vert,frag}`.

Mechanism:

  1. Render terrain + tank into the framebuffer as usual.
  2. Snapshot the framebuffer's depth attachment into a cached
     `GL_TEXTURE_2D` via `glCopyTexImage2D`
     (`Viewer._grab_scene_depth(x, y, w, h)`).
  3. For each decal, draw a UNIT CUBE in world space oriented
     to the surface normal + scaled by the desired footprint.
     Cull-face OFF so the camera can be inside the cube.
  4. The fragment shader (`shaders/decal_project.frag`) reads
     scene depth at every cube pixel, reconstructs the world
     point via a pre-multiplied invMVP, clips anything outside
     the unit cube volume, and samples the decal texture from
     `(local.xy + 0.5)`.

Benefits over the flat-quad approach (which the new path
supersedes):

  * Decals conform to underlying geometry -- terrain
    undulations, tank hulls, walls -- without z-fighting.
  * No polygon-offset hacks.
  * Single cube draw per decal regardless of footprint
    curvature.

New files / classes:

  * `shaders/decal_project.vert` + `shaders/decal_project.frag`
    -- ported from nuTerra, re-targeted to GL 3.30 / forward
    pipeline (no UBO, no #include).  Soft fade on the +/-Z
    faces of the cube hides the hard discontinuity where a
    decal cube partially exits geometry.
  * `tankExporterPy/shaders.py` -- new `ScreenSpaceDecalShader`
    wrapper.
  * `tankExporterPy/particles.py` -- new `ScreenSpaceDecals`
    class with `render_impacts()` + `render_single()` helpers.
    Shared cube VBO across all decals.  `_build_decal_matrix`
    builds the orient + scale + translate matrix (frisvad
    basis on the surface normal).
  * `tankExporterPy/viewer.py`:
    - `self._scene_depth_tex` (lazy-allocated, reallocates on
      viewport resize).
    - `_grab_scene_depth()` does the
      `glCopyTexImage2D(GL_DEPTH_COMPONENT24, ...)`.
    - Shellhole decals + aim crosshair now use
      `ScreenSpaceDecals.render_impacts()` /
      `.render_single()`.  Legacy flat-quad `Decals` +
      `AimCrosshair` instances stay loaded as fallback for
      the case where the screen-space shader didn't compile.

Render order unchanged: decals draw FIRST in the transparent
sequence (right after `fire_smoke_particles`) so they sit
beneath every other alpha layer.

### SkyDome seam-hide via 0.99 U scale (1.179.1)

Per Coffee 2026-05-15 ("our skydome texture has a bad visible
seam at the wrap. Can we adjust U scale just a tad.. make it
* 0.99 in the shader?"): contracted the equirect U sweep by
1 % around its 0.5 center in `shaders/skydome_equirect.frag`:

    u = (u - 0.5) * 0.99 + 0.5;

The dome's 360-degree azimuth now samples texture columns
0.005 .. 0.995 instead of 0 .. 1.0, skipping the BC1
compression artefacts at the panorama's left + right edges
that produced a visible hard line at the wrap.  Centering the
contraction on 0.5 keeps the forward-direction's view of the
panorama in the same place; only the seam region shifts.

### Aim crosshair projector replaces the blue ball (1.179.0)

Per Coffee 2026-05-15 ("i need to see the bule ball even if a
tank isn't loaded.. project a crosshair texture at the ball
location and drop the ball" + "those ticks are scale 1 m
spacing"): the old blue debug sphere at `_aim_hit_world` is
replaced with a flat textured reticle on the terrain.

* `resources/Cursor.png` -- the reticle texture, sourced from
  nuTerra (`C:/nuTerra/nuTerra/Resources/Cursor.png`).  Not
  Wargaming-derived, NOT gitignored; ships with the repo.
  Circular crosshair with N/E/S/W compass labels + tick marks
  on each axis at 1 m spacing.

* `tankExporterPy/particles.py` -- new `AimCrosshair` class.
  Single-quad textured projector that reuses the existing
  `DecalShader` (the crosshair is just one more alpha-blended
  textured quad lying on a surface).  `size_m=12.0` default;
  tunable so the texture's tick marks land at 1 m on the
  ground.  No aging -- `u_fade_start=2.0` pins full opacity.
  Orients to the surface normal with the same frisvad-style
  basis recipe `Decals` uses.

* `tankExporterPy/viewer.py`:
  - Removed the `self.aim_hit_sphere.render(...)` call.
    `aim_hit_sphere` stays allocated for now (unused but
    cheap) -- can be deleted in a later cleanup pass.
  - Loads `AimCrosshair` at startup; gated on the existing
    `DecalShader` being available.
  - Renders the crosshair when `_aim_hit_world is not None`
    using the same surface-normal finite-difference recipe
    impacts use.  Cleanup wired in.

* `_drive_aim_from_aim_state` refactor -- the function now
  ALWAYS sets `_aim_hit_world` (when the ray hits terrain).
  The gun-targeting solve below was gated by `if no pivots:
  return` at the TOP, which meant `_aim_hit_world` never got
  set when no tank was loaded.  Moved that gate down past the
  raycast so the crosshair appears with the cursor regardless
  of tank-loaded state.

* `Viewer.render` -- added a second tank-independent call to
  `_drive_aim_from_aim_state(view, None, proj)` AFTER the
  tank-physics branch so the aim hit refreshes every frame
  even with no tank present.  Passes `chassis_pose=None`; the
  refactored function early-returns after the hit-set when
  pivots / chassis_pose are missing.

Net result: the crosshair appears under the cursor as soon as
the terrain is loaded, with or without a tank.  When a tank
IS loaded the gun still tracks the same point as before
(unchanged behaviour for the targeting solve).

---

## 2026-05-15 (early morning)

### SkyDome cull face fix (1.178.2)

Per Coffee 2026-05-15 ("the sky map renderer flip front face
setting for skydome render. I can see it unless im out side
the sphere"): the dome was invisible from INSIDE the sphere
(= normal play view) and only showed when the camera flew
outside.  Fix: change `glCullFace(GL_FRONT)` to
`glCullFace(GL_BACK)` (the GL default) in `SkyDome.render`.
My original winding analysis was wrong -- the UV-sphere
triangle pairs are wound such that GL_BACK gives the desired
"visible from inside, hidden from outside" behaviour.  Also
removed the redundant cull-face restore at the end of render
since it now matches the GL default.

### SkyDome azimuth fix (1.178.1)

Per Coffee 2026-05-15 ("sky is wound backwards"): WoT's panorama
is authored for an azimuth convention opposite to GL's right-
handed (+Z forward, +X right) world axes -- clouds wrapped the
wrong way around the dome.  One-line shader fix in
`shaders/skydome_equirect.frag`: negate `d.x` inside the
`atan` so u sweeps the other direction:

    float u = atan(-d.x, d.z) / (2.0 * PI) + 0.5;

No code or vertex-buffer changes; the patch is purely in the
fragment shader's UV computation.

### Karelia panorama as SkyDome source (1.178.0)

Per Coffee 2026-05-15 ("maps\\skyboxes\\01_Karelia_sky\\
skydome\\sky_karelia_forward.dds  nice blue sky" + "it is
located in 01_karelia.pkg"): teach the SkyDome to sample a
WoT equirectangular panorama instead of (or alongside) the
existing env cubemap.

* `cust_tools/extract_wot_karelia_sky.py` -- mirrors the
  fire / shellhole extractor pattern.  Pulls
  `maps/skyboxes/01_Karelia_sky/skydome/sky_karelia_forward.dds`
  from `01_karelia.pkg`, decodes the BC1 (DXT1) 4096 x 1024
  panorama to PNG, writes to
  `resources/skyboxes/01_Karelia_sky/skydome/sky_karelia_
  forward.png`.  Same "never ship Wargaming pixels" rule --
  output is gitignored; `ensure_karelia_sky()` runs at startup.

* `shaders/skydome_equirect.frag` -- new fragment variant
  that samples a `sampler2D` via the standard equirectangular
  projection:
  ```
  u = atan2(d.x, d.z) / (2*pi) + 0.5
  v = acos(d.y) / pi
  ```
  Underside fog-fade kept identical to the cubemap variant
  so the dome's lower hemisphere stays clean under debug
  views.

* `tankExporterPy/skybox.py`:
  - `SkyDomeShader(mode='cubemap'|'equirect')` -- compiles
    the right fragment shader based on the requested mode.
  - `SkyDome.__init__(cubemap_id=None, radius=..., panorama_
    png=None, ...)` -- panorama wins when present, falls back
    to cubemap when the WoT pkg isn't available.  `cleanup`
    deletes only the panorama texture; cubemap stays owned
    by `Skybox`.
  - `_load_panorama_2d(png_path)` -- standard GL_TEXTURE_2D
    upload with GL_REPEAT on u (azimuth wrap) and
    GL_CLAMP_TO_EDGE on v (pole-clamp so the dome doesn't
    bleed past the texture's vertical edge).

* `tankExporterPy/viewer.py`:
  - Runs the karelia-sky extractor right after the shellhole
    extractor at startup so the PNG is on disk before the
    SkyDome instance is built.
  - Passes the karelia PNG path to `SkyDome(...)`; gracefully
    falls back to the cubemap when the PNG is missing (and
    skips the dome entirely when neither source is available).

Net effect: TEPY's skydome now shows the same blue Karelia
panorama WoT players see in-game on the Karelia map, sized to
`max(50m, terrain.world_size / 2)` and centered at the world
origin.  Future work can pick the panorama by terrain biome
(Iceland / Winter / etc.) the same way the shellhole variants
will eventually be picked.

---

## 2026-05-15 (early morning)

### Procedural skydome -- sphere at world origin, r = map_size / 2 (1.177.0)

Per Coffee 2026-05-15 ("we have skydomes in the game.. make a
sphere. rad = map size / 2"): adds a finite-radius UV-sphere
skydome at the world origin, sampled from the existing env
cubemap.  Distinct from the existing `Skybox` -- that one uses
a z=w trick to sit at infinity and follow the camera, the new
`SkyDome` is a real mesh in world space whose edge marks the
heightmap's outer perimeter.

* `shaders/skydome.vert` -- standard `proj * view * pos`, no
  infinite-distance trick.  The CPU side bakes the radius into
  the vertex positions so the shader doesn't need a uniform.
* `shaders/skydome.frag` -- samples the cubemap by the
  normalised world-space direction, smoothsteps a horizon
  band (sky above `u_horizon_t0`, fog below `u_horizon_t1`)
  so the underside of the dome doesn't show a hard ring
  where it meets the ground for an under-terrain orbit view.
* `tankExporterPy/skybox.py` -- new `SkyDomeShader` +
  `SkyDome` classes alongside the existing `Skybox`.
  `_build_uv_sphere(radius, lat=24, lon=48)` produces a 2,304-
  triangle mesh by default (cheap).  Renders inside-out
  (`glCullFace(GL_FRONT)`) with `GL_LEQUAL` depth-test +
  depth-write OFF so terrain occludes the dome correctly
  from below and the dome doesn't poison later transparent
  passes' depth tests.
* `tankExporterPy/viewer.py` -- new `self.skydome` field +
  `self.show_skydome = True`.  Built after terrain is loaded
  (we need `terrain.world_size` for the radius); reuses the
  existing skybox cubemap so dome + skybox stay in sync
  visually.  Rendered after terrain in the main draw loop
  so depth-test naturally masks it; cleanup wired into the
  Viewer teardown path.

The dome doesn't yet share a UI toggle with the skybox; flip
`self.show_skydome` directly in code or wire a checkbox in a
follow-up.

---

## 2026-05-14 (evening)

### Max shot range = map diagonal (1.176.0)

Per Coffee 2026-05-14 ("make max shot range the size of the
map"): replaced the 1500m hardcoded cap in `_fire_round` with
`sqrt(2) * terrain.world_size` -- the longest finite ray that
can traverse the heightmap.  Used as both the
`_aim_point_on_terrain(max_distance_m=...)` march limit AND
the sky-shot fallback endpoint, so a round fired across the
whole map now ray-marches the FULL distance instead of giving
up at 1500m and stamping a phantom impact mid-air.

Falls back to 1500m when the terrain isn't loaded (rare), and
clamps the lower bound at 50m so a degenerate 0-size terrain
doesn't turn into a zero-range gun.

### Shell-impact decal projector (1.175.0)

Per Coffee 2026-05-14 ("time to add a decal projector. to
where the shells hit" + "look in maps.pkg maps/decals_pbs/
for shellhole"): every projectile-vs-terrain impact now lays
a flat textured shellhole decal on the ground at the hit
point, oriented to the local terrain normal.

* **WoT shellhole extraction**
  (`cust_tools/extract_wot_shellhole_decals.py`).  Scans the
  user's pkg dir for `maps/decals_pbs/PBS_ShellHole_*_AM.dds`
  across `shared_content-part1/2/3.pkg`,
  `shared_content_sandbox-part1/2.pkg`,
  `38_mannerheim_line.pkg`, `120_graf_zeppelin_scc.pkg` (and
  any future pkgs matching the same name pattern), decodes
  each BC3 DDS via Pillow, writes a 1024 x 1024 RGBA PNG to
  `resources/decals_pbs/`.  9 unique shellhole variants on a
  vanilla NA install: PBS_ShellHole_{08,10,10_DDay,10_Iceland,
  11,12,16,114,Winter_08}_AM.  Same "never ship Wargaming
  pixels" rule the fire / smoke atlas follows -- output is
  gitignored, extraction runs at first launch via
  `ensure_runtime_decals(...)` called from `Viewer.__init__`.

* **DecalShader + decal.vert/frag**
  (`shaders/decal.vert`, `shaders/decal.frag`,
  `tankExporterPy/shaders.py`).  Simple textured-quad pipeline:
  the CPU side hands over 6 verts in WORLD space already
  oriented on the surface basis, the vertex stage does
  `proj * view * a_position`, and the frag stage samples the
  shellhole RGBA + applies an age-driven fade-out.  Per-vertex
  `a_age_frac` lets every decal carry its own age without a
  uniform array.

* **Decals projector class**
  (`tankExporterPy/particles.py`).  Loads one shellhole PNG
  (`PBS_ShellHole_10_AM.png` is the default) into a GL_TEXTURE_2D,
  allocates a 64-quad VBO, and per-frame rebuilds 6 verts per
  active `Impact` by sampling `impact.pos + impact.normal *
  bias` and stretching corners along an orthonormal basis on
  the surface normal (frisvad-style robust-up: pick the world
  axis with smallest |n.dot| as the seed, cross with `n`).
  Renders alpha-blended with depth test on / depth write off +
  glPolygonOffset(-2, -2) so the quad sits ABOVE the terrain
  pixel-by-pixel without z-fighting at slope changes.
  Tunables on the instance (size=4 m, bias=0.05 m,
  fade_start=0.85, lifetime_s=6.0) are live-mutable.

* **Render order**
  (`Viewer.render` -- alpha-pass).  Decals drawn FIRST in the
  transparent layer sequence so they composite directly onto
  the terrain underneath every other transparent layer (trail
  smoke, muzzle smoke, fire billboards, impact dust, muzzle
  flash, impact fire).  Early-out on
  `self.impacts.has_alive == False`.

Known gaps -- carried to next session:

* Only terrain impacts have decals (object / hull hits need
  raycast picking first; same constraint as the explosion
  billboards).
* One fixed shellhole texture per session.  Future work: pick
  per terrain type (sand vs grass vs snow -> different shellhole
  variants) or rotate through the 9 extracted PNGs for visual
  variety so 5 impacts in the same area aren't identical.
* No normal-mapped decals yet -- the WoT shellhole NM + GMM
  files are NOT extracted (the regex filter only matches AM).
  Forward-pipeline TEPY doesn't have a PBR decal shader to
  consume them.

---

### Cursor visibility -- load screen + mesh window follow-up (1.174.0)

Two follow-ups to the cursor-hide work landed in 1.173.0:

* Per Coffee 2026-05-14 ("load screen doesn't show mouse
  either"): on a fresh OpenGL canvas SDL can leave the cursor
  in a "not yet decided" state so the arrow doesn't paint
  during the splash / load-screen window.  `Viewer.__init__`
  now explicitly calls `pygame.mouse.set_visible(True)` at the
  top of the splash setup (before the `Splash` instance is
  built) and seeds `self._cursor_visible_state = True` from
  the same site.  Removed the later "= None" init that would
  otherwise overwrite that True back to unset before
  handle_input even runs once.

* Per Coffee 2026-05-14 ("mesh window will work if its
  visible?"): the floating mesh-visibility window sits INSIDE
  the 3D viewport's screen bounds, so the bare
  `_cursor_in_render_window` check from 1.173.0 hid the cursor
  when the user moused over it.  `_update_cursor_visibility`
  now consults `UIManager.is_pointer_over_ui(x, y)` FIRST --
  if the pointer is over any floating UI (mesh window, info
  spine, info panel body, console, modal dialogs) the cursor
  stays visible, regardless of whether (x, y) happens to fall
  inside the viewport rect.

### Hide OS cursor over the 3D viewport (1.173.0)

Per Coffee 2026-05-14 ("can we remove the mouse cursor if its
in the render window"): the system pointer used to sit on top
of the in-scene crosshair / aim ball when the user moved over
the 3D viewport.  Now hidden over the render area and shown
everywhere else.

New in `tankExporterPy/viewer.py`:

* `self._cursor_visible_state` flag (init in `__init__`).
* `_update_cursor_visibility()` method -- reads
  `pygame.mouse.get_pos()`, queries the existing
  `_cursor_in_render_window(x, y)` helper, and only calls
  `pygame.mouse.set_visible` when the desired state CHANGES.
  Prevents per-frame visibility churn that can flicker the
  cursor on some Windows configs.
* Called once at the end of `handle_input()` so the visibility
  always tracks the latest pointer position.
* `WINDOWLEAVE` handler also force-shows the cursor on the way
  out so it can't get stranded hidden if the user warped the
  pointer off-window while it was inside the viewport.

UI / console / panels keep a normal pointer; only the 3D
viewport hides it.

### Particle alpha/additive ordering fix (1.172.0)

Per Coffee 2026-05-14 ("gun fire overlapping ground explosions
looks bad"): with the impact explosions landed in 1.170.0, the
render order ended up mixing alpha and additive passes such
that an alpha layer drew over a previously-rendered additive
layer.  Concretely:

    1. shot_trail (alpha)
    2. muzzle smoke (alpha)
    3. muzzle FLASH (additive)     <- bright burst painted on
    4. fire_billboards (alpha)
    5. impact dust (alpha)         <- dimmed the burst here
    6. impact fire (additive)

The dust quad's `(1 - src_alpha)` term darkens the bright
muzzle-flash pixels wherever the dust covers them on screen,
even when the flash is supposed to be physically IN FRONT of
the dust cloud.  Depth-write is off on every transparent pass
(intentional -- particles shouldn't occlude each other), so
the depth buffer can't fix this.  The correct rule for mixed
alpha + additive without depth writes is:

    Draw ALL alpha layers first (back-to-front ideally), then
    ALL additive layers on top.  Additive is commutative so
    order WITHIN the additive group doesn't matter.

Fix in `tankExporterPy/viewer.py`:

* `_render_muzzle_flash` now takes a `phase` arg
  (`'smoke'` / `'flash'` / `'both'`).  `'smoke'` runs just the
  alpha smoke loop; `'flash'` runs just the additive burst.
  `'both'` keeps the old behaviour for back-compat.
* Render loop split: the first call site invokes
  `phase='smoke'` (alpha) immediately after the shot-trail
  smoke loop, alongside the other alphas.  A second call
  invokes `phase='flash'` (additive) after every alpha pass
  has run, alongside the impact-fire additive.
* Impact pair similarly split: `impact_dust` (alpha) runs in
  the alpha group, `impact_fire` (additive) runs in the
  additive group.

New global render order:

    alpha:   shot_trail -> muzzle smoke -> fire_billboards
             -> impact dust
    additive: muzzle flash -> impact fire

No more dimmed muzzle flashes when a still-playing impact
dust quad happens to fall in front of one on screen.

### Terrain raycast at fire time -- no more shooting through hills (1.171.0)

Per Coffee 2026-05-14 ("we need to cast a ray when we fire and
see if it ever hits terrain before making it to the target.  I
am able to shoot though hills"): the per-shot `target_world`
came from `self._aim_hit_world` -- the CAMERA's cursor-vs-terrain
hit point.  That broke whenever a hill stood between the gun and
the camera's aim point: the camera could see over the hill but
the muzzle couldn't, so the projectile visibly passed through
the hillside.

Fix in `Viewer._fire_round` (`tankExporterPy/viewer.py`):
replace `target_world = self._aim_hit_world` with a fresh
`_aim_point_on_terrain(muzzle, fwd_world, 1500, 1.0)` call.
That helper already does coarse-then-refine 1m marching along
the ray and returns the first terrain intersection (or None for
sky shots), so the projectile now stops at whatever hill the
gun's actual barrel direction hits first.  Sky shots still
fall through to the `muzzle + 1500 * fwd_world` fallback so
tracer trails / smoke have a finite endpoint to emit toward.

The fix is symmetric with the impact-pool wire-up landed in
1.170.0: the projectile stops on the hill, `s.impact_pos` is
stamped at that hill point, and the same impact-transition
block fires `self.impacts.hit(...)` so the dust + fire
explosion plays at the hill instead of at some point past it.

### Impact explosion billboards (1.170.0)

Per Coffee 2026-05-14 ("now we need explosions where the round
hits an object.. we only have terrain now.  there are a few
good image sets in the eff_text file." + "1024,1024. 2 sets in
that area"): fired-shell terminations now play a fireball +
dust burst at the impact point.

* **Flipbook extraction**
  (`cust_tools/extract_wot_fire_atlas.py`).  Added two new grid
  defs sliced from `eff_tex.dds` at atlas region (1024, 1024)
  through (2048, 2048):
  - `explosion_fire`: top half (1024, 1024)-(2048, 1536),
    8x4 grid of 128 px frames -> 32 frames orange fireball
    -> black smoke.
  - `explosion_dust`: bottom half (1024, 1536)-(2048, 2048),
    8x4 grid of 128 px frames -> 32 frames dustier ground-
    impact variant.
  Both included in `RUNTIME_TARGETS` so they're sliced on
  startup alongside the existing fire / smoke / gun_flash
  sets.  `smoke_dark` kept as a legacy alias.

* **One-shot billboard pool**
  (`tankExporterPy/particles.py` -- new `ImpactBillboards`
  class).  Distinct from `AnimatedBillboard` (continuous
  loop) and `ParticleSystem` (spawned cloud): each impact
  becomes ONE camera-facing quad whose frame is driven by
  `age / lifetime`.  When `age >= lifetime` the quad skips
  rendering, even if the `ImpactPool` slot is still alive
  for its scorch-decal phase.  Supports per-instance
  `blend` ('additive' for fire, 'alpha' for dust),
  `rise_speed` (m/s upward drift while playing), and per-
  call `y_offset` (lifts fire above dust so the two layers
  separate visually instead of merging).

* **Impact-pool wire-up**
  (`tankExporterPy/viewer.py` -- inside the existing shot-
  trail impact-transition block).  When a Shot's
  `projectile_alive` flips False, the viewer now also calls
  `self.impacts.hit(impact_pos, fwd, normal)`.  The surface
  normal is computed via a 4-tap finite-difference on
  `terrain.sample_height` at +/- 0.5 m; falls back to +Y if
  no terrain or sampling fails.  ImpactPool was already
  allocated at startup but had no producer before this --
  every slot stayed inactive forever.

* **Two-layer render**
  (after `fire_billboards.render` in `render()`):
  - Dust under fire so the fireball isn't darkened by the
    dust alpha.  `impact_dust_billboards` rendered first
    with `y_offset=0.5` + alpha blend, then
    `impact_fire_billboards` second with `y_offset=1.5` +
    additive blend.  Tunables (size, rise_speed) live on
    the class instance; bump them via `self.impact_*` if
    explosions read too tame / too crazy.
  - Both early-exit on `self.impacts.has_alive == False`,
    so idle frames pay one bool load + a compare.

* **Lifetime**
  -- 32 frames @ 30 fps = 1.067 s per explosion.  ImpactPool
  keeps the slot alive for 6 s (scorch decal lifetime) but
  the billboards stop drawing after the flipbook completes,
  so the GPU only pays for in-flight explosions.

Known gaps -- carried over to next session:
* Only terrain hits trigger impacts.  Object / tank-hull
  hits need raycast picking + an `impact_pool.hit()` call
  from that path.
* `explosion_fire` flipbook is loaded but currently rendered
  at EVERY impact (no surface-type branching).  When object
  hits land, the call site should pass a flag so terrain
  hits get dust-only and object hits get fire-only (or
  both, but with smaller fire-scale).
* No scorch / crater decal renderer yet -- ImpactPool tracks
  `scorch_phase` already; renderer would lay a flat textured
  quad on the ground at impact_pos for 6 s.

---

## 2026-05-14 (afternoon)

### Per-tank gun-bone classification table + stretch-aware recoil (1.160.0)

Closes "gun deform".  The recoil rule we shipped in 1.154.0
(`iii.x not in {0, 6}`) capped at ~95 % accuracy because the
classification semantics live in BONE NAMES, not in byte
patterns, and palette layouts vary per tank (Tiger ships
`G_BlendBone` at idx 0, Russian guns ship it at idx 1, twin-
gun GB147 ships none of the simple names).  Replaced with a
per-tank lookup table + per-slot weighted shader.

* **Offline corpus walk** -- `cust_tools/scan_gun_iii.py`,
  `scan_iii_triples.py`, `scan_vertex_blend_categories.py`,
  `dump_all_iii_triples.py`, `find_recoil_bit_mask.py`,
  `find_recoil_bit_combo.py`, `brute_force_mask_search.py`,
  `scan_gun_materials.py`.  Established:
  - WoT corpus = 1,185 parseable gun meshes (of 1,188 vehicle
    XMLs).
  - 221 distinct (iii.x, iii.y, iii.z) triples used anywhere;
    iii.w is universally 0 (= SC_UBYTE4_REVERSE_PADDED slot 3
    is padding).
  - 94 % of all gun verts live in triples that mean different
    things on different tanks -- no universal byte-pattern
    classifier exists above ~97 % accuracy.
  - The 3 vertex categories (recoil_only, rigid_only, stretch)
    fall out of WEIGHTED bone-category membership: vert
    classifies by which bones in its 4 slots are recoil bones
    and what fraction of the weight is on them.  ~1.3 % of
    all gun verts (across 167 tanks) are stretch verts that
    blend recoil + rigid -- the cloth / rubber drapes.

* **Per-tank palette table** -- `cust_tools/build_gun_palette_
  table.py` walks every gun visual, name-classifies each
  palette bone (`G_*` = recoil, `Gun_*` = rigid, `static_*` /
  `joint_*` = rigid, `*Cover/Pusher/etc.` = autoloader),
  trims to bytes actually present in the vertex stream,
  writes `gun_palette_table.json`.  0 unknowns across the
  corpus.  Spatial verification confirmed `G_*` bones span
  the BARREL (Z = -5.44 to -1.43 on Tiger) and `Gun_*` bones
  sit at the MOUNT (Z = -1.43 to -0.01) -- WoT's naming is
  the inverse of intuition; the offline scan caught it.

* **Runtime wire-up** (`tankExporterPy/viewer.py`):
  - Startup: `_load_gun_palette_table()` reads the JSON
    into `self._gun_palette_table` (1,185 entries).  Missing
    file -> fall back to the Tiger-style heuristic.
  - Per tank load: `_build_palette_recoil_flags(tag)` produces
    a 64-int array (1 at palette idx if that bone recoils,
    else 0) -- stashed on `self._palette_recoil_flags`.
  - Per draw: gun meshes upload the flags as
    `uniform int u_palette_recoil[64]`; every non-gun mesh
    uploads all zeros so the recoil branch can't trip on
    chassis / hull / turret verts.

* **Shader** (`shaders/mesh.vert`):
  - Replaces every byte-pattern heuristic (`iii.x in {0, 6}`,
    `(iii.z & 1) == 0`, `iii.x bit1 == 0`) with a per-slot
    weighted contribution:
    ```glsl
    vec3 gr_effective_t = vec3(0.0);
    if (u_gun_recoil_byte >= 0) {
        for each slot s in {x, y, z, w}:
            if (u_palette_recoil[iii[s] / 3] != 0)
                gr_effective_t += ww[s] * u_gun_recoil_translation;
    }
    pos_local.xyz += gr_effective_t;
    ```
  - Pure recoil vert (all weight on recoil bones) -> full
    translation; pure rigid -> 0; stretch (50/50 mix of
    recoil + rigid bones) -> half translation = naturally
    interpolated drape.  Same math WoT does, just with the
    bone classification pre-computed offline rather than
    looked up by name at runtime.

* **Double-translation bug killed** (`tankExporterPy/gun_recoil.py`):
  `bone_matrix_array` used to fold the recoil translation into
  `bone[pick_recoil_bone(palette)]`, which was (a) often the
  wrong bone (`pick_recoil_bone` matched on the misclassified
  name) and (b) applied IN ADDITION to the new shader-uniform
  path -- so the WRONG verts moved via the bone palette while
  the RIGHT verts moved via the uniform.  Now returns identity
  for every palette slot; the uniform is the single source of
  truth.

* **WoT-defaults mouse split** (`tankExporterPy/viewer.py`):
  - LMB-click  = fire (unchanged).
  - RMB-drag (no modifier) = orbit camera (unchanged).
  - **RMB-drag + Shift** = XZ pan (moved off LMB).
  - **RMB-drag + Ctrl**  = Y-lift (moved off LMB).
  - **RMB held**         = freeze aim (`_drive_aim_from_aim_state`
    early-exits while RMB is down so the turret stops chasing
    the cursor during camera moves).
  - Legacy LMB-drag ortho-pan removed -- RMB+Shift handles both
    perspective and ortho pan.

* **WoT naming convention captured** (now in
  `cust_tools/build_gun_palette_table.py` docstring + the
  spatial verification scans):
  - `G_BlendBone`            = THE BARREL (recoils).  Naming
                               is the inverse of intuition.
  - `Gun_BlendBone`          = the GUN ASSEMBLY mount (rigid).
  - `G_R_BlendBone` /
    `G_L_BlendBone`           = twin-barrel right / left
                               (both recoil).
  - `G_Cover_*`, `G_Pusher_*`,
    `G_Close_*`, etc.         = autoloader mechanism (own
                               anim, not recoil).
  - `static_*`, `joint_*`,
    `statik_*`, `Join_*`      = rigid mounts.

* **Diagnostic dumps** (gitignored): `gun_iii_scan.txt`,
  `gun_iii_triple_scan.txt`, `gun_iii_unique_tuples.txt`,
  `gun_palette_table.txt`, `gun_material_fx_scan.txt`,
  `gun_vertex_blend_scan.txt`, `bit_mask_analysis.txt`,
  `bit_combo_analysis.txt`, `mask_sweep_results.txt`.  All
  regenerable by re-running the corresponding cust_tools
  script.

Outcome: barrel slides through stationary mantle; twin barrels
both recoil correctly on GB147; cloth drape verts stretch
between the moving barrel and the rigid anchor.  Three
categories from one piece of weighted math.

---

## 2026-05-14

### Gun recoil universal rule + tracer trails (1.154.0)

Long session covering gun recoil, projectile spawn, shot pool,
tracer particles, and assorted debug tooling.  Vapor trails are
considered done.

* **Gun recoil rule** (`shaders/mesh.vert`):
  `gr_force_recoil = (u_gun_recoil_byte >= 0 && iii.x != 0 &&
  iii.x != 6)`.  The recoil membership of a vertex is decided
  by the FIRST iii byte alone, regardless of palette ordering:
  `iii.x == 3` → recoiling barrel verts (German + most
  nations); `iii.x == 0` → rigid mantlet / breech / hull-bolted
  parts; `iii.x == 6` → cloth / rubber overlay on a small set
  of outsourced models that lost the colour-ID convention.
  Recoil translation arrives via a dedicated
  `u_gun_recoil_translation` uniform, bypassing the bone palette
  entirely.  Universal across G78, A38, T92, Tiger, etc.

* **Aim system** (`viewer.py`): mouse-driven turret yaw + gun
  pitch with screen-cursor unprojection, terrain raycast, XML
  pitch-curve + turret-yaw-limit parsing, shortest-path angular
  wrap, and a rate-limited integrator that respects the per-tank
  rotation speed cap.

* **Shot pool + projectile** (`shot_pool.py`): 50-deep
  `ShotPool` of `Shot` slots.  Each shot carries `pos`, `fwd`,
  `cur_pos`, `prev_pos`, `target_pos`, `projectile_alive`,
  `impact_pos`, `velocity_mps`, `trail_alive`, `impact_logged`.
  Velocity sourced from gun XML `<shells><speed>`, scaled by
  `_PROJECTILE_VIS_SLOWDOWN = 20`.  Linear trajectory snaps to
  target on impact.  Slot frees only when `smoke_phase >= 1.0`
  AND `trail_alive == False`.

* **HP_gunFire muzzle** (`viewer.py`): per-tank-load walk of the
  gun's `.visual_processed` for every `hp_gunfire*` node.  Each
  match's local position (Z-flipped BW → GL) is appended to
  `_gun_muzzles_local_gl`; `_fire_round` picks via a round-robin
  cursor so twin-gun tanks alternate L → R → L → R on
  consecutive shots.

* **Tracer trails** (`particles.py` + `viewer.py`):
  50 dedicated `ParticleSystem` instances (one per shot slot),
  3000-slot buffers each.  New `one_shot_pool` mode:
  - **Rule 1** -- `max_particles` is the SOLE per-shot budget.
  - **Rule 2** -- monotonic `_next_slot` cursor; dead slots
    are NEVER reused.  `reset_pool()` is the only way slots
    come back, called at fire time.
  - **Rule 3** -- particles die at alpha-zero (computed from
    `fade_end_frame / num_frames * lifetime`), not at
    `lifetime`.  No second age trigger.
  Distance-based spawn (`spawn_per_meter = 5.0`) plus sweep-
  spawn between `prev_pos` and `cur_pos` distribute the per-
  frame particles uniformly along the segment the bullet flew
  -- continuous trail at any bullet speed.  Per-shot lifetime
  = bullet flight time so the muzzle particle dies exactly at
  impact.  `cap = ideal_particles` per shot.  Hard freeze at
  impact: `ps.max = ps._next_slot`, `ps._spawn_accum = 0`.

* **Trail look**: no fade-in (tracer is brightest at ignition);
  fade ramp 0 → num_frames so the alpha is a smooth gradient
  from bright bullet → invisible muzzle (comet → real tracer).
  `pos_jitter = 0`, full random screen-plane rotation per
  particle.  Speed + drag = 0 so particles freeze at their
  spawn point on the path.  Billboarding via `cam_right` /
  `cam_up` in `particle.vert`.

* **Shader fade fix** (`shaders/particle.frag`): fade ramps now
  use the CONTINUOUS `frame_t = v_t * num_frames` instead of
  the floored `fi`.  The floored index clamped at
  `num_frames - 1` left fade_out stalled at ~0.13 alpha, so
  particles popped at the cull instead of fading smoothly.

* **Camera default** (`viewer.py`): on startup AND on R key,
  the orbit camera snaps to world (0, 5, 8) looking at origin
  (`yaw=0`, `pitch=atan2(5,8)`, `distance=sqrt(89)`).  R no
  longer calls `fit_to_bounds` -- deterministic view, not a
  content-fit zoom.

* **Aim ray on Debug toggle** (`viewer.py`): the 1500 m cyan
  line from the gun is now gated on `self._debug`, matching
  HP markers / wheel contact / picker overlays.

* **Debug tooling**: `DEBUG_FILE_DUMPS` master switch (default
  `False`).  When on, each shot writes a per-impact PNG +
  particles/age dump + emit-pos dump + fire_log line under
  `<project>/debug_screens/`.  Startup runs
  `_flush_debug_screens_dir` with multiple safety guards
  (path realpath, basename check, extension whitelist, no
  symlinks) so the folder can be wiped safely at every launch.
  In-app console gets the same per-shot stats either way.

* **Track / homie logs**: master gate
  `TRACK_PHYSICS_VERBOSE = False` silences the
  `[track-bones]`, `[homie]`, `[homie-physics]`, `[homie-diag]`,
  `[track-sag]`, `[rotation]`, and "chassis params from XML"
  prints unless explicitly re-enabled.

---

## 2026-05-13

### Turret yaw + gun pitch + aim ray (1.122.0)

Coffee: "find the guns projectile gun location in the tank def
file.  cast a ray to where gun is pointing from start to 1500m
down range.  add turret rotation and gun elevation to the mouse
controls for panning while mouse up, Limits if for eachs travel
is in the tank def xml".

* **State + limits**: `Viewer._turret_yaw_deg` and
  `_gun_pitch_deg`, clamped to `_aim_yaw_min/max_deg` +
  `_aim_pitch_min/max_deg`.  Limits parsed from
  `_pending_chassis_info['gun']` (the existing XML cache):
  `<pitchLimits><minPitch>` / `<maxPitch>` → degrees;
  `<turretYawLimits>` "min max" pair → degrees (empty = full
  360 traverse).  Pivots come from the first turret / gun
  submesh's `bind_model_matrix` translation column -- the same
  chassis-local offset the loader baked from `gunPosition` plus
  the turret offset.  See `Viewer._capture_aim_pivots`.

* **Mouse aim**: when NO mouse buttons are held, MOUSEMOTION
  events accumulate `event.rel` into yaw / pitch (0.20 / 0.15
  deg per pixel).  Existing camera orbit / pan handlers all
  gate on buttons[N] held, so the no-button branch is a clean
  separate input lane.  Motion is incremental so the cursor
  drifting over a UI panel doesn't cause an aim jump when it
  re-enters the 3D viewport.

* **Mesh pose**: the per-frame `model_matrix = chassis_pose @
  bind_model_matrix` composer now branches by component:
  - turret meshes  → `chassis_pose @ yaw @ bind`
  - gun meshes     → `chassis_pose @ yaw @ pitch @ bind`
  - everything else → unchanged
  Order is pitch-first-then-yaw so the pitch axis stays in
  the bind frame and the yaw sweeps the pitched gun about the
  turret pivot.  See `Viewer._aim_yaw_pitch_matrices`.

* **Aim ray**: 1500m cyan line from gun pivot along the gun's
  forward axis (= chassis -Z rotated by pitch, yaw, and the
  chassis pose).  Drawn via a dedicated `LineBatch` after the
  main mesh pass.  See `Viewer._aim_ray_segments`.

Coexists with the gun recoil from 1.120.0+: recoil applies in
MESH-LOCAL space via the bone matrix array, and the turret /
gun pitch wrap the whole component-level pose transform.  Both
fire simultaneously without conflict.

### Revert to 1.121.1 state (1.121.4)

Coffee: "nope.. revert 2 back 2".

1.121.2's veto-everything rule killed the recoil entirely on
the user's tank.  Rolled the shader back to the 1.121.1 fabric
carve-out (`ww.x >= 0.90` required for veto to fire) AND
dropped the 1.121.3 load-time gun-palette dump.  Net file
state matches 1.121.1.

### Fabric carve-out reverted (1.121.2)

Coffee: "fabrics don't recoil, they bend".

1.121.1 added a `ww.x < 0.90` carve-out so fabric verts (multi-
bone blend including the recoil bone) would partially follow
the barrel via the weighted skin.  Wrong: the canvas mantlet
cover is a TURRET-attached collar the barrel slides through,
not a piece that translates with the barrel.  Letting the
recoil contribute to the weighted blend pulled the fabric off
its turret anchor.

The "bend" Coffee wants is the visual deformation of the cloth
as the rigid breech / barrel passes through it -- that comes
from the OTHER authored bones in the fabric's blend moving,
not from the recoil bone's translation.

Veto now applies to every secondary-slot recoil reference
regardless of `ww.x` (= reverted to the 1.121.0 rule).  Red
Debug highlight reverted in step: only `iii.x == recoil_byte`
verts glow red.

### Fabric carve-out for gun recoil veto (1.121.1)

Coffee: "flexible fabrics on guns are 0,6,3".

Mantlet canvas covers + gun shrouds are authored as multi-bone
blends where the recoil bone (palette index 1 -> byte 3) sits
in the THIRD slot of `iii` with meaningful authored weight in
`ww.z` -- the WoT signature pattern `iii = (0, 6, 3, ?)`.  The
1.121.0 veto froze these to their non-recoil anchors, which
would have torn the fabric away from the barrel visually when
the gun recoiled.

Refinement: the veto now only fires when `ww.x >= 0.90`, i.e.
the vertex's weight is concentrated in slot 0 (= a rigid part
with a stray secondary weight on the recoil bone -- the
original "parts that animate backwards" symptom).  Real
multi-bone blends with `ww.x < 0.90` keep the full weighted
skin including the recoil bone's contribution, so the fabric
stretches naturally with the authored gradient.

Red Debug highlight (mesh.vert) updated to match: now paints
both full-recoil verts (`iii.x == recoil_byte`) AND fabric
partial-recoil verts (`ww.x < 0.90` AND any secondary slot is
recoil) so the user can verify both populations.

### Gun recoil veto + red highlight (1.121.0)

Coffee: "we have the parts that animate backwards.. if of the
III values, bone weight's that are = t0 3 recoil" + "the first
index must be 3 to recoil" + "red i guess".

1.120.0's recoil moved every vertex whose iii contained the
recoil bone byte ANYWHERE in the 4-slot blend -- weighted by
that slot's `ww`.  Symptom: mantlet / breech / hardpoint verts
carried the recoil bone as a SECONDARY influence and got
partially dragged backward.  Visible as "parts animating
backwards" alongside the barrel.

Fix in `shaders/mesh.vert`:

* New uniform `int u_gun_recoil_byte` (= picked palette index
  * 3, or -1 for non-gun meshes).
* In the skin sum, when `u_gun_recoil_byte >= 0` AND
  `iii.x != u_gun_recoil_byte`, any secondary slot whose iii
  byte matches the recoil byte falls back to identity.  So
  the recoil translation only contributes to a vertex's
  weighted skin when iii.x (the FIRST slot) names the recoil
  bone.
* Chassis path is byte-for-byte unchanged: the >= 0 guard
  short-circuits on -1.

Debug overlay (Coffee "red i guess"): when Debug is on AND a
recoil bone is picked, verts with `iii.x == u_gun_recoil_byte`
get painted red via the existing `v_wheel_state == 1` path
(reused so no new fragment-shader code).  `viewer.py`'s
`_upload_skinning` forces `u_contact_mode = 1` for gun meshes
in this case, since the wheel-state-driven mode flip only
fires for chassis wheels.

Picker (`gun_recoil.pick_recoil_bone`) also tightened: dropped
the `G_*` fallback (was catching `G_BlendBone`, the gun root)
and added explicit `G_Recoil` / `G_Barrel` patterns.  Bones
not matching any pattern return None now -- safer than picking
the root.  `RECOIL_BONE_OVERRIDE` constant added for hardcoded
per-tank overrides when the heuristic misses.

### Gun recoil on SPACE (1.120.0)

Coffee: "We are going to add gun recoil next. Guns are skinned."
+ "space bar".

Per the 1.119.0 lock on `tank_physics.py`, recoil state lives in
a new standalone module `tankExporterPy/gun_recoil.py` rather
than as a new field on TankPhysics.  The module mirrors the
`TankPhysics.bone_matrix_array(palette)` interface so the
viewer's existing `_upload_skinning` helper just picks which
provider to query based on `mesh.component`:

* `mesh.component == 'gun'`  -> `self.gun_recoil.bone_matrix_array(palette)`
* otherwise (chassis skin)   -> `self.tank_physics.bone_matrix_array(palette)`

State machine (one cycle per SPACE press):

* OUT    -- 60 ms linear back-stroke (0 -> 0.40 m)
* DWELL  -- 40 ms hold at full travel
* RETURN -- 300 ms ease-out cubic forward to 0
* IDLE   -- offset = 0, awaiting next SPACE

Repeat SPACE presses while a cycle is in flight are ignored
(idempotent trigger).

Recoil bone is auto-picked from the gun's bone palette by name
score: `recoil` (100) > `barrel` (80) > `gun` not `gunner` (60)
> any `G_*` (40).  Highest score wins; ties broken by declaration
order.  Tanks whose gun has no recognisable bone fall through
to all-identity -> visually rigid, no error.

Translation direction is mesh-local +Z (gun points at -Z in
chassis convention, so +Z = backward into the turret).  Single
axis; the bone's bind orientation is preserved.

Wiring touched only the viewer (no edits to track / physics code,
per the 1.119.0 lock):

* SPACE added to `pygame.locals` import + KEYDOWN dispatch.
* `self.gun_recoil = GunRecoil()` built once at viewer __init__.
* `self.gun_recoil.update(dt)` called once per frame right after
  `tank_physics.update(...)`.
* `_upload_skinning` branches gun meshes to gun_recoil.
* F1 help popup picks up the new SPACE binding.

### PBR track-pad shader (1.119.0)

Coffee: "lets go ahead and add PBR to the segment models. load
once, use everywhere".

The instanced track-pad renderer previously used a flat-color
Lambert shader (`pad.vert` + `pad.frag` -- pre-1.119); the pad
meshes were carrying full diffuse/normal/AO/GMM texture IDs
from `track_pads.py`'s `PadVisualLoader` but those textures
went unused.  This commit:

* Rewrites `pad.vert` to compute a world-space TBN basis off
  `u_chassis_pose * a_pad_xform` (no skinning -- pads are rigid
  instances) and outputs the full mesh-style `VS_OUT { position,
  normal, TBN, uv0, pad_local_pos }`.
* Rewrites `pad.frag` as a slimmed sibling of `mesh.frag`:
  Cook-Torrance specular + IBL split-sum, four PBR textures
  (diffuse / normal / AO / GMM), three scene lights, ACES
  tonemap.  Strips skinning, wheel-state highlight, crash
  damage, armor color -- pad shoes don't need any of those.
* Keeps the `u_show_face_debug` +X/-X face tint behind the
  Debug checkbox (Coffee 2026-05-13).
* `Viewer._render_track_pad_body` now binds the global PBR
  state ONCE per frame before the per-renderer loop: lights,
  view_pos, slider knobs (metal_scale / shine_scale /
  use_normal_map / invert_shine), and IBL textures on units
  4/5/6.  The "load once" part of the user request.
* `TrackPadRenderer.render` binds its OWN pad mesh's textures
  on units 0..3 each draw -- so `segment` and `segment2`
  meshes can carry distinct materials without state bleed.
  The "use everywhere" part: every instance in this side's
  N-pad draw call uses the same shared material.
* `PadShader` (shaders.py) grows `set_vec3`, `set_vec3_array`,
  `set_float`, `get_uniform` mirroring `ShaderProgram`.

Net visual: pads now respond to the scene lighting + skybox
the same way the rest of the tank does, including normal-map
relief, AO cavity shadow, and the gloss/metal gradient
authored into the GMM texture.

### Wheel sag now drives the spline (1.118.117)

Coffee: "wheel sag does not put the spine down".

`_track_current_bone_positions` was writing the suspension
residual to whichever bone-name form `_resolve` matched
(`W_L<i>_BlendBone`) but `track_homie._collect_wheels` reads
the BARE form (`W_L<i>`) first.  The bare entry stayed at bind
position so the chain never followed the wheel down.

Fix: after writing the residual to the matched key, write the
same offset to the sibling form (bare <-> `_BlendBone`) when it
exists.  Both entries stay synced; whichever consumer reads
which form, the residual is there.

### Wheel rotation L/R speed swap + R+track_thickness rolling radius + half-omega on wide turns (1.118.116)

Coffee: "speed calculation for inner and outer tracks flipped..
same reason" + "(R + offset to ground) * Pi = distance" +
"1/2 the angle if both are moving forward or backward".

Three fixes to the per-side track-speed math driving both the
chain animation s_offset and the wheel-rotation angle delta:

1. **L/R differential swap.**  Same Z-frame mismatch as the
   wheel-orbit-chassis-center fix at 1.118.115.  v_L now uses
   `speed - omega*b`, v_R uses `speed + omega*b`.
2. **Rolling radius = R + track_thickness.**  Chain rides on
   the outer face of the track ribbon, not the bare rim.  Use
   `(R + track_thickness)` in the `omega_wheel = v / R_eff`
   divisor.  `track_thickness` is the `<trackThickness>` or
   `<renderModelOffset>` from the chassis XML, already loaded
   into `TankPhysics.track_thickness`.
3. **Halve omega on same-sign tracks.**  When `v_L * v_R > 0`
   (both forward or both backward = wide differential turn,
   not a pivot), divide the omega contribution to each track
   by 2.  Pivot turns (opposite signs) keep the full
   differential.

### Extra rotating wheels: Z-flip the hub before registering (1.118.115)

Coffee: "I see whats wrong.. we have the verts rotation around
the wrong wheel center".

Diagnostic on G78 showed bone position from the visual walk
(`parse_chassis_bone_world_positions`) has Z with the OPPOSITE
sign vs the chassis mesh vertices.  Road wheels work because
`TankPhysics.from_chassis_meshes` computes `self.wheels[i]`
from mesh-vertex means (matching the vertex frame).  The new
`set_extra_rotating_wheels` was being fed Z straight from the
visual walk -- so the rotation centre landed on the mirror
image of the wheel across the chassis Y-X plane, and the
entire wheel mesh orbited chassis origin as theta advanced.

Fix: negate Z in viewer.py when building `_ex_hubs` -- single
line.

### Rotate all wheels, not just road (1.118.114)

Coffee: "rotate all wheels" + "spinning backwards".

Sign of the angle delta flipped from `+(v/R)*dt` to
`-(v/R)*dt`.  And added `TankPhysics.set_extra_rotating_wheels
(names, hubs, radii)` to register drive sprockets, idlers,
and return rollers alongside road wheels.

* New state: `extra_rotating_bones / hubs / radii /
  angles_rad`, plus `_extra_rot_name_to_idx` for fast lookup
  in `bone_matrix_array`.
* `advance_wheel_angles` now iterates both road wheels (via
  `self.wheels`) and extras (via `extra_rotating_hubs`).
* `bone_matrix_array`: any palette bone matching the extras
  table gets `T(hub) . Rx(theta) . T(-hub)` composed in, with
  NO Y residual (sprockets / idlers / rollers are chassis-
  rigid in suspension terms).

Wired from the viewer once at chassis load, right after
`_track_chassis_bones_bind` is set.

### Wheel rotations -- per-wheel angle accumulator + bone matrix rotation (1.118.113)

Coffee: "we are adding wheel rotations. we will want to spin
the verts in ZY about center of each wheel".

`TankPhysics.wheel_angles_rad` -- one accumulated angle per
road-wheel bone in radians.  Caller advances via
`advance_wheel_angles(v_L, v_R, dt)` once per frame, using the
SAME side-differentiated speeds (`v_L = speed + omega*b`,
`v_R = speed - omega*b` at the time of this rev; swap +
halving landed at 1.118.116) that drive the chain s_offset.

`bone_matrix_array` wheel-bone slot changed from a pure Y
translation to:

```
M_wheel = T(hub + (0, ry, 0))  .  Rx(theta)  .  T(-hub)
```

For a bind-pose vertex p: shifts to hub-centred, rotates in
the YZ plane about the wheel hub, translates back to (hub +
Y residual).  Vertex p rotates about the wheel's centre on the
side-view plane.  Per-wheel ID is implicit in the bone palette
/ `iii` indexing -- same path the contact-state colour
highlight uses.

### Differential track speeds drive the chain animation (1.118.112)

Coffee: "speed of segment movement does not match our actual
movement on the terrain.. inside and outside should have
different speeds based on our current turning radius".

`_compute_homie_chain_for_frame` was advancing both
`_track_chain_s_offset_L` and `_track_chain_s_offset_R` at the
SAME `cur_forward_mps * dt`.  Tracked vehicle differential
drive needs per-side track-contact speeds:

```
omega = yaw_rate (rad/s)         from tp._last_yaw_rate_dps
b     = gauge_x / 2              half-track gauge
v_L = speed_mps + omega * b      left contact-point speed
v_R = speed_mps - omega * b      right contact-point speed
```

(Sign convention swapped at 1.118.116.)  Pivot turns naturally
fall out: when speed_mps = 0 and omega > 0, v_L and v_R have
opposite signs -- inner track runs backward, outer forward,
matching real neutral steer.

### Bottom-wheel pad rotation rate (1.118.110)

Coffee: "we lost the rotation fix at the bottom wheels for the
segments.. it should be 1/2 like the end wheels".

Re-enabled the **central-chord tangent rebuild** at the top of
`_compute_homie_chain_for_frame`:

```python
chord_i = pos[(i+1) % N] - pos[(i-1) % N]
tan_i   = chord_i / |chord_i|
```

Pads on road-wheel arcs now rotate by Delta/2 between adjacent
pads instead of Delta -- matches the symmetric "swing both
directions from centre" rate on the sprocket / idler arcs.
Sag, central-chord re-run, and PBD step stay gated below.

### Virgin homie -- post-build massaging gated off (1.118.109)

Coffee: "virgin homie.. poor guy".

The homie chain output (after Z-flip to renderer frame) is
now the FINAL chain that feeds the renderer.  Four post-build
modifiers were `if False:`d out in
`_compute_homie_chain_for_frame`:

1. Central-chord tangent override #1 (re-enabled at 1.118.110)
2. Sag bias via `track_sag.interp_sag_y` on bottom-run pads
3. Central-chord override #2 (post-sag)
4. PBD step (additionally gated with `False and ...` even when
   F9 is on)

Re-enable any by flipping its `if False:` guard.

### F1 = key bindings popup (1.118.101)

Coffee: "add a pop up of key bindings when I press F1".

New `_show_keybindings_popup` reuses the existing Tkinter
`messagebox.showinfo` path (no GL overlay machinery) and lists
every active binding by category (Camera, Tank Drive, Display,
Recorders, Track Debug, Screenshot / Clipboard, Other).  F1
key handler placed just after the K_ESCAPE branch.

### Pose carry-over across tank loads + emitter chassis-pose sync (1.118.99 -> 1.118.103)

Coffee: "put the previous loaded tank's direction and position
in when a tank loads" + "we need to figure out where the
emitters need to be -- vector and location based on the
just-loaded tank that has been transformed to a new location".

At `load_vehicle`, snapshot the outgoing tank's solver pose
(`pos / yaw_deg / pitch_deg / roll_deg`) AND its render-pose
integrator state (`_render_pos_y / _render_pitch_deg /
_render_roll_deg`) BEFORE `from_chassis_meshes` is called.
After the new instance is built, write all of those back so
`chassis_matrix()` returns the carried-over world transform
from frame 0 (no integrator catch-up ramp).

Smoke + fire emitter `bind_pos` snapshot at load made
conditional (`if 'bind_pos' not in hp:`) so it no longer
overwrites the lazy snapshot from
`_update_emitters_for_chassis_pose` with already-transformed
post-pose world values.  Bug was that the lazy snapshot
correctly captured chassis-local bind during the per-component
render frames at the OLD tank's pose, then the explicit
snapshot at load end overwrote with world-at-OLD-pose values,
which the carry-over then double-shifted on the new tank's
pose for the next render frame.  See CLAUDE.md "Where we left
off" for the detailed timeline.

The post-load emitter chassis-pose sync (X / Z translation +
yaw rotation only, since Y / pitch / roll are integrator
state that hasn't settled yet) lives right after the
`set_emitters` calls in `load_vehicle`.

### Skinning bone-weight normalisation in shader (1.118.106)

Coffee: "figure out why the rubber band tracks
track_mat_L_skinned is deformed".

`shaders/mesh.vert` now divides the weighted-sum `skin` matrix
by `(ww.x + ww.y + ww.z + ww.w)` before applying to vertex
position.  BigWorld packs the per-vertex weights as 4 uint8s
that can round down to a byte-sum < 255 (= < 1.0 after the
loader's `/255` scale).  G78 had 240 ribbon verts at
weight-sum ~ 0.91, which compressed their X by ~9% toward
origin via `skin * vec4(pos, 1)`.  Normalising at the shader
keeps the skin a true convex combination of bone matrices --
vertex data on disk untouched, round-trip safe.

### Track-chain wheel ordering, gravity-aware PBD, gravity removed, attractors removed (1.118.105 -> 1.118.108)

A sequence of PBD chain-solver experiments that ended with the
solver gutted to a constraint-only relaxation:

* **1.118.105** -- bind gate switched from "lower half"
  (`d . g > 0`) to **correction-vs-gravity dot product**
  (`corr . g > 0`).  Correct on outside / above hub AND
  inside / below hub at once; the original lower-half check
  was the wrong half for end-wheel wraparounds.
* **1.118.107** -- gravity bias removed from the Verlet
  predict step.  `_gravity_yz` field + `set_chassis_gravity`
  plumbing kept in place; just bypassed.
* **1.118.108** -- bilateral wheel BIND removed entirely.
  Wheels are now pure SOLID OBJECTS via the unilateral push-
  out only.  No attractors.

Net `step()` per relaxation iteration: bending + distance +
skip-K + unilateral push-out.  Chain holds the seed shape and
only deforms in response to seed-pose changes (wheel
deflection moves hubs, push-out responds).

### Per-side track-speed differential plus PBD wheel-kind filtering and gravity vector projection (1.118.102 -> 1.118.104)

* **1.118.102** -- world gravity projected to chassis-local
  YZ each step (`g_local = R^T . (0, -9.81, 0)`) so PBD
  always pulls along world -Y regardless of chassis tilt.
  Reverted at 1.118.107.
* **1.118.103** -- gravity-aware bind gate gated on
  `corr . g > 0` (later replaced by full removal at .108).
* **1.118.104** -- `_bone_yz` in the PBD setup now negates Z
  so PBD hubs match the post-Z-flip pad positions.  Earlier
  the chain visibly mirrored: chain pad at renderer Z =
  -2.893 vs hub at +2.719, distance 5.4 m, never bound.

### Pose snapshot + drop-from-1m experiments (1.118.99 -> 1.118.100)

A couple of intermediate revs experimented with dropping the
new tank from 1 m above terrain at the outgoing tank's XZ
spot.  Replaced by full pose carry-over at 1.118.100.

### CHANGELOG / docs catch-up

Versions 1.118.55 - 1.118.97 represent in-progress work that
landed at the 1.118.98 cumulative commit (the homie chain
rewrite + assorted bug fixes).  See the 2026-05-11 entries
below.

---

## 2026-05-11

### Skinned-track ribbon defaults to UNCHECKED in mesh panel (1.118.98)

Coffee: "un check the track_mat_r/l_skinned on the meshes
visibility panel".

When the mesh-visibility panel populates, each
`track_<L|R>Shape*` (= the skinned rubber-band track ribbon)
row now starts UNCHECKED and the corresponding
`Mesh.visible = False`.  The pad-mesh chain is the visible
chain by default; ribbon is on standby for A/B comparisons --
click the checkbox to bring it back any time.

Implemented in `_repopulate_mesh_visibility_panel`: after
`mw.populate(...)`, the per-row sync loop now also checks each
real-mesh row's name; if it starts with `track_` and contains
`Shape` (covers `track_L_Shape`, `track_LShape`,
`track_LShape_split_0`, etc.), the row + the mesh both get
flipped to `visible = False`.

### Skip-K tensioners between segments (1.118.97)

Coffee: "can we apply tensioners between segments?  max dist?
sag cant cause each one to fall to far..  hanging bridge deck
sorta thing?".

New constraint in `track_chain_pbd.TrackChainPBD`: long-range
distance ties between non-adjacent pads.  Each pad i is bound
to pad (i + SKIP_K) at the SEED distance captured at
`seed_from_homie` time.

  * `SKIP_K = 8` -- spans 8 pads ~= 1.4 m of chain, big
    enough to cross the inter-road-wheel free run.
  * `SKIP_STIFF = 0.30` -- soft enough that local link-to-link
    distance constraints still dominate; firm enough to catch
    the long-wavelength sag before the chain falls past its
    authored shape.
  * Rest length per-pad from seed -- straight runs land at
    K * seg_len; wheel wraps land at the chord across K pads
    (less than K * seg_len) -- so the constraint respects
    authored geometry instead of pulling wrapped sections
    apart.

Bridge-deck analogy: each pad has cables across to its
8-pad-distant neighbours.  Gravity can pull individual pads
down a little, but the cables limit how far ANY section can
sag.  Without it, all the pads in a free run between road
wheels would collectively fall under gravity.

Also bumped `N_RELAX_ITERS` 10 -> 14 so the extra constraint
gets enough iterations to settle.

### Tank load preserves pose; R key now resets everything (1.118.96)

Coffee: "1. when i load a new tank, i do not want the camera or
tank position reset.  I want the smoke and fire particle
emitters locations set to the tanks locations using its
position and angle.  2. R key should reset tank, cam and
emitters cam should be set to free look."

Two fixes in `viewer.handle_input` / load:

**1. Tank load preserves the world pose.**  Removed the
`tank_physics.pos[:] = 0.0` / yaw / pitch / roll / vy reset
that fired on every load_vehicle.  Emitters (`_exhaust_points`
for smoke, `_fire_points` for fire) are chassis-LOCAL and the
per-frame `update_emitter_positions(chassis_frame)` call
transforms them into world via the CURRENT chassis pose -- so
when you load a new tank, the emitters automatically end up
at the new model's HP_Smoke / HP_Fire bone positions
transformed through whatever pose the camera was looking at,
not snapped back to origin.

**2. R key full-session reset.**  Was camera-only; now also:
  * Zeros `tank_physics` pose (pos, yaw, pitch, roll, vy) plus
    the integrator's render-side state (`_render_*`,
    `omega_*`) so the inertia spring restarts clean.
  * Sets `camera_mode = 0` (free look / orbit) regardless of
    whether the user was in first-person or driver POV.
  * Camera yaw/pitch/distance reset + fit_to_bounds as before.
  * Logs `R: tank + camera reset, camera -> free look`.

Emitter positions follow the chassis frame automatically, so
resetting the tank pose to origin lands the emitters at origin
too -- no separate emitter reset needed.

### PBD tuning: tighter bind tol + lower gravity + 10 iters (1.118.95)

Coffee: "chain_runtime is not smooth ffs..  plot the segments
and the spline over".

Three tuning changes to the PBD solver:

  * `BIND_TOL` 0.020 -> 0.003.  The 2 cm bind grace was
    catching pads on the inter-wheel tangent line (which sit
    only ~1.5 cm outside the wheel rim when adjacent to a
    wheel).  Binding them locked the chain to the wheel rim
    and forced it to wrap each road wheel by ~5 deg.  3 mm
    catches genuine arc pads (which sit at distance R
    exactly per the homie geometry) and excludes tangent
    pads.
  * `GRAVITY_SCALE` 1.0 -> 0.10.  Full 9.81 m/s^2 over 60
    settle steps dropped the chain ~10 cm between consecutive
    Tiger road wheels (spacing ~0.5 m), way more than real
    tank chains sag.  Seed already carries authored sag from
    the skinned-track ribbon; PBD now runs primarily as a
    constraint smoother with a small gravitational nudge so
    bound pads slide to the lowest tangent point on each
    wheel.
  * `N_RELAX_ITERS` 6 -> 10.  More iterations per step =
    cleaner constraint satisfaction; cost is negligible
    (~7 ms per side per step at numpy speed).

Diagnostic plot (`plot_chain_runtime.py`) gains larger figure
size + numbered pivots every 4th pad + per-pad forward-
direction tick so segment orientation is visible alongside
the spline, addressing "plot the segments and the spline over".

### PBD bending constraint kills spline zig-zag (1.118.94)

Coffee: "spline is a zig zag mess" -> "you are applying to the
spline and not the segments".

The v1.118.93 PBD solver had distance + wheel bind/push but no
ANGULAR constraint between consecutive pads.  Pads adjacent to
each other could zig-zag freely as long as the inter-pad
distance = seg_len.  Result: chain spline (the pad anchor
positions) wiggled, which the renderer faithfully reproduced as
sharp angle changes between consecutive pad meshes.

Fix: bending constraint per iteration in
`TrackChainPBD.step`.  Each FREE pad (not bound to a wheel)
gets pulled toward the midpoint of its prev / next
neighbours:

    delta = (pos[i-1] + pos[i+1]) / 2 - pos[i]
    pos[i] += BEND_STIFF * delta

Bound pads (on wheel rims) skip the bend pass -- their position
is determined by the wheel rim.  Default `BEND_STIFF = 0.20`
gives visible chain smoothness without over-flattening the sag
between wheels.

Order in relaxation loop is now:
    2a.  Bilateral wheel bind  (bound pads -> rim)
    2a-bis. Bending            (free pads -> neighbour midpoint)
    2b.  Distance              (adjacent pads at seg_len)
    2c.  Unilateral wheel push (free pads only)

### PBD bilateral wheel binding + render-faithful diagnostic plot (1.118.93)

Coffee: "i dont see a change here" -> "plot what you are
creating as if it was rendered".

The v1.118.91 PBD solver used unilateral wheel constraints
only -- "chain pad can't penetrate the rim" pushed pads OUT of
wheels, but offered nothing to anchor the chain TO the wheels
against gravity.  Result: chain dropped through everything and
settled as a free hanging loop below the wheels.

Fix: BILATERAL bind at seed time.  Any pad within
`R + BIND_TOL` (2 cm) of a wheel hub when the chain is seeded
gets locked to that wheel; per step, the solver snaps the
bound pad's distance to the hub back to exactly `R`, then
applies distance constraints between adjacent pads.  Free
pads (between wheels) sag with gravity until the inter-pad
distance constraints from neighbouring bound pads pull them
taut.  The unilateral push-out still resists wandering into a
wheel.

Stored per-pad on `_bound_wheel` (N int32, -1 = free).
Computed inside `seed_from_homie` from the homie geometric
chain positions.  Step now runs:

   2a. Bilateral wheel bind  (bound pads -> rim)
   2b. Distance constraints  (adjacent pads at seg_len)
   2c. Unilateral wheel push (free pads only, drift catch)

New diagnostic `cust_tools/plot_chain_runtime.py` runs the
ENTIRE runtime pipeline offline (homie seed + Z-flip + central
chord + sag bias + central chord + PBD with 60 settle steps)
and draws what the renderer would show.  4 panels: Tiger L,
Tiger R, T110E4 L, T110E4 R.

Output: `math_images/chain_runtime.png`.

### Drop tangent-flip sweep -- flip is the "home" signal (1.118.92)

Coffee: "you can never flip the direction vector..  that's the
you are home signal".

Removed the v1.118.x tangent-continuity sweep in
`track_homie.assemble_chain_arrays`:

    if N >= 2:
        for _ in range(2):
            for i in range(N):
                j = (i - 1) % N
                if dot(tan[i], tan[j]) < -1e-6:
                    tan[i] = -tan[i]    # <-- DELETED

The flip was masking a real signal.  When two consecutive pads
have anti-aligned tangents it means either (a) the chain has
genuinely closed back on itself = "you are home" loop marker,
or (b) the chain math has produced a bad tangent.  Silently
flipping in software hid both cases.  Now the tangents flow
through unmodified; downstream consumers can detect a flip
with `dot(tan[i], tan[i-1]) < 0` and act on it as a signal.

(In a correctly closed CW-traversed loop, no consecutive pads
should ever have anti-aligned tangents.  A real flip means
something upstream needs investigation.)

### Position-based-dynamics chain solver, all-wheel constraints (1.118.91)

Coffee: "we need a radial resolver for force on the chain.. ZY
.. never X.. can we do all wheels if the physics works?".

New module `tankExporterPy/track_chain_pbd.py`:

  * `TrackChainPBD` -- per-side position-based-dynamics solver.
    Particles in 2D chassis-local (Y, Z); X fixed at +/- gauge/2.
    Per step:
       1. Verlet predict with gravity (-9.81 m/s^2 on Y).
       2. 6 relaxation iterations alternating
          (distance constraint, wheel radial constraint).
       3. Implicit velocity from `pos - prev_pos`.
  * Distance constraint: adjacent pads stay at `segmentLength`
    (closed loop, half-split correction).
  * Wheel constraint = the radial resolver you described:
    `|pos - hub| < R` -> push pad to `hub + R * radial_hat`
    (deepest penetration wins per pad per iteration).  Direction
    locked to the radial vector = "directional lock on the
    wheel" -- tangential motion is the only DOF left at the rim.
  * ALL wheel roles fed in: drive sprockets, idlers, road
    wheels, return rollers.  Solver doesn't distinguish; every
    wheel is a circle the chain can't enter.

Wiring (`viewer.py`):

  * F9 toggles `_use_chain_pbd`.  Default OFF -- chain stays on
    geometric homie until you flip it.
  * `_step_chain_pbd` called from `_compute_homie_chain_for_frame`
    when the toggle is on.  Lazy-creates one `TrackChainPBD` per
    side after seeding from the homie geometric chain on the
    first tick after each toggle -- avoids cold-start explosion.
  * Real-time `dt` from wall clock (decoupled from physics tick);
    clamped to 40 ms to survive frame stutters.

Sanity: T110E4 has 13 wheels per side, T110E4 chain = 80 pads
per side, 6 iterations -> ~6000 constraint checks per side per
frame.  Vectorised over (pad, wheel) pairs.

### Rubber-band track ribbon rendered again (1.118.90)

Coffee: "we need the rubber band back".

The skinned-track ribbon (`track_<L|R>Shape` /
`track_<L|R>Shape_split_<N>` inside
`Chassis.primitives_processed`) has been gated off since the
Phase A track-physics roadmap started.  Pad meshes (homie
chain) covered the visible chain, but with no ribbon under
them you could see between pads.

Removed the `RENDER_RUBBER_BAND_TRACK = False` short-circuit
in `viewer.render`'s three mesh passes (solid, wireframe,
normals) and the matching skip in `picker.py`.  The ribbon
now:

  * Renders with the rest of the chassis meshes via the
    standard `_upload_skinning` path (per-bone matrix array
    upload picks up suspension deflection automatically).
  * Sits UNDER the homie pad render (pads still draw on top),
    filling the visible gaps between pads.
  * Reports as a pickable mesh in the picker FBO.

### Per-side road-wheel Z range for sag mask (1.118.89)

Coffee: "you forgot about the other wheels".

The 1.118.88 sag-mask Z range was derived from `road_wheels_L`
only and reused for both sides.  WoT XMLs author L and R road
wheels with slightly different Z positions (T110E4 W_L1 Z =
+1.261 m vs W_R1 Z = +1.183 m), so the single L-derived range
clipped a sliver off the R-side bottom run.

Fix: compute `(z_lo, z_hi)` per side from the respective
`road_wheels_<L|R>` role list.  Each side's sag mask now uses
its own road-wheel extremes.

### Sag mask by road-wheel Z range; no chain breaks (1.118.88)

Coffee: "we have breaks in our chain?  are we sure we are in
order?".

The v1.118.86 sag bias used `Y < Y_mid` to identify bottom-run
pads.  On a sprocket wrap the chain swings through the full Y
range (rim bottom Y to rim top Y), so individual pads on the
arc oscillated above / below `Y_mid` -- some got the sag bias,
some didn't.  That created visible Y discontinuities at the
sprocket wraparound = "breaks" in the chain.

Fix: mask by Z range instead.  Bottom-run = chain pads with
Z between the front-most and rear-most ROAD WHEEL Z.  This
selects only the section riding under the road wheels (where
sag physically makes sense) and excludes the sprocket / idler
arcs cleanly.  Boundary is continuous because the sag curve
tapers to zero at the road-wheel Z extremes (the ribbon
authored hugs the wheel rim there).

### Re-run central-chord tangent AFTER sag bias (1.118.87)

Coffee: "the grounds wheel's segments need the same 1/2 rotation
factor as the drive wheel's".

The v1.118.83 central-chord tangent override ran ONCE before
the v1.118.86 sag bias.  After sag reshaped bottom-run pad Y
positions, the tangents still pointed along the pre-sag flat
line -- so ground-wheel pads kept their forward axis horizontal
even when the chain physically dipped between wheels.

Fix: re-apply the central-chord override `chord_i =
pos[i+1] - pos[i-1]` AFTER the sag bias.  Bottom-run pads now
tilt with the sagged chain shape; the same Delta/2 rotation
halving the drive wheel pads enjoy carries through to ground
wheel pads.

### Chain sag from skinned-track ribbon (1.118.86)

Coffee: "can we add weights to the segments?  I want them to
sag" -> "there are weights in the tank_mat_r/l_skinned for the
rubber band tracks.  Usable?" -> "of course.  I didn't think
that was a viable path."

New module `tankExporterPy/track_sag.py`:

  * `extract_sag_curve(prim_path, vis_path, side)` walks the
    skinned-track ribbon mesh (`track_<side>_Shape`,
    `track_<side>Shape`, OR `track_<side>Shape_split_<N>` --
    Tiger I uses the split layout) and pulls the OUTER FACE
    vertices.  Bins them by chassis-local Z (1 cm bins), takes
    the MIN Y per bin, returns `(zs, ys)` -- the authored
    BOTTOM EDGE of the track ribbon vs Z.  This is the chain's
    natural rest sag profile, encoded in the .primitives_processed
    vertex positions.
  * `interp_sag_y(zs_curve, ys_curve, zs_query)` -- np.interp
    wrapper with edge-clamping for runtime lookup.

Wiring:

  * `Viewer._load_track_sag_curves` called once per tank load
    after `_resolve_track_pad_paths`.  Stashes
    `track_sag_L` / `track_sag_R` on `_pending_chassis_info`.
  * `Viewer._compute_homie_chain_for_frame` -- new bias pass.
    For every BOTTOM-RUN pad (Y in the lower half of the
    chain's Y range), interpolate the sag curve at the pad's Z
    and `Y_pad = min(Y_homie, Y_sag)`.  The `min()` makes sure
    the bias only ever DROPS the chain; if homie already
    placed the pad lower (e.g. suspension deflection), that
    wins.

Net visual: chain bottom run now dips between road wheels in
the natural authored shape; on the sprocket arcs the curve
hugs the wheel rim (zero extra sag) so the chain drops cleanly
into sprocket position.  Tiger ribbon extracts 120 samples,
T110E4 extracts 98 -- enough resolution for sub-cm sag detail.

### All spline-radius adjustments removed (1.118.85)

Coffee: "find and remove any adjustments to the spine dia".

Audited every `radii.items()` / `w['R'] *=` / `w['R'] -=`
write site and stripped the remaining shrink:

  * `Viewer._compute_homie_chain_for_frame` -- removed
    `segmentsInnerThickness` shrink.
  * `Viewer._wire_homie_to_physics` -- removed same shrink.
  * `cust_tools/plot_homie_T110E4.py` -- removed same shrink.
  * `cust_tools/plot_homie_compare.py` -- removed same shrink.

Combined with earlier removals:
  * v1.118.73: `<radiusOverrides>` parser stripped from loader.
  * v1.118.84: `track_homie._correct_R` no longer called from
    `build_chain_segments`.

Net: every wheel hits the homie chain math at its authored
`<wheelGroup><groupRadius>` value verbatim.  Chain wraps the
visible wheel rim.

### Authored wheel diameters preserved; L->R radii mirror (1.118.84)

Coffee: "our spline diameters are off..  make sure the correct
dia wheels are use in the proper locations of our spline".

Two fixes:

1.  **`_correct_R` scaling skipped.**  `track_homie.build_chain_segments`
    no longer mutates active wheels' radii to fit the chain
    length to `segmentLength * segmentsCount` exactly.  The
    scaling was inflating sprockets (T110E4 WD_L0 grew from
    0.325 to ~0.5; Tiger WD_L0 grew similarly) because the
    correction had to be absorbed by sprockets + rollers alone
    (road wheels excluded from the active set).  Every wheel
    now keeps its authored radius from `<wheelGroup>
    <groupRadius>` (minus segmentsInnerThickness applied
    upstream).  Pad pitch falls out as `natural_length /
    n_pads`, typically within 1-2 % of `segmentLength`.

2.  **L->R `wheel_radii` mirror in loader.**  WoT chassis XMLs
    list `<wheelGroup>` entries only for the L-side; the R-side
    relied on `_collect_wheels`' fallback to look up the L
    mirror name.  Loader now mirrors `_L` -> `_R` into
    `wheel_radii` directly, so downstream code (track_homie,
    physics, plot tools) has explicit entries for every wheel
    bone.  Diagnostic audit on Tiger + T110E4 now shows
    correct R-side values where it previously showed `<NONE>`.

`cust_tools/plot_homie_compare.py` adds an `AUTHORED wheel
radii by role` table at the top of every run so the wheel-
diameter <-> position map is auditable, and draws both the
bind-pose outline (dashed) AND the post-correction circle
(filled) per wheel.  Post-correction is now identical to bind
since `_correct_R` is skipped.

### Per-pad orientation: central-chord halves prev forward-chord (1.118.83)

Coffee: "the rotation angle to the wheel needs to be half.  we
swing both directions from center so we double the angel to
prev and post pads".

The v1.118.x forward-chord override in
`_compute_homie_chain_for_frame` set each pad's tangent to
`pos[i+1] - pos[i]` -- the chord from THIS pivot to the NEXT
pivot.  That chord direction is rotated by Delta/2 FORWARD of
the pivot's own symmetric chain tangent, where Delta = the
per-pad angular step on the wheel arc (= pitch / R).  Result:
consecutive pads' "forwards" spanned the FULL prev-to-post
angular range -- 2 * Delta/2 = Delta -- which on a sprocket
read as the pads pinwheeling around the wheel twice as fast as
they should.

Fix: central chord (`pos[i+1] - pos[i-1]`).  This is
symmetric about the pivot -- equal pull from prev and next
neighbours -- and matches the exact geometric tangent at the
pivot to leading order.  Consecutive central chords on a
regular sprocket arc are correctly Delta/2 apart.

### Spline rides at (R - innerThickness); no runtime offset (1.118.82)

Coffee: "remove the pad inner thickness when we draw the
spline.. remove any existing offset to the spline and subtract
the inner thickness from all wheel dias".

Wheel radii from the chassis XML's `<wheelGroups>` go to the
OUTER pad face (the rim).  The chain itself rides INSIDE the
rim by `segmentsInnerThickness` -- the pin axle plane sits
between the wheel center and the rim.  So before handing
radii to `track_homie.compute_homie_chain`, we now subtract
the inner thickness from every wheel:

    radii_eff = {nm: max(R - inner_th, 0.01)
                 for nm, R in radii.items()}

Applied in both runtime paths:
  * `Viewer._compute_homie_chain_for_frame`
  * `Viewer._wire_homie_to_physics`
  * `cust_tools/plot_homie_T110E4.py`

Runtime pad offset shift dropped:  the previous
`xform_clamped[:, :, 3] += seg2_offset * +Y` is gone.  Pad
meshes render with their .model-authored origin at the
spline anchor verbatim; the spline now sits at the right
radius and the pad bodies cover [chain-anchor + inner
thickness, chain-anchor + outer thickness] along radial.

Net visual on T110E4 sprocket: pads wrap snugly around the
sprocket rim; chain spline runs through the INNER edge of
each gold pad rectangle; hinge points (green diamonds) sit
just past the chain at +segment2Offset out.

### Parse pad thickness fields; draw pads as rectangles in plot (1.118.81)

Coffee: "draw pads as rectangles the size of pad..  pad space
and pad thickness" + "<segmentsOuterThickness>0.029107..
<segmentsInnerThickness>0.061535".

`loaders.py` now parses `<segmentsOuterThickness>` and
`<segmentsInnerThickness>` from the chassis XML into
`info['chassis']['segmentsOuterThickness']` /
`['segmentsInnerThickness']` (m).  Pad "space" along the chain
already comes from `<segmentLength>`.

Diagnostic plot draws each pad as a gold rectangle:
  * length  = segmentLength                  (along chain)
  * inner   = segmentsInnerThickness         (toward wheel
                                               center / chain)
  * outer   = segmentsOuterThickness         (away from wheel
                                               center / cleat side)
  * centred on the hinge point (chain anchor + segment2Offset
    * outward axis)

Rectangle corners computed from the chain tangent (forward) +
outward perpendicular (radial out on arc pads).  Polygon
patch with translucent gold fill so the chain + sprocket
remain visible underneath.

### Hinge point at segment2Offset; both pad parts share it (1.118.80)

Coffee: "now draw the pad so their hinge point is at the seg 2
offset".

Both segment + segment2 pad meshes now hinge at the same point
in world space: `chain_anchor + segment2Offset * +Y_axis_world`
(radially outward from the wheel center on arc pads, perpendicular
outward from the chain on line pads).  On T110E4 that puts the
hinge at chain_anchor + 0.09 m radial.

`segmentOffset` (0.258) is no longer applied as a runtime
transform -- it represents the larger pad part's authored
geometric extent past the hinge in the .model file itself.

Runtime `_render_track_pad_body`:
  xform_clamped[:, 0:3, 3] += seg2_offset * xform_clamped[:, 0:3, 1]

Same shift for both renderer keys (segmentModel + segment2Model).

Diagnostic plot updated:
  * Green diamond + line = hinge point (segment2Offset out)
  * Faint red dashed line = segmentOffset extent (for context;
    no longer a runtime quantity)

### Pad offsets are RADIAL OUTWARD from wheel center (1.118.79)

Coffee: "seg offsets are from wheel center out.. add them the
the spline point distance to wheel center".

`segmentOffset` and `segment2Offset` are radial outward
displacements from the chain anchor.  On arc pads they push
the mesh outward from the wheel hub (anchor at R, mesh sits
at R + offset).  On line pads they push perpendicular outward
from the chain tangent.  Both cases reduce to "shift along
pad-local +Y axis" -- the orientation pipeline already sets
+Y to "outward from chain interior" on every pad.

Runtime `_render_track_pad_body`:
  xform_clamped[:, 0:3, 3] += offset * xform_clamped[:, 0:3, 1]

(was previously +offset along +Z (forward) in v1.118.68 and
then -offset along +Z in v1.118.77.  The correct axis is +Y.)

Diagnostic plot tick lines now draw outward along the
perpendicular-to-tangent direction (= 90 deg CCW rotation of
chain forward in 2D, which matches the chain's outward normal
for a CW-traversed closed loop).

### Drop pad-mesh offset shift + drive-wheel-only diagnostic plot (1.118.78)

Coffee: "don't offset the rotation point on the pads and redraw
just the drive wheel spline and pads.. keep window on drive
wheel drawing".

Two changes:

1.  `_render_track_pad_body` no longer applies any per-variant
    forward shift along pad-local +Z.  Each pad-mesh part
    renders with its mesh-local origin at the chain anchor
    verbatim.  Whatever forward / backward extent the mesh
    body has stays as authored.  Result: rotation pivot for
    both segment + segment2 is the mesh's own (0, 0, 0) in
    pad-local coords -- the runtime no longer second-guesses
    where the pivot should be.
    Removed `seg_offset` / `seg2_offset` writes into
    `xform_clamped[:, 0:3, 3]`.

2.  `cust_tools/plot_homie_T110E4.py` collapsed from
    LEFT + RIGHT + zoom (3-panel, 22x20") down to a single
    drive-wheel zoom panel (16x14") so the offset geometry
    reads at usable scale on screen.  At each chain anchor on
    the front sprocket arc:
      * magenta dot = pivot (= chain anchor)
      * red line + square = where mesh-local origin would
        sit if you shifted by segmentOffset (0.258 m
        backward along chain forward)
      * orange line + square = same for segment2Offset
        (0.09 m)
    Lets us see the pivot-to-mesh-origin geometry directly
    against the chain spline + sprocket circle.

### segmentOffset is pivot-in-mesh-local, not anchor-to-mesh-forward (1.118.77)

Coffee: "both of the pad pieces should be considered as one.
they need to both rotate at the pivot as one part.  could these
be the offsets to center of rotation".

Yes -- `<segmentOffset>` and `<segment2Offset>` in the gameplay
XML are the LOCATION OF THE PIVOT (= center of rotation = hinge
pin) inside each mesh part's local coordinate frame.  The
runtime had the sign inverted: `+offset * z_axis` moved the
mesh-local ORIGIN forward of the chain anchor, leaving the
pivot 2x the offset ahead of where it should be -- and each
of the two pieces ended up with its own pivot at a different
spot.

Fix in `_render_track_pad_body` dispatch:
  * Flip sign: `xform_clamped[:, 0:3, 3] -= offset * z_axis`
    (shift the mesh BACKWARDS so the mesh-local pivot point
    `(0, 0, offset)` lands at the chain anchor).
  * Both `segment` and `segment2` now SHARE one pivot in world
    space per anchor.  The two pieces extend "behind" the pivot
    along their respective lengths, forming one rigid shoe that
    rotates around the chain anchor as the chain bends.

Pink hinge-pin markers updated to match: one pin per chain
anchor at the SHARED pivot location (the side's pre-offset
xform_world translation), instead of per-renderer at the
old post-shift positions.  Each anchor on T110E4 now gets
ONE pink crosshair (was two -- one for each variant at its
incorrect-by-2x post-shift location).

### Pink hinge-pin markers at every pad transform (1.118.76)

Coffee: "render the pink seg markers at the hinge pin transform
locations".  The pink pin-axis lines (removed in v1.118.58) are
back, but driven by the post-1.118.74 dispatch math so each
2-piece shoe gets TWO pins per anchor at the actual
segmentOffset / segment2Offset world positions -- not just at
the pre-shift chain anchor.

Implementation:
  * `self.pin_lines = LineBatch(line_width=1.8)` constructed
    alongside `self.track_lines` in viewer `__init__`.
  * In `_render_track_pad_body`'s dispatch loop, for each
    renderer key compute:
        pin_origin = xform_clamped[:, 0:3, 3]   # hinge world XYZ
        pin_dir    = xform_clamped[:, 0:3, 0]   # pad-local +X axis
        pin_start  = pin_origin - ex * pin_dir
        pin_end    = pin_origin + ex * pin_dir
    where `ex` = renderer's X half-extent (full pin spans the
    pad's width).  Per-renderer collection so segment + segment2
    pins land at their respective post-shift positions, drawing
    two pink crosshairs per anchor on T110E4 / one per anchor
    on single-variant tanks.
  * Post-dispatch: `pin_lines.update(pin_segments)` +
    `render(self.color_shader, view, proj)` -- pins drawn on
    top of pads with depth test on, so they read clearly
    against the warm tank-tone pad surfaces.
  * Pink colour = `(1.0, 0.40, 0.78)`.

### Extract dialog auto-sizes to fit controls (1.118.75)

Coffee: "the extract window needs to fit the controls".  The
`_show_extract_picker` Tk dialog had a hardcoded `'360x300'`
geometry that cut off the bottom rows (Track Segments + Tank
Definition checkboxes, the Extract-textures toggle, and the
button row) when all controls were present.

Fix in `_show_extract_picker`:
  * `win.geometry('')` -- let Tk size the window from its
    packed children.  Width auto-expands for long Tank
    Definition path text; height auto-expands as rows are
    added.
  * Button row switched from `side='bottom'` to `side='top'`
    so it packs in natural order below the texture toggle
    instead of potentially overlapping under auto-geometry.
  * Added a separator above the buttons for visual grouping.

### 2-piece shoe rotates as one rigid body (1.118.74)

Coffee: "rotate 2 piece segments as one.. not as 2 different
seg parts".  On T110E4 the prior per-renderer terrain clamp
computed a SEPARATE Y lift for segment + segment2 using each
renderer's own `bbox_half_extent` -- on uneven terrain the two
pieces ended up at different world Y, so they read as two
independently-rotating parts instead of one rigid shoe.

Fix: terrain clamp moved OUT of the per-renderer loop.  Per
side:

  1. Compute the worst-case Y half-extent across all renderers
     on that side.
  2. Sample terrain ONCE at every chain anchor's world XZ,
     compute the lift using the shared extent.
  3. Bake the lift into the side's shared `xform_world_L/R`
     before the dispatch loop -- both segment + segment2
     renderers consume the same lifted xform.

Per-renderer dispatch now ONLY adds the per-variant hinge
offset along pad-local +Z (segment gets segmentOffset, segment2
gets segment2Offset).  Rotation and Y are identical between
the two pieces of a given anchor.

Also removed `pin_xform_per_side` / `ex_for_pins` stubs that
were only ever consumed by the magenta pin-axis overlay
deleted in v1.118.58.

### Drop `<radiusOverrides>` pitch-radius override (1.118.73)

Coffee: "the drive wheel offset.. remove it".  The v1.118.56
parsing of `<radiusOverrides>` in `loaders.py` is removed.  That
block had been replacing the drive sprocket / idler outer-tooth
radius (from `<wheelGroups>`) with the smaller chain-pitch
radius so the chain wrapped the inside-of-the-teeth line.

Now the chain uses whatever `<wheelGroups>` supplies for the
drive sprocket -- same source as the road wheels.

Effect on T110E4: WD_L0 R goes from 0.3844 m (pitch override)
to 0.3245 m (wheelGroups).  Chain wraps the smaller radius;
end sprocket pie-slices in the diagnostic plot are smaller.

### Runtime chain pad count: route through `_resolve_pads_per_side` (1.118.72)

Coffee: "math png not what i see".  The offline diagnostic plot
(`cust_tools/plot_homie_T110E4.py`) and the runtime F8 / pad
render disagreed on T110E4 -- the PNG showed 80 pads, the viewer
drew 160.

Root cause: `Viewer._compute_homie_chain_for_frame` read
`n_pads = int(seg_count)` straight off the chassis-info dict,
bypassing `_resolve_pads_per_side()` -- which halves
segmentsCount on 2-part shoe tanks per the 1.118.69 fix.  Tiger
(no segment2) was unaffected because seg_count == anchors there.
T110E4 (segment + segment2) wanted 80 anchors but got 160.

Fix: route `n_pads` through `_resolve_pads_per_side()` (returns
80 for T110E4, 80 for Tiger, etc.).  Single source of truth for
"how many pad anchors per side"; `_wire_homie_to_physics` was
already using it, so they're now in sync.

The diagnostic plot script gains an extra cross-check at the
end -- if `math_images/runtime_dump.json` is present, it
compares offline vs runtime pad-array lengths + per-pad
|delta| to surface this kind of pipeline divergence
automatically.

### Physics plane fit uses homie chain bottom run + XML spline dropped (1.118.71)

Coffee: "now get the physics to use this spline... dont use xml
spline if not needed".

**Physics plane-fit input switched from wheel hubs to homie
chain bottom run.**

`tank_physics.TankPhysics` gains:

  * `_homie_bottom_local_L` / `_homie_bottom_local_R` -- chassis-
    local XYZ of the homie chain's bottom-run pads, per side.
    Set once per tank load.
  * `set_homie_bottom_run(local_L, local_R)` -- public setter
    the viewer calls after building the bind-pose homie chain.

`TankPhysics.update()`: when the chain is set, the lstsq
plane fit consumes chain-pad terrain samples instead of
wheel-hub samples.  Same `fit_plane(targets_local)` recipe,
just denser + along the true contact line.  Per-wheel
suspension classification (CONTACT/HANGING/OVER_COMP) and
iterative refinement still operate on wheels.  Empty / None
chain -> falls back to the old wheel-hub plane fit verbatim.

**XML `.track` spline load path dropped at tank load.**

The legacy `track_spline.TrackSplineLoader.from_pkg` pulled
left.track + right.track Collada files, parsed V_locs, built
a centripetal Catmull-Rom NURB.  Homie (`track_homie`) builds
the same chain shape purely from chassis bones + wheel radii;
the .track files are now dead weight on every tank load.

Removed work per tank load:
  * `.track` file extraction + parse (`TrackSplineLoader.from_pkg`)
  * V_loc -> bone match (`attach_binding`)
  * Rigid pad cache via `build_augmented_control_loop` + CR +
    resample (~2 ms per side)

Kept (homie still needs them):
  * `parse_chassis_bone_world_positions` on
    Chassis.visual_processed -- bind-pose bone dict that both
    homie and physics consume.
  * `_resolve_track_pad_paths` + `_load_track_pad_meshes` for
    the GPU-resident instanced pad renderers.

F8 overlay gate switched from `_track_left/right is not None`
to `_track_chassis_bones_bind` -- chain rendering only ever
read the bone dict, never the spline objects.

`_compute_stock_chain_for_frame` retained as dead code
(self._track_left is always None now so it returns 4xNone on
first check -- safe).

Viewer adds `_wire_homie_to_physics`: computes bind-pose homie
chain, masks bottom-run pads (Y in lower 35 % of chain Y range),
hands them to `tank_physics.set_homie_bottom_run`.  Per-tank-
load log: `[homie-physics] chain bottom run -> physics plane
fit: L=N pads R=M pads (was wheel-hub samples)`.

### Revert CW-arc enforcement, keep 2-part halve fix (1.118.70)

Coffee: "wd_l1 and wd_r1 are figure 8".  The CW-arc enforcement
introduced in 1.118.69 caused front-roller WD_L1 / WD_R1 to draw
a near-360 deg wrap whenever `_correct_R` scaled the adjacent
front sprocket WD_L0 / WD_R0 -- the bigger sprocket tilts the
roller's exit tangent slightly CCW (~6 deg) of its entry, and
the CW enforcement re-routed that as a 354 deg CW loop.  Result:
visible figure-8 on the two frontmost rollers.

Investigation showed the CW enforcement was never necessary in
the first place.  The diagnostic claim "T110E4's rear sprocket
needs >180 deg CW wrap" was a misreading -- the prior
`_short_arc_signed_diff` already returns the correct 144 deg
arc for WD_L6's a_in=-128 deg / a_out=+88 deg case, with
direction = -1 (CW) in `_place_pads`, and the walk traverses
through angle -180 deg (= backmost point of the wheel).  The
chain wraps around the BACK of the sprocket -- which is the
geometrically correct path.

Real fix:  ONLY the segment-count halving from 1.118.69 was
needed.  Reverted:
  * `track_homie._cw_signed_arc` -- removed.
  * `_measure_loop`, `_correct_R`, `_place_pads` -- restored
    to calling `_short_arc_signed_diff`.
Kept:
  * `Viewer._resolve_pads_per_side` -- still halves segmentsCount
    on 2-part shoe tanks (segment2ModelLeft present).

Verification on T110E4 post-revert:
  - mean pad spacing = 0.1716 m (target 0.172, error 0.25 %)
  - std = 0.0025
  - max gap = 0.1724 (no jumps)
  - WD_L1 / WD_R1 wraps = ~1.5 deg (touch, no figure-8)
  - Sprockets WD_L0 / WD_R0 scaled to R=0.469 (natural 0.384;
    `_correct_R` k=1.22 to absorb 0.39 m err to target).

### T110E4 spline fix: CW arcs + halve count for 2-part shoes (1.118.69)

Coffee: "spline is completely wrong on the T110E4.  Pull data
and plot on paper".  Added `cust_tools/plot_homie_T110E4.py`
which dumps every input the runtime homie chain sees
(`bones`, `radii`, `roles`, `segmentLength`, `segmentsCount`,
gauge) AND the resulting pad positions, side by side with a
matplotlib side-view PNG.  Two bugs surfaced:

**Bug 1 -- arc direction at the rear sprocket.**
`track_homie._short_arc_signed_diff` picked the SHORTER arc
at every wheel.  For Tiger / T30 the rear is a small idler
at road-wheel Y, the chain wraps <180 deg, and "shortest" IS
the correct path.  T110E4 has TWO drive sprockets at higher Y
than the road wheels (front WD_L0 at Y=+0.92, rear WD_L6 at
Y=+1.01 vs road wheels at +0.42), so the chain must wrap
>180 deg around the BACK of WD_L6 -- the "short" arc instead
cuts across the inside of the loop (physically impossible).

Fix: `track_homie._cw_signed_arc` returns the CW-enforced
signed arc (angle DECREASING in atan2, matching the loop
traversal sense `_order_loop` produces).  Replaces
`_short_arc_signed_diff` at three callsites:
`_measure_loop`, `_correct_R`, and `_place_pads`.  Arc
direction in `_place_pads` is now hardcoded to `-1` (CW).

**Bug 2 -- segmentsCount counts PARTS, not anchors.**
T110E4 ships TWO shoe parts per pad anchor (`segmentModel*`
+ `segment2Model*`).  The XML's `segmentsCount=160` is the
PART count, not the anchor count -- so the chain wants 80
anchors, not 160.  With 160 the chain math (geometric
length ~13.5 m) was less than half the target (160 * 0.172
= 27.52 m), forcing `_correct_R` to inflate sprocket radii
by 6x.  With 80 the target = 13.76 m, matching the actual
geometry within 2% (an err `_correct_R` cleanly absorbs).

Fix: `Viewer._resolve_pads_per_side` halves the count when
`track_segment_models['segment2ModelLeft']` is present.
Tiger / T30 (no segment2 model) keep the verbatim count.

Verification on T110E4 with both fixes:
  - mean pad spacing = 0.170 m (target 0.172, error 1.4%)
  - std = 0.006 m (was 0.228 m before fix)
  - max gap = 0.172 m (was 2.35 m before fix)
  - Y range [+0.09, +1.27] (all on wheel surfaces)

### Two-part pad assembly: per-renderer hinge offset (1.118.68)

Coffee clarified the T110E4 case: "some tanks have 2 parts but
they are both attached to one point.  there are 2 offsets to
get total pad thickness.  2 pad parts."

Correct model -- the shoe assembly on tanks like T110E4 is TWO
rigid mesh parts that share ONE chain anchor.  `segmentOffset`
positions part 1 along pad-local +Z; `segment2Offset` positions
part 2.  The two together give the total assembled-shoe thickness.
Not alternation -- co-location.

The v1.118.66 stride-2 attempt (which assumed alternation) was
wrong.  This pass implements the actual structure:

  * Removed the bulk `seg_offset` pre-shift of xform_L / xform_R
    (it could only carry one offset).
  * Read both `segmentOffset` and `segment2Offset` from
    `track_segment_models`.
  * In the dispatch loop, pick the offset by the renderer's
    XML key name:
        is_seg2         = 'segment2' in key.lower()
        offset_for_this = seg2_offset if is_seg2 else seg_offset
    then apply it in world-space to a fresh copy of
    `xform_world`:
        xform_offset[:, 0:3, 3] += offset_for_this
                                   * xform_offset[:, 0:3, 2]
  * Both renderers draw the FULL instance set (same chain
    anchors) -- only the forward offset differs.

Tiger I (segmentModel only -> single renderer per side) keeps
its prior behaviour: one offset, all instances.

### Revert v1.118.65 + v1.118.66 T110E4 attempts (1.118.67)

Coffee: "revert last 2 tries".  Both T110E4-targeted changes
backed out:

  * v1.118.65 `<segmentsPlaneOffset>` parser in `loaders.py`:
    removed.  `info['chassis']` no longer carries the field.

  * v1.118.66 `_render_track_pad_body` rewrite in `viewer.py`:
    reverted to single-variant logic.  The bulk segmentOffset
    pre-shift before the dispatch loop is restored
    (`for xform in (xform_L, xform_R): xform[:, 0:3, 3] +=
    seg_offset * xform[:, 0:3, 2]`); the per-renderer stride-2
    slicing + segment2Offset application is removed.  Dispatch
    loop is back to the simple "L gets xform_world_L, R gets
    xform_world_R, every renderer draws every instance" shape.

Tiger I still works as it did pre-1.118.65; T110E4 stays broken
pending a different approach.

### T110E4 dual pad-variant rendering fix (1.118.66)

User report: "tracks are not working on all tanks.. probably a
different naming scheme in the xmls?  tiger 1 great... T110E4 ..
nope".

Root cause -- T110E4's `<trackPair>` ships TWO alternating pad
shoe meshes with DIFFERENT hinge offsets:

    segmentModelLeft   = ...track/segment1.model     offset 0.258
    segmentModelRight  = ...track/segment1.model
    segment2ModelLeft  = ...track/segment2.model     offset 0.09
    segment2ModelRight = ...track/segment2.model

Tiger I only has `segmentModel*` (single variant); T110E4
introduces the alternation.  The previous `_render_track_pad_body`:

  1. Pre-shifted EVERY xform_L / xform_R by `segmentOffset`
     (segmentOffset only -- segment2Offset entirely ignored).
  2. In the dispatch loop, fed ALL N instances to BOTH renderers
     (segment renderer drew N=80 + segment2 renderer drew N=80
     ON TOP of them = 160 pads overlapping per side, with the
     wrong offset on half of them).

Fix:

  * Removed the upstream bulk pre-shift of xform_L / xform_R.
  * Count variants per side at dispatch time (`_l_variants`,
    `_r_variants`) from the renderer key set (any key with
    'Left' / 'Right' in its name).
  * In the dispatch loop, when a side has >= 2 variants:
        is_seg2     = 'segment2' in key.lower()
        stride_start = 1 if is_seg2 else 0
        xform_sub   = xform_world[stride_start::2].copy()
        offset_for_this = seg2_offset if is_seg2 else seg_offset
    so segment gets even instances + segmentOffset, segment2
    gets odd instances + segment2Offset.  Single-variant tanks
    (Tiger I etc.) keep getting all instances + segmentOffset.
  * Hinge-offset shift then applied in WORLD space using
    `xform_sub[:, 0:3, 2]` (the pad-local +Z axis after the
    chassis transform).  Same math as the pre-1.118.66 local-
    space pre-shift, just deferred to where we know the
    variant.

Result on T110E4: 80 segment1 + 80 segment2 alternating along
each side, each at its proper hinge offset.  Tiger I behaviour
unchanged (verified by code-path -- only one variant, no
stride taken).

`<segmentsPlaneOffset>` parsing added to `loaders.py` in 1.118.65
(half-gauge distance from chassis centerline to pad plane) and
remains a defensive fallback for the chain's lateral placement.

---

## 2026-05-09

### Recorder split out of viewer.py (1.113.0)

`viewer.py` crossed 13.4k lines in 1.112; the recorder block
(F3 manual + 1-Turn snapshot/capture/finalize/save_json) was
~880 self-contained lines that don't touch GL state.  Moved
to a new module:

    tankExporterPy/recorder.py   628 lines

Module-level functions take the active `Viewer` as their first
arg and read / write the same `_turn_test_*` / `_manual_record_*`
fields the methods used to operate on -- mechanical extraction,
no behaviour change.

`Viewer` keeps thin `_*` forwarder methods so every existing
call site (button handlers, render hooks, the zigzag step which
delegates back to `_build_recorder_frame`) is untouched.

Net: viewer.py 13360 -> 12535 (-825 lines).  Smoke-tested:

  * 9 forwarder methods on Viewer all resolve.
  * 9 module functions in recorder all resolve.
  * `recorder.build_recorder_frame(viewer, ...)` and
    `viewer._build_recorder_frame(...)` produce frame dicts
    with identical keys (41 top-level + 10 wheels on T30).

The zigzag recorder still lives on `Viewer` -- its
finite-state-machine over driving phases tangles deeply with
the drive code and earns its own follow-up extraction (next
candidates per Coffee 2026-05-09: hud.py for status overlay /
mini-map / contact-trail FIFO; track_spline_overlay.py for
the F8 render block).

`ARCHITECTURE.md` File Map updated.

---

## 2026-05-08

### Spline Z-flip fix verified by `<teethSyncs>` engine data (1.111.0)

Coffee asked me to verify the spline / wheel alignment by
generating the engine-XML tooth positions and comparing them
to V_loc positions.  Confirmed a real bug: V_locs were being
loaded **Z-unflipped** but the chassis-bone frame is
**Z-negated** relative to the .track file's authored frame.

Symptom: spline appeared to drape correctly because the V_loc
set is Z-symmetric (front <-> rear cluster), but the whole
spline was rotated 180 deg about Y.  Lean-pitch (squat under
accel / dive under brake) was being drawn with the wrong sign
at each end -- "spline sinks at rear under accel, front under
brake".

Verification (`cust_tools/plot_t30_spline_math.py`):

  * For each drive wheel (WD_L0 rear, WD_L9 front), tooth k=0
    sits at `(wheel_Y - R*sin(startAngle), wheel_Z -
    R*cos(startAngle))` on the outer track surface.
  * Z UNFLIPPED: best-match d = 4.7 cm; first-tangent V_loc
    on the wrong end of the tank.
  * Z NEGATED:  WD_L0 tooth k=2 -> V_loc31 at **1 mm**, WD_L9
    tooth k=12 -> V_loc19 at **14 mm**.  All 15 teeth align
    with adjacent V_locs at 24-deg spacing.  Machine-precision
    match -- impossible by chance.

Fix in `track_spline.to_chassis_frame()`: `flip_z=True` is now
the default.

Side observations from the math:

  * WD_ = "Wheel Drive" (asset-naming).  Both end wheels are
    drive wheels.  CLAUDE.md's "lowest idx = drive sprocket,
    highest = idler" was loose -- on T30 WD_L0 has 5.9 %
    tooth height, WD_L9 has 1.3 % (sprocket-vs-near-flush
    front guide), but both engage teeth.
  * Middle WD_L1..WD_L7 are return rollers (R=0.19 m, sit at
    Y=+1.05 under the top run).
  * `<wheelGroup><groupRadius>` is the outer track-surface
    radius, NOT the pitch radius.  Pitch radius (for engin-
    eering-correct per-pad spacing) =
    `segmentLength / (2 * sin(pi / teethCount))`.  T30 = 0.320 m
    for both drive wheels.  Difference vs outer = tooth height.

Three verification PNGs saved to `<root>/math_images/`:

  * `T30_spline_zflip_compare.png`     -- before / after side-by-side
  * `T30_drive_wheel_teeth_zoom.png`   -- per-wheel tooth + V_loc overlay
  * `T30_wrap_test_all_wheels.png`     -- all 18 LEFT wheels with circles

Also extended `to_chassis_frame()` with a `flip_z` kwarg so
the standalone probe scripts (`_plot_t30_*.py`) that were
written against the pre-fix convention can opt out
(`flip_z=False`).

`docs/TRACK_PHYSICS.md` "Coordinate-frame conversion" section
rewritten to reflect the verified convention.

### Spline pad count from gameplay XML, no hardcoded fallback (1.110.1)

Per Coffee 2026-05-08: "no hardcoded spline descriptors; get
it from the visual or tank def."  The two `NURB_PADS = 64`
sites in `viewer.py` are gone.  Replaced with
`Viewer._resolve_pads_per_side()` which reads
`segmentsCount` from the gameplay XML (parsed by
`VehicleXMLLoader.parse_info` into
`info['chassis']['segmentsCount']`) and returns
`segmentsCount // 2`.

Plumbing:

* `VehicleXMLLoader.parse_info` now pulls `<segmentLength>`
  and `<segmentsCount>` out of `<tracks><trackPair>` and
  stashes them on `info['chassis']`.  Best-effort: missing
  fields don't fail the parse.
* `Viewer._resolve_pads_per_side()` returns `segmentsCount //
  2` from the active tank's chassis info (sanity-bounded to
  [4, 500] per side -- anything outside that is a malformed
  XML).  Returns 0 when the XML didn't supply a count.
* The rigid-pad cache builder + the spline render block both
  call `_resolve_pads_per_side()` and skip rendering when it
  returns 0 -- no hardcoded fallback.  Mod tanks that ship a
  non-standard chassis XML simply won't render the F8 spline
  overlay; the rest of the viewer is unaffected.

T30 supplies `<segmentsCount>234</segmentsCount>` so it now
renders at 117 pads / side.  The `[track_spline]` print at
load time will surface the resolved value.

### Spline golden-rule clamp + status overlay + mini-map + stable recording filename (1.110.0)

Big batch of UX + correctness work driven by Coffee's "golden
rules" list 2026-05-08.

#### Spline golden-rule clamp (rules 1-3)

> 1. Spline can NEVER sink below terrain.
> 2. Spline can only sag to wheel max-hang length.
> 3. Spline can NEVER go above max-retract height.

Symptom: spline sank into terrain at the rear under accel
(squat) and at the front under brake (dive).  The lean-pitch
target during longitudinal-G transients hits ~9 deg, which
when applied as a chassis_render rotation around chassis-local
origin pulls the bottom-run pads at chassis-local Z = +/- 2.6 m
through the ground by ~10-25 cm.

Fix in the spline render block: at tank load, cache a rigid
(no-residual) per-pad chassis-local position set by running
the augmented control loop + CR + resample with the BIND bone
dict (one CR pass per side, ~2 ms once).  Each frame the
deflected pads are computed as before (~3 ms), but their
WORLD Y is then clamped:

* Lower bound = max(terrain_y_at_pad_xz, rigid_world_y + ext_cap)
* Upper bound = rigid_world_y + comp_cap

`ext_cap` (negative) and `comp_cap` (positive) come from the
suspension envelope on the active TankPhysics instance.
Terrain-Y lookup is `Terrain.sample_heights(xs, zs)`, called
once per side with the full pad xz array.

Per-side terrain-Y lookup time published into `_frame_timers`
as `terrain_y_lookup_spline` so the F3 recorder picks it up.

#### Status overlay -- Heading / XYZ / Speed (per frame)

Three-line live readout in the upper-left, drawn every frame
under the existing physics-timer line:

    Heading:   123.4 deg
    XYZ:    +12.34   +0.45   -7.89
    Speed:   +5.67 m/s  ( +20.4 kph)

Heading = `tp.yaw_deg mod 360`, [0, 360).  XYZ = `tp.pos`.
Speed = `tp.cur_forward_mps` (the ACTUAL drive-layer ramped
speed, NOT the speed-step selector value, per Coffee's
"current speed not selected speed" instruction).

Cached by formatted text -- rebuilds on any value change at
~1-2 ms / rebuild.  Amber tint to read against any terrain
colour.  See `Viewer._render_tank_status_overlay`.

#### Bottom-left mini-map

160 x 160 pixel top-down mini-map at the bottom-left of the
scene viewport (just right of the info panel, just above the
console).  Draws:

* Dark-olive background, amber border.
* Centre crosshair + 'N' label at the top edge.
* Yellow dot at tank world XZ.
* Red-orange line from the dot in the chassis-forward direction.

Map extent comes from `terrain.world_size` (default 40 m so
[-20, +20] m on both X and Z).  Tank pixel position is clamped
to stay visible if the tank drives off the terrain extent.

Rendered each frame to a fresh pygame surface, uploaded as a
texture, drawn as one quad.  Cache key is `(pixel_x, pixel_y,
heading_deg_int)` so a stationary tank doesn't re-upload --
zero-cost when parked.  See `Viewer._render_minimap`.

#### F3 recording: stable filename + rotating backup + sweep

Per Coffee: "pick a name and stick to it.  Back up if useful
for cross-reference."

* Save path is now `test_runs/manual_<tank>_latest.json` --
  always overwrites.
* Before overwrite, the existing `_latest` is renamed to
  `manual_<tank>_prev.json` so the last two runs are always
  available for A-B comparison.
* On F3 START, sweep `test_runs/manual_*.json` and delete any
  timestamped variants left over from older versions.  The
  `_latest` and `_prev` files for the current tank are
  preserved; turn-test / zigzag recordings are NOT touched.

### Y-band filter tightened: T30 idler no longer treated as a road wheel (1.109.0)

The F3 manual recorder (1.107) caught its first real bug.
Coffee reported "it is using the damn idler as a weight wheel
again!!".  The recording confirmed: T30 was reporting
**18 wheels** (9 per side) when the true road-wheel count is
8 per side.  The extra slot per side was a drive-sprocket /
idler that the chassis artist named with a `W_` prefix
instead of `WD_` -- so the auto-extract's name filter took
it at face value, and the Y-band post-pass's 15 cm tolerance
was too loose to catch it.

Per-wheel state distribution from the recording (T30, ~10 s
of driving with terrain on, suspension on):

| slot | bone | HANG % | mean delta_y |
|------|------|-------:|-------------:|
| 0-7  | W_L0..W_L7    | 8-19 %  | -2 to -23 mm  |
| **8**| **W_L8**      | **68 %**| **-137 mm**   |
| 9-16 | W_R0..W_R7    | 8-29 %  | -8 to -107 mm |
| **17**| **W_R8**     | **97 %**| **-139 mm**   |

W_L8 / W_R8 sit at chassis-local Y = 0.533 / 0.566 -- about
105 mm above the eight real road wheels at Y = 0.421-0.430.
That's outside the ground-contact band, so on flat terrain
they HANG (delta_y ~ -14 cm, well below ext_cap).  But on
the 3-32 % of frames they DID touch terrain (uneven ground
lifting their world Y up to the wheel), they entered the
plane fit and yanked the chassis pose.

Fix in `from_chassis_meshes`:

* `Y_BAND` tightened from 0.15 m to 0.06 m.

Real road wheels span only 7 mm on T30, ~10 mm on T110E4.
60 mm is wide enough for any legitimate suspension geometry
variation but tight enough to catch T30's 105 mm gap.
Bourrasque (smallest non-road gap = 232 mm) and Hotchkiss
EBR (all wheels at the same Y) are unchanged.  Verified by
re-running the filter on the T30 recording's wheel set:
W_L8 and W_R8 drop, the eight real road wheels per side
remain.

This is a clean single-constant tweak; no algorithmic
change.  Long-term we should pull `<wheelGroups>` from the
gameplay XML and respect the artist-declared road-wheel set
directly, but that's a bigger change.

### Vectorised track-spline math -- 12x faster overlay (1.108.0)

Coffee reported "fps is shit ~21, way down before spline" --
the F8 NURB overlay was the culprit, and the fix was a pure
numpy vectorisation of the two hot functions.  Output is
**bit-identical** to the previous implementation (max abs
diff vs reference: 0.000e+00).

Microbenchmark on a T30-shape control loop (26 ctrl points x
64 samples per segment, both sides per frame):

| Function | Before | After | Speedup |
|----------|-------:|------:|--------:|
| `centripetal_catmull_rom_closed` | 17.66 ms | 1.45 ms | 12.2x |
| `resample_uniform`               |  0.59 ms | 0.12 ms |  5.0x |
| **per-frame total (both sides)** | **36.51 ms** | **3.13 ms** | **11.7x** |

That's ~33 ms / frame returned to the budget.  At 60 fps
(16.7 ms / frame) the spline alone was busting frame time
twice over -- explains the 27 fps cap from spline math
alone, plus normal scene work driving it down to the
reported ~21 fps.

#### Why so much faster

Both functions had per-sample Python loops doing tiny numpy
operations on 3-element vectors -- the per-call dispatch
overhead of numpy dwarfed the actual arithmetic.  T30 case:
26 segments x 64 samples x 6 ops = 9984 numpy calls per side
per frame, x 2 sides = 19968 dispatched ops.

Fix in both functions: lift the inner per-sample loop to
broadcast `(samples_per_seg,)` weight vectors against the
`(dim,)` control points, computing every sample in one numpy
call per stage.  Output array assembly via
`np.concatenate(out_segments)` instead of Python list of
3-vectors.  `resample_uniform` already used `np.searchsorted`
but called it inside a Python loop -- moved it outside so the
whole `targets` array is looked up in one call.

The closed-loop CR's per-segment knot-time computation
(`np.linalg.norm` of 4 chord vectors) is left as a Python
loop -- only `n` iterations and the broadcast-friendly inner
loop is what was costing the time.

#### Verification

`tankExporterPy/track_spline.py` has a docstring noting the
vectorisation; the functions' contracts (signatures, return
types, shape invariants) are unchanged.  Reference-CR parity
test:

* CR through P[i] for every i (segment-start invariant): OK
* CR vs an obviously-correct slow reference: max diff 0.0
* `resample_uniform` pad tangents are unit vectors
* Total closed-loop length matches old behaviour

### F3 manual recorder + per-section frame timers (1.107.0)

Built to diagnose the chassis-oscillation symptom Coffee
reported after the track NURB overlay was added in 1.105
("oscillating and not settling after spline").  No physics
math was changed; this rev is pure instrumentation.

#### F3 = manual record toggle (was 1-Turn)

Press `F3` to start a recording, `F3` again to stop and write
`test_runs/manual_<tank>_<ts>.json`.  Unlike the 1-Turn /
zig-zag tests, the manual recorder does NOT force auto-circle,
does NOT reset the tank pose on start, and does NOT auto-stop
at any duration -- the user drives normally and we capture
every frame.

The 1-Turn auto-test keeps its "1-Turn Test" button in the
Tools group; it just no longer has a hotkey.  Same for
zig-zag (still on its own button).

#### Per-section frame timers

`Viewer._frame_timers` (a fresh dict cleared at the top of every
`render()`) accumulates per-section CPU wall-clock costs in
milliseconds.  The recorder snapshots a copy per frame.
Sections currently timed:

* `frame_dt_in`     -- the dt actually passed to physics, post-1ms-clamp.
* `physics_update`  -- `tank_physics.update()` wall time.
* `mesh_pose_apply` -- the per-mesh `model_matrix = chassis_pose @ bind` loop.
* `emitters`        -- `_update_emitters_for_chassis_pose`.
* `picker_pass`     -- triangle-picker FBO render.
* `skybox`, `terrain`           -- self-explanatory.
* `spline_overlay`  -- the F8 NURB pass (the suspect).
* `ui`              -- 2-D overlay.
* `frame_total`     -- full CPU-side render() cost before flip().

Sustained dt-spike correlation against `spline_overlay` is the
fastest read on whether the F8 overlay was destabilising the
inertia integrator (omega_n ~ 14 rad/s gives an explicit-Euler
stability ceiling of ~143 ms; safe at 60 fps but not at 5 fps).

#### Expanded per-frame snapshot

`_build_recorder_frame` is the shared helper now used by both
the 1-Turn / zig-zag tests AND the F3 manual recorder.  In
addition to the pose / wheel state already captured in 1.103,
each frame now carries every signal that can plausibly enter
the solver / integrator / classifier feedback loop:

Top-level:
* `target_pitch_with_lean_deg`, `target_roll_with_lean_deg` --
  the actual integrator targets (= solver target + lean offsets).
  The values the second-order spring is chasing.
* `e_pitch_deg`, `e_roll_deg`, `e_y_m` -- spring error terms.
  Persistent non-decay across frames = the integrator is
  fighting an input that keeps changing under it.
* `lift_needed_m` -- render-side terrain-floor lift this frame
  (>0 = the floor pushed the chassis up so a wheel didn't
  penetrate; >0 every frame = wheel can't reach ground).
* `state_changes_count` -- number of wheels whose classifier
  state flipped this frame.  Sustained >0 = chattering.
* `track_spline_active`, `track_test_dy_m` -- spline overlay
  context.
* `cur_forward_mps_input`, `auto_circle_active` -- drive-layer
  context.
* `frame_timers_ms` -- copy of the per-section frame timers.

Per-wheel (`wheels[i]`):
* `delta_y` -- unclamped pre-envelope delta the classifier reads.
* `state_prev_code`, `state_changed` -- classifier transition
  detection.  Lets offline analysis filter for "frames where
  wheels flipped".
* `hyst_dist_to_flip_m` -- signed distance (m) from current
  `delta_y` to the threshold that would flip this wheel's
  state next frame, accounting for hysteresis.  Small positive
  values (`< 0.005`) for many consecutive frames is the
  signature of a wheel chattering at the hysteresis edge.

Plumbing in `tank_physics.py`: `update()` writes
`self.last_delta_y` post-classifier; `_step_pose_integrator()`
writes `_last_target_*_with_lean`, `_last_e_*`, and
`_last_lift_needed_m`.  No solver / integrator behaviour
changed -- only added field assignments.

Output JSON metadata also echoes the integrator constants
(`omegan_pitch/roll/y_rad_s`, `zeta`, `mass_ref_kg`) and the
classifier hysteresis margin so the offline analyser doesn't
have to hunt them down in source.

### Track NURB Y-lift fix + manual deflection test (1.106.0)

Two viewer-side fixes for the F8 spline overlay.

#### Y-lift -- "spline is in the terrain"

`Track_L<i>` bones in `.visual_processed` bind at chassis-local
Y = 0, but the actual TRACK SURFACE sits one wheel-radius +
track-thickness BELOW the wheel hub.  The chassis integrator
positions the chassis-local origin BELOW the terrain so the
wheels reach down to ground.  Together that meant the spline
bottom run was rendering ~7 cm below the terrain
("can't tell, it's in the terrain" -- Professor Coffee).

Fix in `_track_current_bone_positions`: lift each Track_<side>\<i\>
chassis-local Y by `hub_y_local - radius - track_thickness`,
then add the wheel residual on top.  T30: lift = 0.426 -
0.345 - 0.029 = +0.052 m.  M60: lift = 0.448 - 0.357 -
0.0003 = +0.091 m.  Per-wheel hub_y_local because some
chassis variants have wheels at slightly different heights.

The bond between the wheel hub and the track surface is now
geometrically exact -- the spline bottom run rides on top of
the terrain at the same height as the textured rubber-band
ribbon.

#### Manual wheel deflection test (LEFT / RIGHT arrows)

Park-test feature for the spline overlay: while F8 overlay is
on, the arrow keys inject a uniform Y bias into every Track
control point, so you see the bottom run move on a stationary
tank.

* RIGHT arrow held -> all wheels DROP (test droop / extension)
* LEFT  arrow held -> all wheels LIFT (test compression)
* BACKSPACE -> reset bias to 0
* Held rate: 0.5 m / s, clamped to +- 30 cm

The `_show_lookat_lines` flag (the same one that draws the
look-at crosshair while RIGHT-mouse pans) is raised while
either arrow is held, so you've got a consistent visual
anchor (3-axis cross at camera target) for "is the spline
moving?".

Bias is overlay-only -- it does NOT enter the physics
solver, so the tank stays parked and the wheels-on-ground
markers don't lie.  On-screen log shows the current
`track test dy = +/-X.XXX m` value at up to 4 Hz while held.

Use case: you've loaded a tank, F8 the overlay, want to
verify the bottom-run pads track suspension without
having to drive over a heightmap bump.  Hold RIGHT, watch
the bottom run drop; release, watch it spring back to
the resting position the physics solver actually computed.

### Track NURB Phase A3 + B — live deformation overlay (1.105.0)

The spline can now be SEEN.  F8 toggles a per-side closed line
strip rendered through the resampled track loop on top of the
live tank.  The overlay deforms in real time as wheels deflect
over terrain -- bumps push the bottom-run pads up, droops pull
them down, and the V_loc-driven top run / wraparounds stay
locked to the chassis pose through the inertia-damped render
matrix.

Wiring (`viewer.py`):

* New state on Viewer: `self.track_lines` (`LineBatch`),
  `self._track_left / _track_right` (per-side
  `TrackSplineSide`), `self._track_chassis_bones_bind` (bind-pose
  dict captured once at vehicle load),
  `self._show_track_spline` (persisted to config).
* Vehicle-load hook (next to the `from_chassis_meshes` call):
  derives `vehicles/<nation>/<tank>` from the chassis mesh's
  `primitives_zip`, calls `TrackSplineLoader.from_pkg`,
  parses the chassis `.visual_processed` for bones via
  `parse_chassis_bone_world_positions`, attaches per-side
  `TrackBoneBinding`.  Soft-fails on any error -- the rig
  should never block a tank load.
* New helper `_track_current_bone_positions(tp)`: returns a
  `{name: chassis_local_xyz}` dict that's a copy of the bind
  dict with `Track_<side><i>` Y values overridden by
  `tp.smoothed_residual_y[wheel_idx]`.  This is the per-frame
  mapping from suspension state to spline control points.
  Wheel-to-track index by name substitution
  (`W_L0_BlendBone` -> `Track_L0`); the same indexing the
  GPU skinning shader's bone matrix array uses, so the line-
  strip overlay shows the SAME deformation as the textured
  track ribbon.
* Render block in the post-tank overlay pass: per side, builds
  the augmented control loop, runs centripetal CR + uniform
  arc-length resample to 64 pads, transforms chassis-local pad
  positions to world via `tp.chassis_matrix().T` (column-
  vector convention -- the docstring there explicitly says
  `world = chassis_matrix @ vert`, so a batch row-matrix
  multiply uses the transpose), draws closed line strip via
  `LineBatch`.  Yellow = LEFT, cyan = RIGHT.
* `K_F8` keymap added.  Toggling logs "track NURB overlay: on/
  off" + saves to `_cfg['show_track_spline']` so the state
  survives a reload.

Soft-fail on any in-loop exception: the first failure logs a
single warning to the on-screen log and silences subsequent
attempts via `_track_spline_err_logged`, so a glitchy tank
doesn't spam the log every frame.

#### `cust_tools/analyze_track_spline.py` (CLI analyser)

New offline-analysis tool that runs the full spline pipeline
against any tank and prints diagnostics + optional PNG
side-view plot.  Usage:

```
python cust_tools/analyze_track_spline.py A14_T30
python cust_tools/analyze_track_spline.py A14_T30 --plot
python cust_tools/analyze_track_spline.py A14_T30 \
        --bump Track_L4 0.10 --bump Track_R4 -0.05 --plot
```

Reports per side:
* All V_loc names + chassis-local positions
* V_loc -> bound bone (with offset)
* Bottom-run gap detection + chord length
* Track_L\<i\> bones to splice in
* Phase A1 spline metrics (V_locs only)
* Phase A2 spline metrics (augmented w/ Track_L\<i\>)
* Y range of resampled pads -- quick "does the bottom run
  reach the ground?" check.

`--bump <bone> <dy>` injects synthetic Y deflections so you
can verify the spline deforms the right way without needing
the viewer running.  Repeatable.

`--plot` writes `<basename>_track_spline_analysis.png` -- a
side-view (YZ projection) showing V_locs (yellow dots),
augmented control loop (dim yellow/cyan), resampled pad
loop (bright yellow/cyan), and Y=0 ground reference.

### Track NURB Phase A2 — V_loc -> bone binding map (1.104.0)

Extended `tankExporterPy/track_spline.py` with the bone-binding
piece of the kinematic-CR pipeline:

* `parse_chassis_bone_world_positions(visual_path)` -- walks a
  `.visual_processed` node hierarchy and returns
  `{bone_name: world_xyz}` for every named node, multiplying
  parent transforms down so positions are chassis-local.
  Handles BWXML and plain XML.
* `TrackBoneBinding` -- per-side V_loc -> bone map plus the
  bottom-run insertion bracket.  Algorithm:
  1. Filter bones to the requested side
     (`Track_<side>\d+ | Track_VT_<side>\d+ | Track_VD_<side>\d+
     | WD_<side>\d+`).
  2. For each V_loc, nearest bone by 2-D (Y, Z) distance --
     X is essentially constant on a side (T30 left = -1.480
     across every track-related bone), so 2-D suffices.
  3. Detect the bottom-run gap: largest source-order chord
     between consecutive V_locs (T30: V_loc2 -> V_loc15 at
     6.76 m; next-largest 0.4 m).
  4. Order Track_L\<i\> bones by Z for arc-length traversal
     between the gap V_locs (front-to-rear when the
     after-gap V_loc Z is smaller than the before-gap V_loc Z).
* `TrackSplineSide.attach_binding(chassis_bones)` -- attaches a
  binding built by the above.
* `TrackSplineSide.build_augmented_control_loop(bones)` --
  Phase A3's bridge.  Takes a `{bone_name: current_world_pos}`
  dict and returns the full augmented (V_locs spliced with
  bottom-run) `(M, 3)` control array ready for CR.

T30 binding result (sanity check):

| V_loc neighbourhood | Bound bone (LEFT side) |
|---|---|
| V_loc29..V_loc1 (front wraparound)            | `WD_L9` (front idler) |
| V_loc2 (front-bottom transition)              | `Track_L8` |
| V_loc15..V_loc19 (rear wraparound)            | `WD_L0` (rear drive sprocket) |
| V_loc21..V_loc27 (top run, 7 pts)             | `WD_L1..WD_L7` (return rollers, 1:1 in order) |

Geometric improvement vs Phase A1 (V_locs only) on T30:

| Metric | Phase A1 | Phase A2 |
|---|---:|---:|
| Control points         | 17        | 26 |
| Spline length          | 15.126 m  | **15.510 m** |
| Residual vs target     | -2.79 %   | **-0.33 %** (8x better) |
| Pad spacing std        | 0.30 mm   | 0.30 mm |
| Min pad Y              | +0.366 m  | **-0.038 m** (now reaches ground) |

The Phase A1 -2.79 % residual was originally interpreted in
the doc as "track slack budget".  Phase A2 made clear that most
of it was just the missing bottom run -- with the 9 Track_L\<i\>
bones spliced in, the spline matches the gameplay XML's
`segmentsCount * segmentLength / 2` target to 5 cm and the pad
Y minimum reaches -0.038 m (the bottom run now hits the ground
where the wheels are).  Doc updated.

Phase A3 (per-frame deform in `tank_physics.py` driven by
suspension residuals + chassis pose) is now mostly bookkeeping
on top of these primitives -- pull bones, call
`build_augmented_control_loop`, CR, resample.

### Track NURB Phase A1 — track_spline.py module (1.103.0)

Promoted the proven Catmull-Rom + arc-length-resample math from
`_plot_t30_smooth.py` (root-of-tree probe) into a real package
module: `tankExporterPy/track_spline.py`.  Public API:

```python
parse_track_vlocs(text)              # .track XML -> [(name, 4x4)]
to_chassis_frame(vlocs)              # cm -> m, runtime frame
centripetal_catmull_rom_closed(P)    # alpha=0.5 closed CR
resample_uniform(dense, n_pads)      # uniform-arc-length pad transforms
TrackSplineLoader.from_pkg(pe, path) # both sides at once
TrackSplineSide                      # per-side container w/ lazy caches
```

T30 smoke test passes: 17 V_locs / side, total loop length
15.1261 m, pad spacing 0.1292 m mean / 0.30 mm std across 117
pads.  Numbers match `docs/TRACK_PHYSICS.md` exactly.  LEFT/RIGHT
symmetry verified (X = ±1.480 m).

**Coordinate-frame fix.**  The probe negates Y to put top run
below Y = 0 in its plot; the runtime needs the OPPOSITE
convention -- raw V_loc Y matches chassis-bone Y directly,
top run lands at Y = +1.24 m (above the W_L\<i\> wheel hubs at
Y = +0.448 m, which is geometrically correct: the track wraps
over the top of the hull above the wheels).
`to_chassis_frame()` defaults to `flip_y=False` and the docstring
warns against propagating the probe's plot convention.

**Topology discovery, baked into the doc.**  The 17 V_locs cover
ONLY the top run + sprocket / idler wraps -- there are NO
control points along the bottom run (V_loc2 jumps directly to
V_loc15 across a 6.76 m chord at Y ≈ +0.55).  The bottom run
will be synthesised at runtime in Phase A3 by inserting
Track_L\<i\> bone positions (one per road wheel) between V_loc2
and V_loc15 in arc-length order.  This caught a wrong claim in
the original `TRACK_PHYSICS.md` Phase-A1 sketch ("bottom-run
V_locs at Y ≈ 0") -- doc has been corrected.

Phase A2 (bone-binding `{V_loc_i -> bone_name}` map) and Phase A3
(per-frame V_loc update from chassis bones + Track_L\<i\>
insertion + CR + resample) are next.  Module is independent of
viewer + tank_physics so far -- can be exercised standalone.

### Yaw-rate live readout in console panel (1.102.1)

`_dump_bone_angles_to_console` now prints a `yaw rate: X.XX / Y.YY
dps  (XML <rotationSpeed>)` line directly under `yaw`.  X is
`abs(tp._last_yaw_rate_dps)` (instantaneous yaw rate from the
integrator), Y is `tp.max_yaw_rate_dps` (the per-tank cap from the
chassis XML).  When X reaches within 0.5 dps of Y, a `CAP` flag
appears at end of line so cap-engagement is obvious without having
to run the recording analyzer.  Use case: hold A on T110E4 (cap =
26 dps) -- you see `yaw rate: 26.00 /  26.00 dps  CAP`; switch to
M60 (cap = 52 dps) and the same key shows `yaw rate: 52.00 / 52.00
dps  CAP`.  Fast verification that the per-tank cap is wired right.

The static on-screen chassis info panel already showed
`rotationSpeed` (one of the standard `_INFO_SECTIONS` fields), so
no additional UI work was needed there -- the cap value is visible
in the side panel and the realtime rate against it is now in the
stdout panel.

### Per-tank max yaw rate (1.102.0)

Up to now the manual yaw rate was a hardcoded `60 dps` in
`viewer.handle_input` and the auto-circle yaw rate was pure
kinematic `omega = v / R` with no cap.  That meant a 25 m circle at
50 kph asked the chassis to spin at `13.9 / 25 = 0.556 rad/s = 32
dps`, which the renderer applied verbatim regardless of what the
real tank could do.  T110E4's gameplay XML publishes
`<rotationSpeed>26</rotationSpeed>` in degrees per second; T30 ~22
dps, M60 ~48 dps.  We were over-spinning every heavy tank.

Wiring (each step kept thin so a future change can swap one piece
without touching the others):

* `loaders.VehicleXMLLoader.parse_info`: the chassis section's
  `<rotationSpeed>` was already harvested by `_scalars` into
  `info['chassis']['rotationSpeed']` as a string.  No loader change
  needed -- the conversion to float happens at the use site (same
  pattern as `groupRadius_road` / `minOffset` / `maxOffset`).
* `tank_physics.TankPhysics.__init__`: new
  `max_yaw_rate_dps=60.0` ctor arg, stored as `self.max_yaw_rate_dps`.
  60 dps default preserves the previous behaviour for tanks whose
  chassis XML doesn't publish the field.
* `tank_physics.from_chassis_meshes`: forwards the new kwarg to
  both the real-rig and the T110E4-fallback `cls(...)` call.
* `viewer.py` chassis-kwargs construction (~line 8836): coerces
  `_ci.get('rotationSpeed')` to float and adds it to the
  `from_chassis_meshes` call.  Defensive: skip when missing,
  unparseable, or non-positive (the 60 dps default in
  `TankPhysics.__init__` then wins).
* `viewer.handle_input` drive block: replaced the literal
  `yaw_speed = -60.0` with `-tp.max_yaw_rate_dps`.  Sign flipped
  for the same TEPY-Z reason `move_speed` is.
* `viewer.handle_input` auto-circle block: clamps
  `omega_deg = math.degrees(v / R)` to `tp.max_yaw_rate_dps`.
  Effect when a small radius + high speed exceeds the cap: the
  tank can no longer hold the requested radius and visibly traces
  a wider arc.  This is the physically correct outcome -- a real
  T110E4 cannot carve a 25 m circle at 50 kph.

The yaw rate stays INSTANT (no rotational-inertia ramp).  Real
tracked vehicles spool yaw fast because both tracks contribute
torque immediately; the visible "tank pivots quickly when stopped"
behaviour is preserved.  If a future session wants angular
inertia on the chassis (the v1.93.0 attempt that got reverted),
the math should integrate `omega_yaw` against a yaw moment of
inertia rather than re-introducing the contact-classifier
feedback loop -- see PHYSICS.md "Reverted experiments".

### C-key crash on 3rd press (1.101.0)

Pressing `C` cycles camera mode `0 -> 1 -> 2 -> 0`.  The 3rd press is
the only one that takes the `2 -> 0` (commander -> orbit) branch, which
saves commander's current eye + look-at into the orbit camera so free
cam doesn't yank back to a stale pose.  That save block walked
`self.meshes` looking for the gun, with:

```python
_bm = (getattr(_m, 'bind_model_matrix', None)
       or getattr(_m, 'model_matrix', None))
```

`bind_model_matrix` is a 4x4 numpy array on every loaded mesh.  Python's
`or` short-circuit has to evaluate `bool(arr)` on the LHS to pick a
side, and `bool(ndarray-with-more-than-one-element)` raises
`ValueError: The truth value of an array with more than one element is
ambiguous`.  Since this code path only fires on the 3rd `C` press, the
crash was silent until then -- and looked like "the app closes when I
press C three times".

Fix in `viewer.py` C-key handler (~line 9767):

* Replaced the `or` chain with explicit `is not None` fallthrough.
* Wrapped the entire commander -> orbit save block in
  `try/except Exception`, logging the failure to the on-screen log
  and falling through (free cam keeps its previous orbit state)
  rather than killing the session.  This is the right resilience
  posture for a code path that runs once every three presses --
  any future regression here would otherwise wait days before
  surfacing.
* Added `np.isfinite` guards on `offset` / `target_world` before
  writing into `self.camera`, so a NaN'd chassis matrix would
  also fall through cleanly.

### Tank-physics + camera deep-clean (1.100.0)

Long second session covering wheel-physics correctness, camera UX
overhaul, and the tooling spin-out into per-domain docs +
`cust_tools/arch.py`.  All bugs identified by Professor Coffee in
real-time testing; every fix traced back through the math before
landing.

#### Wheel physics: correctness fixes

* **Z-frame double-flip removed.**  `from_chassis_meshes` was negating
  the Z component of every extracted wheel centroid (`tup = (cx, cy,
  -cz, ...)` at `tank_physics.py:693`) on top of the chassis vert
  positions which were ALREADY in GL frame (`flip_z = False` for
  skinned meshes per `loaders.py:336`).  Net effect: physics held
  wheels at `+Z = front` while the rendered chassis had front at
  `-Z` -- markers and contact-hit points appeared mirrored about Z.
  Fixed at the extraction site; `T110E4_WHEELS` hardcoded fallback
  was un-negated at the use sites (`for_t110e4`, the
  `from_chassis_meshes` fallback path) for consistency.  Stale
  comment about "rear-to-front by NEGATED Z" updated to
  "front-to-rear by ascending Z".
* **WD_ wheels filtered out when W_ exists.**  T30 in particular
  carries `WD_L0` (drive sprocket) just inside the 15 cm Y-band, so
  the existing band post-pass leaked it through and the rig had 10
  wheels per side instead of 9.  New filter (`tank_physics.py:699`)
  drops every accepted `WD_` candidate from a side IF that side
  has at least one `W_` road-wheel bone -- safe rule for tracked
  tanks; Hotchkiss-EBR-style rigs (no W_ bones at all) keep their
  WD_ road wheels because the `has_w` check is false there.
* **`track_thickness` regression in v1.93.2 rollback fixed.**  When
  rolling tank_physics.py back to remove the WD_ partial-follow
  Pass 3, the `from_chassis_meshes` signature lost its
  `track_thickness` kwarg.  Viewer was passing it via
  `_chassis_kwargs` from the per-tank XML parse, and the call
  raised `TypeError` -- caught silently at the try/except, leaving
  the rig stuck on the T110E4 hardcoded default (which has T110E4
  wheel positions, not the loaded tank's).  Stars / Xs drew at
  T110E4 positions on every tank.  Fixed by re-adding the kwarg +
  forwarding it to the constructor in both the extracted-rig and
  fallback paths.
* **`renderModelOffset` sign convention normalised.**  T30 publishes
  `<renderModelOffset>-0.029</renderModelOffset>`, T110E4 publishes
  `+0.016`.  Both mean "track ribbon is N metres of rubber between
  wheel hub and ground" -- a strictly positive geometric quantity.
  TankPhysics' formula `target_centre = ty + radius +
  track_thickness` was sinking T30's wheels 2.9 cm into the dirt
  with the negative value.  Fixed at the parse site
  (`loaders.py:2962`) -- `abs(float(el.text))` so downstream code
  never sees the sign convention difference.
* **Residual sign convention cleaned end-to-end.**  Pre-fix:
  `last_residual_y = -(target - rigid)` (counter-intuitive
  negative-meaning-up), `bone_matrix_array` Pass 1 used `out[pi,
  1, 3] = ry` (no flip).  An old "shader sign quirk" docstring
  claimed the shader chain flipped the sign empirically, requiring
  the inverted storage convention.  **It doesn't.** Walked
  `shaders/mesh.vert` end-to-end: standard `world = model * skin *
  position` with no flip anywhere.  Bone Y > 0 produces visible
  vertex up.  The "quirk" was a misdiagnosis years ago that's been
  propagating since.  Fixed: `_compute_residual_y` now publishes
  `target - rigid` cleanly (+up = wheel rises into hull), and Pass
  1 / Pass 2 of `bone_matrix_array` write `out[pi, 1, 3] = +ry`
  directly.  Verified end-to-end: T30 on flat terrain, all 18
  wheels render with center at world Y = 0.374 = exactly
  `terrain + radius + track_thickness` (delta = 0.0000 across
  every wheel).  Wheel BOTTOMS sit at `+0.029` -- exactly track
  thickness above terrain -- so the outer track face kisses
  ground, no rim submerged.  Old "wheels up to their rims"
  symptom is gone.
* **Stale "Sort rear-to-front by NEGATED Z" comment fixed** to read
  "Sort front-to-rear by ascending Z" since the negation no longer
  applies after the Z-frame fix.

The corrected pipeline was verified by a Python probe that builds a
TankPhysics rig from T30's chassis primitives, ticks 50 settle
frames against flat terrain, and dumps per-wheel target / residual
/ rendered Y -- all 18 wheels show delta of 0.0000 m vs target.
Probe lives at the experiment-tree root and is not shipped.

#### Camera + mouse: UX overhaul

* **Mouse rebind: LEFT = orbit, RIGHT = pan, WHEEL = zoom.**
  Previously RIGHT was orbit and MIDDLE was pan -- standard mod-
  driven 3D-tool convention, but Professor Coffee preferred the
  CAD-style left-orbit / right-pan layout.  UI safety preserved:
  a slider drag started inside the panel that wanders outside no
  longer simultaneously updates the slider AND orbits the camera
  (added `slider_active` guard at `viewer.py:8975`).
* **Right-button shows the look-at crosshair.**  Pale-pink XYZ
  cross now appears whenever RIGHT (pan) OR MIDDLE (Y-lift /
  crosshair-only) is held.  Depth test explicitly enabled around
  the draw so the crosshair occludes correctly behind tank /
  terrain -- the look-at point inside geometry shows only the
  parts that emerge to the surface.
* **Camera mode now starts on chase (1) and never persists.**
  Default at `Viewer.__init__` is `camera_mode = 1`; every tank
  load reset to 1 explicitly inside `load_vehicle` (was
  accidentally only firing inside `load_mesh`, the FBX/GLB path).
  `camera_mode` is touched by EXACTLY two sites by rule: the
  `load_vehicle` reset and the C-key cycle handler.  Susp toggle
  no longer changes camera mode (verified) and does not move the
  camera position either (Susp ON → OFF transition snaps
  `tp.pos`, `pitch_deg`, `roll_deg`, `vy` back to 0 so chase /
  commander cameras don't anchor to a stale settled pose).
* **Chase cam (mode 1) is now chassis-locked in yaw + pitch +
  roll.**  Stored independently in `_chase_yaw_deg`,
  `_chase_pitch_deg`, `_chase_distance` (chassis-local frame).
  LEFT-drag in chase mode rotates the chassis-local orbit angle
  around the driver, NOT the world-frame orbit.  Result: drag
  to look at the tank's right side, then drive into a turn --
  the camera STAYS on the right side as the tank yaws.  Same
  "child looking out a side window" behavior the user specified.
  Wheel zoom is mode-aware: scales `_chase_distance` in chase,
  `self.camera.distance` in orbit, no-op in commander.
* **Commander cam (mode 2) gains head rotation.**  LEFT-drag in
  commander mode now rotates the head -- `_head_yaw_deg`
  (left/right) + `_head_pitch_deg` (up/down, clamped to ±85°) --
  in chassis-local space, so head orientation persists through
  tank turns.  Reset to 0 / 0 (looking straight forward, level)
  on every entry into commander mode so each "stepping into the
  seat" starts known.
* **C cycle: 0 → 1 → 2 → 0, with commander view exported on the
  2 → 0 hop.**  When leaving commander, the head-rotated eye +
  target are baked into `self.camera.center / yaw / pitch /
  distance` so free cam picks up exactly where commander was
  looking instead of yanking back to a stale pre-commander
  pose.  Free cam was being "left behind" when the tank had
  driven a long way during commander mode -- this fix snaps it
  forward to the chassis-locked commander position.

#### Render passes: depth-test + ordering fixes

* **Wheel markers now respect the z buffer.**  Pink ground stars,
  cyan wheel-target stars, yellow suspension shafts, and blue
  physics-hit Xs were drawn BEFORE the tank mesh pass (with depth
  test disabled, inheriting state from the grid/axes background
  pass).  Result: markers always-on-top regardless of geometry.
  Moved to AFTER the tank-mesh + wireframe + normals + picker
  passes (line ~9897), with explicit `glEnable(GL_DEPTH_TEST)`
  before the draw -- markers now correctly occlude behind the
  hull / chassis.  Per-bone console-dump kept at its old location
  (CPU print, no GL state).
* **Wireframe overlay polygon offset re-asserted in-place.**  The
  pre-existing `glEnable(GL_POLYGON_OFFSET_FILL); glPolygonOffset(4,
  4)` block ran way at the top of `render()` -- before the
  terrain, picker FBO, hull-bbox, etc. passes.  Any of those could
  legitimately call `glDisable` / `glPolygonOffset(0, 0)` for their
  own state, leaving the offset stale by the time the solid mesh
  pass actually fired -> wireframe lines z-fought the surfaces
  again.  Fix: removed the early enable; new in-place enable +
  offset is set ONE STATEMENT before the `for mesh in self.meshes:`
  loop.  Bumped (4, 4) -> (8, 8) for additional margin at far-from-
  camera distances and grazing angles.  Explicit `else: glDisable`
  so wireframe-off frames don't inherit a stale enable.

#### Track render

* **Rubber-band track ribbon (`track_LShape*` / `track_RShape*`
  inside `Chassis.primitives_processed`) blanked off.**  Skipped
  in the solid + wireframe + normals + picker passes via the
  `RENDER_RUBBER_BAND_TRACK = False` gate at `viewer.py:9793`.
  Set to `True` to bring the rubber band back for an A/B against
  the upcoming kinematic-bone-driven NURB pads.  Per-tank bone-
  palette layout dump (T30 baseline) added to `docs/PHYSICS.md`
  so future readers know the W_ road-wheel bones live in
  `chassis_*Shape21_split_0`, NOT in the track meshes.

#### Docs split

The single 700-line `ARCHITECTURE.md` got split into per-domain
files under `docs/`:

* `docs/INDEX.md` -- topic → doc map; entry point.
* `docs/PHYSICS.md` -- `tank_physics.py`: contact classification,
  plane fit, drive controls, bone matrix passes, residual sign
  convention, settling math, T30 bone-palette table.
* `docs/TRACK_PHYSICS.md` -- the in-progress NURB track roadmap
  (Phase A → E + open probes); was at the bottom of `ARCHITECTURE.md`.
* `docs/RENDERING.md` -- viewer.py render pass order, four mesh-
  iteration sites + their gates, camera modes, look-at crosshair,
  wireframe polygon-offset recipe.

`cust_tools/arch.py` is the entry-point search/list/stale tool:

```
python cust_tools/arch.py search "<topic>"   # phrase + token-AND fallback
python cust_tools/arch.py list                # every doc heading
python cust_tools/arch.py stale               # docs older than the .py they describe
```

`CLAUDE.md` updated with the new "use arch.py first" rule -- before
grepping source, search the docs to find which one covers the
topic.

### Track-physics roadmap committed (1.99.0)

Today opened the rewrite of the track render system.  The current
"rubber-band" track -- a single welded ribbon mesh
(`track_LShape12` / `track_RShape12` inside
`Chassis.primitives_processed`) UV-scrolled to fake motion --
gets replaced with a **kinematic-bone-driven NURB** with arc-
length-uniform pad resampling.

This commit lands no engine code changes for tracks yet -- the
deliverable is the design and the proof.  See the new "Track
physics roadmap" section at the bottom of `ARCHITECTURE.md` for
the full plan, phased Phase A (physics + spline) → Phase E
(export / import).  README's new "Current work" note links
there.

The probe scripts that proved the pipeline live at the
experiment-tree root and are not shipped:

* `_plot_t30_track.py`  — overlay of T30 left-track NURB V_loc
  control points, road wheels W_L*, and the `Track_L*` /
  `Track_VT_L*` / `Track_VD_L*` bone bend points in YZ side
  view.  Confirmed Track_L bones sit at Y=0 directly under each
  wheel (ground-contact anchors), Track_VT at top-run sag
  points, and Track_VD wraps the drive sprocket / idler.
* `_plot_t30_segs.py`  — same overlay plus
  `segmentLength × segmentsCount/2` = 117 pad markers placed at
  uniform arc-length along the closed V_loc polyline, plus the
  drive sprocket (`WD_L0`, r=0.339 m from `groupRadius_road`)
  and idler (`WD_L<MAX>`, r=0.324 m) circles at their true
  chassis-bone positions.  Confirmed the spline ends wrap
  exactly around the drive sprocket and idler bones, the pads
  sit cleanly on the spline path, and the chord polyline
  length 15.062 m matches the 15.561 m target
  (117 × 0.133 m) within ~3 %.
* `_plot_t30_smooth.py`  — closed centripetal Catmull-Rom
  (α=0.5) through the 17 V_locs + cumulative arc-length
  resample at uniform arc steps.  Solves the chord-vs-target
  gap.

Spline-fit numbers on T30 (centripetal CR vs alternatives):

| Curve | Length | Δ vs 117 × 0.133 |
|-------|-------:|-----------------:|
| Chord polyline                              | 15.062 m | -0.499 m (-3.21 %) |
| Uniform CR (α = 0)                          | 16.558 m | +0.997 m (+6.40 %) |
| **Centripetal CR (α = 0.5)**                | **15.126 m** | **-0.435 m (-2.79 %)** |
| Target = `segmentsCount × segmentLength / 2` | 15.561 m | — |

Uniform CR overshoots the sprocket / idler bends and produces
self-intersecting loops; centripetal CR (α=0.5) passes through
every V_loc with no overshoot.  Resampled pad spacing on T30:
mean 0.1292 m, **std 0.3 mm** across all 117 pads -- machine-
precision uniform once the cumulative-arc-length table is in.

The 2.79 % shortfall is the **track slack budget**: pad rest-
length sum naturally exceeds the rigid spline path, exactly
the deformation reserve the in-game runtime burns to let the
track sag on the top run / hug curves on the bottom.  No
spring physics needed for visuals; springiness emerges from
CR interpolation across bone-driven V_locs.

Why kinematic-bone-driven and not spring-mass / per-pad rigid
chain: every chassis already ships the bone scaffold the
runtime deforms with -- `Track_L<i>` under each road wheel for
ground contact, `Track_VT_L<i>` for top-run sag,
`Track_VD_L<i>` for sprocket / idler wraparound.  Bind each
V_loc 1:1 to its bone, let physics drive the bones (we already
settle wheel Y under gravity in `tank_physics.py`), let
centripetal CR smooth-interpolate the V_locs each frame.
Deterministic, replay-stable, ~17 V_loc updates / side per
frame instead of 117 RBs + 117 hinges + a solver.

DX → GL frame conversion at load time (mirroring the existing
"Coordinate space" rule):

```python
gl_pos     = (dx_pos[0], -dx_pos[1], -dx_pos[2])
gl_tangent = (dx_t[0],   -dx_t[1],   -dx_t[2])
# Z-flip reverses traversal direction along the closed loop, so
# also reverse the V_loc array order so pad index 0 still walks
# rear → top → front → bottom → rear in GL.
```

Files: `ARCHITECTURE.md` (new "Track physics roadmap" section
at end, ~190 lines), `README.md` (new "Current work" note near
the top, links to the roadmap).

### Per-tank chassis params + total weight from gameplay XML (1.98.0)

* **`VehicleXMLLoader.parse_info` now extracts** the
  selected-chassis-specific physics inputs:
  * `groupRadius_road` -- main road wheel radius from
    `<wheelGroups>` (the wheelGroup whose names match
    `^W_[LR]\d+$`).  T30 = 0.345 m; T110E4 = 0.330 m.
  * `minOffset` / `maxOffset` -- suspension envelope from
    the first `<groundNodes><group>` block.  T30 =
    (-0.06, +0.06); T110E4 = (-0.04, +0.08).
  * `renderModelOffset` -- track thickness, prefers
    `<trackThickness>` over the legacy nested
    `<renderModelOffset>` path.  T30 = -0.029; T110E4 =
    +0.016.
  All three blocks are nested DEEP inside the chassis
  hierarchy (`<tracks><trackPair><trackDebris>
  <physicalParams>`), so the parse uses descendant-search
  (`.//tag`) instead of direct-child lookup.
* **Total tank weight** = sum of every component's
  `<weight>` (hull + chassis + turret + gun + engine +
  radio + fueltank).  Surfaced as `info['total_weight_kg']`
  and logged in the in-app console at load.  T30 = 61.0 t;
  T110E4 ~ 65 t.
* **TankPhysics now consumes the per-tank values** at
  load.  Previously every tank used the T110E4 defaults
  (radius 0.33, envelope -0.04 / +0.08, track thickness
  0.016); now each tank gets its real numbers from the
  XML.  Falls through to the defaults when the field is
  missing (rare; some modded tanks drop the blocks).
* **Vehicle XML parsed ONCE per load**.  Was being parsed
  twice (early for nothing + late for the info panel);
  now early-parsed and the result reused.

### Startup window size: 1024 x 576 (banner aspect, math fixed) (1.97.7)

* Recomputed: banner is 1672 x 941 (aspect ~1.778).
  `1024 * 941 / 1672` rounds to 576.  Banner now fits the
  startup window pixel-for-pixel.

### Startup window size: 1024 x 433 (fits banner aspect) (1.97.6)

* Tightened startup window to 1024 x 433 to fit the splash
  banner's wider-than-tall aspect.  Was 1024 x 768; banner
  had visible empty padding above / below.

### Startup window size: 1024 x 768 (was splash image dims) (1.97.5)

* Initial window opens at 1024 x 768.  Was sized to the splash
  image dims (1672 x 941) which felt big at launch.  Splash
  image still fills the client area as a textured quad --
  it auto-scales.  Maximise / fullscreen state remembers the
  pre-maximise dims for F11 SW_RESTORE.

### F11: explicit client-rect poll forces immediate render reshape (1.97.4)

* After `_toggle_fullscreen` calls `SW_MAXIMIZE` / `SW_RESTORE`,
  we now explicitly call `GetClientRect` to read the new content-
  area dims and call `_on_resize` directly.  Belt-and-braces for
  the case where the WM_SIZE -> SDL_WINDOWEVENT -> pygame
  VIDEORESIZE chain hadn't pumped yet on the same frame.
* The OS chrome (title bar / resize border) handles its own
  drawing -- we just pass the maximize / restore command along
  via `ShowWindow` and the OS does the rest.  GL viewport fills
  the client rect (chrome NOT included, which is correct).

### Borderless toggle: simpler 2-bit mask (chrome restore now sticks) (1.97.3)

* The aggressive "WS_OVERLAPPEDWINDOW <-> WS_POPUP swap" in the
  borderless toggle was conflicting with SDL2's own window-state
  bookkeeping on the restore path.  Splash chrome flipped off
  fine, but the OFF call didn't visibly bring the chrome back
  even though SetWindowLong returned success.
* Replaced with a simple two-bit mask: only toggle WS_CAPTION
  (title bar) and WS_THICKFRAME (resize border).  Leave
  WS_SYSMENU / WS_MINIMIZEBOX / WS_MAXIMIZEBOX / WS_VISIBLE /
  WS_POPUP alone -- SDL set them up at window creation and
  doesn't expect us to mess with them.

### Look-at crosshair: drawn AFTER tank meshes (no more occlusion) (1.97.2)

* Bug: the depth-test-off look-at crosshair was being drawn
  BEFORE the tank mesh pass, so the tank rendered ON TOP of
  the crosshair pixels.  Result: the crosshair was hidden
  behind the hull even with depth test disabled (the disable
  applied to the crosshair draw, but the later tank draw
  overwrote those pixels with depth-test-on geometry).
* Fix: move the crosshair draw to AFTER every tank-mesh /
  wireframe / picker pass, just before the UI render.  Now
  the crosshair composites on top of the rendered tank with
  depth test off, staying visible whenever the wheel button
  is held.

### Splash teardown: restore window chrome at end of preload, not at run() (1.97.1)

* Bug: title bar / resize frame / min-max-close buttons were
  not visible after the splash teardown on Windows.  Restoring
  the chrome in `run()` (immediately before `SW_MAXIMIZE`) raced
  the WM message queue -- the WM_NCCALCSIZE + WM_NCPAINT
  triggered by the style flip hadn't drained before the
  maximise asked the OS to fill the screen, so the maximise
  took effect on a popup-style window and the chrome was lost.
* Fix: do the chrome restore at the END of `__init__`, after
  every preload step (PkgExtractor, IBL, ItemList rebuild,
  etc.) is done.  Splash stays visible while __init__ runs;
  the chrome flip processes through the message loop with
  a `pygame.event.pump()` while the splash is still on
  screen, and by the time `run()` does splash cleanup +
  `_go_maximized()`, the window already has its chrome
  intact and the maximise keeps it.

### Skinning: drive sprocket + idler partial-follow nearest road wheel (1.97.0)

* New pass 3 in `bone_matrix_array`: scan the palette for
  `WD_<side>\d+` bones, identify the lowest-numeric-index
  per side (drive sprocket, rear) and the highest (idler,
  front).  Drive sprocket inherits 50 % of the rearmost
  road wheel's residual; idler inherits 50 % of the
  frontmost road wheel's residual.
* Fixes the visible tear / kink in the track ribbon's
  wraparound section near the front tensioner on tanks
  like A14_T30 (16 wheels, very tight Z-spacing between
  the front-most road wheel and the idler).  The 50 %
  weight is enough to blend the seam without making the
  idler visibly bob with every wheel deflection.
* The track-ribbon mesh's existing per-vertex `iii / ww`
  blend between adjacent bones now smoothly interpolates
  through the WD_ partial-follow region, since the GPU
  skinning shader does the weighted-bone-matrix sum
  automatically.

### Look-at: Shift requires middle-mouse + crosshair always visible (1.96.4)

* **Look-at crosshair (pink XYZ lines) draws with depth-test
  disabled.**  Stays visible even when buried inside geometry
  or behind terrain.  GL state is restored immediately after
  so the suspension overlay (next pass) behaves correctly.
* **Shift + middle-mouse for Y-lift, not Shift alone.**  Was:
  any Shift-held mouse drag would lift / drop the look-at on
  Y -- accidental Shift during keyboard typing yanked the
  camera upward whenever the mouse happened to move.  Now:
  shift is a modifier on the middle-button drag.  Middle
  alone = XZ pan (unchanged); Shift + middle = Y lift.
* Crosshair visibility tied to the middle button only --
  Shift alone no longer flashes the lines.

### Physics-timer overlay: moved out of the left info panel (1.96.3)

* The grass-green `physics: X.XX ms` readout was at window-x=10
  which fell INSIDE the left info / tree panel (280 px wide).
  Moved to `x = INFO_PANEL_W + 10` so it lands in the actual
  3D scene area, where the user can see it without the info
  panel covering it on a typical tank load.

### Bug: top speed was 3.6x too high (units were already kph) (1.96.2)

* `VehicleXMLLoader.parse_info` was multiplying
  `<speedLimits><forward>` by 3.6 on the assumption the value
  was in m/s.  It's actually in **kph** -- verified against
  A14_T30 (`<forward>35</forward>` → in-game top 35 kph,
  not 126).  T30 was reporting 126 kph (35 * 3.6); now
  reports 35 kph.
* Removed the conversion -- pass-through value as kph.
  Affects `_top_speed_kph` everywhere downstream: speed-step
  selector, auto-circle, cruise, ramped accel/decel, all
  scale to the corrected value automatically.

### Splash teardown: ensure window comes back fully visible (1.96.1)

* Added `SWP_SHOWWINDOW` flag to the `SetWindowPos` call that
  restores the window chrome after splash, plus an explicit
  `ShowWindow(SW_SHOW)` afterward.  Some drivers leave the
  window in "style changed but not redrawn" state after a
  bare `SetWindowLong`, which manifested as a partially-
  invisible / chromed window after the splash teardown.
  Belt-and-braces both signals so the window comes back
  fully visible regardless of driver behaviour.

### Tank-math timer overlay (1.96.0)

* New grass-green readout in the upper-left of the main
  window: `physics: X.XX ms`.  Times the full tank-math step
  (`tank_physics.update()` + per-mesh model-matrix recompose)
  with `time.perf_counter`, smoothed via 5-frame exponential
  trail (alpha=0.20) so the value doesn't flicker.
* Drawn AFTER the UI overlay so it sits on top of any panels
  / tree.  Cached text texture rebuilds only when the
  formatted string changes (every 0.01 ms step), so per-frame
  cost is one quad draw + a dict lookup.
* Visible only when Susp is engaged (no tank physics = no
  number to show).

### Splash: window client area sized to splash image (1.95.5)

* Peek the splash image dimensions before `set_mode` so the
  initial window client area matches the image exactly.
  Splash fills the window with no letterbox / padding -- the
  borderless splash window now reads as a clean freestanding
  image (1672 x 941 for the current `tepy_banner.png`).
* After splash teardown the maximise path takes the window
  to full-screen as before, so the splash-fit sizing only
  governs the startup view.

### Drive: X is a backup-key alias for Z (1.95.4)

* X now drives backward, same as Z.  Same finger-position
  argument as W / S sharing forward duty -- different
  reach, same effect.

### Splash: window chrome hidden while splash is showing (1.95.3)

* Title bar / resize frame / min-max-close buttons are
  HIDDEN during the startup splash and restored when the
  real UI takes over.  Splash now reads as a clean
  freestanding image instead of a frosted-window image.
* Implementation: Win32 `SetWindowLong(GWL_STYLE, WS_POPUP)`
  + `SetWindowPos(SWP_FRAMECHANGED)` -- mutates the window-
  style bitmask in place so the GL context survives (the
  pygame `NOFRAME` flag would have required a `set_mode`
  re-create, which destroys textures + VBOs).  Restored to
  `WS_OVERLAPPEDWINDOW` when `run()` calls
  `_set_window_borderless(False)` right before the maximise.
* No-op on non-Windows.

### Drive: S is hold-to-go (no toggle) (1.95.2)

* S used to be a cruise toggle (press once to start, again to
  stop).  Now S is momentary like W -- hold to drive forward,
  release to stop.  W and S do the same thing; pick whichever
  finger position feels right.
* Removed `_auto_forward` state and the cruise / auto-circle
  mutex.

### Drive: default speed step is now 8 (~5 kph) (1.95.1)

* `_speed_step` initial value changed from 0 to 8.  Tank moves
  at a slow-readable pace the moment the user hits W on a fresh
  session, instead of feeling inert until they tap a number key.
  Step 8 maps to ~5 kph at the 50 kph default cap; rescales to
  whatever the per-tank XML max says when a tank loads.

### Drive: accel + decel ramp on forward / backward (1.95.0)

* Tank no longer snaps to full step speed on W press or stops
  dead on release.  Instead ramps `_current_forward` toward
  the target speed:
  * **`DRIVE_ACCEL = 5.0 m/s^2`** -- spool-up (tank accelerates
    to step-1 max in ~2 seconds at default values).
  * **`DRIVE_DECEL = 10.0 m/s^2`** -- braking + direction
    reversal.  2x accel so slowing / reversing feels
    responsive, matching real tank handling where the
    transmission can brake harder than the engine pushes.
* Auto-circle now uses the same ramped `_current_forward` --
  the circle starts tight and spirals out as the tank spools
  up, then settles to the target radius.
* Yaw rate (A / D / Q / E) stays instant -- tanks pivot
  briskly and the ramp on yaw felt sluggish in testing.
* Step changes (0-9) work mid-drive: pressing 5 while
  cruising at step 3 ramps DOWN smoothly to the new target
  speed via the decel rate.

### Debug checkbox: gates were reading the previous frame's value (1.94.5)

* **Bug**: turning the Debug checkbox ON didn't bring up the
  per-wheel terrain markers OR the wheel red/green highlight.
* **Cause**: the `_debug_cb -> self._debug` mirror was running
  near the END of `render()`, after every gate that consumed
  `self._debug` had already evaluated.  Result: gates checked
  the PREVIOUS frame's `_debug` value -- toggled on the same
  frame the checkbox flipped, the markers were one frame
  behind; in the worst case (single-frame state changes from
  other code paths) they never lit up at all.
* **Fix**: moved the mirror to the TOP of `render()` so every
  downstream gate reads the current-frame state.  Susp checkbox
  -> `tank_physics_enabled` mirror moved alongside for the
  same reason.

* **Drive-controls log on Susp toggle** updated to match the
  new WASD layout (was still printing "arrow keys").

### Particles: per-frame chassis-pose tracker no longer breaks spawn timing (1.94.4)

* **Bug**: one of two engine-exhaust smoke emitters appeared
  dead while the other spawned normally; fire never animated
  coherently when the chassis-pose tracker was active.
* **Cause**: `Viewer._update_emitters_for_chassis_pose` called
  `set_emitters(...)` every frame to refresh world positions.
  `ParticleSystem.set_emitters` resets `_spawn_accum = 0.0` --
  intentional on tank load, catastrophic per-frame at typical
  spawn rates (15-30 emitters/sec, 2 HPs, 60 fps): the per-
  frame contribution to `_spawn_accum` is < 1.0, so `floor()`
  is always 0, so nothing spawns until a frame happens to
  cross 1.0 alone.  Worse, the random per-frame jitter made
  one emitter "win" the race and the other "lose".  Same
  shape on the fire side: `AnimatedBillboard.set_emitters`
  re-rolls all per-layer RNG phases on every call, so a per-
  frame call meant the flipbook never advanced coherently.
* **Fix**: new `update_emitter_positions(emitter_list)` on both
  `ParticleSystem` and `AnimatedBillboard`.  Refreshes pos /
  fwd in place without touching `_spawn_accum` or RNG state.
  Falls back to `set_emitters` if the emitter count changed
  (tank reload).  `_update_emitters_for_chassis_pose` now
  uses the new method.
* Result: both exhaust emitters spawn smoothly and stay glued
  to the HP positions as the tank drives, fire animation
  advances frame-by-frame as designed.

### Drive: A/D now turn the chassis (no strafe) (1.94.3)

* A turns the chassis left (CCW), D turns right (CW) -- standard
  tank-tiller behaviour.  Was strafe in v1.94.0; tanks don't
  strafe.  Q / E remain bound as legacy yaw aliases.
* Hold W + A to drive in a curving left arc, just like a real
  tank.

### Drive: backup key swapped X -> Z (1.94.2)

* Backup key moved from X to Z.  Z sits directly under A on QWERTY
  (home-row reach without moving the hand), and is the more common
  "back when S is taken" binding in older PC games.

### Sticky camera + speed; hull bbox gated by Debug (1.94.1)

* **Hull bounding box** -- now gated by the Debug checkbox (was
  always-on with Susp).  Off when Debug is off.
* **Speed step persists across tank loads.**  Was being reset to 0
  on every load_vehicle; now sticks.  Speed magnitude rescales
  automatically to the new tank's `_top_speed_kph`.
* **Camera state persists across tank loads.**  `fit_to_bounds`
  fires only once per session (the first load); subsequent loads
  keep the camera's yaw / pitch / distance, AND the camera_mode
  (chase / commander / orbit) sticks too.  R key still does an
  explicit reset + re-fit.
* **Debug-toggle no longer affects chase / commander cameras.**
  The cam paths in `_anchored_view_matrix` are independent of
  `_debug`; what was making them feel "disabled" was the
  always-on hull bbox cluttering the chase view + the markers
  appearing/disappearing as Debug toggled.  Both are now
  consistently Debug-gated, the camera mode itself is stable.

### Drive controls: WASDX standard handles + S = cruise + F2 = wireframe (1.94.0)

* **Wireframe key moved from `W` to `F2`.**  W is now reserved
  for forward drive in the new tank-handles layout.  F1 is
  reserved for a future help overlay.
* **Tank-handles layout (WASD-style)**:
  * `W` -- forward drive
  * `X` -- backward drive
  * `A` -- strafe left
  * `D` -- strafe right
  * `Q / E` -- yaw left / right (unchanged)
  * `S` -- cruise toggle.  Tank rolls forward at the current
    speed step (1-9) without holding `W`.  Press `S` again to
    stop.  Mutually exclusive with `O` (auto-circle).
* **Arrow keys removed from drive controls.**  They were the
  legacy bindings; replaced by WASD/X.
* **Help / docs** updated in README_TANK_VIEWER.md to match.

### Revert: mass-inertia angular integrator removed (1.93.2)

* **Backed out the v1.93.0 mass-inertia layer.**  Tank was making
  wild jumps -- the second-order integrator interacted badly with
  the iterative-refinement loop's repeated `chassis_matrix()`
  calls (each iteration uses `self.pitch_deg/roll_deg`, but
  those were the LAGGED values during integration, not the
  target ones).  Refinement saw stale poses, classified wheels
  wrong, plane fit jumped to wild new targets, integrator
  chased them -- positive feedback loop.
* Behaviour reverts to the pre-1.93.0 snap-to-plane chassis pose.
  Per-wheel residual damping (asymmetric `smoothed_residual_y`)
  stays.
* Mass-inertia is the right idea but needs to be done OUTSIDE
  the iterative refinement loop -- compute the converged target
  once, then ramp the chassis toward it across frames.  Saved
  for a future attempt.

### Debug checkbox now gates wheel highlight + terrain markers (1.93.1)

* **Wheel red/green/over-comp highlight** -- gated by Debug.
  When Debug is unchecked, the shader's `u_contact_mode` stays
  at 0 and the wheels render with normal PBR.  H key still
  toggles `_highlight_contacts` independently in case you want
  the highlight off even when Debug is on.

* **Per-wheel terrain markers (pink stars, cyan, yellow,
  blue X)** -- gated by Debug AND Susp.  The chassis still
  responds to terrain via the per-wheel suspension solve
  regardless of Debug state; we just don't draw the markers
  when Debug is off.

### Mass-inertia: angular acceleration on chassis pitch / roll (1.93.0)

* **No more instant-snap pitch / roll.**  The chassis pose now
  evolves under a critically-damped second-order system:
  ```
  alpha = -K * (current - target) - D * omega
  omega += alpha * dt
  current += omega * dt
  ```
  with the plane-fit value as the target.  Result: realistic
  angular acceleration -- the chassis ramps toward the target
  pitch / roll instead of jumping.
* **Hard caps**:
  * MAX_OMEGA = 90 deg/sec (chassis can't rotate faster than
    a real tank's pitching limit even if the contact set
    changes wildly between frames).
  * MAX_ALPHA = 720 deg/sec^2 (acceleration cap).
* **Tuning**: omega_n = 4 rad/s natural frequency settles a
  step input in ~250 ms.  Critical damping = no overshoot.
  Constants live on `TankPhysics._ANG_*` class attrs.
* **Verified**: 20 cm step under one wheel produces a smooth
  ramp from 0 deg to target with peak omega around 4 deg/sec
  (well under the 90 cap), no oscillation, monotonic decay
  toward steady state.

### Auto circle drive (`O` toggles) (1.92.0)

* **`O` toggles auto-circle drive.**  When ON, the tank ignores
  arrow / Q / E keys and instead pulls a steady circular arc:
  * Radius `_auto_circle_radius` = 25 m (tunable in code).
  * Linear speed comes from the current speed step (1-9).
  * Yaw rate `omega = v / R` -- exactly the value that keeps
    the chassis facing the tangent at every point on the circle
    (kinematic, no slip).
  * Sign convention matches arrow keys' forward direction so
    the tank pulls a smooth left turn at the right linear
    speed regardless of how the user changes step.
* Manual keys re-engage when `O` is toggled off.  Combine with
  the chase / commander cameras to watch the chassis tilt
  cleanly as the tank pulls a steady arc on the heightmap.

### Cam 2 (chase): free camera with locked look-at on driver (1.91.4)

* **Mode 1 chase camera reworked**: camera position is now the
  ORBIT camera (mouse-driven, user can spin / pan / zoom freely),
  but the look-at point is LOCKED to a driver-position in
  chassis-local space `(+0.7, +0.6, -2.0)` and transformed by
  `chassis_pose` each frame.  Result: you can fly the camera
  anywhere, but it always points at the driver side of the tank
  even as the tank turns / pitches / rolls.

### Smoke emitters: snapshot bind_pos at load (no transform race) (1.91.3)

* **Explicit `bind_pos` / `bind_fwd` snapshot at the end of
  `load_vehicle`** for every exhaust + fire hardpoint.  The
  v1.83.2 lazy snapshot inside `_update_emitters_for_chassis_pose`
  worked in the common case but had a subtle race: any code path
  that touched `hp['pos']` between load and the first physics tick
  would corrupt the snapshot.  Doing it explicitly at load time
  makes `bind_pos` authoritative regardless of what runs in
  between.

* **Console dump of every exhaust HP at load** so we can spot a
  rogue hardpoint at a glance.  Prints name, component, bind
  pos, bind fwd -- if one HP shows a position wildly off from
  the others, that's likely the smoke-framing-bug culprit.

### Camera 2 (chase): rotated 90 deg in Y to side-view (1.91.2)

* **Chase camera moved from behind to right-side**: eye now
  at chassis-local `(+6.0, +2.6, -1.4)` looking at
  `(-3, +0.5, 0)`.  Same magnitude, just rotated 90 deg about
  the chassis Y axis -- camera sits off the tank's right side
  with a 3/4 view of the hull profile.

### Drive speed: hard cap at per-tank XML max + metre conversion (1.91.1)

* **`_kph_for_step` clamps to `_top_speed_kph`** as a hard cap.
  No number-key step can produce a speed above the per-tank
  `<speedLimits><forward>` value from the gameplay XML.  Step 1
  hits the cap exactly; steps 2-9 interpolate downward.

* **Conversion switched from yards/sec to metres/sec.**  The
  scene units are metres (chassis primitives + gameplay XML
  both use metres), so the previous yard-conversion was off
  by ~9% (tank moved faster than the displayed kph would
  suggest).  Now `kph * (1000 / 3600)` straight to units/sec.

* **In-app log adds the cap value** so the user can see at a
  glance what each step is bounded by:
  `speed: step 1/9  =  35.00 kph  (9.722 m/s)  [cap 35.0 kph]`.

* **Backwards-compat alias** `_speed_yards_per_sec` is kept
  pointing at the new metre-based calc; older comment lines
  that still reference yds/s aren't lying about the math, just
  about the unit name.

### Three-cam mode (orbit / chase / commander) + `C` toggle (1.91.0)

* **`C` cycles cameras**:
  * **Mode 0 -- orbit** (default): existing trackball / mouse
    camera.  Free-fly around the whole scene.
  * **Mode 1 -- chase (driver side)**: camera anchored to
    chassis-local `(+1.4, +2.6, +6.0)` looking at
    `(0, +0.5, -3)`.  Sits behind + above + on the driver-side
    of the tank, follows pitch / roll / yaw via
    `chassis_matrix()` so the view rolls with the tank.
  * **Mode 2 -- commander (turret POV)**: camera anchored to
    chassis-local `(0, +1.95, 0)` looking forward along
    chassis-local `-Z`.  Hides turret + gun meshes (in main +
    wireframe + normals passes) so the user gets a clear
    view-out from inside the turret.

* **Camera follows tank tilt + heading.**  Modes 1 + 2
  multiply chassis-local eye / target / up vectors through
  `chassis_matrix()` (translation + yaw + pitch + roll), so
  the anchored cameras always sit in the same chassis-relative
  spot regardless of how the tank is oriented.

* **Mode label printed to in-app console** on each `C` press
  (`camera: chase (driver side)` etc.).

### Force-balance pos_y wired into runtime physics (1.90.0)

* **Force-balance Newton iteration** (proven in
  `cust_tools/test_force_balance.py`) now drives the chassis
  `pos_y` calculation in `update()`.  Replaces the geometric
  `mean(target) - mean(local_y)` snap with a vertical
  equilibrium solve: chassis weight pushes wheels into the
  ground until summed spring forces match.  Each wheel acts
  as a unit-stiffness spring; bottomed-out wheels deliver
  their max force; the terrain floor + iterative refinement
  catch any residual constraint violations downstream.

* **Lstsq plane fit kept for angles.**  Pitch / roll come
  from the same chassis-local-frame plane fit as before.
  The force-weighted-fit experiment (also in the test file)
  produced over-aggressive tilts and weight collapse on
  edge cases; lstsq is the proven choice for now.  The
  weighted variant stays available in `test_force_balance.py`
  for future iteration.

* **Visible result**: a flat-ground tank now sits ~2 cm
  LOWER than before -- each wheel compresses to its rest
  load.  Slope angles unchanged (still -1.146 deg on a 2%
  slope at any yaw).

### Live console + hull bbox overlay (1.89.0)

* **Live per-bone console dump.**  When tank-physics is active,
  every render frame clears the terminal (ANSI cursor-home +
  clear-screen) and reprints the chassis pose + every wheel's
  state.  Slows the render loop noticeably -- one cls + one
  print per wheel per frame -- but the user explicitly asked
  for non-stop updates and accepted the cost.  Output:
  ```
  TEPY tank-physics live state
  ================================
  pos     : (+0.000, -0.040, +0.000) m
  yaw     : +0.000 deg
  pitch   : +0.000 deg
  roll    : +0.000 deg
  vy      : +0.000 m/s

   #  bone                     state    resid_y  susp_arm_deg  target_y  terrain
   0  W_L0_BlendBone           CONTACT  -0.0000        -0.00   +0.330    +0.000
   1  W_L1_BlendBone           CONTACT  +0.0012        +0.21   +0.330    +0.000
   ...
  ```
  Per-wheel data: bone name, state (CONTACT/HANGING/OVER/NONE),
  residual Y in metres, approximate suspension-arm rotation in
  degrees (residual / arm_length), target wheel-centre Y, and
  terrain Y.

* **Hull bounding-box overlay.**  Builds an orange wireframe
  AABB around every `component=='hull'` mesh on tank load
  (cached as `_hull_box_local`); transformed by `chassis_pose`
  per frame so the box rides with the tank as it pitches /
  rolls / yaws.  Useful for visual sizing + future collision
  / fit-check work.  Bbox is in CHASSIS-LOCAL space so the
  pivot-rotation behaviour matches the rendered geometry.

### Suspension overlay: blue X at per-wheel hit location (1.88.4)

* **Blue X marker** drawn at each wheel's physics-computed hit
  location: `(last_wheel_world[i].xz, last_terrain_y[i])`.
  Same position as the pink star but rendered as a diagonal
  cross 1 mm above the ground plane so it reads as a distinct
  marker for visual diagnosis -- sits on top of the pink star
  and lets you confirm at a glance that the physics's computed
  hit location matches what's directly under the rendered
  wheel.

### Physics: full-pose wheel XZ + track thickness back to 16 mm (1.88.3)

* **Wheel terrain-sampling XZ now uses full chassis pose**
  (translation + yaw + pitch + roll), not yaw-only.  Pitch /
  roll shift each wheel's world XZ by a few cm on tilted ground
  -- enough to put the terrain sample meaningfully off the
  rendered wheel.  Visible as the pink-star contact markers
  drifting away from the actual wheel positions on uneven
  terrain (bug Professor Coffee flagged).  All three downstream
  consumers (terrain sampling, classifier rigid_y, plane-fit
  XZ) now agree on the same per-wheel world position.

* **Track thickness reverted to 16 mm** (the literal
  `<renderModelOffset>` from the gameplay XML).  60 mm was
  overcompensating -- on flat ground it lifted the chassis
  6 cm too high so red CONTACT wheels visibly hovered above
  the sand.  16 mm matches the in-game value and lands the
  track outer face on the terrain without lifting the wheel
  hubs off where they should be.

### Physics: track thickness bumped to 60 mm for visible ground gap (1.88.2)

* **Default `track_thickness` raised from 30 mm to 60 mm.**  The
  literal value from T110E4's gameplay XML
  (`<tracks><trackPair><trackDebris><physicalParams>
  <renderModelOffset>0.016214</renderModelOffset>`) is 1.6 cm,
  which reads as essentially zero at TEPY's render scale.  60 mm
  gives a visible-yet-realistic gap between the track outer face
  and the wheel hub.

* The track-thickness field IS in the per-tank def file (the user
  reminder); per-tank parse can override the default later when
  we hook the chassis-XML walker.  For now the empirical 60 mm
  default does the job for any tank loaded.

### Physics: track thickness lifts wheels above terrain (1.88.1)

* **`track_thickness` (default 30 mm)** added to TankPhysics ctor.
  Bug: the wheel-CENTRE target was just `terrain_y + radius`,
  putting the wheel BOTTOM exactly at terrain level.  The track
  ribbon is BELOW the wheel bottom (wheel rolls on the inner
  surface of the track, outer surface contacts ground), so with
  the old target the rendered track sank into the sand by its
  full thickness.  Now: `target = terrain_y + radius +
  track_thickness`, lifting the wheel hub by the track height
  so the OUTER face of the track is what rests on the terrain.
  3 cm is a typical modern-tank value; per-tank tuning would
  come from `<track>` gameplay XML data (not yet parsed).

* **Verified**: flat-ground settled chassis now sits at
  pos_y = -0.04 m (was -0.07 m).  Visible difference: track
  texture no longer pokes into the sand.

### Physics: priority + emergency-fit refinement (1.88.0)

* **Iterative refinement after the snap pose** -- up to 3
  passes per frame.  Each pass: re-classify wheels with the
  current pose (hysteresis-aware), and if the contact set
  shrunk to a different shape AND >= 3 wheels remain in
  contact, refit the plane through the new set.  This is the
  PBD-flavoured "process most-stressed constraints first"
  loop the user asked for: the most-compressed wheel sets the
  floor (already implicit in the `max()` of `_apply_terrain_
  floor`), then refit picks up the post-floor contact set
  cleanly so pitch / roll match the post-lift truth.

* **Emergency plane-fit when stuck in gravity branch.**  Was:
  if `n_contact < 3`, gravity-drop the chassis.  Problem: a
  tank that was tilted on a spike and then drove off ended up
  with the classifier marking most wheels HANGING (because
  the stale tilt put their `rigid_y` outside the envelope
  with hysteresis preventing recovery), leaving the chassis
  stuck above the ground.  Now: when classified `n_contact <
  3`, run a TENTATIVE plane fit through ALL wheels (no
  hysteresis), then count how many wheels would be inside
  their envelope under that pose.  If `>= 3`, commit the
  emergency fit -- the system was stuck.  If `< 3`, restore
  the previous pose and do gravity-fall (genuine free-fall).

* **No more pitch / roll decay during gravity branch.**  An
  earlier attempt scaled `pitch *= 0.95` per frame to
  unwind stale tilts, but it eroded LEGITIMATE tilt while a
  tank was still on a spike (the spike-on test ended up
  flat-and-floating after ~80 frames).  Replaced with the
  emergency-fit-and-test approach above, which preserves
  legitimate tilt indefinitely while still recovering from
  stale tilts in one frame.

* **Verified**: 30 cm spike under one wheel produces a
  stable -4.29 deg pitch + roll tilt that holds for 20+
  frames with no drift.  Spike removal returns the chassis
  to flat (-0.07 m, 0 deg) in one frame.

### Physics: hard terrain floor (no wheel below the surface) (1.87.0)

* **New constraint** `_apply_terrain_floor`: enforces that no
  wheel CENTRE can sit more than `comp_cap` (= +0.04 m default)
  below its terrain target.  In other words: a wheel can compress
  up to its full suspension travel into the hull, but the wheel
  bottom can never punch through the ground.

* **Wired into all three update paths**: the gravity-drop branch
  (n_contact < 3), the FALL_THRESHOLD branch (chassis chasing a
  fast-falling target), and the snap branch (post-plane-fit).
  Each one sets pos[1], then calls `_apply_terrain_floor` to
  raise pos[1] if any wheel would otherwise punch through.

* **The "broken-axle / chassis-on-dirt" case** falls out of this
  naturally: when terrain spikes higher than `comp_cap` under
  one wheel, that wheel bottoms out at the compression cap and
  the chassis ITSELF rides up on the wheel.  Verified: with a
  30 cm terrain spike under the rear-left wheel (against a 4 cm
  compression cap), the chassis lifts from -0.07 m to +0.043 m
  and tilts -4.29 deg pitch + roll, with NO wheel intersecting
  terrain.  Stable, no oscillation.

* **Vertical velocity resets to zero whenever the floor catches
  the chassis** -- can't fall further if a wheel is already
  bottomed out.

### Physics: 6g gravity (1.86.6)

* Bumped to 6 * real = 58.92 m/s2.  3g still felt slow at the
  scale we're rendering at; 6g brings the cliff-jump fall closer
  to the "tank disappears" Hollywood-cut feel.

### Physics: 3g gravity drama factor for visible fall speed (1.86.5)

* **Gravity bumped from real-world 9.82 to 3*real = 29.46 m/s2.**
  Real gravity at TEPY's scale (1 unit = 1 yard, terrain quad =
  256 yards) covers ~5 yards in the first second of free fall --
  a tank disappearing off a cliff falls only ~2% of the visible
  terrain in that second.  Reads as sluggish on screen.  Standard
  game-feel trick (Half-Life uses ~18x real, GTA ~2x).  3x is
  the lowest multiplier where the fall reads as "weighty drop"
  without hitting cartoon territory; tune `gravity=` in the
  `TankPhysics(...)` ctor or the `for_t110e4` factory if it
  feels off.

### Physics: 9.82 m/s2 gravity + snap chassis pose (1.86.4)

* **Real-world gravity 9.82 m/s2** (was 9.80).

* **Dropped the asymmetric chassis-pose damping.**  Hysteresis on
  the contact set (1.86.2) already kills the contact-set flapping
  that was the root cause of oscillation.  The asymmetric alpha
  layer on top was a band-aid that made the tank feel like it was
  floating sluggishly toward equilibrium instead of snapping onto
  its wheels.  Now: chassis pose snaps directly to plane-fit
  output once the contact set is determined.

* **Per-wheel residual damping kept** because that drives the
  shader bone-deflection animation, which benefits from a few
  frames of visual smoothing without affecting the physics solve.

* **Verified**: bump test shows pitch / roll spike on the bump
  frame, gone next frame, no oscillation.  Slope-settle test
  still produces correct final pose (-1.146 deg pitch on the
  2% slope at all yaw values).

### Suspension: asymmetric shock damping (fast comp, slow ext) (1.86.3)

* **Real-world shock absorber asymmetry.**  Replaced the
  symmetric `ALPHA = 0.35` low-pass filter with an asymmetric
  pair:
  * `ALPHA_COMP = 0.50` -- applied when the chassis is rising
    OR pitch / roll magnitude is shrinking (chassis levelling
    out).  Compression direction = wheel pushed UP into the
    hull.  Fast.
  * `ALPHA_EXT  = 0.15` -- applied when the chassis is
    sinking OR pitch / roll magnitude is growing.  Extension
    direction = wheel dropping below the hull.  Slow.
  Mirrors the racing-suspension recipe "rebound damping >
  compression damping" -- the wheel reacts fast to a bump
  (compression) but rebounds slow afterward so it stays
  planted on the ground (extension).

* **Per-wheel asymmetric damping** on the residual that drives
  the shader bone Y translation.  New
  `self.smoothed_residual_y` array is the asymmetric-blended
  version of `last_residual_y`; `bone_matrix_array` consumes
  it.  `last_residual_y` keeps the raw lstsq output for
  diagnostics + the offline test scripts.

* **Side effect: oscillation actually dies now.**  The previous
  symmetric filter still left small pitch / roll ringing
  because both halves of each cycle had the same time
  constant.  With ALPHA_EXT < ALPHA_COMP, the slow-extension
  half of each cycle accumulates less than the fast-compression
  half clears -- residual oscillations naturally damp out.
  Verified: a 5 cm transient bump under the rear-left wheel
  produces a roll spike that fades to zero in ~10 frames with
  ZERO sign flips (purely monotonic decay).

### Suspension: hysteresis + low-pass damping kills roll oscillation (1.86.2)

* **Bug**: tank stuck oscillating in roll on uneven terrain.  The
  cause: a borderline wheel flapping between CONTACT and HANGING
  every frame, dragging the plane fit's pitch / roll with it,
  driving the chassis_matrix to wobble at the visible-render
  scale.

* **Fix 1 -- contact-set hysteresis.**  Sticky classifier:
  * A CONTACT wheel only switches to HANGING / OVER_COMP when
    its delta clears the envelope by more than `HYST` (= 2 cm).
  * A HANGING wheel only switches back to CONTACT when its
    delta is at least `HYST` ABOVE the extension cap.
  * A OVER_COMP wheel only switches back when its delta is at
    least `HYST` BELOW the compression cap.
  This kills the per-frame contact-set flap at the source.

* **Fix 2 -- low-pass filter on chassis pose.**  The post-
  classification "snap to plane fit" path was setting
  `pos_y / pitch / roll` directly to the lstsq output, so any
  remaining single-frame perturbation (e.g. a transient bump
  under one wheel) propagated at full amplitude.  Replaced
  with `new = ALPHA * raw + (1 - ALPHA) * old` where
  `ALPHA = 0.35` (response settles in ~6 frames at 60 fps).
  The combined effect: a single-frame 5 cm terrain bump under
  one wheel now produces a roll spike that decays MONOTONICALLY
  to zero over ~14 frames -- no overshoot, no oscillation.

* **Verified** on the slope test from 1.86.1 (still settles to
  -1.146 deg pitch at all yaw values; just takes ~6 frames
  longer to reach steady-state, which is invisible in
  practice).

### Plane fit in chassis-local frame so yaw is honoured (1.86.1)

* **Bug**: a tank turning across a slope tilted the WRONG way --
  visible as the chassis "rolling" when it should "pitch", or
  vice-versa, depending on heading.

* **Cause**: the lstsq plane fit ran against WORLD XZ wheel
  positions, returning a WORLD-frame normal -> world-frame
  pitch / roll.  These got applied as `Rx(pitch) @ Rz(roll)`
  inside `chassis_matrix()` BEFORE the `Ry(yaw)` rotation, i.e.
  around CHASSIS-LOCAL X / Z axes.  At yaw = 90 deg, world-X
  has rotated to chassis-local-Z, so what should have been a
  pitch came out as a roll on the rendered tank.

* **Fix**: feed `fit_plane` the chassis-LOCAL XZ (`local[:, 0]`
  / `local[:, 2]` -- the bind-pose coordinates already stored
  rotation-free) plus the world Y target (Y is yaw-invariant).
  The lstsq solves `target_y_world = a*local_x + b*local_z + c`
  and the resulting normal is in chassis-local frame, so
  `normal_to_pitch_roll` produces chassis-local pitch / roll
  ready for `Ry(yaw) @ Rx(pitch) @ Rz(roll)` to render
  correctly at any heading.

* **Verified** on a 2% slope in world +Z:
  * yaw =   0 -> pitch -1.146, roll  0.000
  * yaw =  90 -> pitch  0.000, roll -1.146  (slope is now sideways)
  * yaw = 180 -> pitch +1.146, roll  0.000  (sign flipped -- front faces other way)

### Per-wheel contact classification + red/green highlight (1.86.0)

* **Contact-aware suspension model.**  Each wheel is now classified
  per frame as one of:
  * `CONTACT`     -- touching ground inside the suspension envelope.
  * `HANGING`     -- terrain too far below; droops at extension cap.
  * `OVER_COMP`   -- terrain too high; bottomed-out at compression cap.
  * `NONE`        -- pre-update default.

  Classification is done by comparing the per-wheel terrain target
  Y against the rigid pose's Y at that wheel using the CURRENT
  chassis pose (not the v1.83 "flat guess"), against the engine-
  XML envelope `(min_offset, max_offset)`.

* **Plane fit through CONTACT wheels only.**  Was: every wheel
  whose flat-guess delta cleared the lower envelope joined the
  lstsq fit.  Now: only wheels actually classified CONTACT or
  OVER_COMP contribute.  HANGING wheels can't bear weight, so
  they don't bias the tilt -- the previous behaviour had a tank
  driving off a cliff dragging its nose down because the in-air
  front wheels were still pulling the lstsq solve toward where
  they "would have" landed.

* **Iterative settling.**  When fewer than 3 wheels are in
  contact, the chassis falls under gravity.  Next frame, more
  wheels reach the ground and the plane fit re-asserts.  No
  spring-damper, no oscillation -- the kinematic settling
  converges in 2-4 frames after a contact set change.

* **State-aware residual clamp.**  `_compute_residual_y` now
  honours per-wheel state:
  * CONTACT  -- residual = lstsq fit error, clamped to envelope.
  * HANGING  -- residual = extension cap (full droop visually).
  * OVER_COMP -- residual = compression cap (bottomed into hull).
  * NONE     -- envelope clamp only.

* **Shader highlight: red for contact, green for hanging.**
  Replaced the v1.85 four-int `u_contact_bones` with a 64-int
  `u_wheel_state[]` array indexed by palette-index.  Each chassis
  sub-mesh maps `tank_physics.wheel_bone_names[i]` to its OWN
  palette slot and writes the per-wheel state code there.
  Fragment shader picks colour from the propagated `flat in int
  v_wheel_state`:
  * `1 / 3` (CONTACT / OVER_COMP) -> mix toward red
  * `2`     (HANGING)             -> mix toward green
  60 / 40 mix preserves shading + texture detail.

* **Test on a synthetic cliff edge** (4 wheels on solid ground
  at z>0, 4 in a -3 m chasm at z<0): all 4 in-chasm wheels
  classified HANGING with residual -0.08 (extension cap), all 4
  on-ground wheels CONTACT, chassis settled to expected Y.

### Drive: bump key-9 creep speed to 0.5 kph (1.85.2)

* **Step 9 = 0.5 kph** instead of 0.1.  0.1 felt unresponsive
  when nudging one wheel onto a divot for the suspension test.
  Linear ramp from step 1 (top speed) to step 9 (0.5 kph) is
  preserved; steps 2-8 shift proportionally.

### Wheel-highlight: scan all 4 iii slots for dominant bone (1.85.1)

* **Bug**: only one wheel painted red instead of four, and the
  one that did paint was sometimes a non-road wheel (idler /
  drive sprocket).  Cause: the v1.85.0 shader took `iii.x` as
  "the dominant bone slot", but the WoT vertex format does NOT
  guarantee descending-weight ordering.  For verts where the
  writer put the bone in slot 1, 2, or 3, `iii.x` pointed at a
  pad slot (typically `byte 0` = `palette[0]` = V_BlendBone or
  some unrelated bone) and the membership check missed.

* **Fix**: vertex shader now finds the highest-weight slot via
  four compares on `ww`, then reads `iii` at that slot.  Mirrors
  the CPU-side `int(bi[v][int(np.argmax(bw[v]))])` recipe used
  in `from_chassis_meshes`.  Also gates the contact check on
  `dom_w > 0.0` so a vert with all-zero weights (degenerate
  pad) doesn't accidentally pick up bone 0's matrix.

### Contact-wheel red highlight in mesh shader (1.85.0)

* **Four corner wheels paint red** when Susp + the highlight
  toggle (default ON) are both active.  Visual answer to "which
  wheels is the plane fit anchoring on".  Toggle with `H`.

* **`mesh.vert`** now declares `flat out int v_is_contact` plus
  two new uniforms: `int u_contact_mode` (0 = off, 1 = mix red
  in fragment) and `int u_contact_bones[4]` (palette indices,
  pre-divided-by-3, with `-1` for unused slots).  Each vertex
  checks if its dominant `iii.x / 3` matches any of the four;
  flat-out the result.  Wheel meshes are 1-bone-bound so the
  flat / provoking-vertex interpolation is exactly the "all 3
  verts of this face match the contact set" check requested.

* **`mesh.frag`** mixes the final colour 60 / 40 toward red
  when `v_is_contact == 1`.  Detail + texture + lighting all
  show through (the goal is to identify wheels, not paint over
  them).

* **`TankPhysics.corner_wheel_indices()` /
  `corner_wheel_bone_names()`** identify the 4 corners
  (rear-left / front-left / rear-right / front-right) from the
  rear-to-front-sorted `wheels[]` array.  Tanks with < 4 wheels
  total return < 4 entries; the shader handles missing slots via
  the `-1` sentinel.

* **`Viewer._upload_skinning`** resolves each corner bone NAME
  to a per-mesh palette index (one mesh's chassis renderSet
  declares the bones in different order than the next; turret /
  hull / track-ribbon meshes don't carry the wheel bones at all
  and end up with all -1).  Uploads via the new
  `ShaderProgram.set_int_array` (mirrored on `ImportedShader`)
  in one `glUniform1iv` call -- per-element location lookups
  past index 0 are implementation-defined.

* **`H` key** toggles `_highlight_contacts`.  Independent of
  Susp so the user can keep the physics rig running while
  turning the visual aid on / off.

### Drive speed: stepped 0-9 selector tied to per-tank top speed (1.84.0)

* **Keys 1-9 + 0 set drive speed.**  `0` = stopped (default on tank
  reload).  `1` = current tank's max forward speed in kph.  `9` =
  0.1 kph (creep).  `2..8` linearly interpolate between 1 and 9.
  Bound on the number row only; numpad untouched so future zoom /
  camera shortcuts can claim it.

* **Per-tank top speed parsed from gameplay XML.**
  `VehicleXMLLoader.parse_info` now reads `<speedLimits>`
  `<forward>` / `<backward>` (source values are m/s; converted to
  kph via × 3.6) and surfaces them under `info['speed']`.  The
  viewer pulls `forward_kph` on each load and stashes it as
  `_top_speed_kph`; falls back to 50 kph when the block is absent.

* **Scene-unit conversion: 1 unit = 1 yard.**  TEPY's terrain quad
  is 256 yards on a side, so the drive code converts kph -> yds/s
  via `(1000 / 3600) * (1 / 0.9144)` = 0.30378.  The arrow-key
  drive logic now reads `_speed_yards_per_sec()` instead of the
  hardcoded `-5.0 m/s` it used at the v1.81.0 first cut.

* **In-app feedback.**  Each speed change writes a coloured line
  to the console (`speed: step 5/9 = 25.05 kph (7.610 yds/s)`),
  and tank load prints a one-shot reminder of the new top speed
  + the 1-9 / 0 controls.  Re-arms the `_drive_keys_seen`
  diagnostic so changing speed mid-drive prints a fresh tank-pos
  trace on the next arrow-key press.

* **Yaw rate unchanged.**  Q / E still rotate at the original
  fixed 60 deg/s.  Could later be tied to the speed step too,
  but turn-in-place has its own feel and Professor Coffee can
  flag if it ever needs the same step ladder.

### Wheel deflection: sign flip + suspension envelope clamp (1.83.3)

* **Per-wheel residual sign flipped.**  Empirical: the un-flipped
  residual translated geometry the WRONG way (terrain rising under
  a wheel pushed the wheel mesh DOWN instead of compressing it
  UP into the hull).  `_compute_residual_y` now negates the
  `target - rigid` delta so the convention is "+ residual = wheel
  pushed UP" everywhere downstream.

* **Suspension envelope clamp.**  Per the gameplay XML's
  `<groundNodes>` `(minOffset, maxOffset)` (default `(-0.04, +0.08)`):
  * Compression cap (wheel rises into hull) =  `-min_offset` =  +0.04 m
  * Extension cap   (wheel drops below hull) =  `-max_offset` =  -0.08 m
  Anything beyond either bound is clamped at the source.  A wheel
  that would otherwise punch through the chassis when terrain
  spikes high, or stretch infinitely into a chasm when terrain
  drops, instead bottoms-out at the respective limit.  Free-fall
  of the whole chassis past the envelope is still handled by the
  existing `FALL_THRESHOLD` path in `update()`.

### Particle hardpoints follow chassis pose (1.83.2)

* **Smoke + fire emitters now ride the tank.**  Engine exhaust
  + fire spawn hardpoints (`HP_engineExhaust_*`, `HP_Fire_*`,
  `HP_Track_Exhaus_*`, ...) were captured at load time as bind-
  pose world coords.  After the tank-physics pipeline started
  wrapping every per-mesh transform in a `chassis_pose`
  (translation + yaw + plane-fit pitch / roll), the hardpoints
  stayed pinned at their bind-pose world locations -- so the
  smoke plume kept emitting from the spot the tank started at
  while the tank drove away.

* **New `Viewer._update_emitters_for_chassis_pose(pose)`** runs
  once per frame right after the physics tick.  Lazily snapshots
  each hardpoint's `bind_pos` / `bind_fwd` (one-time per load),
  then transforms them through the current `chassis_pose`
  (full 4x4 for position, rotation 3x3 only for direction) and
  re-publishes the resulting list to `smoke_particles`,
  `fire_smoke_particles`, and `fire_billboards` via
  `set_emitters`.  Also rebuilds the cyan exhaust-direction
  debug lines from the transformed values so the HP marker
  overlay stays glued to the tank too.

* **Cost**: O(n_hardpoints) per frame.  Typical tank carries
  4-8 exhaust + 2-4 fire hardpoints; the matmul + dict-build
  is microsecond-scale.

* **Restore-to-bind path**: when physics is OFF (terrain
  toggled off, Susp checkbox unchecked), the same routine is
  called with an identity matrix so the emitters snap back to
  bind pose.  Without this, the last-transformed values would
  linger after physics was disabled.

### Skinning: idlers / drive sprockets stay rigid (1.83.1)

* **`bone_matrix_array` now consults `wheel_bone_names`** -- the
  list of bone names that actually got accepted as road wheels
  during `from_chassis_meshes` -- instead of re-running the
  `_wheel_side_from_name` regex.  The regex returns a side for
  every `WD_<L|R>\d+_BlendBone` (because the Hotchkiss EBR's
  WD_ bones ARE real road wheels), so skinning was deflecting
  tracked-tank idlers, drive sprockets, and return rollers --
  which had been correctly rejected by the Y-band post-pass
  during extract but then re-admitted at draw time.

* **`from_chassis_meshes` records per-wheel bone names.**  Each
  accepted wheel now carries its source bone name through the
  Y-band post-pass + sort + side-split.  The names land on
  `inst.wheel_bone_names` after construction; the bone-matrix
  builder uses them as the source of truth for "this bone gets
  a residual."

### GPU skinning: per-wheel deflection drives chassis + track geometry (1.83.0)

* **New skinning path in `mesh.vert`.**  Two new vertex attributes
  (location 5 `uvec4 iii`, location 6 `vec4 ww`) plus a 64-slot
  `mat4 u_bones[]` uniform array and an `int u_skinned` toggle.
  When `u_skinned == 1` the shader resolves the SC_UBYTE4_REVERSE_
  PADDED bone byte stream into a weighted sum of bone matrices --
  the same shape every Bigworld game uses.  Position, normal,
  tangent, binormal all skin so normal-mapped surfaces stay
  lit correctly on deflected geometry.  When `u_skinned == 0`
  the path collapses to identity, so non-skinned meshes (hull /
  turret / gun, every imported FBX) draw exactly as before.

* **`Mesh.build_vao` uploads bone bytes + weights** when both
  `bone_indices` and `bone_weights` are populated (every chassis
  + track ribbon mesh on a skinned WoT tank).  Bone bytes go
  through `glVertexAttribIPointer` so they arrive as `uvec4`
  in the shader -- no implicit normalisation, no float-cast
  surprises.  Source data is padded to width 4 if the format
  carried fewer slots; the all-zero pad is silently ignored by
  the weighted sum.

* **`TankPhysics.bone_matrix_array(palette)`** builds the
  per-frame matrix array.  Each entry is mostly identity; the
  road-wheel bones (matched against the palette via
  `_wheel_side_from_name`) get a translation in mesh-local Y by
  the wheel's residual -- the difference between the rigid
  plane fit's Y and the actual terrain target Y.  Track-segment
  bones (`Track_<L|R><i>_BlendBone`) inherit the matrix of
  their companion road wheel (`W_<L|R><i>_BlendBone`) so the
  track stays glued to the wheel as it deflects.  V_BlendBone
  + WD_ + everything else stays identity.

* **Per-wheel residual stashed each frame.**  After the rigid
  pose is set, `update()` calls `_compute_residual_y()` which
  subtracts the rigid pose's Y at each wheel from the terrain
  target Y.  By construction these are the lstsq fit residuals
  -- on flat ground they're near zero (no skinning visible);
  on bumpy ground (one wheel in a divot) they approach the
  divot depth and the skinning wraps the geometry around the
  terrain.

* **`Viewer.render` uploads bones once per skinned mesh.**  New
  `_upload_skinning(mesh)` closure handles both the main pass
  and the wireframe overlay.  Non-skinned meshes set
  `u_skinned = 0` and never touch the bones array.

* **`ShaderProgram.set_mat4_array`** uploads `mat4[N]` uniforms
  with the same `transpose=GL_TRUE` orientation the per-matrix
  helper uses, so per-bone matrices and the model / view /
  projection uniforms agree on row vs column major.

* **Picker, overlay, normals, and imported-mesh shaders
  unchanged.**  Each one binds its own vertex shader; the new
  attribs are silently ignored by shaders that don't declare
  them at the same layout locations.  Same for `u_bones` /
  `u_skinned` -- not declared, not consulted.

### Tank physics: bone palettes + WD_ wheel acceptance + Y-band cleanup (1.82.0)

* **Plumb `bone_palette` through `Mesh`.**  New
  `VisualLoader.parse_renderset_bones(visual_file)` returns
  `{primitive_group_name: [bone_name, ...]}` in declaration order
  -- the canonical mapping from raw `iii` byte / 3 to bone name.
  `Viewer.load_vehicle` now calls it once per chassis and attaches
  the result as `mesh.bone_palette` on every Mesh built from the
  group.  Was already on the dump-track-skinning script side; this
  closes the loop on the runtime side.

* **Wheel auto-extract was broken on every non-T110E4 tank.**  Two
  bugs together: (1) the v1.81.0 runtime path never received the
  bone palette, so the name-based filter was never exercised --
  every tank fell into the heuristic-only path; (2) the heuristic
  was too loose (vert count >= 50, dy / dz <= 1.0) and accepted
  Track_*_BlendBone groups (52-88 verts, dy ~ 0.14-0.20) as fake
  wheels.  Result: T92 came out with 21 "wheels" per side at three
  different Y bands (real road wheels + track-bottom segments +
  chassis-side track plates).  Plane fit through that mess
  produced garbage tilt, so every tank LOOKED like it was using
  the T110E4 hardcoded rig because the auto-extract was either
  failing or emitting bad data.

* **Tightened name-based filter.**  Three accepted patterns now,
  documented in `_WHEEL_NAME_PATTERNS`:
  * `W_<L|R>\d+_BlendBone` -- standard tracked-tank rig (T110E4,
    T92, Bourrasque, Object 268/4).
  * `W_F_<L|R>_BlendBone` -- wheeled-rig front pair (Lynx-style
    naming convention).
  * `W_R\d+_<L|R>_BlendBone` -- wheeled-rig back axles.
  * `WD_<L|R>\d+_BlendBone` -- ambiguous (decorative on tracked,
    real wheel on Hotchkiss EBR); accepted IFF post-name shape +
    cy band check confirms it's a road wheel.

* **Tightened heuristic shape check.**  `_looks_like_wheel`:
  vert count >= 100, dy + dz both in [0.30, 1.50] m, cy <= 0.80 m.
  Bumped the dy / dz upper bound from 1.0 m to 1.5 m to admit
  Hotchkiss EBR's 1.21 m road wheels; bumped the cy threshold
  from 0.55 to 0.80 to admit EBR wheels (cy = 0.619) without
  admitting tracked drive sprockets / idlers / return rollers
  (cy >= 0.85 across every tracked tank we sampled).

* **Y-band post-pass.**  After the per-bone collection, drop any
  wheel whose cy is more than 15 cm above the LOWEST accepted
  wheel on the same side.  Catches the Bourrasque case (real
  road wheels at cy = 0.395, drive sprocket at cy = 0.749, idler
  at cy = 0.627 -- all three pass name + shape but only the road
  wheels are real contact points).  No-op on rigs where every
  wheel sits at the same Y (Hotchkiss EBR -- all at 0.619).

* **Aggregate verts by bone NAME across all chassis sub-meshes.**
  A primitive group split by `_split_into_submeshes` into N Mesh
  objects (one per material) was previously being seen N times
  by `from_chassis_meshes`, producing duplicated wheel positions
  if a wheel's verts straddled multiple sub-meshes.  Now we
  bucket every chassis vert into a single dict keyed by bone
  name (or by `__byte_<idx>__<mesh.name>` when no palette is
  available), then compute one centroid per bucket.

* **Verified on four reference tanks**, palette-based and
  heuristic-only paths producing identical results:
  T110E4 = 6L+6R @ cy 0.425, T92 = 7L+7R @ 0.396,
  Bourrasque = 7L+7R @ 0.395, Hotchkiss EBR = 3L+3R @ 0.619.

* **New diagnostic tool `cust_tools/test_wheel_extract.py`.**
  Loads a chassis offline, dumps the per-bone group breakdown
  with the SAME filter logic the runtime uses (imports
  `_wheel_side_from_name` and `_looks_like_wheel`), and prints
  the resulting wheel rig.  Use it to verify any future tank's
  rig before launching the GUI.

### Tank physics: any-tank auto-extract + sign fixes (1.81.0)

* **Auto-extract wheel rig from any chassis.**  New
  `TankPhysics.from_chassis_meshes` factory walks every loaded
  mesh whose `component == 'chassis'`, groups verts by their
  dominant `iii` byte (the SC_UBYTE4 bone-index byte), and
  extracts road-wheel centroids via a three-criterion heuristic:
  bbox compact (excludes V_BlendBone hull strip), Y < 0.55 m
  (excludes return rollers / idler / drive-sprocket-on-most-tanks),
  vert count >= 50.  Tested on T110E4 (6+6), T92 (14+14 -- paired
  twin-wheel design), AMX 13 105 (7+7), Object 268/4 (9+9).
  Falls back to the T110E4 hardcoded rig if extraction yields
  no road wheels (very rare).  Wired into `Viewer.load_vehicle`
  so every tank load auto-rebuilds the physics rig.

* **Z-direction fix.**  Wheel positions were being placed at
  the wrong end of the tank because TEPY renders the skinned
  chassis at the primitives' raw Z while flipping the non-
  skinned hull -- so the chassis-mesh-local Z direction runs
  opposite to TEPY's rendered world Z direction.  Both the
  hardcoded T110E4_WHEELS data and the auto-extract centroids
  now negate Z on the way through, so the physics samples
  terrain at the visually-correct world XZ for each wheel.

* **Drive controls flipped.**  Same root cause as the Z fix:
  arrow keys / Q / E were driving the tank in the wrong world
  direction relative to its visible front.  Speeds inverted
  (move_speed = -5.0, yaw_speed = -60.0) so UP = forward,
  LEFT = strafe-left, Q = yaw-left match the visible tank.

* **Plane fit hardened.**  Replaced the covariance-eigenvector
  plane fit with `np.linalg.lstsq(y = a*x + b*z + c)`, which is
  immune to the degenerate "terrain varies along only one
  horizontal axis" case (where the eigvec method went to 90
  degrees of pitch / roll).  Same result on healthy inputs;
  graceful on synthetic single-axis slopes.

* **Roll sign corrected.**  The original `atan2(+nx, ny)` lifted
  the wrong side of the tank on side slopes.  Negated to
  `atan2(-nx, ny)` so terrain rising on +X side correctly
  raises the right-side wheels (aviation +roll convention per
  the diagram Professor Coffee posted).

### Suspension-test checkbox + Normals-header dedup (1.80.1)

* New right-panel **`Susp`** checkbox under the Debug section,
  third on the same row as `PerVtx` and `Debug`.  Drives
  `self.tank_physics_enabled` -- check it to engage per-wheel
  terrain conformance, uncheck to restore the old "tank floats
  at world origin" view.  State persists in
  `tankExporterPy.json` under `suspension_test`; default OFF
  so the tank loads at origin like before until you opt in.
  When unchecked mid-session, the next render-frame restores
  every sub-mesh's bind-pose `model_matrix` so the toggle is
  bidirectional + instant.  Localised across all 21 catalogs
  via the standard sync (`Susp` falls back to msgid in non-
  English -- consistent with the existing NMap / AO / PerVtx
  abbreviation convention).

* Right-panel sub-header dedup.  When a slider group has
  exactly one slider AND the slider's label matches the group
  label (the way `Normals` group + `Normals` slider stacked
  the same string twice), the group sub-header is now skipped.
  The right-panel height calculation got a matching tweak --
  `n_headers` (the post-skip count) drives the sub-header
  contribution, while inter-group spacing still scales by
  `n_groups` so groups remain visually separated.  Net effect:
  ~16 px reclaimed in the Debug body for the same content.

### Tank-on-terrain physics, rigid-body conformance (1.80.0)

The first slice of "tank rides the terrain" -- a per-wheel
ground-collision + plane-fit solver that pitches / rolls /
translates the loaded tank so its wheels visually rest on the
sand-detail-displaced heightmap.  Drives the user input arrow
keys / Q / E so you can drive the tank around the world and
watch the suspension respond.

What it does
* Per frame, samples `terrain.sample_heights` at every wheel's
  world-XZ (12 wheels, vectorised) and computes target wheel-
  centre Y as `terrain_y + group_radius`.
* Determines support: a wheel is supported if its required
  suspension delta lies within the gameplay XML envelope
  (`<groundNodes>` minOffset / maxOffset).  Out-of-envelope
  wheels are noted but don't pull the plane fit.
* Fits a least-squares plane through the supported wheels'
  targets -> chassis pitch + roll.  Sets chassis Y so the
  average supported wheel sits at its target.
* Falling: when the kinematic target is far below current
  pose (more than the suspension travel range -- e.g. tank
  driven off a cliff edge), integrates gravity * dt vertical
  velocity until at least one wheel catches.

What it does NOT do (yet)
* Per-vertex track sag.  The tank moves as a rigid body --
  individual track segments don't sag per-wheel.  That needs
  a real skinning shader pipeline (the next bone-tools arc).
* Wheel rotation around its own axis (track scroll / wheel spin
  during travel).  Static visuals for now.
* Lateral / longitudinal friction, slope-induced sliding.
  This is a kinematic positioner -- "tank reads as on the
  terrain", not a full vehicle dynamics sim.

Wheel rig data is hardcoded for T110E4 right now (the wheel
positions, radius, suspension envelope).  Other tanks will load
and use the same rig parameters -- visually plausible but not
calibrated.  A future pass should auto-extract this from the
loaded chassis primitives + gameplay XML so the physics is
correct for any tank.

Files
* `tankExporterPy/tank_physics.py` -- new module with the
  `TankPhysics` class + plane-fit math + matrix helpers.  A
  `for_t110e4()` factory pre-loads the right rig parameters.
* `tankExporterPy/viewer.py` -- physics tick wired into
  `render()` immediately after camera matrices, with the
  `chassis_pose @ bind_model_matrix` composition applied
  before any per-mesh draw.  Bind-pose preserved on first
  frame after each load so toggling terrain on/off bounces
  cleanly.

Controls (terrain on, tank loaded):
* Arrow keys -- drive tank XZ along its current heading.
* Q / E      -- yaw the chassis.
* Debug checkbox -- shows the per-wheel overlay: pink dots
  at ground contact, cyan at target wheel centre, yellow
  suspension shaft connecting them.

### dump_track_skinning auto-resolves geom names + `--part chass` (1.79.1)

Generalised the dumper so it works across the WoT chassis
geometry-naming variants we've found in the wild:

  * `track_<side>_Shape`           (T110E4, T92, Bat-Chat, AMX 13)
  * `track_<side>Shape`            (AMX 50B, older French)
  * `exportChass<side>1_Shape`     (modern default)
  * `exportChass<side>1_Shape1`    (Object 268/4 family)
  * `chassis_<side>Shape_split_<N>` (AMX 50B multi-split chassis)

The `--part chass` flag analyses the WHEEL / sprocket / roller
sub-mesh instead of the track ribbon -- complementary view of the
same skeleton.  Cross-tank summary written to
`hand_off/CHASSIS_RIG_VARIANTS.md`: T110E4 hides its suspension
in `V_BlendBone` (no animated arms), but Bat-Chat / AMX 13 / AMX 50B /
Object 268/4 all bind their suspension-arm geometry to
`Track_L<i>_BlendBone` so the arm visibly rotates with the wheel.
No tank has a separate `Shock_L<i>` bone -- the shock cylinder is
either static (heavy TDs) or rolls along with the arm.

### Bone-skinning analysis + terrain-Y sampler (1.79.0)

Two new tools and a new on-class API land together to support
the bone-blending investigation and the eventual tank-on-terrain
physics.

**`cust_tools/dump_track_skinning.py`** -- chassis track-skinning
dumper.  For a given tank tag + side, walks
`Chassis.primitives_processed` + `Chassis.visual_processed` and
writes:

* the renderSet bone palette (vertex `iii` byte / 3 = palette
  index, the SC_UBYTE4_REVERSE_PADDED convention every Bigworld
  game uses),
* an explicit vertex-group table grouping every track vertex by
  dominant `iii` byte, with per-group Z-window + centroid + bone
  name,
* a 2-D side-view PNG (z, y) of the track mesh with one colour
  per dominant bone -- so the per-wheel influence regions of
  the bottom run pop visually.

Run it on T110E4 + T92 to confirm the rig pattern: the bottom
track run is segmented per-wheel into ~30 cm Z windows; each
wheel "owns" ~138 verts; transition zones use 2-bone blends at
discrete weights (0.502 / 0.6 / 0.7 / 0.8 = byte 128 / 154 /
179 / 204 quantised); top run + ends bind 100% to `V_BlendBone`
(chassis-rigid).  Reference write-up:
`hand_off/TRACK_SKINNING_T110E4.md`.

**`Terrain.sample_height(x, z)` + `sample_heights(xs, zs)`**.

The `Terrain` class now exposes a runtime API for "what's the
terrain Y at this world (x, z)?" -- bilinear interpolation of
the composed heightmap (macro image + tiled sand-detail
displacement, both baked into `self._heightmap` at init).
Vectorised path is fully numpy: 12 wheels x 60 FPS lookup
rounds to single-microsecond cost when batched.  Out-of-bounds
queries return `base_y` so downstream physics doesn't have to
special-case the world edges.  Two free helpers
(`bilinear_sample_height`, `bilinear_sample_heights`) live at
module scope so headless / offline tools can sample the same
height grid without needing a GL context.

**`cust_tools/demo_terrain_corners.py`** -- sanity demo for the
sampler.  Builds the terrain heightmap headlessly (re-uses the
same `_heightmap_from_image` / `_make_heightmap` /
`_detail_displacement` helpers `Terrain.__init__` calls, just
without the VAO upload), samples Y at known points, drops a
virtual T110E4 chassis at a configurable world (X, Z) and yaw,
samples the terrain Y under the four corner wheels, then fits a
plane through the four wheel-centre points -> chassis pitch +
roll in degrees.  All the math (`fit_plane`,
`normal_to_pitch_roll`) is in the same file for easy lifting
into a real physics pass later.

### Wireframe polygon offset bumped to 4 (1.78.1)

The stacked `+2 / -2` recipe in 1.78.0 still flickered at grazing
angles on dense / coplanar geometry.  Reverted to the simpler
"push fills only" form but at `glPolygonOffset(4.0, 4.0)`.  Four
depth-buffer-units of clearance is enough to win z-test against
the surface the lines ride on, on every driver / view-distance
combo we've tested, without introducing the visible-gap failure
mode you'd get pushing further.  Line pass renders at natural Z
with both offset flags off.

### Camera + viewport polish round (1.78.0)

A pile of usability + correctness fixes accumulated while we
exercised the new triangle picker against real tank meshes.

**Picker: per-mesh model_matrix in pick + overlay**

The picking shader was projecting every mesh from MODEL space
while the visible scene rendered each mesh at its WORLD-space
position via `mesh.model_matrix` (turret rotation, gun pitch,
hardpoint offsets).  Symptom: hover the (correctly-positioned)
tank, get hits on empty space underneath where the model-space
geometry happened to land in screen.

Fix: added `uniform mat4 u_model` to both `picking.vert` and
`overlay_solid.vert`, uploaded per-mesh in `update_pass`, cached
on hit so `draw_overlay` re-applies the same transform to the
highlight.  Picker FBO now matches the visible scene
pixel-for-pixel.

**Picker: state-stack hygiene**

`draw_overlay` was leaving `GL_CULL_FACE` disabled (from pass 1's
filled-triangle draw) and `glPolygonOffset(-1, -1)` live in the
GL state.  The overlay now restores both at exit and documents an
explicit "state contract" -- the depth, blend, polygon-offset,
line-width, point-size, and cull-face state on exit equals the
state on entry.

`update_pass` was already defensive at entry (forcing GL_LESS,
GL_DEPTH_TEST, GL_DEPTH_MASK, disabling polygon offset and blend)
but wasn't matching the main render's per-mesh culling
convention.  Now it does -- `mesh.double_sided` -> disable
GL_CULL_FACE for that mesh, else enable + GL_BACK.  Same surface
the user sees, no back-faces sneaking in to "win" depth on
curved surfaces.

**Picker: Pick Tri button localised**

Added `Pick Tri` to all 21 locale catalogs via the canonical
sequence (en .po sync, msgid sync to other locales, per-language
translations in `seed_locale_translations.py`, recompile).
ASCII-light fallbacks for languages where pygame's Calibri panel-
label render path drops accented chars.

**Look-at crosshair + Shift-drag Y-lift**

New camera affordance: while Shift is held OR the middle mouse
button is held, three perpendicular pale-pink lines (10 units
total per axis: X / Y / Z) draw at `camera.center` so the user
sees exactly where the orbit pivot sits.  Hidden otherwise.

Shift+drag now LIFTS the look-at point on the world Y axis at
half the existing pan-XZ speed, with the sign reversed from the
naive convention -- drag DOWN raises the look-at, drag UP drops
it (matches "push the world down to lift my gaze").  Suspends
the regular orbit / pan handlers while held so the modifier
mode is unambiguous.

**Polite focus-steal on cursor enter (Windows)**

`pygame.WINDOWENTER` now triggers a `SetForegroundWindow(my_hwnd)`
after saving whichever window held the foreground before.  No
more "first click is eaten by the OS giving the app focus" --
the very first click on a TEPY button now does what the user
expects.

`pygame.WINDOWLEAVE` restores the saved HWND so we're not
permanently stealing input from whatever was focused before the
cursor crossed in.  Win32-only path; Linux / macOS short-circuit
because SDL2 already handles cursor-enter focus correctly there.

**Wireframe Z-fight fix**

Old recipe used `glPolygonOffset(1.0, 1.0)` on the solid pass and
no offset on the line pass.  Worked on most drivers but z-fought
at grazing angles on dense / coplanar geometry (track segments).

New recipe stacks both offsets:
* solid pass: `GL_POLYGON_OFFSET_FILL` + `(2.0, 2.0)` -> push
  fills back 2 units;
* line pass: `GL_POLYGON_OFFSET_LINE` + `(-2.0, -2.0)` -> pull
  lines forward 2 units.

~4 units of total separation -- lines win z-fight on every
driver tested.  `GL_POLYGON_OFFSET_FILL` does not apply to
GL_LINE polygon mode (only the dedicated `_LINE` flag does), so
the previous "disable offset for the line pass" path was
relying entirely on the fills being far enough back.  Now both
sides contribute.  Cleanup also resets the offset value to
`(0, 0)` so the negative line bias doesn't leak into later code.

### Phantom right-panel widgets at top-left fix (1.77.1)

The "left pane wacked" pattern that's been quietly biting us
across the v1.72 saga had one more culprit.  When the Debug
section is collapsed at startup (or any time `_on_resize` runs
with the right-panel body-block skipped), the right-panel
sliders / checkboxes never got positioned -- they kept their
default `track_x = 0` (sliders) or `cx = SLIDER_CB_X = 278`
(checkboxes).  Both are `< INFO_PANEL_W = 280`, so the spine-
collapse override loop at the end of `_on_resize` classified
them as left-panel widgets and force-set `visible = True`.

The renderer then drew them at (0, 0) -- "phantom right-panel
controls floating over the top of the left pane".

Fix: discriminate by `group_id` instead of x-coordinate.  Every
right-panel slider / checkbox carries `group_id` in
`{'smoke', 'fire', 'normals'}` (assigned at `add_slider` /
`add_checkbox` time).  The spine override now skips any widget
whose `group_id` is in that set, regardless of position.
Robust against the right-panel-skipped-layout case AND any
future widget-position changes.

The button loop in the spine override still uses the x-based
check because all buttons are left-panel and positioned
in `_build_ui` -- nothing changes there.

### Triangle picker (1.77.0)

New diagnostic in the Tools group: **Pick Tri**.  Toggles an
off-screen colour-pick pass that, every frame the tool is on,
renders the loaded tank into a hidden RGBA8 FBO using a picking
shader that encodes (mesh_id, gl_PrimitiveID) into the pixel
colour.  A single `glReadPixels` at the mouse position recovers
the hovered triangle without any CPU-side raycast math.

When the hovered triangle changes, the picker:

* Clears the console and writes three colour-coded lines (one
  per vertex) with that vertex's bone indices + bone weights --
  exactly the data we need to start figuring out how WoT's
  chassis / track skinning is laid out against the skeleton.
* Renders an overlay on the live scene: the picked triangle
  filled in `theme.c1` (alpha-blended so the underlying material
  shows through), the three edges as a closed line loop in
  `theme.c2`, and three points at the vertices in red/green/blue
  matching the colour `#` markers in the console.

Encoding: 8 bits mesh ID + 24 bits primitive ID into the RGBA8
attachment.  Up to 255 sub-meshes per tank, 16 M tris per mesh
-- comfortable margin over real WoT data.

New files:

* `shaders/picking.{vert,frag}` -- the picking shader (vertex
  pass-through; frag emits the encoded RGBA).
* `shaders/overlay_solid.{vert,frag}` -- a tiny uniform-colour
  shader for the highlight passes.
* `tankExporterPy/picker.py` -- `TrianglePicker` class owning
  the FBO, the shader programs, the highlight VAO, and the
  hover-change dispatch.

Hooked into `Viewer.render` via two calls: `picker.update_pass`
right after the camera matrices are computed (before the main
mesh pass), and `picker.draw_overlay` between the main mesh
pass and the 2-D UI pass.  When the picker is off, both calls
short-circuit -- no FBO bind, no readback, no overlay -- so the
feature has zero cost when not in use.

### Terrain rebuild: image heightmap, sand texture, detail displacement (1.76.0)

Pulled the procedural ground from a "small Perlin patch under the
tank" into a real desert landscape.  Several layered changes that
collectively land 1.76.0:

**IQ-flavoured terrain shader rewrite (`shaders/terrain.frag`)**

Replaced the three-band hard-RGB palette with techniques borrowed
from Inigo Quilez's published landscape shaders (techniques only;
no IQ source code reproduced):

* Domain-warp on the colour-band sampling so the bands wobble
  organically across (xz) instead of running as horizontal
  contours that expose the heightmap grid.
* IQ cosine palette (`a + b*cos(2π*(c*t+d))`) for a smooth earthy
  gradient -- replaces the three hardcoded RGB colours that were
  fighting each other.
* Slope desaturation: cliffs trend toward neutral grey while
  keeping a hint of the palette tone.
* Sun-direction warm/cool tint -- subtle warm bias on the lit
  side, cool bias in shadow.
* Distance fog: exponential falloff to a soft sky-tone so far
  peaks fade and near terrain reads sharp.
* Optional sand-texture path: when a diffuse texture is bound to
  unit 0, the cosine palette is bypassed and the surface samples
  the texture as the base albedo, tiled at the configured tile
  size with a subtle UV warp to break grid alignment.  Lambert
  + sun tint + fog still apply.  No specular -- pure diffuse
  ground.

**Image-driven heightmap (`tankExporterPy/terrain.py`)**

`Terrain` accepts `image_path` -- any Pillow-decodable grayscale
image is loaded, resampled (LANCZOS), Gaussian-smoothed (kills
JPEG / bilinear stair-stepping), edge-faded (so the mesh doesn't
end in a vertical cliff at the world edge), and curve-remapped
before scaling to world units.  Lowest pixel anchored at y=0 so
the tank's tracks always sit on flat ground.  Auto-detected at
`resources/heightmap.png` or via the `terrain_heightmap` config
key.  The procedural Perlin path is preserved as a fallback when
no image is configured.

**4x world expansion**

`world_size` 40 m → 160 m so the tank reads as inside a real
landscape rather than perched on a small island.  Both the
image-loaded and procedural Perlin paths now anchor the lowest
sample at y=0, so `base_y` is no longer needed and the tank's
tracks meet flat ground at the world origin without an offset.

**Sand-diffuse texture pipeline**

`Terrain` accepts `sand_path` + `sand_tile_size`.  Auto-detected:
config override > `resources/sand_painted.png` > `resources/sand.png`.

* Pillow load + RGB convert + Y-flip (OpenGL bottom-left origin
  convention).
* Mip chain generated at upload time (no per-frame JIT stall).
* Trilinear filtering (`GL_LINEAR_MIPMAP_LINEAR` / `GL_LINEAR`).
* `GL_REPEAT` wrap on both axes so the tile cleanly stitches.
* Anisotropic filtering at the GPU's reported max (typically 16x
  via `EXT_texture_filter_anisotropic`) -- huge win on grazing-
  angle terrain samples receding into the horizon.
* Tile size default 50 m per repeat (config:
  `terrain_sand_tile_size`).

**Sand-desert painter tool (`cust_tools/paint_sand_desert.py`)**

CLI tool that paints a tileable procedural sand-desert texture.
Output: `resources/sand_painted.png` (colour) + a grayscale
`sand_painted_height.png` companion (the underlying surface
field, suitable for displacement).  Default 8192² (~85 MB
colour, ~23 MB height); supports up to 16384² with `--size`
(`GL_MAX_TEXTURE_SIZE` on every desktop GPU).

Layered build (all FFT-domain tileable so output wraps perfectly
under `GL_REPEAT`):

* Two warp passes (medium + fine) that pre-displace the (xz)
  coordinates so ripple lines bend organically.  Direction-
  rotation was tried but rotating the sine wave breaks the
  integer-cycles-per-tile invariant; the warp gives directional
  variety while staying tileable.
* Primary sin ripples on X at 1.5 cycles/m (~67 cm wavelength)
  -- 75 whole cycles per 50 m tile, exact integer = perfect
  periodicity.
* Cross sin ripples on Y at 0.7 cycles/m (35 cycles / tile,
  also integer).
* Fine grain noise via FFT-low-pass-filtered Gaussian random
  (~2.5 cm features at 8K / 50 m).
* FFT high-pass post-process kills any signal below 12 cycles
  per tile so far mip levels render as a uniform colour.  This
  is what eliminates the visible "rectangle grid" you'd
  otherwise see at distance when GL averages each tile down to a
  pixel.
* Three-stop palette ramp (trough → face → crest) with
  smoothstep transitions.
* Cyclic-gradient fake-shading -- gradient via `np.roll +/-1`
  (not `np.gradient`, which has open boundaries that break
  tileability) dotted with a "light from upper-left" direction.

Per-block-mean variation at far-mip block sizes (128² and
larger) measures < 1 % of dynamic range, so the texture reads
as essentially uniform colour at distance with crisp ripple
detail close-up.

**Detail-displacement layer (`Terrain.detail_image_path`)**

A second grayscale heightmap, tiled at `detail_tile_size` metres
per repeat, sampled and added on top of the macro heights.
Bilinear sampled, min-lifted to zero so it only adds height (no
digging negative under the tank).  Auto-paired with
`sand_painted_height.png` when `sand_painted.png` is the active
diffuse, so colour ripples and geometry ripples line up
perfectly.  Default amplitude 5 cm (config:
`terrain_detail_height_scale`).

**Mesh density bump 257 → 1025 verts/side**

~1 M verts / ~2 M tris on the 160 m terrain.  At 15.6 cm per
quad, the 67 cm primary ripples now get ~4 verts across --
visible as actual geometry.  Static mesh, single draw call --
comfortable for any modern desktop GPU.

### Wireframe override: nearly black (1.75.1)

`mesh.frag` and `imported.frag` wireframe override (the
single-colour fragment emitted when `wireframe_mode == 1`)
changed from light grey `(0.75, 0.75, 0.75)` to nearly black
`(0.02, 0.02, 0.02)`.  Lines now pop against the bright IBL-lit
tank surface instead of vanishing into specular highlights.

### Shaded mode flat colour: hardcoded light grey (1.75.1)

`imported.frag` `use_flat_color` branch now writes
`vec3(0.22, 0.22, 0.22)` directly instead of reading the
theme-tracking `flat_color` uniform.  The 0.22 linear value
lifts to roughly mid/light grey after the Lambert multiply
(~3.3x at full lit) plus the sRGB gamma encode at the end of
main(), giving a neutral matte surface for shape evaluation
regardless of the active theme.  The `flat_color` uniform is
still set per-frame from python (silent no-op) so the path can
be re-enabled by reverting one line if a future change wants
theme tracking back.

### Locked-on button border (1.76.0)

When a button is `active`, the renderer draws four 2-px wheat-
coloured strips along the inside perimeter (`(0.96, 0.87, 0.70,
1.0)`).  Independent of the active fill colour, so it reads as
a clear "toggle is ON" affordance regardless of theme / accent.
Drawn after the fill but before the text/icon so the centred
label still renders on top.

### Light/Ambient slider width centred + narrowed (1.76.0)

`viewer.py` `_layout_widgets` `L_TRACK_W` reduced from 160 to
128 (20 %) and the track is now horizontally centred in the
info panel: `L_TRACK_X = (INFO_PANEL_W - L_TRACK_W) // 2`.
Value-text gap bumped 6 → 10 px so the digits don't crowd the
handle when the slider is at value_max.  Init-time `tw` is
still passed to `add_slider` for consistency, but it's
overridden every layout pass -- a NOTE comment was added there
warning future-me about the override.

### G00_ prop filter (1.75.1)

Extended the non-tank tag filter in `PkgExtractor.list_vehicle_xmls`
(loaders.py ~line 2168) to drop tags containing the `G00_` token
in addition to the existing `StoryMode` substring drop.

`G00_` is the prefix WoT uses for German "vehicle" entries that
are actually props / scenery -- bunkers, pillboxes, coastal-gun
emplacements, the Fireball bomber.  They have a tier and a class
in `list.xml` so they ride the same loader path as real tanks,
but there's no real tank visual behind them and loading one ends
in a broken mesh.  All known entries on a current install:
G00_Bomber_SH, G00_Pillbox_Gun_{10cm,15cm,38cm,7_5cm}_SM24,
G00_Pillbox_Tank_Turret_SM24.

Both tokens are unioned through a single `DROP_TOKENS` tuple +
`any(tok in t for tok in DROP_TOKENS)` so adding the next prop
prefix is a one-line tuple edit.  Single chokepoint = every
downstream consumer (tier tree, load dialog, FBX auto-twin,
ItemList rebuild) sees a clean list with no extra plumbing.

### res_mods button accents (1.75.2)

Tagged the three res_mods-section buttons with theme slots so
they tint per-preset instead of rendering with the default
"untagged" accent:

* **Extract**             -> `c1` (primary action)
* **Open Extract Loc**    -> `c2` (secondary / navigation)
* **Remove from res_mods** -> `c4` (warm-but-distinct warning)

c3 is intentionally skipped on this row -- it's the
text-on-dark slot (wheat cream on TEPY Default) and reads as
near-white when used as a button fill, so the Remove button
took the c4 slot instead (burnt yellow on default; warm
tertiary on every other preset).

Applied at `_build_ui` time alongside the existing
`btn.accent_color = _theme.cN()` / `btn.theme_slot = 'cN'`
pattern, so `_apply_theme_live` re-tints the trio correctly
when the user switches preset.  No changes to layout,
click-handlers, or hit-tests.

### G00_ prop filter (1.75.1)

Extended the non-tank tag filter in `PkgExtractor.list_vehicle_xmls`
(loaders.py ~line 2168) to drop tags containing the `G00_` token
in addition to the existing `StoryMode` substring drop.

`G00_` is the prefix WoT uses for German "vehicle" entries that
are actually props / scenery -- bunkers, pillboxes, coastal-gun
emplacements, the Fireball bomber.  They have a tier and a class
in `list.xml` so they ride the same loader path as real tanks,
but there's no real tank visual behind them and loading one ends
in a broken mesh.  All known entries on a current install:
G00_Bomber_SH, G00_Pillbox_Gun_{10cm,15cm,38cm,7_5cm}_SM24,
G00_Pillbox_Tank_Turret_SM24.

Both tokens are unioned through a single `DROP_TOKENS` tuple +
`any(tok in t for tok in DROP_TOKENS)` so adding the next prop
prefix is a one-line tuple edit.  Single chokepoint = every
downstream consumer (tier tree, load dialog, FBX auto-twin,
ItemList rebuild) sees a clean list with no extra plumbing.

### Wireframe override: nearly black (1.75.1)

`mesh.frag` and `imported.frag` wireframe override (the
single-colour fragment emitted when `wireframe_mode == 1`)
changed from light grey `(0.75, 0.75, 0.75)` to nearly black
`(0.02, 0.02, 0.02)`.  Lines now pop against the bright IBL-lit
tank surface instead of vanishing into specular highlights.

### Shaded mode flat colour: hardcoded light grey (1.75.1)

`imported.frag` `use_flat_color` branch now writes
`vec3(0.22, 0.22, 0.22)` directly instead of reading the
theme-tracking `flat_color` uniform.  The 0.22 linear value
lifts to roughly mid/light grey after the Lambert multiply
(~3.3x at full lit) plus the sRGB gamma encode at the end of
main(), giving a neutral matte surface for shape evaluation
regardless of the active theme.  The `flat_color` uniform is
still set per-frame from python (silent no-op) so the path can
be re-enabled by reverting one line if a future change wants
theme tracking back.

---

## 2026-05-07

A long session covering the full theme-system rewrite, the new
res_mods extract workflow, crash-damage shader support, window-
management polish (maximize-after-splash, F11), and a Debug
collapsible group on the right panel.  Every new user-facing
string in this session ships with English msgids in
`locale/en/LC_MESSAGES/tepy.po` and gets compiled to all 21
locale `.mo` catalogs (translations for non-English languages
fall back to English msgids until the seed-translations table
is updated).

### StoryMode tank filter (1.72.3 → 1.72.4)

`PkgExtractor.list_vehicle_xmls` now drops every tank tag whose
name contains `StoryMode` (substring match, case-sensitive).
Real install survey: 21 variants on a current WoT install, in
suffixes `_StoryMode`, `_StoryModeStealth`, `_StoryModeHard`,
`_StoryMode_3_4`.  Single chokepoint = every downstream consumer
(tier tree, load dialog, FBX auto-twin) sees a clean list with
no extra plumbing.

Maximize-after-splash hardening (1.72.4): `_go_maximized` now
goes through pygame's SDL2-backed `Window.maximize()` rather
than Win32 `ShowWindow(SW_MAXIMIZE)` directly, then drains up
to 8 `pygame.event.pump()` cycles harvesting any `VIDEORESIZE`
that lands.  The actual size carried by the resize event drives
an explicit `set_mode` + `_on_resize`, so the first frame after
maximize lays panels out for the real client area instead of
the pre-maximize 1280×720.  Earlier revisions read
`pygame.display.get_surface().get_size()` which lagged a frame
behind the OS resize and produced stale dimensions.

### Right-panel Debug collapsible group (1.72.0 → 1.72.2)

All right-panel widgets (smoke / fire / normals sliders +
PerVtx / Debug checkboxes) now live under one collapsible
section header titled **Debug**.  Same chevron + click-zone
treatment as the left-panel section headers — `▼ Debug` when
expanded, `▶ Debug` when collapsed.  Click anywhere along the
header bar to toggle.  Persisted under
`section_collapsed['Debug']` in `tankExporterPy.json`.

When collapsed, `RIGHT_CONTROLS_H` shrinks to `HEADER_BAR_H`
(~26 px); the panel BG follows automatically because
`right_controls_rect` reads `RIGHT_CONTROLS_H` after
`_layout_widgets` runs.  GL viewport reclaims the freed space
so the tank renders into a nearly-full-window viewport with
the section folded.

`_toggle_section_collapsed` (left + right) now calls full
`_on_resize(self.width, self.height)` instead of just
`_layout_widgets()`.  Without this, the tank-list tree height
+ tab anchor + thumbnail position stayed at pre-collapse
dimensions; clicking the chevron didn't grow the tree to fill
the freed space.

Files: `tankExporterPy/viewer.py`, `tankExporterPy/loaders.py`.

### Window management: console minimize, maximize after splash, F11 (1.70.0 → 1.71.1)

Three startup-polish items:

1. **Console window minimised at launch.**  New module
   `tankExporterPy/win_console.py` exposes `minimize_console()`
   / `hide_console()` / `restore_console()` — all soft-fail on
   non-Windows / no-console / ctypes failure.  `tankExporterPy.py`
   calls `minimize_console()` as its first action so the cmd
   window briefly flashes and then ducks to the taskbar.
   Stdout still goes to the console; click the taskbar entry
   to read it.

2. **Maximize after splash, not fullscreen-exclusive.**  Splash
   renders in a normal decorated 1280×720 client window so it
   stays geometrically centred in a known viewport; once init
   completes, `_go_maximized()` (via SDL2 `Window.maximize()`)
   resizes to fill the screen while keeping title bar +
   taskbar + ALT-TAB working.  Different from
   `pygame.FULLSCREEN`: app stays a normal window citizen.

3. **F11 toggles maximized ↔ windowed.**  Standard
   convention.  Handler in `handle_input` calls
   `_toggle_fullscreen()` which dispatches to `_go_maximized`
   / `_go_windowed`.  SDL2 remembers the pre-maximize
   dimensions so F11 from maximized restores to exactly where
   the user had the window.

Earlier revision (1.70.0) used Win32 `SetWindowLongW` to strip
the title bar during splash; that left pygame's GL viewport
mismatched against the now-larger client area and the splash
appeared offset.  Reverted in 1.71.0 — splash uses the
decorated window throughout, maximize comes after.

Files: `tankExporterPy/viewer.py`, `tankExporterPy/win_console.py`,
`tankExporterPy.py`.

### Wireframe / Shaded button refactor + flat-color Shaded mode (1.69.0 → 1.69.2)

Layout swap:

| Group | Was | Now |
| --- | --- | --- |
| UI | Grid · Axes · Light / Orbit · Skybox · **Wireframe** / **Terrain** | Grid · Axes · Light / Orbit · Skybox · **Terrain** |
| Model | Meshes · Flip · Compare | Meshes · Flip · Compare / **Wireframe** · **Shaded** |

Wireframe and the new **Shaded** toggle moved to the Model
group (theme `c1`) since they mutate how the model is rendered
rather than the viewport decoration.  Terrain claims
Wireframe's old slot in the UI group so the row still lays out
as a clean 3-cell row.

**Shaded toggle** flips the WoT mesh path to the same simple
diffuse + bump shader the FBX import path uses — no PBR / IBL
/ GMM / damage layer / armour tint.  Plus a flat-colour
override: when on, the diffuse texture is ignored entirely and
the material is rendered in `theme.c1() × 0.75` (burnt orange
on TEPY Default, theme-aware on every other preset).  Normal
map still applies, so surface detail reads through bump
shading on the flat color.

GLSL: new uniforms `use_flat_color`, `flat_color` in
`shaders/imported.frag`; per-frame setup in `viewer.py`'s
global-uniform stage when `shaded_mode` is True.

Files: `shaders/imported.frag`, `tankExporterPy/viewer.py`,
`tankExporterPy/locale/*/LC_MESSAGES/tepy.{po,mo}`.

### Cross-tab tank deselection on load (1.68.5)

Each tier tab's `UITreeView` carries its own `selected_node`,
so loading a tank on tier-7 left tier-5's "loaded" highlight
still glowing.  New helper
`Viewer._clear_selection_in_other_trees()` walks the cached
per-tier tree array and nulls `selected_node` on every tree
except the currently-active one.  Called inside `_on_load`
right before `_load_tank_with_options` fires — only on
successful Load click, so cancelled modals leave the previous
load's highlight untouched.

Files: `tankExporterPy/viewer.py`.

### Fire billboard recentered on HP_Fire (1.68.4)

Earlier revisions used `_CORNER_OFFSETS_BOTTOM` (bottom-center
anchored) for the fire billboard so flames "rose out of the
deck", but WoT authors HP_Fire hardpoints at the centroid of
where the artist wanted the flame.  Bottom-anchoring shifted
every flame upward by `size × 0.5`.  Switched to centered
offsets (`_CORNER_OFFSETS`) so the quad's centroid coincides
with the emitter; yellow debug outline (Show HP) updated to
match.

Files: `tankExporterPy/particles.py`, `tankExporterPy/viewer.py`.

### Crash damage shader -- PBS_tank_crash.fx support (1.68.0 → 1.68.3)

Damaged tanks render with the PBR-tank path PLUS a damage-
layer multiply, replicating WoT's `PBS_tank_crash.fx` look.

**Discovery.** WoT ships only compiled `.fxo` (DX11 bytecode);
no source `.fx`.  Surveyed 52 real `/crash/` visual_processed
files — every PBS_tank_crash material declares the SAME shared
detail tile path:

```
crashTileMap     vehicles/russian/Tank_detail/crash_tile.dds
g_crashUVTiling  Vector4 (typical 2,2,0,0)
crash_coefficient float (24/52 set explicitly; 28/52 fall
                  through to the shader's default ~1.0)
```

The base AM / ANM / AO / GMM textures on `/crash/` materials
match the `/normal/` variant exactly — damage is purely a
shader pass, not a separate texture set.

**Parser.** `VisualLoader._extract_material_xml` captures
`crashTileMap`, `g_crashUVTiling`, `crash_coefficient`, and
the `<fx>` filename into the per-material dict.

**Mesh.** New fields `crash_tile_tex_id`, `crash_tile_path`,
`crash_uv_tiling`, `crash_coefficient`, `fx`.  Crash tile
texture is uploaded once per session via `_shared_tex_cache`
(it's literally the same file across every crashed tank).

**GLSL** (`shaders/mesh.frag`).  Three RGB channels of
`crash_tile.dds` carry three grayscale damage variants.  A
~0.6 m world-space hash picks one channel per region, and a
per-load `crash_channel_offset` uniform rotates which channel
each region picks — so reloading the same damaged tank cycles
through all three variants.  Blend is multiplicative AM ×
mask (no colour from the tile, just darkening), capped by
`crash_coefficient`:

```glsl
mask = damage_rgb[chan]                        // pick channel
effective = mix(1.0, mask, crash_coefficient)  // attenuate
diff_samp.rgb *= effective                     // multiply AM
metallic *= (1 - dirt) * 0.85;  gloss *= ...   // dirty surfaces lose chrome
```

Earlier revisions (1.68.0, 1.68.1) used full-RGB multiply and
alpha-mask blend respectively before settling on the
single-channel grayscale path.

`crashTileMap` lives in `particles.pkg`; current `TheItemList`
rebuild excludes that pkg, so resolution falls back to scan-
fallback on first load (slow once, cached after).  Adding
`particles.pkg` to the ItemList scan list is on the deferred
list.

Files: `tankExporterPy/loaders.py`, `tankExporterPy/mesh.py`,
`tankExporterPy/viewer.py`, `shaders/mesh.frag`.

### res_mods workflow: Extract / Open / Remove + Load-from-res_mods checkbox (1.67.0 → 1.67.3)

New top-of-left-panel `res_mods` section -- three buttons that
operate on the user's res_mods tree, plus a persistent
checkbox in the load dialog.

| Button | What it does |
| --- | --- |
| **Extract** | Tk dialog with checkboxes for Hull / Chassis / Turret / Gun + an "Extract textures" toggle.  Writes the chosen `.primitives_processed` + `.visual_processed` (+ optional `.model`) into `<res_mods>/vehicles/<nation>/<tank>/<variant>/lod0/`.  Damaged-variant tanks redirect `/normal/` → `/crash/`.  **Textures are never overwritten** — existing files are logged as `[keep tex]` and skipped. |
| **Open Extract Loc** | Opens Explorer at the variant subfolder Extract just wrote into (`.../normal/` or `.../crash/`).  Falls back to the whole-tank dir if only the OTHER variant is present.  When nothing's been extracted, asks via Tk yes/no whether to run Extract now. |
| **Remove from res_mods** | Deletes the tank's whole-tank res_mods folder (both variants).  Strong confirmation -- a Tk dialog requires the user to *type* the tank's xml basename to enable the Delete button.  Counts files first, prunes empty parent dirs (`vehicles/<nation>/`) after. |

`Viewer._stash_extract_paths` runs at the end of every
`load_vehicle` and stamps the active MeshSet with:

```
extract_tank_root    <res_mods>/vehicles/<nation>/<tank>/
extract_variant_dir  <tank_root>/<crash|normal>/
extract_damaged      bool
```

Path is derived from the FIRST loaded mesh's `primitives_zip`
(literal pkg path the loader actually consumed) so Open
Extract Loc lands exactly where Extract wrote, without
re-deriving from xml basename + nation parsing.

`_open_in_explorer` uses `os.startfile()` on Windows — the
canonical "open this directory in Explorer" path.  Replaces
the earlier `subprocess.Popen(['explorer', path])` which had a
known quirk of falling back to Documents on any path-parsing
hiccup.

**Section-header collapse chevrons** (left + right panels).
`UIManager.add_panel_label` extended with `section_key` +
`click_w` parameters; section headers render as `▼ <name>`
(expanded) / `▶ <name>` (collapsed) and become wide click
zones spanning the full panel width.  Click handler in
`handle_input` calls `_toggle_section_collapsed(key)`,
persisted under `section_collapsed` in config.  Section keys
are stable English strings (`res_mods` / `UI` / `Model` / `IO`
/ `Tools` / `Debug`) so a runtime language switch doesn't
desync the persisted state from the displayed labels.

**Persistent "Load from res_mods" checkbox** added to
`UILoadTankDialog` between the title and the first skin
block.  Defaults to checked.  When unchecked, the loader
passes `prefer_res_mods=False` to `load_vehicle`, which blanks
the `res_mods_root` so `VisualLoader.resolve_hd_path` skips
its disk-walk and reads textures + visuals straight from pkg.

**FBX-import → PKG twin auto-load** is hard-wired to
`prefer_res_mods=False` per project policy: imported FBX
tanks are always paired with pkg textures, never res_mods.

**`start.bat` → `launch_skip_deps.bat` (1.67.3).**  Renamed
because the obvious-sounding name was steering new users away
from `go.bat`'s install-check path.  Companion bats list in
README updated; tankExporterPy.py docstring history line
updated.

Files: `tankExporterPy/viewer.py`, `tankExporterPy/ui.py`,
`tankExporterPy/loaders.py`, `tankExporterPy/config.py`,
`tankExporterPy/locale/*/LC_MESSAGES/tepy.{po,mo}` (all 21),
`launch_skip_deps.bat` (renamed), `tankExporterPy.py`,
`README.md`.

### Theme system: live preview + barb-arrow chevrons (1.63.0 → 1.66.0)

The Phase 1 motif system from yesterday gets renamed to
**theme** (user pick), gains a **fourth** named accent slot,
swaps to a swatch-preview picker with live update (no
restart), and gets new collapse-affordance chevrons.

**Module renamed.**  `tankExporterPy/motif.py` →
`tankExporterPy/theme.py` (via `git mv` -- history preserved).
`Theme` dataclass replaces the old `Motif`; the public API is
`set_active(name)` / `get_active()` / `get_active_name()` /
`set_bg(rgba)` plus per-colour shortcuts `c1()` / `c2()` /
`c3()` / `c4()` / `bg()`.  Backward-compat: `Viewer.__init__`
reads `theme` from config, falls back to legacy `motif` key,
falls back to `DEFAULT_NAME`.  Same fallback chain on
`theme_bg` ← `motif_bg`.

**Fourth accent slot c4 (burnt yellow).**  Save Prim wired to
`_theme.c4()` -- restores the original burnt-yellow Save Prim
accent that was lost when the Phase 1 work folded everything
into c1/c2.  Every preset now ships its own c4: Solarized
orange, Dracula purple, Nord aurora purple, etc.

**Swatch-preview picker** (`Viewer._on_theme_clicked`).  Tk
Toplevel with a scrollable list of preset rows; each row
shows five coloured squares (c1 / c2 / c3 / c4 / bg) followed
by the preset name.  Click a row → highlights it.
**Set** commits and applies live; **Cancel** closes.
Mouse-wheel scrolls.  `(current)` tag on the active preset's
row at open.

**Live re-tint, no restart.**  New
`Viewer._apply_theme_live(name)` flips the active theme +
updates `glClearColor` + walks every button reassigning
`accent_color` from its tagged `theme_slot` (`c1` / `c2` /
`c3` / `c4`).  Works because the renderer reads
`btn.accent_color` and `glClearColor` afresh every frame.
Each accent button is tagged once at `_build_ui` so this is a
constant-time lookup.

**Collapse-bar chevron upgrade (1.66.0).**  The console
collapse and info-spine collapse glyphs swap from BLACK
TRIANGLE / Wingdings to wide-headed barb arrows from the
Supplemental Arrows-C block:

| Affordance | Codepoint | Font |
| --- | --- | --- |
| console expand   | `🡩` U+1F869 | Segoe UI Symbol 14 |
| console collapse | `🡫` U+1F86B | Segoe UI Symbol 14 |
| spine expand     | `🡨` U+1F868 | Segoe UI Symbol 16 |
| spine collapse   | `🡪` U+1F86A | Segoe UI Symbol 16 |

Segoe UI proper doesn't carry U+1F8xx; Segoe UI **Symbol**
does and ships with every Windows 8.1+ install.  Wide-headed
barbs read heavier than the BLACK-TRIANGLE glyphs they
replaced (▲ / ▼ / ◀ / ▶) and match the section-header
chevrons used by the new collapse system.

Files: `tankExporterPy/theme.py` (renamed from `motif.py`),
`tankExporterPy/viewer.py`, `tankExporterPy/ui.py`,
`tankExporterPy/config.py`,
`tankExporterPy/locale/*/LC_MESSAGES/tepy.{po,mo}` (all 21).

---

## 2026-05-06

### Phase 1: motif system + curated presets + 110% font scale (1.63.0)

Foundation for the upcoming theme work.  Three named accent
colours + a free-form background drive the entire UI palette:

* **`motif.c1`** -- primary / warm accent.  Used by Export,
  Save Prim, the Model-tool buttons.
* **`motif.c2`** -- secondary / cool accent.  Used by Import
  and the UI display toggles.
* **`motif.c3`** -- text-on-dark accent.  Console pane lines
  default to this colour.
* **`motif.bg`** -- form / clear-colour.  Free-form via the
  upcoming colour picker.

Ten curated presets sourced from popular IDE / editor schemes:

```
TEPY Default   (the original burnt-orange / olive / wheat)
Solarized Dark
Dracula
Nord
Gruvbox Dark
Tokyo Night
Monokai
Catppuccin Mocha
One Dark
Material Dark
```

Wiring:

* `Viewer.__init__` calls `motif.set_active(...)` from the
  persisted `motif` key in `tankExporterPy.json` BEFORE any
  widget gets built.
* `glClearColor(*motif.bg())` replaces the hardcoded clear
  colour.
* Every `accent_color` assignment in `_build_ui` now reads
  `motif.c1()` / `motif.c2()` instead of a literal RGBA.
* `UIConsole.add_line` defaults its text colour to `motif.c3()`
  instead of the hardcoded `(220, 222, 230)` light grey.

UI:

* New **`Motif`** button in the IO group.  Tk dropdown picker
  listing every preset; persists to config as `motif`;
  restart-to-apply (next `_build_ui` reads the new colours).
  Translated for all 21 languages (`Motiv` / `Тема` / `テーマ`
  / `主题` / etc.).

Font scaling:

* Module-level **`FONT_SCALE = 1.1`** in `ui.py` with helper
  `_scaled_font(family, size, bold)` -- every `pygame.font.SysFont`
  call site inside `ui.py` routes through it.  10% upsize on
  the button labels, slider labels, value text, the spine
  chevron, the icon glyphs.  Widget heights unchanged so a
  larger scale than ~1.15 may overflow -- tweak `FONT_SCALE`
  if a tester pushes for bigger / smaller.

Phase 2-5 (deferred):

* In-app draggable scrolling motif picker with c1 / c2 / c3
  swatch previews per preset (replaces the Tk dropdown).
* Free-form bg colour picker.
* Mid-session texture rebuild so motif changes apply without
  a restart.
* General "drag any popup" infrastructure across both Tk
  dialogs and in-app pygame dialogs.

Files: new `tankExporterPy/motif.py`; edits to
`tankExporterPy/viewer.py`, `tankExporterPy/ui.py`,
`tankExporterPy/config.py`,
`tankExporterPy/locale/*/LC_MESSAGES/tepy.{po,mo}` (all 21),
`cust_tools/seed_locale_translations.py`.

### Spine icon: Wingdings 't'/'u' (Segoe UI tofu fix) (1.62.2)

Tester reported the spine glyph rendering as a tofu rectangle.
Some Windows Segoe UI installs lack the BLACK LEFT/RIGHT-
POINTING TRIANGLE codepoints (U+25C0 / U+25B6) at the
supplementary-symbol range, so v1.61.1's `◀` / `▶` showed
empty boxes.

Switched to **Wingdings** -- Microsoft's universally-shipped
symbol font (ships with every Windows install since 3.1).
Wingdings maps ASCII codepoints to fixed icon glyphs:

  - `'t'` -- expanded state (panel open)
  - `'u'` -- collapsed state (panel closed)

Easy to swap to other Wingdings letters if these aren't right.
Bottom-console chevron still uses Segoe UI ▲ / ▼ (no tofu
report there yet); if it lands on the same machine we'll move
it too.

Files: `tankExporterPy/ui.py`.

### README: tkinter-required documentation (1.62.1)

A tester hit the "can not open file picker" failure mode (which
v1.57.4 added an in-app diagnostic for) -- root cause was the
python.org installer's *optional* "tcl/tk and IDLE" component.
README now makes this prominent so future installs avoid the
trap entirely:

* **Requirements** section gets a top-level Tkinter bullet
  pointing at the new `Tkinter is required` subsection.
* New **Step 3: Install Python** between "Where to put it" and
  "Run it" with a warning callout to check the `tcl/tk and
  IDLE` box during install.
* New **Tkinter is required** subsection after the package list
  with: a one-line probe to verify the install
  (`py -3 -c "import tkinter; print(tkinter.TkVersion)"`),
  the Settings -> Apps -> Modify repair walkthrough for
  installs that already missed it, and a note that TEPY
  surfaces the failure in-app via the v1.57.4 startup probe so
  testers don't need cmd-window detective work.

Files: `README.md`.

### Folder icon on Set Paths + per-button icon support (1.62.0)

`Set Paths` now reads as a file-picker button at a glance --
folder glyph (📁) left of the label.  Generic per-button icon
support so future "this opens a thing" buttons can adopt the
same pattern without each rolling its own.

Plumbing:

* New `UIButton.icon_tex` + `icon_w/h` fields (None default).
* New `UIManager._make_icon_tex(glyph)` -- builds the texture
  with **Segoe UI Symbol** (the button's own Calibri Bold lacks
  the supplementary-plane folder glyphs).  Stays at point size
  14 to balance Calibri 13's text x-height.
* `UIManager.set_button_icon(btn, glyph)` -- public attach API,
  frees any prior icon's GL texture before replacing.
* Button render path: when both icon AND text are present, the
  pair is centred as a unit (icon + 4-px gap + text) instead of
  centring just the text.  Icon-only and text-only buttons keep
  their existing behaviour.
* `cleanup()` frees `icon_tex` alongside `text_tex`.

Wiring:

* `Set Paths` calls `set_button_icon(..., '📁')` after
  construction -- the only button using the new API for now.
  Adding a gear icon to a future Settings button would be:
  `self.ui.set_button_icon(btn, '⚙')`.

Files: `tankExporterPy/ui.py`, `tankExporterPy/viewer.py`.

### Left-spine icon: solid filled triangles (matching console) (1.61.1)

`UIManager._ensure_chevron` rendered the info-panel spine glyph
as `<` / `>` (ASCII line characters).  v1.56.1 had already
upgraded the bottom-console chevron to solid triangles
(`▲` / `▼`).  The two toggles now read as a coherent family:

| Toggle | Expanded | Collapsed | Glyph type |
| --- | --- | --- | --- |
| Info panel spine | `◀` | `▶` | solid filled triangles |
| Bottom console   | `▼` | `▲` | solid filled triangles |

Direction logic unchanged -- arrow points where the click sends
the panel.

Files: `tankExporterPy/ui.py`.

### Split UI section: UI toggles (olive) + Model tools (orange) (1.61.0)

The single `UI` section in the left panel mixed display
toggles (Grid / Axes / Skybox / Wireframe / ...) with
model-tool actions (Meshes / Flip / Compare).  Two distinct
roles, same neutral grey colour -- hard to scan.

Split into two sections:

* **UI** (display toggles -- 7 buttons across 3 rows):
  Grid / Axes / Light / Orbit / Skybox / Wireframe / Terrain.
  Olive accent in the IDLE state.  When a toggle is ON, the
  existing global burnt-orange "this is on" colour still wins,
  so active state still pops.
* **Model** (model-tool actions -- 3 buttons in 1 row):
  Meshes / Flip / Compare.  Burnt-orange accent.

`Model` translated for every supported language: `Modèle` /
`Modell` / `Модель` / `モデル` / `모델` / `模型` / etc.

Files: `tankExporterPy/viewer.py`,
`tankExporterPy/locale/*/LC_MESSAGES/tepy.{po,mo}` (all 21
languages), `cust_tools/seed_locale_translations.py`.

### Save Prim accent: burnt yellow (1.60.3)

Continues the IO-section warm-palette trio.  `Save Prim` (the
WoT-native `.primitives_processed` writer) gets `accent_color =
(0.68, 0.52, 0.10)` -- a burnt yellow that slots between
Export's burnt orange and Import's olive so the three IO
actions read as a related family at a glance.

| Button | Accent |
| --- | --- |
| Export    | burnt orange (0.65, 0.32, 0.10) |
| Save Prim | burnt yellow (0.68, 0.52, 0.10) |
| Import    | olive        (0.42, 0.45, 0.18) |

Files: `tankExporterPy/viewer.py`.

### Export / Import on one row at equal widths (1.60.2)

`v1.59.0` stacked Export and Import on separate full-width rows
because the 3-col / 70-px grid couldn't divide evenly between
two buttons.  This pass extends the layout packer to recognise
a **list** entry in `button_groups` as "pack these evenly on a
single row" -- each gets `(full_row_width - gap) / 2` pixels,
guaranteed identical.

```python
(_('IO'), [
    (_('Set Paths'), 0, 3),
    [_('Export'), _('Import')],   # <-- list = even-split row
    (_('Save Prim'), 0, 3),
    (_('Language'),  0, 3),
]),
```

Single-button (3-tuple) entries continue to work unchanged --
the type check is `isinstance(item, list)`.  Side-effect: the
packer can now do 3-or-more-button even-split rows for free
(e.g. for a future `[Save, Apply, Cancel]` toolbar row).

Burnt-orange Export and olive Import accents from v1.59.0 carry
through unchanged; they're per-button, not per-row.

Files: `tankExporterPy/viewer.py`.

### Suppress phantom animation block on every export format (1.60.1)

Tester saw an unwanted Take001 animation track on the exported
FBX (and presumably similar phantoms on GLB / GLTF).  Blender's
exporters write an animation block by default even when no mesh
has any animation data attached -- 3ds Max then picks it up as a
static animation channel the user has to manually delete.

Killed across every output format:

* **FBX**:  `bake_anim=False` on `export_scene.fbx`
* **GLB**:  `export_animations=False` on `export_scene.gltf`
* **GLTF**: `export_animations=False` on `export_scene.gltf`

OBJ has no animation concept so no change there.

TEPY only exports static geometry; we never want an animation
block on any output.  Tank meshes' transforms are baked into
their world matrices at load time; rigging / bone weights ride
through the per-vertex `WoTBoneIdx` / `WoTBoneWeight` color
attributes (decoded by the importer for round-trip).

Files: `tankExporterPy/exporters/_blender_runner.py`.

### FBX export: vertex normals visible in 3ds Max 2018 (1.60.0)

Tester reported "3ds Max isn't finding the vertex normals" on
exports.  Two causes in the Blender FBX exporter call:

1. **`mesh_smooth_type='EDGE'`** writes per-edge smoothing data,
   which Blender + Maya ingest but 3ds Max ignores.  Result:
   Max shows flat-shaded geometry with no usable normals in
   the editable-mesh modifier.  **Switched to `'FACE'`** --
   per-face smoothing groups, the form Max expects.

2. **`use_custom_normals` was missing** (default False on
   Blender 2.8x and some 3.x builds).  Without it, the FBX
   exporter discards the custom split normals
   `_build_mesh_object` set via
   `me.normals_split_custom_set_from_vertices(...)` and
   recomputes from smoothing groups -- so WoT-authored creases
   were silently wiped at export time.  **Set `=True`** so the
   normals the WoT artists painted survive the round trip.

Wrapped in try/except so an ancient Blender that doesn't
recognise the kwarg still exports -- it retries without
`use_custom_normals`, just losing custom-normal preservation
on that run.

Files: `tankExporterPy/exporters/_blender_runner.py`.

### Reorg Import / Export -- equal width, swapped order, accent colours (1.59.0)

The IO group used to render Import as a 2-cell-wide button next
to a 1-cell-wide Export, with Import on the left.  Both visually
similar to the neutral-grey toggles around them.

Now:
* Both buttons span the full 3-cell row width -- guaranteed
  identical size.
* Export sits ABOVE Import (flipped from the previous order).
* Per-button accent colour drives visual identity:
    - **Export** -> burnt orange (0.65, 0.32, 0.10)
                    "this exits the app's data boundary"
    - **Import** -> olive       (0.42, 0.45, 0.18)
                    "this brings data IN"
  Hover state brightens the accent ~25%.  Active toggles still
  use the existing global burnt-orange "this is on" colour --
  accents only override the IDLE state.

Implementation: new `UIButton.accent_color` field (None = default
neutral grey).  Render path checks it before falling back to the
old behaviour.  Other buttons unaffected; only Export and Import
opt in via assignments in `_build_ui`.

Files: `tankExporterPy/ui.py`, `tankExporterPy/viewer.py`.

### Tank-tree clicks dropped while a load is in flight (1.58.0)

Tank loads take 1-7 seconds; previously the user could click a
second tank during the load and queue up confused state -- the
in-flight load was already mutating MeshSet, the second click
restarted the same path, and intermediate frames showed
half-decoded geometry.

New `self._tank_loading` flag, set the moment a Load Tank
dialog's `_on_load` callback fires, cleared after the load
returns (try/finally so an exception still releases the gate).
While set, `_on_tree_tank_selected` early-returns with a status-
strip note ("Load in progress -- click ignored") instead of
processing the click.

Doesn't touch the dialog itself -- that's the active flow the
user just initiated.  Only blocks NEW selections from the tree
while one is in flight.  Hover thumbnails still update so the
user can preview "what would I click next" without committing.

Files: `tankExporterPy/viewer.py`.

### Startup tkinter probe -> in-app warning (1.57.4)

A tester reported "can not open file picker" and the source
turned out to be `import tkinter` itself failing -- the python.org
installer makes "tcl/tk and IDLE" an OPTIONAL component, and if
it's unchecked at install time, you end up with a Python that
can't open any file dialog.

Previously the failure printed to stdout only -- a tester running
TEPY by double-clicking `go.bat` rarely reads the cmd window
behind the pygame surface, so they just saw "Set Paths / Import /
Export buttons do nothing."

`Viewer.__init__` now probes `import tkinter` at the same time as
the PkgExtractor / ItemList readiness logging.  If it fails, four
console-pane lines explain the diagnosis and the fix:

    tkinter missing from this Python install -- Set Paths /
        Import / Export / Language pickers WILL NOT OPEN.
    underlying error: <exception>
    Fix: re-run the python.org installer, choose Modify, and
        check the 'tcl/tk and IDLE' option.
    Or in Windows Settings > Apps, find Python > Modify >
        check 'tcl/tk and IDLE' > Repair.

Files: `tankExporterPy/viewer.py`.

### Set Paths picker hardening + error logging (1.57.3)

`UIPathsDialog._pick_path` used to swallow any Tk failure
silently -- if the dialog refused to open (Tcl init error, weird
DPI scaling, missing `tcl/` install, ...) the user would just see
"nothing happened" with no diagnostic.

Hardened in three ways:

1. **Wrapped Tk calls in try/except** that prints the failure to
   stdout (`[ui] file picker failed: <exc>`).  Future
   "picker doesn't work" reports come with an actual cause.
2. **Forced focus / topmost** -- `update_idletasks()` + `lift()` +
   `focus_force()` + the explicit `parent=root` argument fix the
   common-on-Windows symptom where the dialog opens BEHIND the
   pygame window and looks like nothing happened.
3. **Sanitised `initialdir`** -- only passed when the path
   actually exists on disk.  Older Tk on Windows refused to
   open the dialog at all when given a stale path.

Files: `tankExporterPy/ui.py`.

### go.bat: --user fallback + per-package failure diagnostics (1.57.2)

Hardening on top of 1.57.1's `py -3` switch.  When pip's default
install fails (the most common Windows cause: a Python under
`C:\Program Files\` whose `site-packages\` the current user can't
write to), `go.bat` now retries with `--user` so the packages
land under `%APPDATA%\Python\...`.  If that ALSO fails, the
script dumps:

  * the resolved `sys.executable` and `sys.path` so you can see
    which Python pip was running against,
  * a per-package `find_spec` probe so you see exactly which
    of `pygame / OpenGL / numpy / PIL` is missing,
  * a copy-paste manual fallback command.

Previously a partial-failure install (e.g. numpy didn't pick up a
cp313 wheel) would show "imports still failing" with no detail
and the user was stuck rerunning uninstall/reinstall blind.

Files: `go.bat`.

### Bat-file launcher: switch from `python` to `py -3` (1.57.1)

Bug: on Windows 10/11 the `python` command on PATH usually
resolves to `C:\Users\<user>\AppData\Local\Microsoft\WindowsApps\
python.exe` -- a Windows-Store redirector stub that opens the
Microsoft Store rather than running an actual Python.  Even users
with a real python.org install have the stub ahead of it on PATH
(it's added by Windows itself).  Result: `go.bat`'s import probe
ran against the stub, failed (no deps in the stub's env even when
it does run), and the install path then `pip install`-ed against
the wrong interpreter (or against nothing if the stub redirected
to the Store).  Net effect: dependencies "would not install."

Fix: every bat file now prefers `py -3` over `python`.  The `py`
launcher is bundled with the python.org installer, ignores the
WindowsApps stub by design, and routes to the user's real
Python 3.x install.  Falls back to bare `python` only when `py`
itself isn't found (rare; some Anaconda installs).  go.bat also
prints the resolved interpreter path on launch so future
"is it picking the right Python?" questions answer themselves at
a glance:

    Using Python: C:\Users\<user>\AppData\Local\Programs\Python\
                  Python313\python.exe

Files: `go.bat`, `start.bat`, `uninstall.bat`, `reinstall.bat`.

### Procedural terrain (tank stops floating in space) (1.57.0)

Tanks were rendering against a skybox with nothing under them.
Added a procedural ground mesh -- Perlin fractal Brownian motion
heightmap, smooth-shaded mesh, three-band height + slope colour
blend (grass / dirt / rock).  Toggle is the new **Terrain**
button in the left-panel UI section, off by default so a clean
tank screenshot still works.

Why Perlin fBm specifically: diamond-square (the textbook
"implement-in-30-minutes" alternative) produces visible diagonal
creases at quad boundaries from its midpoint-displacement
structure, and is locked to a 2^n+1 grid.  fBm Perlin has neither
problem and is the algorithm every modern terrain generator
(Skyrim, Elite Dangerous's planet engine, No Man's Sky, World
Machine, Gaea) is built on.  Cost is about 30 extra lines of
numpy.

New module: `tankExporterPy/terrain.py`
* `Terrain` class -- heightmap generation + indexed-triangle mesh
  build + smooth-shaded vertex normals + render.  Default 257-
  vertex grid (66 049 verts, ~131 K triangles) over 40 m of
  world; configurable via constructor args.
* `_make_heightmap` -- standalone Perlin fBm generator that
  vectorises every cell across numpy arrays.  ~50 ms for the
  default size on a typical CPU; runs once at viewer construction.
* `_vertex_normals` -- smooth normals via face-area-weighted
  averaging (no normalisation of face contributions before
  summing; gives noticeably better creases on irregular slopes).

New shader pair: `shaders/terrain.{vert,frag}` + `TerrainShader`
class in `shaders.py`.
* Vertex passes world position + normal + raw Y to the fragment.
* Fragment does a two-segment height blend (grass -> dirt over
  the lower band, dirt -> rock over the upper) plus a slope
  override that biases steep faces toward rock.  Single
  directional light, generous ambient (0.35) so cliffs don't
  go to coal-black.

Localised: `Terrain` is wired through `_()` like every other
button, with translations seeded for all 20 supported languages
(Gelände / 地形 / Местность / Terreno / ...).

Persistence: `show_terrain` saves to `tankExporterPy.json` so
the user's last preference survives across sessions.

Files: new `tankExporterPy/terrain.py`,
`shaders/terrain.{vert,frag}`.  Edits to
`tankExporterPy/shaders.py`, `tankExporterPy/viewer.py`,
`tankExporterPy/locale/<lang>/LC_MESSAGES/tepy.{po,mo}` for all
21 languages.

### Console chevron matches the left-panel spine style (1.56.1)

The bottom console's collapse / expand button used `+` / `-`
glyphs in the small Calibri 13 font.  Switched to `▲` (up) and
`▼` (down) triangles in Segoe UI 16 bold, the same weight the
left info-panel spine uses for its `<` / `>` chevrons.  Direction
follows the click action: collapsed -> arrow up (click expands
upward into view), expanded -> arrow down (click collapses
downward out of view).

The two toggles now read consistently: spine left/right for the
side panel, console up/down for the bottom panel.

Files: `tankExporterPy/ui.py`.

### Seed real translations into all 20 languages (1.56.0)

The 1.55.0 framework shipped 20 stub catalogs that fell back to
English.  This pass fills them in with actual translations for
every UI string TEPY exposes -- 33 entries × 20 languages = 660
new translations.

Coverage: every left-panel button (Grid, Axes, Set Paths, Save
Prim, ItemList, ...), every right-panel slider (Sm Start, Fire
Size, Normals, ...), every checkbox (NMap, AO, PerVtx, Debug),
every section header (UI, IO, Tools), and every popup title
(FBX upgraded, FBX import aborted, FBX import failed).

Languages translated:

| Code   | Language                  |
| ------ | ------------------------- |
| de     | German                    |
| ru     | Russian                   |
| fr     | French                    |
| es     | Spanish (Castilian)       |
| es_ar  | Spanish (Latin American)  |
| pt_br  | Portuguese (Brazil)       |
| pl     | Polish                    |
| cs     | Czech                     |
| it     | Italian                   |
| hu     | Hungarian                 |
| bg     | Bulgarian                 |
| ro     | Romanian                  |
| tr     | Turkish                   |
| uk     | Ukrainian                 |
| ko     | Korean                    |
| ja     | Japanese                  |
| zh_cn  | Chinese (Simplified)      |
| zh_tw  | Chinese (Traditional)     |
| vi     | Vietnamese                |
| th     | Thai                      |

Translations are practical and recognition-oriented for a 70-px
button width.  Technical abbreviations -- `UI`, `AO`, `NMap`,
`FBX`, `Sm`, `Prim` -- stay as-is across every language because
they're standard in 3-D / graphics tooling worldwide.  Where a
natural translation would overflow ("Drahtgittermodell" for
Wireframe), uses an abbreviation native to the language
(`Drahtgit.`).

New tool: `cust_tools/seed_locale_translations.py` -- holds the
master translation table and a one-shot apply-then-recompile
runner.  Re-run any time the table changes; existing
translations get refreshed in place.  Idempotent.

Native speaker review welcome -- this is a practical first pass
to make the language picker visually working across every WoT
locale, not a polished L10n release.  Anyone fluent in a
language can edit the corresponding `.po` and re-run
`build_locale_mo.py`.

Files: `cust_tools/seed_locale_translations.py`,
`tankExporterPy/locale/<code>/LC_MESSAGES/tepy.{po,mo}` for
all 20 non-English languages.

### Multi-language UI via gettext catalogs (1.55.0)

TEPY now ships its own `gettext` translation pipeline -- the same
mechanism WoT uses for in-game text.  21 language stubs covering
every locale WoT supports across all regions:

```
en, ru, de, fr, es, es_ar, pt_br, pl, cs, it, hu, bg, ro, tr,
uk, ko, ja, zh_cn, zh_tw, vi, th
```

Each ships at `tankExporterPy/locale/<code>/LC_MESSAGES/tepy.po`
(human-editable source) + `tepy.mo` (binary catalog the runtime
reads).  English (`en/`) carries the canonical msgids -- 34
strings spanning every button, slider, checkbox, section header,
and popup title in the UI.  Other languages start as stubs (empty
msgstrs); gettext echoes the msgid for any miss, so untranslated
languages render as English.  French (`fr/`) seeded with full
translations as a working demonstration.

New module structure:

* `tankExporterPy/localization.py` -- adds `Translator` class,
  `set_active_language(code)`, `get_active_language()`, and the
  conventional `_(msgid)` shorthand.  All UI strings call `_(...)`;
  `_()` is deterministic per-session so widget-label lookups stay
  consistent.
* `cust_tools/build_locale_mo.py` -- pure-stdlib `.po` -> `.mo`
  compiler (no `msgfmt` dependency).  Walks every `.po` under
  `locale/` and writes the sibling `.mo`.  Run after editing any
  translation.
* `tankExporterPy/locale/<lang>/LC_MESSAGES/tepy.{po,mo}` -- 21
  catalogs, ~840 KB total (the empty stubs are 60 bytes each).

UI:

* New **Language** button in the left-panel IO section.  Opens a
  Tk dropdown picker (Combobox) listing every supported language
  with its native-script name (`Français`, `日本語`, `Русский`,
  ...).  Selection persists in `tankExporterPy.json` under the
  `language` key.
* Restart-to-apply: every label texture is built once at
  `_build_ui` time, so the picker explains the change takes
  effect on next launch.

Wired:

* Every left-panel button label (Grid / Axes / Light / Orbit /
  Skybox / Wireframe / Meshes / Flip / Compare / Set Paths /
  Import / Export / Save Prim / Language / ItemList).
* Every right-panel slider label (Light / Ambient / Sm Start /
  Sm End / Sm Speed / Sm FadeS / Sm FadeE / Fire Size / Normals).
* Every right-panel checkbox label (NMap / AO / PerVtx / Debug).
* Section headers (UI / IO / Tools).
* `button_groups` table in `_layout_widgets` uses the same
  `_('...')` form so `btn_by_label` lookups resolve correctly
  in whichever language is active.

Console log messages and dynamic strings (file paths, counts,
status updates) are intentionally left in English -- they're
diagnostic output, not UI.  Translations stop at the boundary
between "thing the user clicks" and "thing scrolling past in the
console pane."

Files: new `cust_tools/build_locale_mo.py`,
`tankExporterPy/locale/.../tepy.po` x 21, `tepy.mo` x 21.
Edits to `tankExporterPy/localization.py`,
`tankExporterPy/viewer.py`, `tankExporterPy/config.py`.

### Rename `tankviewer/` package -> `tankExporterPy/` (1.54.0)

Final naming alignment.  The package directory was the last thing
still called by the old project name; everything else (entry-point
script, config JSON, GitHub repo, brand) had already migrated to
`tankExporterPy`.

`git mv tankviewer tankExporterPy` for the directory; bulk
search-and-replace of `tankviewer` -> `tankExporterPy` across
every `.py`, `.md`, and `.bat` file with a reference, EXCEPT
`CHANGELOG.md` (historical entries describe what files were
called at the time).  20 files updated, ~100 line edits in total.
Smoke-tested via `python -c "from tankExporterPy.viewer import Viewer"`
and `python tankExporterPy.py --help` -- both clean.

The launcher script `tankExporterPy.py` and the package directory
`tankExporterPy/` now share a name.  Python resolves
`import tankExporterPy` to the package (it's a directory with
`__init__.py`); the script remains directly runnable as
`python tankExporterPy.py`.  No collision in practice -- nothing
imports the launcher script.

Files: every reference outside `CHANGELOG.md`.

### Refresh tank tree after ItemList rebuild (1.53.1)

`_rebuild_itemlist_now` used to leave the in-memory tier-tree
unchanged.  After a rebuild that picked up new tanks (game patch,
different WoT install via Set Paths in the same session), the
tree would still show whatever was there at session start until
the user restarted TEPY.

Now the rebuild path finishes by:

1. Dropping `self._tanks_display_map` so `_load_tanks_txt`
   re-reads on next access.
2. Detaching `self.ui.tree` so it doesn't point into a
   soon-to-be-freed cached tree.
3. Calling `_build_all_tier_trees()` to rebuild every tier from
   the current `list_vehicle_xmls()` view.

Tab bar isn't touched (same 11 tier slots); only the contents
rebuild.  Failure of this step is logged but non-fatal -- the
on-disk artefacts are still the critical outputs.

Files: `tankviewer/viewer.py`.

### Pull tank names from WoT's localization catalogs (1.53.0)

WoT stores user-facing strings -- tank names, descriptions,
module labels -- in standard gettext binary catalogs at
`<wot_root>/res/text/lc_messages/<catalog>.mo`.  Each tank's
`list.xml` entry carries a `userString` of the form
`#<catalog>:<key>` (e.g. `#usa_vehicles:A37_M40M43`), which
resolves to the friendly localized display name (`M40/M43`).
That's the same string the user sees in their WoT client, in
whatever language they have it set to.

New module `tankviewer/localization.py` with `WoTLocalizer`:
* Lazy-loads each `.mo` catalog via Python's stdlib `gettext`
* Caches every catalog after first hit
* `lookup('#cat:key')` -> localized string, or bare key when the
  catalog / key is missing (gettext's natural echo-on-miss
  behaviour) so callers never have to special-case None
* `lookup_basename(...)` for UI code that wants a guaranteed
  non-empty label

Threading the data through:
* `PkgExtractor._read_tank_table` now captures `userString` from
  each `<tank>` block in `list.xml`.
* `list_vehicle_xmls(with_tier=True)` carries `user_string` in
  every entry alongside `tier` / `vclass`.
* `Viewer.__init__` constructs a `WoTLocalizer` from the
  configured `pkg_dir` (parent's parent = WoT root).

UI consumption:
* When `tanks.txt` is missing, the tier-tree builder now resolves
  each entry's `user_string` via the localizer instead of
  falling back to the raw basename.  A tier-1 USA tank shows up
  as "T1 Cunningham" (matching the in-game UI) instead of
  "A01_T1_Cunningham".
* `tanks_index.txt` (regenerated on every ItemList rebuild) gains
  a 4th tab-separated column -- the friendly name -- so external
  tooling can use it as a name lookup without re-parsing the
  catalogs.

Files: new `tankviewer/localization.py`; edits to
`tankviewer/loaders.py`, `tankviewer/viewer.py`.

### Tree fallback when tanks.txt is missing (1.52.1)

The tier-tree builder filtered against `_tanks_display_map`
(loaded from `tanks.txt`).  When that file went missing, the map
came back empty and EVERY tank got skipped -- empty tree across
every tier, no useful UI.

Now the builder detects an empty `tanks_txt_map` and treats every
`list.xml` entry as in-scope, using the basename as the display
name.  Loses the curated short labels (`T-34-85` instead of
`A14_T_34_85`) but the tree is usable.  A console line tells the
user what happened.

The `tanks_txt_map[base]` lookup further down also got a `.get`
with a basename fallback so a partial map (some entries missing)
no longer KeyErrors.

Files: `tankviewer/viewer.py`.

### Fuzzy tank-name match + auto tank-list rebuild (1.52.0)

Two related FBX-import quality-of-life fixes:

**1. Fuzzy match on tank-name lookup.**  When an imported FBX's
basename doesn't match any nation's `list.xml` exactly,
`_resolve_tank_nation` now falls back to
`difflib.get_close_matches` against every basename across every
nation (cutoff 0.6, top match wins).  Smoke emitters / hardpoints /
exhaust spec all light up for files that previously fell through
silently because the name wasn't byte-identical.

Example match (the case that prompted the fix):
* FBX named `Type 59.fbx` -> WoT XML `Ch01_Type59.xml`
* `T-34-85_typo` -> `T-34-85_2`

The substituted basename is logged so the user sees what was
matched.  Cutoff of 0.6 catches single-letter typos and missing
prefixes without false-matching unrelated names (`lion` doesn't
match `It21_Lion`; you'd want `It21_Lion` or `Lion_KL_3DSt`).

**2. Tank list rebuild piggy-backs on ItemList rebuild.**  Every
time `_rebuild_itemlist_now` finishes a successful
`TheItemList.xml` rebuild, it now also calls
`_rebuild_tank_list_now` to dump a flat `tanks_index.txt` next
to the project's `tanks.txt`.  Format is one tab-separated row
per tank:

    british     1   GB01_Vickers_Medium_Mk_I
    chinese     10  Ch28_WZ_111_5A
    ...

Sorted by nation -> tier -> basename so diffs across game
patches stay readable.  Atomic write (`<path>.tmp` + os.replace).
File regenerated from scratch on every rebuild so it always
reflects the current `list.xml` state of the user's WoT install.

Failure of the tank-list step is non-fatal -- ItemList itself
remains the critical artefact.

Files: `tankviewer/viewer.py`.

### Auto-upgrade pre-7.1 FBX on import (1.51.0)

Blender's FBX importer rejects binary FBX files with header
version < 7100 (FBX 7.1).  The legacy WoT tank exporter wrote
6.1, and so does anything else from the FBX SDK 2009 era -- so
attempting to import an old archive popped a "version too low"
error and stopped dead.

New flow: when `Import` picks an `.fbx` file, the import handler
probes its 27-byte binary header for the version code.  If the
file is below the floor, `tankviewer.importers.fbx_version.
ensure_modern_fbx` looks up Autodesk's free FBX Converter 2013
in the standard install paths (override via `fbx_converter_exe`
in tankExporterPy.json), runs it headless against the file, and
hands the upgraded `<basename>_v73.fbx` to Blender.  The original
is preserved untouched.

A Tkinter `messagebox.showinfo` dialog tells the user what
happened ("FBX 6.1 is too old for Blender; auto-upgraded to
FBX 7.3.  Source: ...  Converted: ...").  When the converter
isn't installed, a `messagebox.showerror` explains that the user
needs FBX Converter 2013 (or an explicit `fbx_converter_exe`
config override) and the import aborts.

Helpers also exposed:
- `read_fbx_version(path)` -> int|None
- `fbx_version_pretty(code)` -> str
- `find_fbx_converter(override)` -> str|None
- `convert_fbx_to_modern(src, exe, dst)` -> (ok, dst|err)
- `ensure_modern_fbx(src, override)` -> (path, action, message)

Files: new `tankviewer/importers/fbx_version.py`; edits to
`tankviewer/viewer.py` (import flow + popup helpers) and
`tankviewer/config.py` (new `fbx_converter_exe` slot).

### Rename `_active_group` -> `_active_engine_class` (1.50.3)

Cosmetic-only rename for clarity.  The field tracks which WoT
engine class (`gas_small`, `diesel_large`, ...) is currently
selected for slider editing; the old name was a holdover from when
the radio-checkbox UI also used the term "group" and could be
mistaken for "tier" or "selection group" by anyone reading the
code cold.

Two methods that ended in `_active_group` came along for the ride:

* `_load_active_group` -> `_load_active_engine_class`
* `_set_active_group`  -> `_set_active_engine_class`

No behavioural change.  All 24 references in `viewer.py` updated
in one sweep + the `ARCHITECTURE.md` Viewer-method table line.
Historical `CHANGELOG.md` entries left untouched -- they describe
what the code said at the time.

Files: `tankviewer/viewer.py`, `ARCHITECTURE.md`.

### Single-source-of-truth slider persist (1.50.2)

Refactored slider persistence into one routine, `_persist_all_sliders`,
that uses the engine class (`self._active_group`) as the routing
reference.  Replaces the prior `_save_active_group` /
`_persist_slider_state` / inline cleanup logic which had three
copies of the same routing logic that could (and would) drift.

The new sub:

* Reads `self._active_group` (the engine level: `gas_small`,
  `diesel_large`, etc.) once.
* Routes per-engine slider values (smoke / fire) into the matching
  slot of `self._smoke_groups` / `self._fire_groups`.
* Sends global sliders (Light / Ambient / Normals) to flat keys.
* Optionally writes JSON via `_config.save` (caller's choice).

Three callers now go through it:

| Caller             | `write_json` |
| ------------------ | ------------ |
| Mouse-up release   | True         |
| Per-frame mirror   | False        |
| `cleanup()` exit   | False (cleanup writes once at the end after also folding in checkboxes + legacy-key drops) |

Files: `tankviewer/viewer.py`.

### Persist sliders on mouse-up, not just on window close (1.50.1)

Slider tweaks used to write to `tankExporterPy.json` only when the
window closed cleanly.  If you wanted to confirm a change had stuck
without quitting + relaunching, you couldn't -- and a kill via
Task Manager / a crash dropped every unsaved tweak.

Now `handle_input` snapshots whether a slider was being dragged
before `handle_mouse_up` clears the active-slider reference; if
yes, `_persist_slider_state` runs after the release.  That:

* Mirrors `_save_active_group()` to capture the active engine
  class's per-slider values into `self._smoke_groups` /
  `self._fire_groups`.
* Copies both dicts onto `self._cfg`.
* Snapshots Light / Ambient / Normals sliders the same way.
* Writes the JSON via `_config.save`.

Fires once per mouse-up (not per pixel of drag) so disk cost is
trivial.  cleanup() still saves on exit as a belt-and-braces
fallback for keys that aren't slider-driven (debug flag, paths,
etc.).

Files: `tankviewer/viewer.py`.

### Single Debug master toggle (1.50.0)

Replaced the **Show HP** + **Show Fire** checkboxes with a single
**Debug** master toggle in the right control panel.  When checked,
every on-screen debug overlay lights up at once -- HP markers, fire-
billboard outlines, and anything else we add later.

The convention going forward: any new diagnostic-only render gets
gated on `self._debug`.  Comment in `_debug_cb`'s declaration spells
this out so future overlays don't grow their own one-off toggles.

Backward-compat: pre-v1.50 split the state across `show_hardpoints`
and `show_fire_cards` keys.  Startup OR's both keys with the new
`debug` key, so a user who had either enabled comes up in debug
mode.  cleanup() drops both legacy keys at save time so the JSON
ends up with only `debug`.

`PerVtx` stays where it was (it's a normals-shader visualisation
mode, not debug per se).  Right panel is back to one checkbox row
(`PerVtx | Debug`); `RIGHT_CONTROLS_H` shrinks accordingly.

Files: `tankviewer/viewer.py`.

### Show Fire debug outlines (1.49.0)

New checkbox in the right control panel: **Show Fire** -- when on,
draws a yellow rectangle around each fire billboard tracing the
quad's four world-space corners.  Useful for verifying HP_Fire
placement, billboard size, and the bottom-anchored math after the
v1.46.1 corner-offset fix.  Off by default; persisted in
`tankExporterPy.json` under `show_fire_cards`.

How it works:

* New `LineBatch` instance `self.fire_outlines` lives next to
  `self.hp_lines`.
* Each frame, when the toggle is on AND `fire_billboards.emitters`
  isn't empty, the render loop computes four corners per emitter
  using the same camera-facing math the vertex shader uses
  (`pos +/- cam_right * 0.5*size + cam_up * (0 or 1)*size`),
  pushes them as four `GL_LINES` segments per quad, and renders
  via the existing `SimpleColorShader`.
* Cheap (~4 segments x 1-4 emitters = 4-16 segments per frame),
  so no perf consideration.

Layout: `Show Fire` sits on its own row below `PerVtx | Show HP`
in the right panel; `RIGHT_CONTROLS_H` auto-grows by one
`CB_ROW_H` to fit.

Files: `tankviewer/viewer.py`.

### Ship the default config (1.48.3)

`tankExporterPy.json` is now tracked + shipped with the repo so a
fresh clone boots with sane defaults instead of an empty config:

* Per-engine-class `smoke_groups` and `fire_groups` filled with
  the v1.46.0 built-in defaults plus any tuning the project owner
  has dialled in.
* `pkg_dir` / `res_mods` / `lookup_xml` carry the project owner's
  paths -- the **Set Paths** dialog (left panel, IO group)
  overwrites these on first launch so users point TEPY at their
  own WoT install.

`.gitignore` updated: `tankExporterPy.json` removed (now tracked),
`tankviewer.json` (the pre-v1.48.2 name) still ignored so a stale
file from an older session can't sneak back in.

Files: `.gitignore`, `tankExporterPy.json`.

### Config rename + key-whitelist removal (1.48.2)

Two related fixes that explain why users were still seeing old-format
config files even after running v1.46+:

**Filename rename**: `tankviewer.json` -> `tankExporterPy.json` so
the config matches the launcher (`tankExporterPy.py`), the GitHub
repo (`Tank-Exporter-PY`), and the TEPY brand.  `config.py` does
a one-time `os.rename` on first run when the new file is missing
and the old one exists -- the user's existing settings carry over
unchanged.  Both filenames are now gitignored.  Untracked the old
`tankviewer.json` from the repo so a fresh clone doesn't ship one
user's machine paths.

**Key-whitelist removal**: `config.load()` and `config.save()` used
to filter against `_DEFAULTS` -- a defensive measure that turned
into a bug when the schema grew nested dicts.  Result: the new
`smoke_groups` / `fire_groups` keys we added in v1.46.0 were
silently dropped during both load AND save, so the JSON never
showed the new structure no matter how cleanly the user exited.
Now `load()` reads + `save()` writes whatever the dict carries;
`_DEFAULTS` is just the floor for missing-key fill-in.  The
viewer's `_migrate_legacy_smoke_fire_config` and the cleanup()
key-drop list (added in 1.48.1) now actually have a path to run.

After one clean exit on v1.48.2, your `tankExporterPy.json` will
finally show the per-engine-class structure -- with whatever you
had tuned in the old flat keys folded into `gas_medium`.

Files touched: `tankviewer/config.py`, `.gitignore`,
`tankExporterPy.py`, `cust_tools/extract_wot_fire_atlas.py`,
`uninstall.bat`, `README.md`, `README_TANK_VIEWER.md`,
`ARCHITECTURE.md`, plus the on-disk file rename.

### Migrate + drop legacy smoke/fire config keys (1.48.1)

A bug from the v1.46.0 per-engine-class refactor: cleanup() wrote
the new `smoke_groups` / `fire_groups` dicts but never deleted the
old flat keys (`smoke_start_size`, `smoke_end_size`, `smoke_speed`,
`smoke_fade_start_frame`, `smoke_fade_end_frame`, `fire_size`,
`fire_fps`).  Result: any config written by v1.45 or earlier kept
those legacy keys forever, and the new `smoke_groups` /
`fire_groups` never appeared in the JSON because we hadn't loaded
them on startup either -- so users saw "old-format" config files
even after running the new build.

Two parts to the fix:

1. **Read-side migration**: `Viewer._migrate_legacy_smoke_fire_config`
   runs once at startup and folds any legacy keys into the
   `gas_medium` slot of the new dicts.  Runs only when neither
   `smoke_groups` nor `fire_groups` is already present (i.e. no
   re-migration after first clean save).
2. **Write-side cleanup**: cleanup() now `pop()`s every legacy
   key from `self._cfg` after writing the new dicts, so the JSON
   ends up with only the new structure.

Legacy keys lists live in `_LEGACY_SMOKE_KEYS`,
`_LEGACY_FIRE_KEYS`, and `_LEGACY_DROPPED_KEYS` so reader and
writer can't drift.  After one clean exit on v1.48.1, the JSON
will look like the per-engine-class structure documented in the
v1.46.0 entry; existing tuning is preserved into `gas_medium`.

Files touched: `tankviewer/viewer.py`.

### Stop shipping WoT pixels + entry-point rename (1.48.0)

Every PNG that lived under `resources/fire/` and
`resources/smoke/` was a slice of Wargaming's `eff_tex.dds`
particle atlas.  We can't redistribute their artwork, so:

* Both folders are now in `.gitignore` (along with
  `resources/_wot_eff_tex.dds`, `resources/fire_sets/`, and the
  legacy `resources/fire_legacy_explosion/`).
* All previously-tracked PNGs were removed via `git rm --cached`
  (the local copies stay on disk; only the index changed).
* `cust_tools/extract_wot_fire_atlas.py` got refactored to expose
  importable helpers: `ensure_atlas_local`, `slice_set`, and
  `ensure_runtime_flipbooks`.  The CLI still works the same; the
  module-level `GRID_DEFS` and `RUNTIME_TARGETS` tables let the
  viewer call the same logic in-process.
* `Viewer.__init__` calls `ensure_runtime_flipbooks` BEFORE
  `FlipbookTexture` init.  When `resources/fire/` or
  `resources/smoke/` is empty, the helper locates `particles.pkg`
  (preferring the user's configured `pkg_dir` from
  `tankviewer.json`, then falling back to the hardcoded NA / EU /
  RU / standalone paths), caches the atlas to
  `resources/_wot_eff_tex.dds`, and slices `fire_BIG` +
  `smoke_white` into the runtime folders.  Steady-state cost when
  the folders are populated = two `os.listdir` calls.

Net effect: a fresh clone produces a working burning-tank effect
on first launch as long as WoT is installed somewhere we can find
it.  No Wargaming bytes ride along in the repo.

**Entry-point rename**: `tank_viewer.py` -> `tankExporterPy.py`
so the launcher name matches the GitHub repo (`Tank-Exporter-PY`)
and the user-facing brand (TEPY).  `go.bat`, `start.bat`,
`README.md`, `README_TANK_VIEWER.md`, `ARCHITECTURE.md`,
`COORDINATE_SYSTEMS.md`, `tankviewer/config.py`, and
`tankviewer/loaders.py` all updated.  Historical CHANGELOG entries
left unchanged -- they describe what those files said at the
time.

Files touched: `cust_tools/extract_wot_fire_atlas.py`,
`tankviewer/viewer.py`, `tankviewer/config.py`,
`tankviewer/loaders.py`, `tankExporterPy.py` (renamed),
`go.bat`, `start.bat`, `.gitignore`, `README.md`,
`README_TANK_VIEWER.md`, `ARCHITECTURE.md`,
`COORDINATE_SYSTEMS.md`, `CLAUDE.md`.

### Pillow + GL pre-warm via Details_map.dds (1.47.1)

The 1.47.0 pre-warm only addressed XML caches (a few hundred ms at
most).  Profiling showed the real first-tank-load cost was Pillow's
DDS codec + the GL driver's BC-format / mipmap-gen pipeline warming
up.  Measured: same tank loaded twice -- 7582 ms first, 1132 ms
second, with PkgExtractor at 55 ms vs 0 ms (i.e. extraction is NOT
the bottleneck).  The ~6.5 s gap matches "~48 textures pay
~130 ms cold-init each, then ~25 ms warm each" exactly.

`_prewarm_first_load_caches` now pushes ONE real DDS through the
full `TextureLoader.load_texture` pipeline at splash:
`resources/Details_map.dds`, which is the shared scratch noise map
every tank references and is already cached on disk after the first
ever session.  That single upload warms Pillow's plugin registry,
the GL driver's tex-upload memory pools, and the `glGenerateMipmap`
JIT, then stashes the result into `_shared_tex_cache` so the first
mesh that requests 'detail' skips the load entirely.

Edge case: on a fresh install the detail map isn't cached yet.  In
that one case we leave the warm-up lazy (the file gets written to
`resources/` on first tank load, so every session after that benefits).

Files touched: `tankviewer/viewer.py`.

### Pre-warm first-tank-load caches at splash (1.47.0)

The first `load_vehicle` call used to stall for a beat *after* the
per-component meshes (Hull, Chassis, Turret, Gun) finished loading.
Subsequent loads were instant.  Two lazy-init caches were the cause:

1. **`ArmorColorLoader.ensure_loaded()`** -- extracts and parses
   WoT's `base_paints.xml` on first `get()` call.  Hundreds of ms.
2. **`VehicleXMLLoader._shared_xml_cache`** -- a class-level dict
   keyed by `<nation>/components/<file>.xml`.  The first tank of
   any nation pays for 5 BWXML decodes (engines, radios, fuelTanks,
   guns, shells) inside `parse_info()`, which fires AFTER the
   per-component loop.

Both now warm during the splash sequence via the new
`Viewer._prewarm_first_load_caches`, called between the tier-tree
build and "Almost ready..."  The user sees a status line
("Pre-warming armor-paint + component XML caches...") and pays the
cost once, up-front, where they already expect to wait.  No-ops
cleanly when the PkgExtractor isn't configured yet (fresh install,
first run).

Files touched: `tankviewer/viewer.py`.

### Bottom-anchor fire billboards (1.46.1)

`AnimatedBillboard` was placing the emitter position (HP_Fire_*) at
the **center** of each flame quad, so the lower half of every flame
sank into the tank.  Added `_CORNER_OFFSETS_BOTTOM` -- y runs
`0.0 .. 1.0` instead of `-0.5 .. 0.5` -- and switched
`AnimatedBillboard.render` to use it.  Smoke `ParticleSystem` keeps
the original centered offsets (smoke wants to drift in all
directions from the exhaust).  Net effect: the bottom edge of every
flame now sits AT the hardpoint and the flame rises upward, like
fire does.

Files touched: `tankviewer/particles.py`.

### Per-engine-class smoke / fire settings (1.46.0)

The smoke-particle and fire-billboard sliders now save into
**one slot per WoT engine class** rather than a single global
slot.  Engine class is the value WoT stores in each tank's
`<exhaust><pixie>` block: `gas_small`, `gas_medium`, `gas_large`,
`diesel_small`, `diesel_medium`, `diesel_large`, `diesel_strv`.

How it works:

* When a tank loads, `load_vehicle` (and `_populate_exhaust_for_tank`
  for the FBX-import path) reads the def XML's pixie value and
  calls `_set_active_group(pixie)`.
* `_set_active_group` saves the previous class's slider values into
  `self._smoke_groups[old_class]` / `self._fire_groups[old_class]`,
  switches `self._active_group`, then loads the new class's stored
  values onto the sliders.
* While that tank is on screen, every render frame copies the live
  slider values back into the active class's slot via
  `_save_active_group` -- so a tweak made on a tier-1 (`gas_small`)
  doesn't bleed into `diesel_large` when a heavy is loaded next.
* On exit, both dicts (`smoke_groups` / `fire_groups`) are written
  to the config file as a unit; legacy keys (`smoke_start_size`,
  `smoke_end_size`, `fire_fps`, etc.) are no longer emitted.  Any
  old config that still has them is ignored on load -- the merge
  helper just falls through to the built-in defaults.

UI changes:

* **Removed**: `Fire FPS` slider.  We never wanted it user-tunable
  (the 30 fps loop was already baked).  The right control panel is
  one row shorter as a result.
* **No new UI** for the per-class switching.  An earlier draft
  added an `Sm | Md | Lg` radio row but the loaded tank's engine
  class auto-wires the active slot, so the radio was redundant.

Built-in defaults are seeded so `_small` classes spawn visibly less
smoke than `_large` -- a tier-1 light no longer puffs Maus-scale
clouds out of two pinholes.  User tweaks override per class and
persist across sessions.

Files touched: `tankviewer/viewer.py`.

## 2026-05-05 (afternoon)

Tooling pass: a fresh-rebuild path for `TheItemList.xml`, an in-app
button to invoke it, a left-panel reorganisation that groups every
button by category (UI / IO / Tools), a Windows .bat launcher
trio (`go.bat` / `uninstall.bat` / `reinstall.bat`), and a TEPY
rebrand (window title + tepee icon).

### WoT-painted fire/smoke flipbook sets (1.37.0)

Replaced the procedural fire frames with **WoT's own painted
flame texture**, ripped straight from the master particle atlas.

Discovery chain:
* `scripts/destructible_entity_effects.xml` references
  `<pixie><file>particles/.../*.eff</file></pixie>`.
* The `.eff` files (legacy ASCII) no longer exist; their compiled
  `.effbin` successors do.  Each `.effbin` is a tiny manifest
  pointing at the real definition: `.vfxbin` files under
  `particles/content_deferred/PFX/Tank/destruction/`.
* The `.vfxbin` is binary (undocumented format), but ASCII strings
  inside reveal that EVERY burning-tank effect samples ONE master
  atlas: `particles/content_deferred/PFX_textures/eff_tex.dds`
  (4096x4096) and references it via integer region IDs.
* The atlas itself is organised into ~10 visually-obvious
  flipbook grids (regular MxN tilings of same-sized animation
  frames), each fed to a different particle layer at runtime.

`cust_tools/extract_wot_fire_atlas.py` extracts the atlas from
`particles.pkg` (cached on disk for repeat runs) and slices it
into `resources/fire_sets/<name>/`:

    fire_BIG/                 64 frames @ 128px  -- main fire animation
    fire_small/              256 frames @  64px  -- small flickers
    fireball_blast/           32 frames @ 128px  -- ammo cookoff blast
    flame_columns_black/      32 frames @ 128px  -- vertical flames (dark)
    flame_columns_light/      32 frames @ 128px  -- vertical flames (light)
    smoke_white/              64 frames @ 128px  -- pale smoke clouds
    smoke_dark/               64 frames @ 128px  -- dark grey smoke
    smoke_cream/              64 frames @ 128px  -- warm-tone smoke
    smoke_dirt/               64 frames @ 128px  -- ground impact dust
    smoke_under/              64 frames @ 128px  -- under-tank smoke dome

Plus `resources/fire_sets/README.txt` -- catalogue manifest the
script writes automatically so the user doesn't need to re-run to
see the inventory.

After extraction the script ALSO copies `fire_BIG/` into
`resources/fire/` so the runtime FlipbookTexture loads the WoT
fire on the next launch with no manual file shuffling.  To switch
to a different set later, drop the contents of any other
`fire_sets/<name>/` into `resources/fire/`.

Why slice manually instead of parsing the .vfxbin: a brute-force
probe of the binary near each named region returns particle
SIMULATION parameters (rotation in radians, lifetime fractions,
scale curves) -- the atlas-rect indices are stored separately,
keyed against an undocumented offsets table.  Visual inspection
of the atlas is fast and reliable; the catalogue is a fixed
table the user can tweak by editing the rectangles in the script.

### Real fire flipbook (1.36.1)

`resources/fire/` previously held a single-blast explosion sequence
(`explosion 1_rgb*.png`).  Replaced with a procedurally-rendered
91-frame fire animation -- continuous flame, ignition / mature /
dissipation lifecycle, upward-scrolling turbulence, no white-hot
"steam" tip (palette tops out at warm yellow).  Legacy explosion
frames moved to `resources/fire_legacy_explosion/` for reference.

Generator: `cust_tools/make_fire_frames.py`.  All knobs (palette,
phase fractions, flame envelope, noise scales, scroll rate) live
at the top of the file; re-run to regenerate.  Frozen RNG seed so
two runs at the same settings produce byte-identical output --
clean git diffs of generated assets.

### Burn the damaged tanks (1.36.0) -- HP_Fire_* particle system

When the user loads a tank with the "Load Damaged" checkbox ticked,
TEPY now spawns a flipbook-billboard fire plume from every
`HP_Fire_*` hardpoint the tank's visual_processed files declare.
Sibling system to the existing engine-exhaust smoke; the same
ParticleShader, the same flipbook-on-an-array-texture pattern,
just routed to a separate ParticleSystem instance with its own
emitter list and tunables.

Implementation:

* New `VisualLoader.find_fire_nodes(visual_path)` -- mirrors
  `find_exhaust_nodes` but filters on the `_FIRE_KEYWORDS` set
  (currently just `'fire'`) and explicitly skips any node that
  also matches an exhaust keyword (defensive against weird
  `HP_engineExhaust_fire_*` mod names; live tanks don't do this).
* `Viewer.fire_flipbook` / `Viewer.fire_particles` -- separate
  `FlipbookTexture` (`resources/fire/`, 91 frames) +
  `ParticleSystem(max_particles=512)`.  Default tunables tuned
  for "burning hulk" not exhaust: smaller spawn footprint,
  faster vertical drift, less drag, shorter lifetime.
* `Viewer._fire_points` -- list of {component, name, pos, fwd}
  populated by `load_vehicle` ONLY when `damaged=True`.  Walks
  every component visual (hull, chassis, turret, gun) for
  HP_Fire_* nodes and applies the standard BW->GL Z-flip on
  the position.  The forward vector is REPLACED with world-up
  `(0, 1, 0)` regardless of what the artist's node oriented to
  in 3DS Max -- fire goes UP, period.  No other vector makes
  sense for damaged-tank flames.
* New left-panel sliders in the existing right-panel slider
  block: **Fire Start**, **Fire End**, **Fire Speed**.
  Persisted to `tankviewer.json`.  Live values are pushed onto
  the ParticleSystem each frame from the slider widgets, same
  as the smoke sliders.
* Render-loop pass parallel to smoke: update by `_frame_dt`,
  then alpha-blended draw via the existing ParticleShader.
  Free when no emitters are registered (undamaged load), so
  the block is safe to run every frame unconditionally.

### Right panel auto-computes its height too (1.36.0)

`RIGHT_CONTROLS_H` is now computed from the actual slider count
in `_layout_widgets`, same instance-shadows-class-constant
pattern the LEFT panel got in 1.34.2.  Adding the three fire
sliders (5 smoke + 3 fire + Normals = 9 total) would have
overflowed the previous fixed `200 px` cap; now the panel
self-adjusts and the tank-list tree above resizes to match.
The class-level `RIGHT_CONTROLS_H = 280` stays as a `max()`
floor.

`_on_resize` was reordered so `_layout_widgets()` runs FIRST.
The right-side tree-height computation and both control rects
now read the freshly-computed instance heights, so a future
slider addition needs zero constant-bumping.

### Banner re-skin: bottom-centred + edge-weathered + 1876 postcard palette (1.35.2)

`resources/tepy_banner.png` re-rendered with new layout and
colours.  Three iterations boiled down to:

* Title + subtitle now anchored to the BOTTOM-CENTRE of the
  splash (above: top-centre then upper-right corner -- both
  fought the existing scene composition).
* Title in vivid burnt orange `(228, 132, 64)`, subtitle in
  warm cream-gold `(232, 200, 142)`.  Earlier muted-brown
  attempts vanished into the dirt; pushing the chroma up until
  the title reads as "paint on a stagecoach side panel" was
  the right move for the 1876-postcard look the user asked for.
* Subtitle bumped to ~55 % of title cap height (was ~42 %) so
  the tagline carries weight.
* No drop shadow.  Glyph rims weathered via edge-only alpha
  jitter -- a body-vs-edge mask gates jitter to pixels in the
  blurred-alpha transition band, so glyph interiors stay solid
  (legible) while the rims erode into the substrate (looks
  painted-on, not sticker-applied).
* Frozen RNG seed so two regen runs at the same settings give
  byte-identical PNG output -- clean git diffs of a generated
  asset.

Generator: `cust_tools/make_banner.py`.  Knobs at the top of the
file: `TITLE_HEIGHT_FRAC`, `BOTTOM_PAD_FRAC`, `COLOR_TITLE`,
`WEATHER_FADE_BAND`, `EDGE_LOW` / `EDGE_HIGH`, etc.

### Damaged-variant `_damaged` filename tag (1.35.0)

When a tank is loaded with the "Load Damaged" checkbox ticked
(destroyed/crashed model variant), the variant info now propagates
through every export / import / save-prim path so writes don't
silently land in the wrong res_mods folder:

* `load_vehicle(damaged=True)` stores `self._loaded_damaged = True`.
* `_on_export_clicked` appends `_damaged` to the default save name
  (e.g. `G102_Pz_III_damaged.fbx`).  Idempotent -- a re-export of
  an already-tagged set doesn't double the suffix.
* `_on_import_clicked` checks the input filename's stem for the
  `_damaged` suffix (case-insensitive) and restores
  `self._loaded_damaged` accordingly so the round-trip is loss-less.
* `_on_save_prim_clicked` swaps `/normal/` -> `/crash/` in the
  canonical pkg path before writing, so a damaged-variant save
  lands at `res_mods/<ver>/vehicles/<nation>/<tank>/crash/lod0/...`
  instead of overwriting the normal-variant file.  The visual-file
  lookup (used to copy referenced textures) follows the same
  redirect so the cracked/damaged textures get pulled.

Without this, the variant info dropped at the first hand-off and
the user had no way to tell from the FBX filename alone whether
they had the damaged or normal variant -- and a reflexive Save
Prim could overwrite the live normal-variant primitives with the
modified damaged geometry.

### Particle-integration `dt` regression fix (1.34.3)

Side effect of the FPS rewrite: `_frame_dt` was being computed as
`perf_counter() - _last_frame_end`, but `_last_frame_end` is set
AFTER `pygame.display.flip()` returns, so that delta was just the
`handle_input()` cost -- tens of microseconds, not the ~16 ms the
particle integrator needs.  Symptom: particles appeared to "update
every 5th frame" because each per-frame integration step advanced
the simulation by next to nothing, and the visible motion only
crossed a pixel boundary every several frames.

Fix: standard game-loop pattern -- carry the PREVIOUS frame's
measured wall time forward in `_prev_frame_ms` and feed it as `dt`
for the next frame's simulation step.  Initialised to one 60 Hz
frame so the very first iteration doesn't get `dt=0`.

### Left-panel height is now auto-computed (light sliders fix)

`LEFT_CONTROLS_H` was a fixed `274` magic number that went stale
when the button block grew (display toggles -> +UI/IO/Tools section
headers -> +ItemList button).  Symptom: the lighting sliders +
NMap/AO checkboxes dropped past the 274 px boundary and visibly
overlapped the tank-info tree below.

Fix: `_layout_widgets` now measures the actual bottom of the last
checkbox row and writes the result back into `self.LEFT_CONTROLS_H`
(instance attr shadows the class constant).  `_on_resize` was
reordered so `_layout_widgets()` runs BEFORE the info-panel
positioning, and the info-tree below reads the freshly-computed
value.  The class constant stays as a `max()` floor, so the panel
never shrinks below the original 274 px even on a hypothetical
zero-button layout.

Knock-on benefit: future button-group reshuffles no longer need a
matching `LEFT_CONTROLS_H` bump -- the layout self-adjusts.

### Force vsync on (`vsync=1` on every `set_mode`)

Both `set_mode` call sites (initial window create + the
`VIDEORESIZE` handler) now pass `vsync=1`, which pygame forwards to
`SDL_GL_SetSwapInterval(1)`.  Without this, `pygame.display.flip()`
returns the instant the swap is SCHEDULED, the driver can drop
or coalesce frames, and the wall-clock between flips is jittery
enough to make the FPS readout look like noise.  Drivers that
refuse vsync (some remote-desktop / virtualised GPUs) silently
ignore the request -- no fallback code needed.

The resize path matters because some platforms treat every
`set_mode` as a fresh window-create and reset the swap interval
to 0 unless we ask again.

### GPU-time profiling via `GL_TIME_ELAPSED` timer queries

Real GPU work-time now reads alongside the wall-clock FPS in the
title bar.  Pattern: ping-pong pool of two GL timer queries
(`glGenQueries(2)`); each frame `glBeginQuery(GL_TIME_ELAPSED, ...)`
at the start of the scene draw and `glEndQuery` right before
`pygame.display.flip()`.  On the SAME frame we then read the OTHER
query's result -- by definition that frame's GPU work is done,
so `GL_QUERY_RESULT_AVAILABLE` returns true and the fetch never
stalls.

Result is in nanoseconds; we convert to ms and accumulate in
`_gpu_accum_ms` parallel with the wall-clock accumulator, then
publish on the same 5-frame block boundary.  Title bar reads:

    TEPY  v1.34.0 -- 60.1 FPS  (cpu 16.65 ms / gpu  3.21 ms)  | ...

making "am I CPU-bound or vsync-bound?" trivially answerable.

Lazy init -- queries are created on the first `render()` call (GL
context exists by then).  Drivers without `GL_TIME_ELAPSED` support
fall through silently: `_gl_query_ids` becomes `[]` and the caption
collapses back to the wall-clock-only form.  Cleanup deletes the
queries on shutdown.

Note: PyOpenGL spells the result getter `glGetQueryObjectui64v`
(with trailing `v`), not `glGetQueryObjectui64`.  Caught the
import error before the runtime would have.

### Custom FPS counter -- 5-frame block average

The viewer no longer leans on `pygame.time.Clock` for FPS.  That
number was just the rate cap we asked for (60 with vsync), not what
the app actually achieved.  Now we measure wall-clock time between
successive `pygame.display.flip()` returns and average it over a
5-frame block:

  * Per frame, add `frame_ms` to a running accumulator.
  * On the 5th frame, divide -> mean ms, then `1000 / mean ms` ->
    FPS.  Refresh the title-bar caption with both numbers.
  * Reset accumulator and counter; start the next block.

Title bar now reads e.g. `TEPY  v1.33.0 -- 60.1 FPS  (16.65 ms)`,
which makes "am I CPU-bound or vsync-bound?" answerable at a glance.

A comment block at the FPS-init site documents the three OpenGL
"frame done" signals (`pygame.display.flip()` implicit, `glFinish()`
explicit blocking, `glFenceSync` non-blocking) for the next time
someone wants to add real GPU-time profiling.

### `TheItemList.xml` is now auto-built on first run

The file is `.gitignored` -- it's 70+ MB, machine-specific to whichever
WoT version / platform pkgs the user has installed, and trivially
regenerable from those pkgs.  Shipping it in the repo bloated the
download for no benefit.

When `viewer.py.__init__` finishes building the UI, it calls
`_ensure_itemlist()`, which:

1. Skips silently when `TheItemList.xml` exists AND parsed to a
   non-empty `_file_to_pkg` dict (steady-state).
2. Otherwise calls `_rebuild_itemlist_now()` -- the same code path the
   ItemList button uses.  The user sees per-step progress
   (`scanning N pkg archives`, `unique files: NNN,NNN`, etc.) stream
   into the in-app console instead of a silent hang on the splash.

Refactor: the rebuild logic was lifted out of
`_on_rebuild_itemlist_clicked` into a shared `_rebuild_itemlist_now`
helper so the button-click and auto-rebuild paths log identically.

### README.md + banner

`README.md` lands at the repo root (the GitHub-rendered front
page).  Hero banner is `resources/tepy_banner.png` -- a copy of
`splash.png` with "TEPY" + "Tank Exporter in PYthon" baked in via
PIL.  GitHub-rendered Markdown can't overlay text on images
natively, so the banner is a pre-composited PNG.

`cust_tools/make_banner.py` regenerates the banner from
`splash.png` whenever the title text or palette changes.  Pillow
only; bundled font candidates fall through Georgia Bold ->
Constantia Bold -> Cambria Bold -> Calibri Bold.  Title at 16% of
image height, subtitle at ~5.5%, both with a drop shadow + 8-way
nudge outline so they pop against the busy sepia background.

README content covers capabilities (read+render / diagnose / export
/ utilities), install (`go.bat` and the trio + manual fallback),
usage (button groups by IO / UI / Tools, camera + key reference),
requirements, donate link, and project layout.

### TEPY rebrand: window title + icon set + launcher / test bats

Added `start.bat` (minimal launcher -- no install dance, just runs
`python tank_viewer.py %*` and pauses on error so you can read the
traceback) and `test_icon.bat` -> `cust_tools/test_icon.py` (a
standalone pygame window that loads the tepee icon BEFORE
`set_mode` and reports the exact path / size / bit-depth used).
Useful when "the icon doesn't show" needs to be split into "icon
file is bad" vs "viewer doesn't attach it correctly" -- if the
test window has the right icon, the bug is viewer-side.

Two real fixes from a "no taskbar icon, title bar too thin" bug
report against the first cut:

* **Taskbar icon was the Python default.**  Fixed via
  `ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
  'mikeoverbay.TEPY.viewer.1')` called BEFORE the window opens.
  Without an explicit AppID Windows groups every script run by
  `python.exe` under one taskbar slot that uses the Python icon;
  setting an AppID makes TEPY a separate taskbar entity that
  honours the icon we install via `pygame.display.set_icon`.
* **Title-bar icon looked thin** because we were handing pygame a
  32x32 PNG that Windows then downscaled to 16 px for the title
  bar, blurring the silhouette.  Added 24 px PNG sidecar
  (`resources/tepy_icon_24.png`); viewer prefers it for
  `set_icon`, falls back to 32 / 48 / .ico in that order.
  `cust_tools/make_icon.py` now emits four PNG sidecars (24 / 32 /
  48 / 256) alongside the multi-size .ico.

Other icon fix that was wrong-on-arrival but caught in code review
before the user re-launched: the original `set_icon` call was AFTER
`set_mode`, which is a silent no-op on Windows (the window already
exists at that point with the default robot icon).  Order is now
AppUserModelID -> set_icon -> set_mode -> set_caption.

Pygame window caption now reads `TEPY  v<version>` instead of
`Tank Exporter PY`.  Both the static caption (set on init) and the
running-FPS caption (rewritten every frame) updated.

`resources/tepy.ico` is a procedurally-rendered tepee (American
Plains tipi) silhouette in the burnt-orange palette the rest of the
UI uses, packed at the seven sizes Windows requests for various
surfaces: 16, 24, 32, 48, 64, 128, 256.  The viewer now calls
`pygame.display.set_icon` on startup; missing icon file is a no-op
(falls back to the default Pygame robot).

Generator: `cust_tools/make_icon.py` -- a single Pillow-only Python
script that draws the icon at every size with size-aware proportions
(line widths and the decorative stripe-band scale or drop out below
~32 px so the silhouette stays legible at taskbar resolution).
Re-run after tweaking the colour constants at the top of the file
to re-skin every size at once; no external .ico authoring tool
required.

### Windows launcher: `go.bat`, `uninstall.bat`, `reinstall.bat`

`go.bat` is the everyday entry point.  Verifies Python is on PATH,
probes `import pygame, OpenGL, numpy, PIL`, and on an import miss
runs the install path:

1. Locate `requirements\requirements.txt` (or restore from
   `resources\requirements_backup\` if the live folder is gone).
2. Make a one-time permanent backup at
   `resources\requirements_backup\` -- never overwritten on later
   runs so a hand-edited backup survives.
3. `pip install --find-links requirements\ -r requirements\requirements.txt`
   (the `--find-links` lets pre-bundled wheels in `requirements\`
   take precedence over PyPI for fully-offline installs; harmless
   when there are no wheels there).
4. Re-verify imports.
5. Delete `requirements\` so the project tree stays clean -- the
   backup folder is now the on-disk source of truth.

Steady-state: every subsequent `go.bat` launch sees the imports pass
and jumps straight to `python tank_viewer.py %*` -- no scan, no
copy, no install spam.

`uninstall.bat` -- `pip uninstall -y pygame PyOpenGL numpy Pillow`.
Touches nothing in the project folder; `resources\requirements_backup\`
is preserved so reinstall.bat can put everything back later.

`reinstall.bat` -- "things went south" recovery.  Copies the backup
back to `requirements\`, then `pip install --upgrade --force-reinstall`
pulls every package fresh (PyPI by default, falls back to bundled
wheels when present).  Useful when a corrupted package or a Python
version change has broken the existing wheels.

### `cust_tools/rebuild_itemlist.py`

CLI tool that walks every kept pkg under `<wot>/res/packages` and
writes a comprehensive `TheItemList.xml` -- a complete index of every
game asset we could ever look up.  Replaces the runtime's incremental
build (`PkgExtractor._persist_entry`), which only ever cataloged files
we'd already looked up.  Symptom that drove the rebuild: the runtime
table had only 296 `scripts/item_defs/vehicles/*` entries because
`tank_def` XMLs weren't a thing the older code paths fetched; the
fresh rebuild pulls in 1497.

Usage:

```
python cust_tools/rebuild_itemlist.py
    # autodetect WoT install (NA/EU/RU/plain), default output path
python cust_tools/rebuild_itemlist.py --extensions xml dds primitives_processed
    # extension whitelist; everything else dropped
python cust_tools/rebuild_itemlist.py --include-all
    # skip the _is_excluded_pkg filter (scan map / event / audio bundles too)
```

Same exclusion filter the runtime uses (`PkgExtractor._is_excluded_pkg`)
so the rebuilt file matches what we'd discover via scan-fallback.
Atomic write via `.tmp` + `os.replace`.  Full G102 NA install: 59 kept
pkgs, ~496k entries, ~449k unique files, 70 MB output, ~3 s wall.

### Reload + invoke from the running viewer

`PkgExtractor.reload_lookup(path=None)` drops `_file_to_pkg`,
`_lower_to_actual`, `_missing`, and the queued `_pending_persists`,
then re-parses the lookup XML in place.  Used by the new in-app
button so a freshly rebuilt table is live immediately, no restart.

New left-panel button **ItemList** (Tools group) imports
`cust_tools.rebuild_itemlist` directly (no subprocess), runs the same
scan + write, then calls `reload_lookup()` and reports the new entry
count in the in-app console.

### Left-panel button regrouping (UI / IO / Tools)

Buttons in the left control panel are now grouped under three
section labels rendered above their rows:

* **UI** -- display toggles (Grid / Axes / Light / Orbit / Skybox /
  Wireframe) plus the windows the user opens to view things
  differently (Meshes, Flip, Compare).
* **IO** -- everything that touches disk: Set Paths configuration,
  Blender-bridge Import / Export (FBX / glTF / OBJ), Save Prim
  (.primitives_processed writer).
* **Tools** -- batch / utility actions; currently just ItemList.
  Reserved for the round-trip self-test, diagnostics dumps, etc.

Implementation: new `UIManager.add_panel_label` / `clear_panel_labels`
plus a `panel_labels` list rendered in the main render pass between
the panel backgrounds and the buttons.  `_layout_widgets` walks a
`button_groups = [(section_name, [(label, col, span), ...]), ...]`
structure, packs each row into the 3-column grid, and emits a header
label above every group.  Slider Y position reads the post-button
cursor directly so the layout self-adjusts when buttons are added or
removed.

### Diagnostic tooling extended

`cust_tools/__init__.py` added so the modules in that folder are a
proper Python package -- lets the viewer do `from cust_tools.X import
Y` instead of subprocessing.

### `cust_tools/rebuild_itemlist.py`

CLI tool that walks every kept pkg under `<wot>/res/packages` and
writes a comprehensive `TheItemList.xml` -- a complete index of every
game asset we could ever look up.  Replaces the runtime's incremental
build (`PkgExtractor._persist_entry`), which only ever cataloged files
we'd already looked up.  Symptom that drove the rebuild: the runtime
table had only 296 `scripts/item_defs/vehicles/*` entries because
`tank_def` XMLs weren't a thing the older code paths fetched; the
fresh rebuild pulls in 1497.

Usage:

```
python cust_tools/rebuild_itemlist.py
    # autodetect WoT install (NA/EU/RU/plain), default output path
python cust_tools/rebuild_itemlist.py --extensions xml dds primitives_processed
    # extension whitelist; everything else dropped
python cust_tools/rebuild_itemlist.py --include-all
    # skip the _is_excluded_pkg filter (scan map / event / audio bundles too)
```

Same exclusion filter the runtime uses (`PkgExtractor._is_excluded_pkg`)
so the rebuilt file matches what we'd discover via scan-fallback.
Atomic write via `.tmp` + `os.replace`.  Full G102 NA install: 59 kept
pkgs, ~496k entries, ~449k unique files, 70 MB output, ~3 s wall.

### Reload + invoke from the running viewer

`PkgExtractor.reload_lookup(path=None)` drops `_file_to_pkg`,
`_lower_to_actual`, `_missing`, and the queued `_pending_persists`,
then re-parses the lookup XML in place.  Used by the new in-app
button so a freshly rebuilt table is live immediately, no restart.

New left-panel button **ItemList** (Tools group) imports
`cust_tools.rebuild_itemlist` directly (no subprocess), runs the same
scan + write, then calls `reload_lookup()` and reports the new entry
count in the in-app console.

### Left-panel button regrouping (UI / IO / Tools)

Buttons in the left control panel are now grouped under three
section labels rendered above their rows:

* **UI** -- display toggles (Grid / Axes / Light / Orbit / Skybox /
  Wireframe) plus the windows the user opens to view things
  differently (Meshes, Flip, Compare).
* **IO** -- everything that touches disk: Set Paths configuration,
  Blender-bridge Import / Export (FBX / glTF / OBJ), Save Prim
  (.primitives_processed writer).
* **Tools** -- batch / utility actions; currently just ItemList.
  Reserved for the round-trip self-test, diagnostics dumps, etc.

Implementation: new `UIManager.add_panel_label` / `clear_panel_labels`
plus a `panel_labels` list rendered in the main render pass between
the panel backgrounds and the buttons.  `_layout_widgets` walks a
`button_groups = [(section_name, [(label, col, span), ...]), ...]`
structure, packs each row into the 3-column grid, and emits a header
label above every group.  Slider Y position reads the post-button
cursor directly so the layout self-adjusts when buttons are added or
removed.

### Diagnostic tooling extended

`cust_tools/__init__.py` added so the modules in that folder are a
proper Python package -- lets the viewer do `from cust_tools.X import
Y` instead of subprocessing.

---

## 2026-05-05

The `.primitives_processed` writer round-trips cleanly through both
TEPY and the WoT engine.  G102_Pz_III loaded from our re-encoded files
in `res_mods\<version>\` -- end-to-end pkg→encode→write→game pipeline
proven on a real tank.

### Two-mode encoder (`tankviewer/writers/_primitive_encoder.py`)

`.primitives_processed` files come in two layouts in the wild and the
visual-side reference style differs between them.  The writer now
detects the source layout from `mesh.name` (the section base name
preserved by `MeshParser.parse_primitives_processed`) and emits the
matching layout:

* **Bare-shared** -- one global `indices` + one global `vertices`
  section, with the indices header reporting N primitive groups + N
  per-group metadata entries.  Used by hull / turret on G102.  Detected
  by every `mesh.name == ''`.
* **Named-per-mesh** -- one `<base>.indices` + `<base>.vertices` (+
  optional `<base>.uv2`) per mesh, each with `group_count = 1` and a
  single primitive-group entry covering the whole local buffer.  Used
  by chassis / gun on G102.  Detected by every `mesh.name != ''`.

Mixed input (some named, some empty) raises `ValueError` rather than
silently corrupting the visual↔primitives binding.

The previous encoder always wrote a single bare-shared section pair
regardless of source -- that produced an "empty or out of range"
engine error on chassis because the visual references each primitive
group by name and the engine couldn't find a matching named section.

### Per-mesh format flags (encoder)

Format-string detection (`want_bones`, `want_tangents`) is now per-mesh
in named layout, file-wide in bare layout (since bare shares one
format string across the whole stream).  Previous encoder used `any()`
across all meshes for both layouts, which gave non-skinned meshes
phantom `iiiww` tokens in skinned files.

### UV2 sidecar preamble fixed (encoder + loader)

The BPVT-mirroring UV2 preamble is **136 bytes**, not 132:

```
0..67    primary format string ('BPVSuv2', 68 bytes)
68..131  secondary format string ('set3/uv2pc', 64 bytes)
132..135 uint32 vertex_count
136..    body (N pairs of float u, float v)
```

Mirrors the matching `.vertices` section's primary/secondary/count
preamble exactly.  The loader's probe list (`_UV2_PROBE_OFFSETS`)
previously had `(132, 68)`, which was a silent off-by-4 bug:
`(13240 - 132) // 8 = 1638` matched the expected vertex count by
integer-division coincidence (real value `1638.5`).  That meant we
were reading the count `uint32` as the first u-value and shifting the
entire UV stream forward by 4 bytes -- `uv1[0]` came back as
`(2.295e-42, 0.0)` (a denormal float).  We then wrote that garbage
back into the round-trip file at the same offset, so file sizes
matched and the bug stayed hidden.  Fixed:

* Probe list is now `(136, 68)`.
* Probe match additionally requires `(size - body_offset) % 8 == 0`
  to kill any future false-positive from integer-division truncation.

### Section names preserved verbatim (encoder + FBX exporter)

Encoder uses `mesh.name` directly; never falls back to
`mesh.identifier` (the WoT *visual-side* material label like
`tank_chassis_01_skinned`) -- substituting that as a section name
would break engine lookups.

FBX exporter (`exporters/common.py`) also drops the `_{i}` suffix it
used to append to every mesh name on the way out.  Original WoT names
(`exportChassL1_Shape`, `track_R_Shape`, etc.) go through verbatim;
Blender's own `.001` mechanism handles real collisions on import.

### `.fbm` folder no longer created on FBX export

`bpy.ops.export_scene.fbx(path_mode=...)` was `'COPY'`, which made
Blender clone every referenced texture into a `<basename>.fbm/` folder
next to the FBX -- duplicates of files we already wrote to the
`<basename>_textures/` sidecar folder.  Switched to `'AUTO'`: paths
in the FBX point at the existing `_textures/` folder, no copy.

### Per-component Save Prim feedback

Save Prim now logs a clear `Hull: written successfully -- wrote
Hull.primitives_processed (NN.N KB)` line per component (or
`FAILED -- <reason>`) in the in-app console + stdout.

### Diagnostic tooling

Two new scripts under `cust_tools/`:

* `dump_sections.py <local_file>` or `<pkg> <internal>` -- walks the
  trailing section-table-offset and prints every entry as
  `(size, name)`.
* `compare_sections.py <res_mods_tank_dir> <wot_packages_root>` --
  walks every `.primitives_processed` we've written under a res_mods
  tank folder, finds the matching original inside the WoT packages,
  and prints the section table side-by-side with size diffs.  How we
  verified G102_Pz_III round-trips at the section-table level after
  the UV2 preamble fix.

### Verified round-trip (G102_Pz_III)

| File         | Layout      | Sections                                  | Result |
| ------------ | ----------- | ----------------------------------------- | ------ |
| Hull         | bare        | `indices`, `vertices`                     | ✅ size + names match pkg |
| Turret_02    | bare        | `indices`, `vertices`                     | ✅ size + names match pkg |
| Gun_06       | named       | `gun_06_Shape.{indices,vertices}`         | ✅ size + names match pkg |
| Chassis      | named (4×)  | `track_{R,L}_Shape.{indices,vertices,uv2}`, `exportChass{L,R}1_Shape.{indices,vertices}` | ✅ size + names match pkg |

Game accepts the re-encoded files: tank loads in the garage and renders
with all components visible.

The non-byte-identical match between our output and the pkg original
in section bodies (bytes 8..63 of each section) is metadata BigWorld
embeds during compilation -- pointer-shaped values, sentinels (`-2` as
int64), small counts (`0x07FE`) -- that the engine does NOT validate
at load time.  Our writer leaves that area zero-filled and the game
still loads cleanly.

---

## 2026-05-04

Major architectural session.  Two big features, a polish pass, and a
documentation deep-dive against the official WoT format definitions.

### Compiled shader extraction (late session)

Cracked WoT's compiled-shader container so we can inspect any
shipped shader.  Layered format:

  `res/packages/shaders.pkg` (ZIP)  →  `<shader>.dx11.fxo` (ZIP)
  →  `effect` blob (`ARIEDX11` wrapper)  →  N × DXBC chunks
  (one per shader stage)

Each DXBC is the standard DX11 bytecode container with RDEF /
ISGN / OSGN / SHDR / STAT chunks; `fxc.exe -dumpbin -Fc`
produces a full HLSL-equivalent disassembly listing including
constant-buffer layouts, resource bindings, and input/output
signatures.

#### Tooling

New script: `tools/extract_shader.py` — usage:

  `python tools/extract_shader.py PBS_tiled_atlas`
  `python tools/extract_shader.py --list atlas`
  `python tools/extract_shader.py --dependencies <name>`
  `python tools/extract_shader.py --no-disasm <name>`

Auto-detects the WoT install (NA/EU/RU subdirs), auto-detects
the newest `fxc.exe` in any installed Windows SDK, splits the
.fxo into per-stage `.dxbc` files plus a parallel `.disasm.txt`
listing for each, classifies each blob as vs / ps / gs / hs /
ds / cs from the SHDR-chunk version dword, and dumps the source
`.fxh` dependency list to `dependencies.txt`.

#### Initial finding -- atlas buildings

`PBS_tiled_atlas.11` decompiled cleanly.  Constant buffer +
resource bindings reveal:

- 3 textures: `g_atlasAH` (atlas albedo+height), `g_atlasBlend`
  (tile-selection map), `g_dirt` (overlay).
- 3 tile tints: `g_tile0Tint`, `g_tile1Tint`, `g_tile2Tint`
  (float4 each).
- `g_atlasIndexes` (float4) + `g_atlasSizes` (float4) drive the
  per-pixel tile-offset math.
- VS input: position + packed normal/tangent/binormal + UV0 + UV1
  + per-vertex COLOR.  The COLOR is the 3-tile blend weight,
  which is exactly the vertex-colour stream we just wired up
  for tank meshes -- atlas buildings reuse the WoT-wide convention.

This means atlas buildings render via:

1. VS passes UV0 / UV1 / world-space tangent frame / vertex COLOR
   to PS.
2. PS samples blend map -> 3 tile indices, samples atlas 3× at
   those offsets, tints each with `g_tileNTint`, blends by COLOR
   weights, applies dirt overlay, runs PBR lighting.

### Format-definition deep-dive (late session)

Discovered that WoT ships authoritative element-type descriptions for
every vertex layout inside `res/packages/shaders.pkg` under
`shaders/formats/` -- 92 BWXML files including:

- `uv2.xml` -> `<TEXCOORD semanticIndex=1 stream=10 type=FLOAT2>`
  (confirms our sidecar-on-stream-10 reading).
- `colour.xml` / `colour2.xml` -> `<COLOR stream=11>` at semanticIndex
  0 and 1.  Revealed that the colour channel has TWO separate sets
  (`semanticIndex 0` and `1`) -- we were only claiming `.colour`.
- All `set3/*` formats use `UBYTE4_NORMAL_8_8_8` for normals and
  `FLOAT2` (8-byte) for UVs -- matches our BPVT path.
- All `set1/*` formats use `SHORT2` (4-byte) for UVs -- a layout we
  haven't seen in live tank data but which exists in the engine.
- Bone data is officially `SC_UBYTE4_MULT3_REVERSE_PADDED_IIIP` /
  `SC_UBYTE4_REVERSE_PADDED_PWWP` -- byte-reversed and padded.  The
  current viewer reads them as plain `4 × uint8` because we only
  round-trip through Blender colour attributes; respecting the
  reverse-padding will matter if we ever drive a skin cluster.

#### Loader fix
- `_COLOUR_SUFFIXES` widened to claim `.colour2` / `.colours2` /
  `.color2` / `.colors2` so tanks shipping a second colour channel
  don't drop the data.

#### Documentation
- `VISUAL_PROCESSED_FORMAT.md` gained a full "Official Format
  Definitions" section with the stream-assignment table, every
  element type from the format XMLs, the BPVT-vs-set3 naming
  reconciliation, and a SHORT2 caveat for the future.


### Dual mesh storage (`MeshSet` refactor)

Goal: hold both an imported FBX scene **and** the matching WoT-pkg scene at the
same time so the user can A/B-compare them and align mesh order before writing
back to `.primitives_processed`.

- New `MeshSet` class (top of `tankviewer/viewer.py`) holding every per-load
  attribute: `meshes`, `source_type`, `source_tank_name`, `exhaust_points`,
  `exhaust_pixie`, `armor_color`, `scene_bbox`, `tank_info`.  Has its own
  `cleanup()` so each set frees its GPU resources independently.
- `Viewer` now owns two of them — `self._fbx_set` and `self._pkg_set` — plus
  `self._active_set_name ∈ {'fbx', 'pkg'}`.  Properties forward `self.meshes`,
  `self.source_type`, `self._exhaust_points`, etc. to the active set so every
  existing call site keeps working unchanged.
- `_clear_scene()` clears **only the active set** — loading FBX no longer wipes
  a previously loaded PKG, and vice versa.
- `load_mesh` / `load_vehicle` switch active → `'pkg'` first; `load_imported_payload`
  switches to `'fbx'` first.  Each loader owns its own side.
- `_flip_active_set()` toggles which set is rendered, re-fits the camera,
  rebuilds the mesh-visibility window, restores the info panel + thumbnail +
  exhaust direction-vector lines + smoke emitters from whichever set you flip
  to.  No-op when the other side is empty.
- After `load_imported_payload`, when the FBX carried a `_WOT_TANK_<name>`
  empty (or the filename matches a known WoT tank XML), the matching pkg
  geometry is **auto-loaded** into `_pkg_set`.  The user keeps seeing the FBX
  they just imported but Flip / Compare work immediately.
- `_align_fbx_to_pkg_order()` reorders the FBX-set meshes so each index lines
  up with the PKG-set's order — the foundation for writing back to
  `.primitives_processed`.  Match key is `f"{display}_{i}"` (case-insensitive,
  `.001` suffix tolerated).  Runs automatically right after the auto-load.

### Top-bar UI

- New **Flip** button (row 4, left half) — calls `_flip_active_set`.
- New **Compare** button (row 4, right) — opens a tkinter Treeview window
  showing per-mesh stats side-by-side: `#`, `name`, `verts`, `faces`, `inds`,
  `UV2`, `Col`, `fmt` for both FBX and PKG sides.  Mismatched rows
  (different vert/index counts) highlighted.  Same data dumped to console.
- `LEFT_CONTROLS_H` bumped from 222 → 248 to fit the new row.
- Slider-y0 layout offset moved from `4 *` to `5 *` button rows.

### Mesh display name fixes

- `_populate_mesh_window` fallback chain corrected: `mesh.identifier` →
  `mesh.name` → `f'mesh_{i}'`.  Previously imported FBX meshes showed
  `mesh_0..N` because `mesh.identifier` is empty for non-WoT loads.
- `exporters/common.py` exporter name now uses
  `(identifier or name or f'mesh_{i}') + f'_{i}'` instead of bare
  `mesh.name + f'_{i}'`.  Stops Blender showing hull/turret as `_4` / `_5`
  when the source primitive-group section name was empty.

### UV2 support — interleaved formats

- `MeshParser._parse_vertices` detects `has_uv2 = format_str.count('uv') >= 2`.
- Static `stride_map` extended with the eight dual-UV variants
  (`xyznuvuv`, `xyznuvuvtb`, `xyznuvuviiiww`, `xyznuvuviiiwwtb`, plus their
  BPVT counterparts).
- New `_compute_stride()` walks the format string token-by-token
  (`xyz=12`, `n=4 packed / 12 real`, `uv=8` each, `iii=4`, `ww=4`, `tb=8`
  each) and acts as a fallback when a format isn't in the static map —
  no more falling back to a hardcoded stride 32 that misparses.
- Per-vertex loop reads 8 extra bytes immediately after `uv0` when UV2 is
  present (matches WoT's `xyznuvuv*` layout).
- Returned vertex dict gains a `'uv1'` key (None when format had no UV2).

### UV2 support — sidecar `.uv2` sections

- Section grouper now claims four suffixes per primitive group:
  `.vertices`, `.indices`, `.uv2`, `.colour`.  Generous on UV2 spelling
  (also tries `.uvs2`, `.uv1`, `.uvs1`, `.uvb`, `.uvsb`, `.uv`, `.uvs`)
  so a one-character WoT typo doesn't cost us the data.
- New `MeshParser._parse_uv2_section` reads the layout
  *(64-byte format string, `uint32` count, then float32 (u,v) pairs)*
  but **probes both 68-byte and 132-byte preambles** because BPVT-format
  primitive groups carry an extra 64-byte BPVT header on their UV2
  sidecar too.  Picks whichever offset gives `(size - offset) / 8 ==
  expected_count`.
- Doesn't trust the in-section `count` field (one observed live tank has
  it set to 0 while the body still carries the correct number of pairs).
- Sidecar UV2 also lands in `vertex_list['uv1']` so downstream code is
  format-agnostic.
- `mesh.uv1` is None when neither path applied.
- Diagnostic logging: `*** UV2 DETECTED ***` per group, `[uv2] '<name>':
  format=...  body_offset=...` on a successful sidecar parse,
  per-file `UV2 SUMMARY: N of M group(s)` roll-up plus a `Distinct
  vertex formats in this file:` list.
- Per-group section flag in the load log is now
  `[+v +i +u2 -c]` — instantly says which sidecar sections each group has.

### `.colour` sidecar sections

- New `MeshParser._parse_colour_section` mirrors the UV2 parser but
  with 4-byte BGRA `uint8` entries.  Probes both 68 and 132-byte
  preambles.  Decodes BGRA → RGBA float in [0, 1] so downstream code
  can multiply against texture samples without an extra normalisation
  step.
- `Mesh.colour` field captures it; None when absent.
- `COLOUR SUMMARY:` log line surfaces it for tanks that ship per-vertex
  colours.
- Compare dialog gains a `Col` column.

### FBX round-trip for UV2 (Blender bridge)

- `exporters/common.py` payload gains `'uvs2'` (None when WoT side had no UV2).
- `exporters/_blender_runner.py` creates a second `UVMap2` Blender layer when
  `uvs2` is present; per-loop UV writes mirror the primary path.
- `exporters/_blender_importer.py` walks UV layers by index — slot 0 →
  `uvs`, slot 1 → `uvs2` — so any rename in Blender survives the
  round-trip.  Returns `'uvs2'` in the per-mesh payload.
- `viewer.load_imported_payload` reads `uvs2` off the payload and stores
  it as `uv1` in the parsed_group dict; `Mesh.__init__` already picks
  it up via `parsed_group['vertices'].get('uv1')`.
- Result: an A38_T110E4 export now ships UV2 on the track meshes;
  re-importing brings it back, and the Compare dialog shows **Y** in
  both FBX and PKG UV2 columns for the matching rows.

### Mesh class additions

- New `self.format` field stores the source vertex format string
  (`'xyznuvtb'`, `'BPVTxyznuviiiwwtb'`, `'imported'` for FBX, etc.).
  Read by Compare dialog.
- New `self.uv1` field — second UV channel, None when absent.
- New `self.colour` field — per-vertex RGBA float (already swizzled
  from BigWorld's BGRA `uint8`), None when absent.

### Compare dialog (tkinter Treeview, non-modal)

- Side-by-side stats grid:
  `# | name | verts | faces | inds | UV2 | Col | fmt` for FBX and PKG.
- Mismatch tag highlights rows where vert or index count differs between
  the two sides (background colour `#fff0e0`).
- Console fallback: if tkinter is unavailable, dumps the same data as
  formatted text.  Always also prints to console (so the same data is in
  the terminal log).
- Pumped via `win.after(33, _pump)` so the pygame loop keeps running
  while the compare window is open — non-modal A/B-compare workflow.

---

## 2026-05-03

Imported from the prior conversation's summary; finer-grained ordering not
recoverable.  This is the day the README's `Last updated` field was set.

### FBX / GLB / OBJ import via Blender bridge

- New `Import` button on the top bar.
- `tankviewer/exporters/_blender_importer.py` runs inside a headless
  Blender subprocess, walks every imported mesh, and emits a JSON
  payload (`positions`, `normals`, `uvs`, `indices`, decoded `WoT*`
  per-vertex color attributes for tangent / binormal / bone idx /
  bone weight, `model_matrix`, `diffuse_path`).
- `viewer.load_imported_payload` swizzles Blender Z-up → OpenGL Y-up
  (`(x, y, z) → (x, z, -y)`), re-builds `Mesh` objects and uploads new
  VAOs.
- Tank-name embedded as a magic-named child Empty `_WOT_TANK_<basename>`
  rather than a Blender custom property (per user preference).  The
  importer recovers the name and the viewer auto-resolves engine-exhaust
  hardpoints from the WoT install.
- `'fbx'` / `'wot'` source-type flag drives shader selection; FBX
  imports use a simpler diffuse + bump shader (`shaders/imported.frag`).

### `Compare` of FBX vs PKG (precursor)

The `Compare` button itself landed today (2026-05-04) but the
infrastructure that makes both sets meaningful — the dual `MeshSet`
storage, the round-trip-preserving FBX export with WoT* color
attributes, and the source-type flag — all came from this run.

### Mesh-window display fix

- `_populate_mesh_window` extended to fall back through `mesh.identifier`
  → `mesh.name` → `mesh_{i}` so imported FBX meshes show their actual
  object names in the Mesh window.

### Camera defaults

- Camera reset now lands at `yaw=225` (45° to the right of the front),
  `pitch=+30` (above looking down).  Old `-30` setting hid the smoke
  beneath the hull.

### Status callback for tank loading

- `UILoadTankDialog.set_status()` and a status callback chain through
  `load_vehicle` so the bottom of the load dialog ticks
  `Hull → Turret → Gun` while the load progresses (each component takes
  ~0.3-0.5 s).

### Texture-resolution case-insensitive fallback

- `PkgExtractor` extended with `_lookup_case_insensitive` /
  `_index_pkg_lower`.  Fixes IT21_Lion turret textures: the visual file
  references `Lion_KL_3Dst_turret_01_AM.dds` (lowercase 's') but the
  actual asset is `Lion_KL_3DSt_turret_01_AM.dds` (uppercase 'S') —
  artist typo on the WoT side.  Case-insensitive fallback claims it
  cleanly.

### FBX export polish (continued)

- `exporters/common.py` adds `source_tank` to the export payload.
- Texture sidecar folder (`<basename>_textures/`) holds copies of
  every referenced AM / NM / AO / GMM so the FBX is self-contained.

### UI / control layout

- Left & right side panels host all controls; 3D viewport spans the
  full window height between them.
- Slider widths down ~10 % so they fit better.
- Selected tank tree node stays highlighted in burnt orange.
- Tier tabs collapse all nation trees on each tab.
- Buttons turn burnt orange when toggled on.
- Smoke controls live in a dropdown under the tier tab control set.
- Tab-content tree array — populated once at startup, hidden/shown
  per-tab — so tab clicks are O(1) instead of rebuilding the tree.
- Pan-speed slider removed; mouse pan baked to `0.20`.

### Smoke / particle system

- `ParticleShader` + `FlipbookTexture` + `ParticleSystem`.  91-frame
  flipbook at `resources/smoke/`, second 91-frame set at
  `resources/fire/` (currently empty pending PNG restore).
- Camera-facing billboards with start/end size, speed, fade-start
  frame, fade-end frame sliders; persisted to `tankviewer.json`.
- Engine-exhaust hardpoints discovered via
  `VisualLoader.find_exhaust_nodes`, filtered against the def XML's
  `<exhaust><nodes>` list, applied with the BW → GL Z-flip and the
  per-component world offset.
- Cyan direction-vector lines from each hardpoint (0.5-unit length)
  for visualisation.
- HP-marker sphere can be toggled on/off.

### IBL / PBR pipeline tweaks

- Light slider max 0.25, default 0.10.
- Mix (tint) hardwired to 1.0.
- Ambient default 0.5, max 1.0.
- All slider values persisted to `tankviewer.json`.

### Detail map cache

- Shared scratch noise (metallicDetailMap from visual_processed) is
  now resolved through `_resolve_detail_map_path`, which checks
  `resources/` first and only extracts from the WoT pkg as a fallback.
- GPU upload cached in `_shared_tex_cache` so all sub-meshes share
  one texture object.

---

## 2025-05-01 → 2026-05-02

Initial implementation and prior milestones — granular dates not preserved.

- Binary parser for `.primitives_processed` mesh format.
- `.visual_processed` material / texture extractor (BWXML → ElementTree).
- `PkgExtractor` over the WoT install, indexed by `TheItemList.xml`.
- Vehicle XML loader assembling hull + chassis + best turret + top gun
  with correct per-component offsets, with optional damaged variant.
- Tank browser tree, hover thumbnails, persistent loaded thumbnail.
- PBR rendering pipeline: GGX direct lighting + split-sum IBL
  (Lambertian irradiance, GGX prefiltered specular, BRDF LUT) baked
  from the skybox cubemap on startup.
- Per-nation armor-color tinting.
- Texture / AO / GMM channel handling: skinned vs non-skinned alpha
  routing (`ANM.R` for skinned + alpha-tested static, `AM.A` otherwise).
- Interactive trackball camera (orbit / pan / zoom) with auto-fit on load.
- 2D overlay: Grid / Axes / Light / Skybox / Wireframe toggles, Light /
  Ambient sliders, NMap / AO checkboxes.
- Persistent JSON config (`tankviewer.json`) for `pkg_dir` / `res_mods` /
  `lookup_xml` and slider values.
- FBX / GLB / GLTF / OBJ **export** via Blender bridge (companion to
  the Import added 2026-05-03).
- Mesh visibility window (per-mesh checkbox toggle to hide sub-meshes).
- Set-Paths dialog for first-time WoT-install setup.
- Splash screen.
- 988 bundled tank thumbnails with progressive `_TOKEN`-trim
  fall-through for fuzzy matching.

See `README_TANK_VIEWER.md` for the user-facing feature list and
`ARCHITECTURE.md` for the per-module reference.

---

## File-format support summary (current)

| Layer                  | Status                                                                                   |
| ---------------------- | ---------------------------------------------------------------------------------------- |
| `.primitives_processed` vertex stream | xyz / packed-or-real normals / UV0 / **UV1 interleaved** / iii / ww / tb       |
| `.primitives_processed` sidecar sections | `.vertices`, `.indices`, **`.uv2`**, **`.colour`**                          |
| Index formats          | 16-bit and 32-bit                                                                        |
| Vertex format strings  | All `xyzn*` variants in the static map plus dynamic stride for unknowns                  |
| BPVT mode              | 132-byte preamble for vertices and parallel UV2 / colour sections                        |
| FBX round-trip         | UV0 + **UV1**, tangent / binormal / bone idx / bone weight via WoT* color attributes    |
| Vertex colour          | Read-only (parsed but not yet round-tripped through Blender or written back)             |
