# Changelog — Tank Exporter PY

A Python/PyOpenGL rewrite of the original VB.NET Tank Exporter for World of Tanks.
Entries are ordered newest-first.  Dates are best-effort: today's session is exact;
earlier entries are grouped by the milestone they belong to (no git history was
available at the time this file was written).

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
