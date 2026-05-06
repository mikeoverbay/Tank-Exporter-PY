# Tank Mesh Viewer — Architecture Road Map

A Python + Pygame + PyOpenGL viewer for World of Tanks `.primitives_processed`
mesh files and complete vehicle XMLs.  All application code lives in the
`tankviewer/` package; `tank_viewer.py` is a thin CLI entry point.

---

## File Map

```
tank_viewer.py          CLI entry point — argparse, persisted config, Viewer.run()
tanks.txt               Tank Exporter export of currently-active tanks (canonical filter)
thumb_nails/            Per-tank PNG thumbnails (filename = <xml_basename>.png)
TheItemList.xml         File→pkg lookup index used by PkgExtractor (O(1) extraction)

tankviewer/
    __init__.py         Package marker
    common.py           Shared low-level utilities (no GL): bit-packed normal
                        decoders, BWXML decoder, shader-source loader
    config.py           Persistent JSON config (pkg_dir / res_mods / lookup_xml)
    loaders.py          Binary mesh parser, visual_processed parser, texture
                        uploader, .pkg extractor, vehicle-XML loader
    mesh.py             GPU mesh representation (VAO / VBOs + textures)
    scene.py            Camera + scene geometry helpers (grid, axes, sphere)
    shaders.py          GLSL program wrappers
    skybox.py           Cubemap loader, GPU IBL pre-filter pass, skybox renderer
    ui.py               2-D overlay: bar widgets, tank browser tree, modal dialog
    viewer.py           Main application class, render loop, scene lifecycle
    xloader.py          Text-format DirectX .x file parser (skybox cube)

shaders/
    mesh.vert / .frag           PBR shader: GGX direct + split-sum IBL,
                                WoT GMM channel decode, AO, alpha test
    mesh_blinnphong.frag        Pre-PBR fallback (kept as reference)
    color.vert / .frag          Per-vertex colour (grid, axes, sphere)
    ui.vert / .frag             Orthographic 2-D quad shader (bar, tree, dialog).
                                u_use_tex modes: 0=solid, 1=text-mask, 2=image
    skybox.vert / .frag         Cubemap background (xyww depth trick)
    ibl_prefilter.vert / .frag  GL-3.3 port of Khronos IBL filter:
                                Lambertian irradiance, GGX prefiltered specular,
                                BRDF split-sum LUT

resources/
    environment_maps/   Skybox cube faces (cube1_FR/BK/LF/RT/UP/DN.png)
                        cube_model.x — skybox cube geometry
```

---

## `tank_viewer.py`

Argparse-driven entry point.

| Symbol | Purpose | Input |
|--------|---------|-------|
| `main()` | Parse CLI args, persist any path overrides via `config.save`, construct `Viewer`, call `run()` | `sys.argv` |

**Positional argument**

| Arg | Required | Purpose |
|-----|----------|---------|
| `filepath` | optional | `.primitives_processed` or vehicle `.xml` to load.  Omit to start with an empty scene and pick a tank from the tree panel. |

**Options** (each persists into `tankviewer.json`)

| Flag | Effect |
|------|--------|
| `--pkg-dir DIR` | Path to WoT `res/packages/` |
| `--res-mods DIR` | Path to `res_mods/<version>/` |
| `--lookup-xml FILE` | Path to `TheItemList.xml` |

---

## `tankviewer/common.py`

Shared utilities with no OpenGL dependency.

| Symbol | Purpose | Input → Output |
|--------|---------|----------------|
| `unpack_normal(packed)` | Decode 10:10:10 bit-packed normal (non-BPVT vertices) | `packed` uint32 → vec3 |
| `unpack_normal_bpvt(packed)` | Decode 8:8:8 bit-packed normal (BPVT vertices) | `packed` uint32 → vec3 |
| `read_c_string(data, offset, max_len=64)` | Null-terminated ASCII reader | bytes, offset → str |
| `load_shader_file(path)` | Read GLSL source; CWD → package dir → project root | rel path → str |
| `is_bwxml(data)` | Detect BigWorld packed-XML magic header (`454ea162`) | bytes → bool |
| `decode_bwxml(data, root_tag='root')` | Decode BWXML to a plain XML string | bytes → str |

