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
1.  Background (sky, grid, axes, light spheres -- depth test off)
2.  Procedural terrain (textured displaced quad)
3.  Tank physics step (if Susp + Terrain + tank loaded)
       chassis_pose = tank_physics.update(...)
       for m in self.meshes: m.model_matrix = chassis_pose @ m.bind_model_matrix
4.  Solid mesh pass        -- main PBR / imported / shaded shader
5.  Wireframe overlay      -- iff Wireframe toggle (line GL_LINE pass)
6.  Surface-normal debug   -- iff Normals slider > 0 (geometry-shader pass)
7.  Hardpoint markers      -- iff Debug (HP_* sphere + arrow gizmos)
8.  Particles              -- smoke + fire flipbooks (camera-billboarded)
9.  Contact-stars overlay  -- iff Susp + Debug (per-wheel terrain markers)
10. Hull bbox              -- iff Debug (chassis AABB wireframe)
11. Picker FBO (off-screen)-- iff Pick Tri toggle
12. Picker overlay         -- highlights the picked triangle
13. Look-at crosshair      -- iff middle-mouse held (PINK lines)
14. UI (panels, tree, dialogs)
15. Physics-timer overlay  -- iff Susp (grass-green text upper-left)
```

Items 4-6 ALL iterate `self.meshes`.  Item 11 also iterates meshes
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
