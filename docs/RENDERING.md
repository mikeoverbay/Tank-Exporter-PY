# Rendering — render passes, draw order, mesh gates

> Lives in: `tankExporterPy/viewer.py` (the `render()` method
> starts ~line 8829 and runs to ~line 9930).  Per-mesh GPU state
> in `tankExporterPy/mesh.py`; shader programs in
> `tankExporterPy/shaders.py`; GLSL sources under `shaders/`.

The viewer renders one tank into one full-window GL viewport per
frame.  Multiple passes share the same `self.meshes` list; each
pass iterates and gates differently.  Gate convergence is a
recurring source of bugs (rubber-band track-skip is one such case
— see notes below).

---

## Per-frame pass order

```
 1.  Skybox (camera-locked infinite via z=w trick; depth_test on,
       depth_write off)
 2.  SkyDome (finite-radius sphere at world origin, equirect
       panorama OR cubemap; depth_test OFF, depth_write OFF;
       two passes for seam-blend per 1.185.3)
 3.  Terrain (procedural displaced grid; depth_test on, depth_write on;
       u_world_clip_radius discards past dome circle)
 4.  Shellhole decal pass (screen-space volumetric cube projector;
       depth_test on, depth_write OFF; alpha-blended; reads scene
       depth via _grab_scene_depth which at this point holds
       terrain-only Z so reconstruction lands on the ground;
       see "Aim cursor + shellhole decal subsystem" below)
 5.  Aim-point marker (per 1.204.0 -- TERRAIN hit: SS volumetric
       cube projector renders the reticle on the ground.  DOME
       hit: reticle SKIPPED (sky is a backdrop, decal added
       noise).  Both branches: blue debug ball renders at
       _aim_hit_world as an opaque depth-tested mesh -- this
       is the "you are aiming here" cue regardless of surface.
       Gated on _aim_hit_world is not None.)
 6.  Background helpers (grid, axes; depth test off)
 7.  Look-at crosshair (iff middle-mouse held -- PINK lines)
 8.  Aim ray (iff Debug + tank loaded -- single cyan line down-range)
 9.  Tank physics step (if Susp + Terrain + tank loaded)
        chassis_pose = tank_physics.update(...)
        clamp tank_physics.pos.xz to (dome_radius - 3) (per 1.192.0)
        for m in self.meshes: m.model_matrix = chassis_pose @ m.bind_model_matrix
10.  Solid mesh pass        -- main PBR / imported / shaded shader
11.  Wireframe overlay      -- iff Wireframe toggle (line GL_LINE pass)
12.  Surface-normal debug   -- iff Normals slider > 0 (geometry-shader pass)
13.  Hardpoint markers      -- iff Debug (HP_* sphere + arrow gizmos)
14.  Particles (ALPHA group): shot_trail smoke -> muzzle smoke ->
       fire_billboards -> impact dust (depth_test OFF per 1.204.0
       so impacts on the dome aren't depth-rejected -- see
       "Aim cursor + shellhole decal subsystem" below)
15.  Particles (ADDITIVE group): muzzle flash -> impact fire
       (depth_test OFF as well, same reason)
16.  Contact-stars overlay  -- iff Susp + Debug (per-wheel terrain markers)
17.  Hull bbox              -- iff Debug (chassis AABB wireframe)
18.  Picker FBO (off-screen)-- iff Pick Tri toggle
19.  Picker overlay         -- highlights the picked triangle
20.  UI (panels, tree, dialogs)
21.  Physics-timer overlay  -- iff Susp (grass-green text upper-left)
```

The ALPHA-then-ADDITIVE rule (14 -> 15) matters: alpha layers drawn
AFTER an additive layer darken the additive pixels via the
`(1 - src_alpha)` term, even when the alpha layer is supposed to
be physically BEHIND.  See 1.172.0 CHANGELOG for the muzzle-flash
darkening bug fix.

Why shellhole decals are at #4 (not in the alpha particle group):
the SS volumetric projector reads scene depth in its frag stage
to reconstruct the surface it should paint on.  If the projector
runs AFTER an opaque mesh pass (tanks), the depth buffer at the
decal pixels holds the tank's depth and the reconstruction snaps
onto the tank surface or discards against the cube volume.  By
running before any opaque mesh, scene depth contains only the
terrain heightmap -- the reconstruction lands on the ground
where the impact happened.  Tanks rendering later opaquely cover
the alpha-blended decal at tank pixels via the normal depth
replacement.  See 1.199.0 + 1.200.0 CHANGELOG.

Items 9-11 ALL iterate `self.meshes`.  Item 17 also iterates meshes
(in `picker.py:362`).  Skip filters MUST be applied to all four
sites consistently or you get phantoms (the hull is wireframed
through the rubber-band track ribbon you skipped, etc.).

---