---

## `tankviewer/config.py`

Tiny persistent-config helper backed by `tankviewer.json` next to
`tank_viewer.py`.

| Symbol | Purpose |
|--------|---------|
| `config_path()` | Absolute path of the JSON file |
| `load()` | Return dict with keys `pkg_dir`, `res_mods`, `lookup_xml` (empty strings if unset) |
| `save(cfg)` | Write the dict back |

---

## `tankviewer/loaders.py`

Binary parsers, GPU texture loader, .pkg extractor, vehicle-XML loader.

### `MeshParser`  (all static)

Parses a `.primitives_processed` binary file.

| Method | Purpose | Input → Output |
|--------|---------|----------------|
| `parse_primitives_processed(filepath)` | Full parse | `filepath` str → `list[dict]` one per primitive group |
| `_parse_section_table(data, start_pos)` | Read section table at end of file | `data`, `start_pos` → list of section descs |
| `_parse_vertices(data)` | Decode vertex buffer | bytes → `(format_str, vertex_dict)` |
| `_parse_indices(data)` | Decode index + primitive-group section | bytes → `(indices ndarray, prim_groups list)` |

**Primitive-group dict keys:** `name`, `format`, `vertices` (sub-dict with
`positions`, `normals`, `tangents`, `binormals`, `uv0`), `indices`.

### `VisualLoader`  (static)

Parses a `.visual_processed` for texture paths and material flags.

| Method | Purpose | Input → Output |
|--------|---------|----------------|
| `parse_textures(visual_file, group_names)` | Per-group texture paths + flags | path, names → `dict[group, dict]` |
| `resolve_hd_path(rel_path, res_mods_root, pkg_extractor=None)` | Try `_hd` first, fall back to SD; PKG-extract if missing on disk | rel path, root, optional extractor → `(abs_path\|None, used_hd bool)` |
| `get_node_world_translations(visual_root)` | Walk the `<node>` tree summing `row3` translations | ET root → `dict[lower_name, np.array(3)]` |

**Per-group dict keys:** `diffuse`, `normal`, `ao`, `gmm`,
`alpha_reference`, `alpha_test_enable`, `double_sided`, `identifier`.

### `TextureLoader`  (static)

Uploads image files to GPU textures.

| Method | Purpose | Input → Output |
|--------|---------|----------------|
| `load_texture(filepath, is_normal=False)` | DDS / PNG / JPG → `GL_TEXTURE_2D` | path → GLuint |
| `_load_image_file(filepath, is_normal)` | PIL load, flip Y, RGBA for normals | path, bool → GLuint |
| `create_placeholder(value)` | 1×1 grey RGB texture | float 0-1 → GLuint |

### `PkgExtractor`

Extracts files from WoT `.pkg` archives (standard ZIP).  Uses
`TheItemList.xml` for an O(1) `filename → pkg_basename` lookup; falls back to
scanning `pkg_dir` when the lookup is absent.

