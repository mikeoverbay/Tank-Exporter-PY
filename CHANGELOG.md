# Changelog — Tank Exporter PY

A Python/PyOpenGL rewrite of the original VB.NET Tank Exporter for World of Tanks.
Entries are ordered newest-first.  Dates are best-effort: today's session is exact;
earlier entries are grouped by the milestone they belong to (no git history was
available at the time this file was written).

---

## 2026-05-05 (afternoon)

Tooling pass: a fresh-rebuild path for `TheItemList.xml`, an in-app
button to invoke it, a left-panel reorganisation that groups every
button by category (UI / IO / Tools), a Windows .bat launcher
trio (`go.bat` / `uninstall.bat` / `reinstall.bat`), and a TEPY
rebrand (window title + tepee icon).

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