## Mesh draw gates -- the four iteration sites

| Pass | File / line | Skips on |
|------|---|---|
| Solid    | `viewer.py:9586` | `mesh.visible == False`, `commander mode and component in (turret, gun)`, `RENDER_RUBBER_BAND_TRACK == False and name matches track_*Shape*` |
| Wireframe| `viewer.py:9659` | same set |
| Normals  | `viewer.py:9704` | same set + `mesh.vao is None` |
| Picker   | `picker.py:362`  | `mesh.visible == False`, `mesh.vao is None`, `name matches track_*Shape*` |

All four read the same `mesh.name.startswith('track_') and 'Shape' in name`
filter.  Maintain symmetry — a mesh that's visually hidden but
still picker-clickable is a UX bug.

---

## Camera modes

| Mode | Code | Description |
|------|------|-------------|
| 0 | orbit     | Trackball orbit, mouse-driven, free-fly.  Default for inspecting a static mesh. |
| 1 | chase     | **DEFAULT at startup.**  Anchored behind + above + driver-side of the chassis; rotates with the chassis pose so it follows pitch / roll / yaw. |
| 2 | commander | Anchored at hull centre above the chassis, looking forward along the visible-front axis.  Turret + gun meshes are HIDDEN in this mode for a clear view-out. |

`C` cycles modes.  `R` resets the orbit camera to fit-to-bounds.
Mode is **NOT persisted** to JSON — always starts at 1 on a fresh
process AND on every tank reload (so a session that ended in
commander view doesn't surprise the user by reopening with the
turret hidden).  Wired in `viewer.py:1011` (default) and `viewer.py:7016`
(per-load reset).

---

## Look-at crosshair

`viewer.py:9905-9924`.  Pale-pink lines (1.0, 0.75, 0.85) spanning
10 units along each world axis (X / Y / Z), centred on
`camera.center`.  Drawn AFTER every tank-mesh / wireframe / picker
pass so the tank doesn't overdraw it.