| Method | Purpose | Input → Output |
|--------|---------|----------------|
| `__init__(wot_root, pkg_dir=None, lookup_xml=None)` | Open lookup, pre-list pkgs, allocate temp dir | paths |
| `extract(internal_path)` | Resolve a forward-slash path inside any pkg → local absolute path (or None) | str → str\|None |
| `extract_from_pkg(pkg_basename, internal_path)` | Force extract from a specific archive (bypasses lookup; vehicle XMLs aren't indexed) | str, str → str\|None |
| `list_vehicle_xmls(with_tier=False)` | Enumerate per-nation tank list from each `<nation>/list.xml` | bool → see below |
| `_read_tank_table(scripts_pkg, list_xml_internal, nation)` | Decode one `list.xml` → `{tag: {tier, vclass}}` | — |
| `cleanup()` | Remove temp extraction directory | — |

`list_vehicle_xmls` returns

* `with_tier=False` → `{nation: [xml_filename, ...]}` sorted by name
* `with_tier=True` → `{nation: [{'xml', 'tier', 'vclass'}, ...]}` sorted by `(tier, name)`

`vclass` is one of `'lightTank'`, `'mediumTank'`, `'heavyTank'`, `'AT-SPG'`,
`'SPG'`, or `'other'` (taken from the first whitespace token of the entry's
`<tags>` element).  The `common` folder is always skipped.

### `VehicleXMLLoader`  (static)

Parses a vehicle XML and returns the best-equipped component set with
correct world-space offsets.

| Method | Purpose | Input → Output |
|--------|---------|----------------|
| `parse(xml_path, res_mods_root, pkg_extractor=None, damaged=False)` | Resolve hull / chassis / best turret / top gun | paths, bool → `list[component]` |
| `_pick_best(parent_el, price_tag)` | Highest-`<price>` child element | — |
| `_vec3(el)` | Parse space-separated XYZ text | ET el → np.array(3) |
| `_chassis_attach_y(chassis_visual_path)` | World Y of the chassis V-bone (hull attach height) | path → float |
| `_read_model_base(model_bytes)` | BWXML-decode a `.model` file → embedded canonical visual base path | bytes → str\|None |
| `_entry(label, model_path, res_mods_root, offset, pkg_extractor=None)` | Resolve `.model → .primitives_processed / .visual_processed` (res_mods → PKG fallback) | — |

**`damaged=True`** swaps every `<models/undamaged>` lookup for
`<models/destroyed>`, giving the crashed/wrecked variant of each component.

**Each component dict:** `label`, `primitives` (path or None), `visual`
(path or None), `offset` (np.float32 vec3 in GL space).

---

## `tankviewer/mesh.py`

GPU-side representation of one WoT primitive group.

### `Mesh`

| Method | Purpose | Input |
|--------|---------|-------|
| `__init__(parsed_group)` | Store vertex arrays, set material defaults, compute tangents if absent | one dict from `MeshParser.parse_primitives_processed` |
| `_compute_tangents()` | Fallback tangent/binormal generation | — |
| `build_vao()` | Upload arrays; create VAO + VBOs + EBO | — |
| `render(shader)` | Bind textures (units 0–3) and call `glDrawElements` | already-`use()`d shader |
| `cleanup()` | Free VAO + VBOs + EBO **and** all four material textures (idempotent) | — |

**Vertex attribute layout:** 0=position, 1=normal, 2=tangent, 3=binormal, 4=uv0.

**Material attributes set by `Viewer` after parse:**
`diffuse_tex_id`, `normal_tex_id`, `ao_tex_id`, `gmm_tex_id`,
`alpha_reference`, `alpha_test_enable`, `double_sided`, `identifier`,
`alpha_in_normal_red`, `model_matrix`.

---

## `tankviewer/scene.py`

Camera + simple geometry helpers.

### `Camera`

Trackball orbit camera.

| Method | Purpose | Input → Output |
|--------|---------|----------------|
| `get_view_matrix()` | look-at from yaw/pitch/distance/center | → 4×4 |
| `get_projection_matrix()` | Perspective; near/far scale with distance | → 4×4 |
| `get_model_matrix()` | Identity | → 4×4 |
| `fit_to_bounds(bbox_min, bbox_max)` | Auto-center and frame an AABB | vec3, vec3 |

**Public state:** `distance`, `yaw`, `pitch`, `center`, `fov`, `width`, `height`.
`width` / `height` are the **visible 3-D viewport** (window minus tree panel).

### `Grid` / `Axes` / `Sphere`

Standard helper geometry (lines + a yellow UV sphere for the orbit-light
indicator).  Each has `__init__`, `render(shader, …)`, `cleanup()`.

---

## `tankviewer/shaders.py`

GLSL program wrappers; sources are loaded from `shaders/` at construction.

### `_compile_program(vsrc, fsrc, label='shader')`

Compile + link, raising `RuntimeError` on failure.

### `SimpleColorShader`

`color.{vert,frag}`.  Uniforms: `model`, `view`, `projection`.
`use()`, `set_mat4(name, m)`.

### `ShaderProgram`

Main mesh shader (`mesh.{vert,frag}`).  Full PBR with GGX direct lighting
and split-sum IBL (Lambertian irradiance + GGX prefiltered specular +
BRDF LUT).  Uniforms include `model`, `view`, `projection`,
`light_pos`, `view_pos`, `diffuse_map`, `normal_map`, `ao_map`,
`gmm_map`, `irradiance_map`, `prefiltered_map`, `brdf_lut`,
`use_normal_map`, `is_GA_normal`, `alpha_test_enable`, `alpha_ref`,
`alpha_in_normal_red`, `ao_in_diffuse_alpha`, `has_ao_map`,
`has_gmm_map`, `has_irradiance`, `has_prefiltered`, `has_brdf_lut`,
`armor_color`, `has_armor_color`, `metal_scale`, `shine_scale`,
`invert_metal`, `invert_shine`.
Public methods: `use()`, `set_mat4`, `set_vec3`, `set_int`, `set_float`,
`get_uniform`.

### `UIShader`

`ui.{vert,frag}`.  Uniforms: `projection`, `u_color`, `u_tex`,
`u_use_tex` (0=solid, 1=text-mask, 2=image).  `use()`, `set_mat4`,
`set_vec4`, `set_int`.

### `SkyboxShader`

`skybox.{vert,frag}`.  Uniforms: `view`, `projection`, `skybox`.

### `IBLPrefilterShader`

`ibl_prefilter.{vert,frag}` (offline pass, run once at startup).
Modes selected via `u_distribution` (0 = Lambertian irradiance, 1 = GGX
specular, 2 = Charlie sheen) and `u_isGeneratingLUT` (1 = render BRDF LUT).

---

## `tankviewer/ui.py`

2-D overlay: top menu bar, right-hand tank-browser tree, and a centred
modal load dialog.

### `UIButton` / `UISlider` / `UICheckbox`

Bar widgets.

| Widget | Purpose |
|--------|---------|
| `UIButton` | Labelled toggle.  Has `attr` field used by `Viewer._apply_button_action` to mirror state into a flag. |
| `UISlider` | Horizontal slider, value in `[0, value_max]`.  `set_from_mouse(mx)`, `ensure_value_tex(font)`. |
| `UICheckbox` | Small square toggle with text label. |

### `UITreeNode` / `UITreeView`

Right-hand collapsible tank browser anchored to `(window_w − TREE_PANEL_W, BAR_HEIGHT)`.

`UITreeNode` — branch (children list non-empty → click toggles expand) or leaf
(empty children → click invokes `tree.on_select`).  Carries an opaque
`payload` and a lazy label texture.

| `UITreeView` method | Purpose |
|---------------------|---------|
| `add_root(node)` / `clear()` | Top-level branches |
| `_flatten()` | Yield `(depth, node)` for the currently-expanded set |
| `hit(mx, my)` / `_hit_rows(mx, my)` | Whole panel / rows region only |
| `thumbnail_rect()` | Reserved bottom area (height = `THUMB_AREA_H`) |
| `handle_click(mx, my)` | Toggle a branch or fire `on_select(leaf)` |
| `handle_scroll(mx, my, dy)` | Wheel scroll inside the rows region |
| `update_hover(mx, my)` | Updates `hover_idx`; fires `on_hover_change(leaf_or_None)` when the *leaf identity* changes |
| `ensure_textures(make_tex)` | Lazy build of all node label textures |
| `cleanup()` | Free label textures + held thumbnail textures |

**Thumbnail-strip state** lives directly on the tree (textures owned by
`Viewer`, ID slot owned by tree):

* `loaded_thumb_tex / w / h` and `loaded_thumb_name` — the persistent
  loaded-tank picture and Tank Exporter display name.
* `hover_thumb_tex / w / h` and `hover_thumb_name` — transient hover
  preview (wins over loaded when set).

The display labels above the image are built lazily inside
`UIManager._render_tree_thumbnail` and cached in
`_loaded_name_tex / _loaded_name_str / …` until the string changes.

### `UIConfirmDialog`

Centred modal load-confirm prompt.

| Method | Purpose |
|--------|---------|
| `show(title, on_confirm, make_tex)` | Build the title texture, store the callback (`crashed: bool → None`) |
| `hide()` | Dismiss without firing the callback |
| `handle_click(mx, my)` | Crashed-checkbox toggle / Load / Cancel; click outside the box dismisses; **always** consumes the click while modal |
| `update_hover(mx, my)` | Hover state for the two buttons |
| `cleanup()` | Free the title texture (cached label textures are freed by `UIManager.cleanup`) |

### `UIManager`

Owns all widgets, dispatches events, renders the overlay.

| Method | Purpose |
|--------|---------|
| `add_button(label, x, y, w, h, active=True)` | Bar toggle |
| `add_slider(label, track_x, track_cy, track_w, value=0.5, value_max=1.0)` | Bar slider |
| `add_checkbox(label, x, y, size=14, checked=False)` | Bar checkbox |
| `handle_mouse_down(mx, my)` | Priority: dialog (modal) → tree → bar widgets.  Returns the toggled `UIButton` for `Viewer._apply_button_action`, else None |
| `handle_mouse_drag(mx, my)` | Continue any active slider drag |
| `handle_mouse_up()` | End slider drag |
| `handle_mouse_wheel(mx, my, dy)` | Eats the wheel while modal; routes to tree scroll if the cursor is inside the tree; otherwise returns False so `Viewer` zooms the camera |
| `update_hover(mx, my)` | Bar buttons + tree + dialog buttons |
| `is_pointer_over_ui(mx, my)` | Bar / tree / any active dialog → True |
| `render(width, height)` | Bar background + widgets, then tree, then modal dialog (forces `GL_FILL`) |
| `cleanup()` | Free every label / value / image texture and shared VAO / VBO |

**Constants:** `BAR_HEIGHT = 84`, `BUTTON_PADDING = 8`,
`BUTTON_SPACING = 6`; per-row slider column positions
(`SLIDER_LABEL_X`, `SLIDER_TRACK_X`, …); thumbnail-name strip
height (`THUMB_NAME_STRIP_H = 24`).

`_make_tex(text, color)` → `(tex_id, w, h)` is the shared text-rendering
helper used by every label.  `_draw_tex` uses ui-shader mode 1 (alpha
mask, RGB from `u_color`) for fonts; `_draw_image_tex` uses mode 2
(full RGBA) for thumbnails.

---

## `tankviewer/viewer.py`

Main application class.

### `Viewer`

| Method | Purpose | Input |
|--------|---------|-------|
| `__init__(filepath=None, cfg=None)` | Pygame + GL init, build subsystems, optionally load `filepath` | optional file, optional config dict overrides |
| `run()` | Event loop until quit | — |
| `load_mesh(filepath)` | Single-file load (`.primitives_processed`) | path |
| `load_vehicle(xml_path, damaged=False)` | Multi-component vehicle load via `VehicleXMLLoader` | path, bool |
| `handle_input()` | SDL events + continuous mouse state | — |
| `render()` | Skybox → grid/axes → meshes → light sphere → UI overlay | — |
| `_build_ui()` | Bar buttons + Light/Ambient sliders + NMap/AO checkboxes | — |
| `_on_resize(w, h)` | Update camera aspect (3-D viewport excludes tree) and reflow tree | pixel dims |
| `_apply_button_action(btn)` / `_sync_button_state(attr, value)` | Bar-button ↔ flag mirroring | — |
| `_init_pkg_extractor_early()` | Build `PkgExtractor` from config or default WoT NA path before any mesh load | — |
| `_prewarm_first_load_caches()` | Splash-time pre-warm of the three "lazy on first tank load" caches: `ArmorColorLoader`, `VehicleXMLLoader._shared_xml_cache`, and (the big one) Pillow's DDS codec + GL driver tex-upload pools via one real `Details_map.dds` upload | — |
| `_set_active_group(pixie)` | Switch which engine-class slot (`gas_small` / `diesel_large` / etc.) the smoke + fire sliders edit; auto-called from `load_vehicle` with the loaded tank's `<exhaust><pixie>` value | str\|None |
| `_build_tree_panel()` | Populate the tank-browser tree (filtered by `tanks.txt`) | — |
| `_load_tanks_txt(valid_basenames)` | Parse Tank Exporter's `tanks.txt` → `{xml_basename: display_name}`; resolves 30-char truncations by prefix | set of names |
| `_on_tree_tank_selected(node)` | Open the load-confirm dialog | UITreeNode |
| `_load_tank_from_pkg(nation, xml_name, damaged=False)` | Extract XML from `scripts.pkg`, call `load_vehicle` | — |
| `_clear_scene()` | Free all per-mesh GPU resources, reset bbox / armor color / loaded thumb | — |
| `_load_thumb_texture(png_path)` | Decode + upload a PNG (no Y-flip, for the UI ortho) → `(tex_id, w, h)` | path |
| `_thumb_path_for_xml(xml_name)` | XML basename → PNG path; tries direct match then progressive `_TOKEN` trim; cached | str → path\|None |
| `_set_loaded_thumbnail(xml_basename)` | Persistent thumbnail + display label; called at the end of a successful load | str\|None |
| `_on_tree_hover_change(node)` | Transient hover thumbnail + display label | UITreeNode\|None |

**Class constants**

| Name | Value | Purpose |
|------|-------|---------|
| `TREE_PANEL_W` | 260 | Right-hand tree panel width |
| `THUMB_DIR` | `<root>/thumb_nails` | Per-tank PNG folder |
| `TANKS_TXT` | `<root>/tanks.txt` | Authoritative active-tank list (Tank Exporter export) |

**Display flags (toggled by buttons or keyboard):**
`show_grid`, `show_axes`, `show_skybox`, `show_light`, `wireframe`,
`use_normal_map`.

**FPS:** rolling average over the last 15 frames, displayed in the title bar.

---

## `tankviewer/skybox.py`

Skybox + IBL setup.

### `Skybox`

| Method | Purpose | Input |
|--------|---------|-------|
| `__init__(x_file, image_dir)` | Load `.x` cube geometry + 6 PNG faces; run the IBL pre-filter pass off the cubemap; cache the prefilter shader and quad VAO and free them when done | paths |
| `render(view, projection)` | Draw background cube (GL_LEQUAL depth, no cull) | 4×4 matrices |
| `cleanup()` | Free VAO, VBOs, all cubemap textures and the BRDF LUT | — |

**Public attributes** (consumed by `ShaderProgram`):

| Field | Texture | Format |
|-------|---------|--------|
| `cubemap_id` | Raw HDR-ish skybox cubemap | `GL_RGBA16F` |
| `irradiance_id` | Lambertian irradiance (32×32) | `GL_RGBA16F` |
| `prefiltered_id` | GGX prefiltered specular (128×128, 5 mips) | `GL_RGBA16F` |
| `brdf_lut_id` | GGX split-sum LUT (512×512) | `GL_RGBA16F` |

**Face → GL enum mapping**

| File suffix | GL_TEXTURE_CUBE_MAP_* |
|-------------|----------------------|
| RT | POSITIVE_X |
| LF | NEGATIVE_X |
| UP | POSITIVE_Y |
| DN | NEGATIVE_Y |
| BK | POSITIVE_Z |
| FR | NEGATIVE_Z |

The prefilter bake uses `direction.y = -direction.y`; the mesh shader applies
the same Y-flip when sampling, otherwise the sky and ground swap.

---

## `tankviewer/xloader.py`

Parses text-format DirectX `.x` files (used for the skybox cube).
Binary `.x` is not supported.

| Symbol | Purpose | Input → Output |
|--------|---------|----------------|
| `load_x(filepath)` | Parse a `.x` file → mesh dict | path → dict |
| `_strip_comments(text)` | Remove `//` and `/* */` | str → str |
| `_find_block(text, keyword, start)` | Locate a named `{ }` block | — |
| `_parse_vertex_list(text)` | `count; x;y;z;, ...` → `(N,3)` ndarray | — |
| `_parse_face_list(text)` | Faces (quads split into tris) → uint32 ndarray | — |
| `_parse_uv_list(text)` | `MeshTextureCoords` → `(N,2)` ndarray | — |
| `_parse_normal_section(text)` | `MeshNormals` re-indexed to match positions | — |
| `_parse_material_list(block_inner, defined_materials)` | Texture filenames used by the mesh | — |
| `_scan_materials(text)` | Top-level `Material Name { }` blocks | — |
| `_numbers(text)` | All numeric tokens | — |

**Return dict keys:** `positions`, `normals`, `uv0`, `indices`, `materials`.

---

## Tank Browser Data Pipeline

```
scripts.pkg / <nation> / list.xml
       │
       ▼
PkgExtractor.list_vehicle_xmls(with_tier=True)
       │   {nation: [{xml, tier, vclass}, ...]}  sorted by (tier, name)
       ▼
Viewer._build_tree_panel
       │   filter against tanks.txt (TE / WoT-API authoritative active list)
       │   resolve 30-char truncations via prefix match against list.xml basenames
       ▼
UITreeView roots = nation branches → tank leaves
       │
       │   row label  : "T<tier>  <xml_basename>"   (nation-prefixed ID)
       │   payload    : nation, xml, tier, vclass, display
       │
       │  (hover)                    (click → modal Load dialog → load_vehicle)
       ▼
Viewer._on_tree_hover_change          Viewer._on_tree_tank_selected
       │                                     │
       │  resolve thumbnail PNG               │  show dialog with crashed checkbox
       │  via _thumb_path_for_xml             │  on confirm → _load_tank_from_pkg
       │  (direct match → trim _TOKEN)        │      → extract scripts.pkg/<...>.xml
       │                                      │      → load_vehicle(local_path,
       ▼                                      │                    damaged=…)
tree.hover_thumb_tex / name
                                              ▼
                                       _set_loaded_thumbnail
                                       tree.loaded_thumb_tex / name
```

Hover wins over loaded on screen, so the same render code reads from a
single active slot — the picture and the label always update together.

---

## WoT-Specific Notes

| Topic | Detail |
|-------|--------|
| Vertex format | `BPVTxyznuvtb` (no bones, stride 32) vs `BPVTxyznuviiiwwtb` (bones, stride 40) |
| Winding order | `'iii' in format` → has bones → keep winding; else flip indices[i] ↔ indices[i+2] |
| Normal maps | DXT5nm (GA channel format): X in green, Y in alpha; decode `n.xy = ga*2-1; n.z = sqrt(1-x²-y²); n.x *= -1` |
| Alpha source | Skinned → ANM red channel; non-skinned → AM alpha channel |
| AO source | Skinned → AM alpha channel; non-skinned → ao_map green channel |
| GMM channels | R = gloss → `glossiness = pow(R/0.8, 7)` (HIGH = shiny); G = metallic → `metallic = pow(G/0.5, 5) * 1.5`; B = camo mask (reserved) |
| Direct specular | NDF multiplied by raw `GMM.r`; `specContrib *= S_level * gloss * 6` — rubber (low gloss) → near-zero specular |
| IBL gating | Multiplied by `NdotV * gloss` so rough surfaces also receive less environment light |
| Lighting model | Single directional-style orbit light (`× 10` flat, no distance attenuation), matching `tank_fragment.glsl` style |
| Tonemap | ACES filmic (Narkowicz 2015) + sRGB gamma |
| Armor color | Per-nation linear sRGB tint, blended uniformly with factor `0.55` (camo mask deferred until camo texture support arrives) |
| HD textures | Insert `_hd` before `.dds` extension; fall back to SD if absent |
| Section alignment | `current_offset += current_offset % 4` (Tank Exporter convention) |
| Coordinate space | DirectX → OpenGL: flip Z on positions/normals/tangents/binormals; flip V on UVs |
| Vehicle XML | BWXML binary inside `scripts.pkg`; `<xmlns:xmlref>` element stripped before ET parse |
| `damaged` variant | `<models/destroyed>` instead of `<models/undamaged>` for every component |