**Depth test ON** (per Coffee_'s 2026-05-08 call): when the look-at
point is buried inside geometry the lines emerge only at the surface,
which reads as the natural "your aim point is here" cue.  Earlier
revs disabled depth test so the lines were always fully visible —
revisit only if the always-on look becomes desirable for some new
inspection mode.

Visibility gated by `_show_lookat_lines` = `pygame.mouse.get_pressed()[1]`
(middle / wheel button held).

---

## Aim cursor + shellhole decal subsystem

> Sources:
> `tankExporterPy/viewer.py` (driver + per-frame render block),
> `tankExporterPy/particles.py` (`AimCrosshair`, `ScreenSpaceDecals`,
> `_build_decal_matrix`, `_load_decal_tex_2d`),
> `tankExporterPy/scene.py` (`Sphere`),
> `shaders/decal_project.{vert,frag}` (volumetric projector),
> `shaders/decal.{vert,frag}` (flat-quad fallback).

The aim cursor (where the player's mouse points at the world)
and the shell-impact decals (where projectiles have hit) share
the same screen-space volumetric decal pipeline -- one unit cube
per decal, frag stage reconstructs world position from a scene-
depth snapshot, samples the texture at `local.xz + 0.5`.  The
cursor reticle uses the same projector as the shellholes but
with a single-decal entry point that disables age-fade.  A blue
opaque sphere paints the exact aim point on top of the reticle.

Final per-frame order in `Viewer.render()` (line numbers
approximate; see `docs/INDEX.md` -> `RENDERING.md` for the
top-level pass list):

```
terrain.render          (writes depth)
shellhole-decal pass    (per active impact; alpha; no depth write)
aim-marker pass         (one reticle + one ball; reticle is alpha;
                         ball is opaque with depth write on)
... tanks render later, naturally cover decals + ball at tank pixels
```

### Driver: `Viewer._drive_aim_from_aim_state(...)`

**Where:** `tankExporterPy/viewer.py` ~line 10000.  Runs once per
frame from the input / pre-render pump (well before
`Viewer.render()`).

**What it does:** computes the world-space target of the player's
aim ray (mouse cursor when free, or chase-cam aim when chase
mode active) and stashes the result on the Viewer instance so
the render block can read it directly.

**Inputs (reads from `self`):**
* `self.camera` -- for the inverse view-proj.
* `self.ui` / mouse position -- screen-space cursor pixel.
* `self.terrain` -- for the height-field raycast.
* `self.skydome.radius` -- for the dome fallback intersection.

**Outputs (writes to `self`):**
* `self._aim_hit_world` -- `(3,)` float32 world point of the
  cursor's target.  `None` if the ray missed everything.
* `self._aim_hit_kind` -- one of `'terrain'`, `'dome'`, `'sky'`.
  The render block dispatches on this.
* `self._aim_hit_normal` -- inward sphere normal at the dome hit
  (only meaningful when `_aim_hit_kind == 'dome'`).  Used by the
  flat-quad projector so the cursor decal sits tangent to the
  dome surface.

**Expects:** to run BEFORE any render call in the frame, so by
the time `Viewer.render()` reaches the aim-marker block,
`_aim_hit_world` is valid.  This contract is what lets the
cursor draw live LATE in the pass order (after shellholes) --
1.190.0 - 1.199.0 had the projection running inline with the
render loop and needed an early-frame draw site.  See 1.200.0
CHANGELOG.

### Driver: `Viewer._grab_scene_depth(x, y, w, h)`

**Where:** `viewer.py` ~line 9638.

**What it does:** snapshots the framebuffer's current depth
buffer into a cached `GL_TEXTURE_2D` via `glCopyTexImage2D`.
Returns the GL texture id.

**Inputs:** viewport rect `(x, y, w, h)` in pixels, bottom-left
origin.  TEPY's UI panels push the viewport away from `(0, 0)`,
so `x, y > 0` in practice.

**Output:** integer texture id (or `0` on failure).  The same
texture is re-used across frames -- only re-allocated when the
viewport size changes.

**Expects:**
* The default framebuffer has a `DEPTH_COMPONENT24` depth
  attachment (true for the SDL2/pygame window TEPY creates).
* Whatever depth values the caller cares about are CURRENTLY
  in the depth buffer.  Calling this before any render pass
  gives a cleared buffer (all 1.0s); calling after terrain
  gives terrain Z only; calling after tanks gives terrain +
  tank Z.  **The shellhole-decal pass and the cursor pass
  both call this BEFORE any tank render** so the texture
  holds terrain-only Z.

The SS frag shader (`decal_project.frag`) samples this texture
at `(gl_FragCoord.xy - u_viewport_origin) / u_resolution` to
recover the scene Z at each cube fragment.

### Render block: aim marker (one draw, post-shellholes)

**Where:** `viewer.py` ~line 18245, in `render()`, immediately
after the shellhole-decal block.

**Gates:** `if self._aim_hit_world is not None:` -- nothing else
gates this; in particular **not gated on tank load**.  The cursor
draws whenever the aim ray has a target.

**Dispatch logic (post 1.204.0):**

```
if _aim_hit_kind == 'dome' and _aim_hit_normal is not None:
    # No decal on the dome (per Coffee 1.204.0 -- the sky is
    # a backdrop and a projected reticle on it added noise).
    # Earlier (1.201.0 - 1.203.0) we tried various surface-
    # aligned matrix rotations to land a reticle on the dome
    # via the SS projector; none of the iterations converged
    # to what the user wanted.  Reverted, kept just the ball
    # below as the dome "aiming here" cue.
    pass
else:
    # Terrain path -- compute the surface normal via 4-tap
    # finite difference on the heightmap (used by the flat-
    # quad fallback; the SS projector ignores it).  Prefer
    # the SS volumetric projector (conforms to terrain bumps),
    # fall back to flat quad if the SS shader didn't compile.
    if aim_crosshair_ss and ss_decal_shader:
        depth_tex = _grab_scene_depth(...)
        aim_crosshair_ss.render_single(pos, n_up, ss_decal_shader,
                                         view, proj, depth_tex, ...)
    elif aim_crosshair and decal_shader:
        aim_crosshair.render(pos, n_up, decal_shader, view, proj)

# Always: opaque blue sphere at the same world point.  This
# is the dome's only "aiming here" cue since the reticle is
# suppressed there.
glEnable(GL_DEPTH_TEST)
aim_hit_sphere.render(color_shader, _hit_model, view, proj)
```

**Depth state:** the reticle pass (SS or flat) uses
`glDepthMask(GL_FALSE)` internally; the ball pass leaves depth
mask at the default `GL_TRUE`.  Because the ball runs AFTER the
shellhole-decal pass at this site, its depth write no longer
contaminates the decals' `_grab_scene_depth` snapshot.

**Failures are caught + log-once:** the whole block is wrapped
in `try / except`; any exception sets `_aim_ball_err_logged` so
the message prints once per session and doesn't spam the
console every frame.

### Shellhole decal render

**Where:** `viewer.py` ~line 18187, in `render()`, immediately
after `terrain.render`.

**Gates:** `if self.impacts.has_alive:` -- only runs when there
are live impacts to paint.

**What it does:** one screen-space decal cube per active impact.
Each cube's matrix is built from the impact's `(pos, normal)`
via `_build_decal_matrix(...)`; the impact's age fraction drives
the shader's `u_age_frac` for fade-out.

**Path:**
```
if shellhole_ss_decals and ss_decal_shader:    # primary
    depth_tex = _grab_scene_depth(...)
    shellhole_ss_decals.render_impacts(impacts.active_impacts,
                                         ss_decal_shader, view,
                                         proj, depth_tex, ...)
elif shellhole_decals and decal_shader:         # legacy fallback
    shellhole_decals.render(impacts.active_impacts, decal_shader,
                              view, proj)
```

**Expects:** scene depth contains terrain-only Z when
`_grab_scene_depth` is called.  This is why the shellhole block
sits BEFORE any opaque mesh pass (tanks, hardpoint spheres,
etc.) -- if tanks had already written depth, the SS frag
shader would reconstruct onto tank surfaces and discard
against the cube volume.

### `tankExporterPy/particles.py::_build_decal_matrix(pos, normal, size_x, size_y, size_z)`

**What it does:** composes a 4x4 row-major matrix that maps the
unit cube `[-0.5, 0.5]^3` into the world-axis-aligned volume
centered at `pos`.

**Args:**
* `pos` -- `(3,)` world center of the decal.
* `normal` -- `(3,)` surface normal.  **Currently ignored** (per
  Coffee 2026-05-15: the matrix is always world-axis-aligned so
  the texture's compass directions stay fixed regardless of
  ground slope; rotating by a frisvad basis spun the N/E/S/W
  marks arbitrarily on hillsides).  Kept in the signature for
  API back-compat; future per-surface decals (tank hulls, etc.)
  will branch on surface kind and reintroduce a rotation path.
* `size_x` -- world-X span in metres.
* `size_y` -- world-Z span in metres (named *_y* historically
  to match the shader's UV; the actual world axis is Z).
* `size_z` -- world-Y projection-box thickness.  Half-extent
  above + below the surface point.

**Output:** `(4, 4)` float32 row-major matrix.  Maps:
* local +X axis -> world +X (scaled by `size_x`).
* local +Y axis -> world +Y (scaled by `size_z`) -- the
  projection ("depth") axis.
* local +Z axis -> world +Z (scaled by `size_y`).

The frag shader samples the texture as `local.xz + 0.5` to
keep the ground plane = world XZ.

### `tankExporterPy/particles.py::_load_decal_tex_2d(png_path)`

**What it does:** uploads a PNG into a 2D GL texture with
mipmaps, clamp-to-edge, linear filtering, and a vertical flip
on load.  Returns the texture id.

**Flip:** the PNG file has top-left origin; OpenGL UV (0, 0) is
bottom-left.  `Image.transpose(FLIP_TOP_BOTTOM)` aligns the two
so UV `(0, 0)` reads the bottom-left of the original PNG.

**Used by:** `ScreenSpaceDecals.__init__` for the albedo (and
historically NM + GMM, though the PBR branch is currently
disabled -- see 1.185.0 CHANGELOG).

### `tankExporterPy/particles.py::ScreenSpaceDecals`

The volumetric projector.  One instance per decal kind
(shellholes, cursor).  Each instance owns its own albedo
texture, cube VBO/VAO, and persistent tunables.

**Constructor:** `ScreenSpaceDecals(png_path, size, thickness,
lifetime_s, fade_start)`.
* `size` -- world-XZ span (square footprint).
* `thickness` -- world-Y projection-box span.
* `lifetime_s` -- seconds before a decal fully fades out.  The
  cursor entry point disables this by pinning `u_fade_start`
  past 1.0.
* `fade_start` -- 0..1 age fraction where the linear fade-out
  begins (typical: 0.85 = fade over the last 15 % of life).

**`_begin_pass(shader, view, proj, scene_depth_tex,
viewport_w, viewport_h, viewport_x, viewport_y, ...)`:**
* Sets GL state:
  ```
  glEnable(GL_BLEND); glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
  glEnable(GL_DEPTH_TEST); glDepthMask(GL_FALSE)
  glDisable(GL_CULL_FACE)   # cube needs both faces drawn
  ```
* Uploads shared uniforms: `u_view`, `u_proj`, `u_resolution`,
  `u_viewport_origin`, `u_global_alpha`, `u_fade_start`.
* Binds albedo to texture unit 0, scene-depth tex to unit 1.

**`_end_pass()`:** restores `GL_CULL_FACE`, `glDepthMask(GL_TRUE)`,
disables `GL_BLEND`, unbinds the depth-tex unit (so the unit
doesn't accidentally inherit into later passes).

**`_draw_one(pos, normal, age_frac, shader, view, proj,
size=None, thickness=None)`:** builds the per-decal matrix
via `_build_decal_matrix`, computes `inv(proj * view * M)`,
uploads `u_decal_matrix`, `u_inv_view_proj_decal`,
`u_age_frac`, then issues `glDrawElements` against the cached
unit-cube VAO.

**`render_impacts(impacts, shader, view, proj, scene_depth_tex,
viewport_w, viewport_h, viewport_x, viewport_y)`:** iterates
the list of `Impact` objects (which carry `pos`, `normal`,
`age` fields), converts each age to a fraction
`age / lifetime_s`, and dispatches to `_draw_one` for each
live decal.  Used by the shellhole pass.

**`render_single(pos, normal, shader, view, proj,
scene_depth_tex, viewport_w, viewport_h, ...)`:** convenience
entry for a single decal at age=0 with fade disabled (sets
`u_fade_start = 2.0` so the age-fade term resolves to "always
opaque").  Used by the aim cursor on terrain hits.

**Cleanup:** `cleanup()` deletes the GL textures + VBO/VAO when
the Viewer shuts down.

### `tankExporterPy/particles.py::AimCrosshair`

The flat-quad fallback for the cursor on terrain when the SS
decal shader failed to compile (graceful degradation -- the
cursor still draws, just as a textured quad without terrain-
conformance via depth reconstruction).

Per 1.204.0 this is the ONLY use of `AimCrosshair`: the dome
cursor branch was removed entirely (sky backdrop, ball-only
"aiming here" cue), so the dome-via-flat-quad path no longer
exists.  Earlier revs (pre 1.201.0 + post 1.203.0) used the
class on dome hits with the inward sphere normal -- kept for
the SS-compile-failed terrain fallback.

**Constructor:** `AimCrosshair(png_path, size_m=12.0)`.
* Loads the PNG via Pillow, transposes top-bottom (same flip
  reason as `_load_decal_tex_2d`), uploads to a `GL_TEXTURE_2D`
  with mipmaps + clamp-to-edge + linear filter.
* Allocates a dynamic-draw VBO with room for 6 verts (one
  quad as two tris).  VAO binds positions + UVs + a per-vert
  `a_age_frac` (always 0.0 for the crosshair so the
  age-fade term in `decal.frag` reads "fully opaque").
* Stores `self.size_m` (world diameter), `self.bias = 0.05`
  (push along surface normal so the quad sits ABOVE the
  surface and wins the z-fight pixel-by-pixel), and
  `self.global_alpha = 1.0`.

**`render(pos, normal, decal_shader, view, projection)`:**
* Builds a frisvad orthonormal basis on `normal` (used to
  orient the quad tangent to the surface).
* Builds the quad corners: `pos + normal*bias +/- tangent*half
  +/- bitangent*half`.
* Writes the 6 verts into the VBO via `glBufferSubData`.
* GL state:
  ```
  glEnable(GL_BLEND); glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
  glEnable(GL_DEPTH_TEST); glDepthMask(GL_FALSE)
  glEnable(GL_POLYGON_OFFSET_FILL); glPolygonOffset(-2.0, -2.0)
  glDisable(GL_CULL_FACE)
  ```
* Uploads `u_view`, `u_proj`, `u_fade_start = 2.0`,
  `u_global_alpha`.  Binds the texture.  Draws 6 verts as
  `GL_TRIANGLES`.
* Restores polygon-offset and cull state on exit.

The polygon offset pulls the quad TOWARD the camera in depth
space (negative slope-scale + units), so it always wins the
z-fight with the terrain surface it's projected against, even
on grazing angles.

### `tankExporterPy/scene.py::Sphere`

The `aim_hit_sphere` is a `Sphere` instance built at startup:
`Sphere(radius=0.30, sectors=18, ...)` -- a small smooth blue
sphere with per-vertex colours interpolated from
`SimpleColorShader`.

**`render(color_shader, model_matrix, view, projection)`:**
* `color_shader.use()`, uploads `model`, `view`, `projection`.
* `glDrawElements(GL_TRIANGLES, index_count, GL_UNSIGNED_INT, None)`.

**Does NOT touch:** depth state, blend state, cull state.  Whatever
state the caller has set, the sphere inherits.

**In the aim-marker block:** the caller does `glEnable(GL_DEPTH_TEST)`
just before the render.  Depth mask is left at the default ON,
so the sphere writes its depth to the buffer.  This used to be a
problem in 1.198.0-1.199.0 when the sphere drew BEFORE the
shellhole-decal pass -- the shellhole's `_grab_scene_depth`
captured the sphere's depth and the SS reconstruction snapped
onto the sphere surface, visually pancaking the decal texture
over the ball.  With the single-draw layout in 1.200.0 the
sphere is drawn AFTER the shellholes, so its depth write is
harmless to anything that came before, and tanks rendering
later still cover the sphere correctly via their own opaque
depth pass.

### Shaders

**`shaders/decal_project.vert`:** vertex stage for the SS
volumetric path.  Transforms a unit-cube vertex
(`a_position in [-0.5, 0.5]^3`) by `proj * view * u_decal_matrix`.
Passes `mat4 v_inv_mvp` (computed once on the CPU and
uploaded as a uniform; flat-out so the frag stage sees the
same matrix at every fragment without re-interpolating).

**`shaders/decal_project.frag`:** fragment stage for the SS
volumetric path.  Per fragment:
1. Compute screen-space UV from `(gl_FragCoord.xy -
   u_viewport_origin) / u_resolution`.
2. Sample scene depth at that UV.
3. Rebuild NDC `(uv * 2 - 1, depth * 2 - 1, 1)`, apply
   `v_inv_mvp`, perspective-divide -> world point in decal-
   LOCAL space (because `v_inv_mvp` folds in the decal
   matrix).
4. If `abs(local.x|y|z) > 0.5` -> outside the cube ->
   `discard`.
5. Sample albedo at `local.xz + 0.5` (TEPY's Y-up world,
   ground plane = XZ).
6. Apply edge-fade in `local.y` (smoothstep over the outer
   30 % of the cube's half-thickness), age-fade in
   `u_age_frac`, and `u_global_alpha`.
7. Discard if final alpha < 0.01 (avoids the dim halo that
   alpha-blending an almost-transparent fragment would
   produce around the cube edges).
8. Output `vec4(c.rgb, a)`.

**`shaders/decal.vert` + `decal.frag`:** flat-quad fallback
for the cursor on terrain when `decal_project.{vert,frag}`
fails to compile.  Plain UV-mapped quad with alpha + age
fade.  No scene-depth sampling.

### Impact billboards (fire + dust explosions on impacts)

> Source: `tankExporterPy/particles.py ImpactBillboards`,
> instantiated as `self.impact_fire_billboards` (additive)
> and `self.impact_dust_billboards` (alpha) in the Viewer.

Animated 2D billboards driven off the same `ImpactPool` the
shellhole decals consume.  Renders one camera-facing quad
per active impact at `impact.pos + (0, y_offset + rise*age,
0)`, where `rise_speed` and `y_offset` shift the billboard
"up" so the explosion reads as a 3D fireball lifting off the
ground rather than a flat decal at terrain level.

**GL state during render** (per 1.204.0):
* `GL_BLEND` on; blend func is `(SRC_ALPHA, ONE)` for the
  additive fire pass and `(SRC_ALPHA, ONE_MINUS_SRC_ALPHA)`
  for the alpha dust pass.
* **`GL_DEPTH_TEST` OFF** (per Coffee 1.204.0 -- the
  `y_offset` shift in world +Y pushes the billboard in front
  of the camera at terrain impacts but TANGENT to the dome
  surface for horizon dome hits.  With depth test on, the
  dome-impact billboard's depth ends up roughly equal to or
  slightly farther than the dome surface and `GL_LESS`
  rejects every fragment.  Off-test forces the explosion to
  always render).
* `glDepthMask(GL_FALSE)` (no depth writes -- multiple
  impacts in the same area don't z-fight against each
  other).

**Trade-off of depth-test off**: a tank between camera and a
terrain impact no longer occludes the explosion.  Acceptable
for a debug viewer; revisit if the trade-off becomes
visually objectionable.

### Skipping the shellhole decal on dome impacts

`tankExporterPy/impact_pool.py Impact + ImpactPool.hit` take
a `skip_decal=False` kwarg.  When True, the
`ScreenSpaceDecals.render_impacts` and `Decals.render`
filters skip the impact entirely -- the impact is still
"alive" so the fire + dust billboards fire normally, but no
persistent ground decal lands.

`Viewer._fire_round` impact handler tags dome impacts as
`skip_decal=True`: it compares `|impact_pos|` to
`self.skydome.radius` with 1 m of slop (the impact snaps to
`target_pos` which came from the quadratic dome
intersection, so the radius should match within float
precision).

### Common failure modes

* **Cursor doesn't appear at startup before a tank loads** --
  check that `_drive_aim_from_aim_state` runs in the pre-
  render pump (and not late in the render loop).  This was
  the 1.185.x spiral and the 1.190.0 fix.
* **Cursor invisible under shellholes (terrain)** -- check
  that the cursor draw site is AFTER the shellhole pass in
  the per-frame order.  If you reorder these, the alpha-
  blend order flips and the cursor goes back under the
  decals.
* **Decal "pancakes" onto the ball / a tank surface
  (terrain)** -- the SS reconstruction is snapping to a non-
  terrain depth.  Verify nothing opaque has written depth
  between `terrain.render` and the decal's
  `_grab_scene_depth`.  Order is `terrain -> shellholes ->
  cursor + ball -> tanks` so by construction the decals see
  terrain-only Z.
* **Reticle compass directions wrong (terrain)** -- the
  cursor PNG ships with its Y axis flipped via
  `cust_tools/make_cursor.py (Image.FLIP_TOP_BOTTOM)`.  The
  frag shader's `local.xz + 0.5` mapping reads it as: PNG
  bottom = world +Z (forward), PNG top = world -Z (back),
  PNG right = +X, PNG left = -X.  Regenerate via
  `python cust_tools/make_cursor.py`.
* **No cursor reticle on the dome** -- expected as of
  1.204.0.  The dome cursor decal was removed; the blue
  debug sphere at `_aim_hit_world` is the only "aiming here"
  cue when aiming at the sky.  If a future session wants to
  revisit a dome reticle, see the git history at
  1.201.0 - 1.203.0 for the surface-aligned matrix
  experiments (frisvad basis, bitangent flip, +Y seed for
  stability) -- none converged to what the user wanted.
* **Explosions invisible on dome impacts** -- check that
  `ImpactBillboards.render` has `glDisable(GL_DEPTH_TEST)`
  before the draw and `glEnable(GL_DEPTH_TEST)` in the
  cleanup.  Without this the billboard's depth-test against
  the dome surface depth fails (the `y_offset` shift in
  world +Y doesn't put the quad in front of the dome at
  horizon hits).
* **Shellhole decal on the dome (don't want this)** --
  verify `Viewer._fire_round` is tagging dome hits with
  `skip_decal=True` based on `|impact_pos| ~ skydome.radius`
  (1 m slop).  The decal filters in
  `ScreenSpaceDecals.render_impacts` + `Decals.render`
  check `getattr(im, 'skip_decal', False)`.
* **Seam visible across the dome wrap** -- the equirect
  frag shader does manual LOD from world-direction
  derivatives (not UV derivatives) and the dome renders
  twice with a 0.01-deg Y rotation between draws.  Both
  fixes need to be intact; the seam reappears if either is
  removed.

---

## Skinning upload (`_upload_skinning`)

`viewer.py:9445`.  Per-mesh uniform-array upload of the bone
matrices computed by `tank_physics.bone_matrix_array(palette)`.

* Reads `mesh.bone_palette` (list of bone names in renderSet order).
* Skips entirely if the mesh has no skin data OR Susp is off OR
  no tank_physics object — sets `u_skinned = 0`, shader falls back
  to rigid model_matrix.
* Also uploads `u_wheel_state` (per-bone CONTACT / HANGING /
  OVER_COMPRESSED code) for the contact-highlight shader path.

Called inside the solid + wireframe loops.  NOT called for the
normals or picker passes (those don't deform — they read static
positions).  This is intentional: the picker's hit must report the
RIGID triangle (so we can map back to the source `.primitives_processed`
vert / index for export); the visible-deformed-vert overlay reads
post-skin positions a different way.

---

## Shader programs and where they're swapped in

`viewer.py:9580` upward chooses `active` shader program based on
mesh source:

| Source | Shader file | Notes |
|--------|-------------|-------|
| PKG-loaded WoT mesh | `shaders/mesh.{vert,frag}` | PBR: GGX direct + split-sum IBL, GMM channel decode (R=gloss, G=metallic, B=camo mask), AO, alpha test, crash-tile damage layer.  See `WoT-Specific Notes` in `ARCHITECTURE.md`. |
| FBX-imported mesh   | `shaders/imported.{vert,frag}` | Cheaper Lambert + Phong + bump.  Used on round-tripped meshes that don't have GMM data. |
| Surface-normal debug | `shaders/normals.{vert,geom,frag}` | Geometry shader emits 1-line-per-face (cyan, by-face mode) or 3-axes-coloured-lines-per-vert (by-vertex mode). |
| Picker FBO | `shaders/picker.{vert,frag}` (in `picker.py`) | 24-bit ID render: triangle index encoded into RGB. |

Imported shader's "Shaded" override (line 9514): when the toggle is
on, base colour comes from `theme.c1()` with the per-channel brown
bias (R×0.25, G×0.15, B×0.08) so the lit surface reads as warm
sienna instead of vivid orange even at default light slider.

---

## Resize / fullscreen plumbing

`F11` toggle in `viewer._toggle_fullscreen` (line ~10113).  After
the Win32 `ShowWindow(SW_MAXIMIZE/SW_RESTORE)` call we explicitly
poll `GetClientRect` and call `_on_resize` directly — belt and
braces for the case where the WM_SIZE → SDL_WINDOWEVENT →
pygame VIDEORESIZE chain hadn't pumped on the same frame.

Borderless toggle for splash uses `WS_CAPTION | WS_THICKFRAME`
mask only (line ~9971); leaves WS_SYSMENU / WS_VISIBLE / WS_POPUP
alone since SDL2 set those at window creation and doesn't expect
external mutation.

Initial window 1024×576 (banner aspect 1672×941).

---

## Performance overlays

* **Physics-timer overlay** (line ~9938): grass-green text in the
  upper-left of the main render area showing `_physics_ms`
  (5-frame trailing average of `tank_physics.update()` cost).
  Only drawn when Susp is on.
* **FPS counter**: rolling average maintained in `_frame_dt`,
  surfaced via the bottom panel.

### Per-section frame timers (`Viewer._frame_timers`, 1.107.0+)

`render()` clears `self._frame_timers = {}` at the top of every
frame and populates per-section CPU wall-clock costs (in ms)
inline using `time.perf_counter()` deltas.  Sections currently
timed:

| Key | Wraps |
|-----|-------|
| `frame_dt_in` | `dt` actually passed to `tank_physics.update()` (post 1ms clamp) |
| `physics_update` | `tank_physics.update()` |
| `mesh_pose_apply` | per-mesh `model_matrix = chassis_pose @ bind` loop |
| `emitters` | `_update_emitters_for_chassis_pose` |
| `picker_pass` | `picker.update_pass` (offscreen FBO render) |
| `skybox` | `skybox.render` |
| `terrain` | `terrain.render` |
| `spline_overlay` | F8 NURB control loop + CR + line strip |
| `ui` | `ui.render` (2-D overlay) |
| `frame_total` | full CPU-side `render()` cost before `pygame.display.flip()` |

The F3 manual recorder (`docs/PHYSICS.md` "F3 manual recorder")
copies this dict into every captured frame as `frame_timers_ms`,
so an offline pass can localise dt-spike causes to specific
sections.  Inline timing is cheap (a few `perf_counter()` calls
amount to ~hundreds of ns total) and runs unconditionally --
no toggle needed.

---

## When in doubt about a render pass

1. Run `python cust_tools/arch.py search "<topic>"` first.
2. Find the pass in the order list above.
3. Read the gate set in the table.
4. Verify the four iteration sites (solid + wireframe + normals +
   picker) agree on the gate.

The most common bug class here is "I added a skip but missed a
pass."  The picker is the easiest one to forget.

---

## Subsystems landed in the 2026-05-15 session

(Cross-references; full write-ups in `CHANGELOG.md` and
`hand_off/HANDOFF_2026-05-15_dome_and_cursor.md`.)

* **SkyDome** -- `tankExporterPy/skybox.py`, classes `SkyDome`
  + `SkyDomeShader`.  Procedural UV sphere centered at world
  origin, radius = `terrain.world_size / 2`.  Samples WoT's
  Karelia panorama from `resources/skyboxes/01_Karelia_sky/
  skydome/sky_karelia_forward.png` (auto-extracted by
  `cust_tools/extract_wot_karelia_sky.py`) OR the existing
  cubemap as a fallback.  Shaders: `shaders/skydome.vert`,
  `shaders/skydome.frag` (cubemap), `shaders/skydome_equirect
  .frag` (panorama).  Manual LOD from world-direction
  derivatives -- avoids the atan2 seam-mip artefact.  Two
  passes per frame with 0.01-deg Y rotation between draws
  to soften the wrap seam.

* **Screen-space decal projector** -- `tankExporterPy/particles
  .py`, class `ScreenSpaceDecals`.  Ported from nuTerra's
  `DecalProject.{vert,frag}`.  Each decal is a unit cube
  with the frag stage reading the scene depth buffer (grabbed
  via `Viewer._grab_scene_depth` + `glCopyTexImage2D` into a
  `GL_TEXTURE_2D`).  World-space reconstruction via pre-
  multiplied `inv(proj * view * decal_matrix)`; clip outside
  `[-0.5, +0.5]^3`; UV is `local.xz + 0.5` (world XZ ground
  plane).  Used for shellhole impact decals AND the aim
  crosshair on terrain hits.

* **Aim cursor projector** -- `tankExporterPy/particles.py`,
  class `AimCrosshair` (flat-quad fallback) and the SS-decal
  path above.  `Viewer._drive_aim_from_aim_state` tags each
  hit as `'terrain'` / `'dome'` / `'sky'`; the render
  dispatches to SS-decal (terrain) or flat-quad (dome) based
  on the kind.  Cursor.png ships with the repo (regenerate
  via `cust_tools/make_cursor.py`).

* **Impact effects** -- `tankExporterPy/particles.py`, class
  `ImpactBillboards`.  One-shot animated billboards driven
  off `ImpactPool`.  Fire (additive) + dust (alpha)
  flipbooks sliced from WoT's `eff_tex.dds` at atlas region
  (1024, 1024) by `cust_tools/extract_wot_fire_atlas.py`.

* **Dome containment** -- `scene.Camera.max_eye_radius`
  clamps the orbit + chase eye to within the dome; viewer
  post-physics clamps `tank_physics.pos.xz` to the same
  radius; `terrain.frag u_world_clip_radius` discards
  fragments past the dome's horizontal circle; `_fire_round`
  intersects the gun ray with the dome as the final
  fallback so out-of-map shots terminate on the dome
  surface and trigger impact effects.
