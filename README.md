![TEPY banner](resources/tepy_banner.png)

# TEPY -- Tank Exporter in PYthon

A Python/PyOpenGL rewrite of the original VB.NET Tank Exporter for
*World of Tanks*.  Loads tanks straight from the WoT install, renders
them with PBR + IBL lighting, and round-trips back out as
`.primitives_processed` files the game accepts.

---

## Current work — track physics rewrite (2026-05-08)

The current track render is a static "rubber-band" ribbon (the
welded `track_LShape12` / `track_RShape12` mesh inside
`Chassis.primitives_processed`, UV-scrolled to fake motion).  It
doesn't deform with wheel deflection, has no per-pad geometry,
and can't round-trip as a real animatable track.

In progress: replacing it with a **kinematic-bone-driven NURB**
that resamples to per-pad transforms at uniform arc length, with
each `V_loc` control point bound 1:1 to a chassis bone
(`Track_L*` under wheels, `Track_VT_L*` on the top run,
`Track_VD_L*` on sprocket / idler).  Physics already settles
each road wheel's Y under gravity — the spline rides those.

See **`ARCHITECTURE.md` → "Track physics roadmap"** for the full
plan, the centripetal-Catmull-Rom fit numbers
(15.126 m vs 15.561 m target on T30, std 0.3 mm pad spacing
across 117 pads), the DX→GL frame-conversion rule, and the
phased work order (Phase A spline + physics → Phase E export /
import).

The rubber-band path will be parked behind a `--legacy-tracks`
flag, not deleted, so we keep an A/B fallback while coverage is
verified.

---

## Capabilities

### Read & render
- **Binary mesh parser** -- `.primitives_processed` (every BPVT vertex
  format and both section layouts: bare-shared and named-per-mesh).
- **Visual + vehicle XML loader** -- assembles hull + chassis + best
  turret + top gun with correct world-space offsets; optional
  *destroyed/crashed* variant.
- **`.pkg` extractor** -- pulls assets straight out of WoT's
  `res/packages/*.pkg` archives via `TheItemList.xml` index, no
  manual extraction.  Pre-warms ZipFile handles at startup so
  per-load asset fetches are sub-millisecond.
- **Tank browser tree** -- per-nation, tier-filtered list sourced
  from each `list.xml` and filtered by Tank Exporter's `tanks.txt`;
  hover for thumbnail, click for load-confirm dialog.
- **PBR + IBL rendering** -- GGX direct lighting + split-sum IBL
  (Lambertian irradiance, GGX prefiltered specular, BRDF LUT), all
  baked from the skybox cubemap on startup.  Tuned to WoT's own
  `tank_fragment.glsl` formulas (gloss-gated specular kills rubber
  reflections, etc.).
- **Per-nation armor color** -- linear sRGB tint of the diffuse,
  picked from the tank's nation in the XML path.
- **Particles** -- camera-facing billboard smoke + fire flipbooks
  with adjustable start size, end size, speed, and fade.  Smoke and
  fire scale **per WoT engine class** (`gas_small`, `gas_medium`,
  `gas_large`, `diesel_small`, `diesel_medium`, `diesel_large`,
  `diesel_strv`) -- the loaded tank's `<exhaust><pixie>` value
  auto-wires which slot the sliders read/write, so a tier-1 light
  doesn't puff Maus-scale clouds.  Fire billboards are centred on
  each `HP_Fire_*` hardpoint -- the artist authors HPs at the
  centroid of where the flame should appear, so centered anchoring
  matches in-game placement.
- **Crash damage shader** -- damaged tanks render with the standard
  PBR pass plus a multiplicative damage layer (replicates WoT's
  `PBS_tank_crash.fx`).  A single shared tile
  (`vehicles/russian/Tank_detail/crash_tile.dds`) packs three
  grayscale dirt / scorch variants into RGB; a world-space hash
  picks one channel per ~0.6 m region and a per-load offset rotates
  which channel each region picks, so reloading the same damaged
  tank cycles through every authored variant.
- **Multi-language UI** -- 21 localizations via Python's stdlib
  `gettext` and a curated translation table.  Languages: Bulgarian,
  Chinese (Simplified + Traditional), Czech, English, French,
  German, Hungarian, Italian, Japanese, Korean, Polish, Portuguese
  (Brazilian), Romanian, Russian, Spanish (Spain + Argentina), Thai,
  Turkish, Ukrainian, Vietnamese.  Pick via the **Language** button
  in the IO group; selection persists in `tankExporterPy.json`
  under `language` and applies on next launch.  Untranslated
  strings fall back to the canonical English `msgid` so the UI
  never shows blank labels even if a catalog is incomplete.
- **Themed UI palette** -- 10 curated colour presets (TEPY Default,
  Solarized Dark, Dracula, Nord, Gruvbox Dark, Tokyo Night,
  Monokai, Catppuccin Mocha, One Dark, Material Dark) drive every
  button accent + clear colour + console text colour via four
  named slots (c1 / c2 / c3 / c4) plus a free-form bg.  Pick via
  the **Theme** button in the IO group; live-applies (no restart).

### Diagnose & inspect
- **Surface-normal debug shader** -- geometry-shader pass renders
  short lines along surface normals.  Two modes: by-face (cyan
  centroid + cross-product face normal -- catches reverse-wound
  triangles) and by-vertex (axes-coloured |n.x|=R, |n.y|=G, |n.z|=B
  -- catches custom-normal skew).
- **Wireframe overlay** -- polygon-offset on the fill pass keeps the
  solid mesh under the wires without Z-fighting.
- **Mesh-visibility window** -- per-mesh on/off toggle for hiding
  parts of the loaded tank.
- **Compare dialog** -- side-by-side per-part stats (face / vert /
  index counts, UV2 presence, vertex format) for FBX vs PKG sets;
  burnt-orange highlight on mismatched rows.

### Export & write back
- **FBX / GLB / GLTF / OBJ export** via headless Blender bridge.
  Carries every per-vertex value WoT ships (positions, normals, UV0,
  UV1, tangents, binormals, bone indices, bone weights) through as
  named `WoT*` `FLOAT_COLOR` attributes so the round trip is
  loss-less.  Custom split normals preserved via Blender's
  per-loop normals API.  Texture sidecar in
  `<basename>_textures/`.
- **FBX / GLB / OBJ import** -- read a Blender-edited tank back into
  the viewer's scene; A/B-flip against the still-loaded PKG set
  before re-export.
- **`.primitives_processed` write-back ("Save Prim")** -- emits the
  hull / chassis / turret / gun parts of a loaded tank in WoT's
  native binary format, into `res_mods/<version>/` at the canonical
  pkg paths.  Round-trip-verified against G102_Pz_III: the WoT
  engine and TEPY both accept the re-encoded files.

### Utilities
- **ItemList rebuild** -- one-click button that walks every kept
  pkg and writes a fresh comprehensive `TheItemList.xml`.  Use after
  a game patch to refresh the lookup table so `PkgExtractor` hits
  the O(1) dict path on first try instead of scanning multiple pkgs.
- **`cust_tools/dump_sections.py`** -- prints the section table of
  any `.primitives_processed` (in a pkg or on disk).
- **`cust_tools/compare_sections.py`** -- diffs every
  `.primitives_processed` under a `res_mods` tank against its pkg
  original at the section-table level.
- **`cust_tools/dump_track_skinning.py`** -- for any tank +
  side, walks the chassis primitives + visual_processed and
  dumps the renderSet bone palette, the per-vertex bone-index
  groupings, per-wheel Z-windows, and a side-view PNG colouring
  every track vertex by its dominant bone.  Use to investigate
  how the track skinning rig drives per-wheel sag.
- **`cust_tools/demo_terrain_corners.py`** -- sanity-check the
  Terrain Y sampler and compute chassis pitch/roll for a virtual
  T110E4 placed at any world (X, Z) + yaw on the loaded terrain.
  Builds the heightmap headlessly so it runs without a GL context.
- **`cust_tools/paint_sand_desert.py`** -- generates a tileable
  procedural sand texture + grayscale displacement companion at
  up to 16K resolution (see "Painting a procedural sand texture"
  below).

---

## Install

### 1. Download

Pick whichever you're comfortable with -- both produce the same
working tree.

**Option A: Git clone** (preferred if you have Git -- makes
`git pull` updates trivial):

```
git clone https://github.com/mikeoverbay/Tank-Exporter-PY.git
```

**Option B: Download ZIP** (no Git required):

1. Open <https://github.com/mikeoverbay/Tank-Exporter-PY> in a
   browser.
2. Above the file list, click the green **`<> Code`** button.  A
   small dropdown panel opens.
3. At the bottom of that panel, click **Download ZIP**.  Your
   browser starts a download named
   `Tank-Exporter-PY-master.zip` (~tens of MB; varies with the
   resources baked into the current commit).
4. Open the downloaded file (it usually lands in
   `C:\Users\<you>\Downloads\`).  In Windows Explorer, right-click
   the ZIP -> **Extract All...** -> choose where to extract (see
   "Where to put it" below) -> **Extract**.
5. Windows extracts into a folder named
   `Tank-Exporter-PY-master/` by default.  Rename it to
   `Tank-Exporter-PY/` if you want it to match the Git-clone name
   -- everything else works identically either way.

> **Tip.**  Built-in Windows extraction works fine.  7-Zip /
> WinRAR / similar tools also work; just point them at the same
> destination folder.

> **Updates.**  The ZIP path doesn't auto-update.  When a new
> version lands you can either re-download the ZIP and replace
> the old folder, or switch to **Option A** so `git pull` does
> the update for you.  If you replace the folder, keep your
> `tankExporterPy.json` config and `resources/requirements_backup/`
> from the old install -- the first preserves your WoT paths,
> the second saves the requirements re-download.

### 2. Where to put it

TEPY reads and writes inside its own project folder
(`TheItemList.xml`, the requirements backup, your saved
`tankExporterPy.json` config, etc.), so put it somewhere your user
account can write without UAC prompts.  Recommended:

```
C:\Users\<you>\Documents\Tank-Exporter-PY\
```

or

```
C:\Tools\Tank-Exporter-PY\
```

**Avoid** `C:\Program Files\` and `C:\Program Files (x86)\` --
Windows blocks normal write access to those directories and the
first-run rebuild of `TheItemList.xml` will fail with a permission
error.

It does not have to live next to your WoT install -- TEPY finds
the game via the path you pick in the **Set Paths** dialog (or
the `--pkg-dir` / `--res-mods` CLI flags).  Anywhere on any drive
you can write to is fine.

### 3. Install Python (if you haven't already)

TEPY needs Python 3.10+.  Grab the latest from
<https://www.python.org/downloads/>.

> :warning: **Tkinter must be checked.**  When the python.org
> installer opens, click **Customize installation** (NOT
> "Install Now"), then on the **Optional Features** screen
> make sure :ballot_box_with_check: **"tcl/tk and IDLE"**
> is checked.  Without it, every TEPY file picker
> (Set Paths, Import, Export, Save Prim, Language) silently
> fails -- this is the #1 install gotcha.  See
> [Tkinter is required](#tkinter-is-required) for the repair
> steps if you only realise after the fact.

You can leave the rest of the installer at its defaults.

### 4. Run it -- Windows just double-click

```
go.bat
```

The launcher checks for Python and the four runtime packages
(`pygame`, `PyOpenGL`, `numpy`, `Pillow`).  If any are missing it
installs them, makes a permanent backup of the requirements at
`resources/requirements_backup/`, deletes the live `requirements/`
folder, and launches the viewer.  Subsequent runs skip the install
step entirely and go straight to launch.

On the first launch TEPY also auto-builds `TheItemList.xml` from
your WoT pkgs (~3 seconds) -- you'll see progress in the console
panel.  After that, every later launch is fast.

Companion bats:
- `launch_skip_deps.bat` -- bypass-the-install-check launcher.  No
  import probe, no requirements\ shuffling -- straight to
  `python tankExporterPy.py`.  **Only use this after a successful
  `go.bat`** has installed the runtime packages once; running it on
  a fresh machine just gets you a Python ImportError.  (Was
  `start.bat` pre-v1.67.3 -- renamed because the obvious-sounding
  name was steering new users away from `go.bat`.)
- `uninstall.bat` -- pip-uninstalls the four runtime packages.
- `reinstall.bat` -- restores `requirements/` from the backup and
  force-reinstalls everything.

### Manual install

```bash
python -m pip install pygame PyOpenGL numpy Pillow
python tankExporterPy.py
```

### First-time setup

Point TEPY at your WoT install once (paths get saved to
`tankExporterPy.json` and reused forever):

```
python tankExporterPy.py --pkg-dir  "C:\Games\World_of_Tanks_NA\res\packages"
python tankExporterPy.py --res-mods "C:\Games\World_of_Tanks_NA\res_mods\<version>"
```

Or use the **Set Paths** button in the IO group of the left panel.

---

## Usage

After launch, pick a tank from the right-hand tier-filtered tree,
confirm in the load dialog, and the viewer assembles the full vehicle
(hull + chassis + best turret + top gun) with correct offsets.

The left panel is split into collapsible sections.  Click the
chevron in any section header (`▼ <name>` expanded, `▶ <name>`
collapsed) to fold it; state persists in `tankExporterPy.json`
under `section_collapsed`.  The right panel has one matching
collapsible group titled **Debug**.

### `res_mods` group (top)

Operations against your `<res_mods>/<version>/` folder.

| Button                   | What it does                                                                                                                                                                    |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Extract**              | Tk dialog: tick Hull / Chassis / Turret / Gun + an "Extract textures" toggle.  Copies `.primitives_processed` + `.visual_processed` (+ optional `.model`) to res_mods at the canonical pkg paths.  Damaged tanks land under `/crash/`; normal under `/normal/`.  **Existing textures are never overwritten** -- they're kept so you can't accidentally clobber edits.  |
| **Open Extract Loc**     | Opens Explorer at the variant subfolder Extract just wrote into (`.../normal/` or `.../crash/`).  Falls back to the whole-tank dir if only the *other* variant has been extracted.  Asks via Tk yes/no whether to run Extract now if neither is present. |
| **Remove from res_mods** | Deletes the loaded tank's whole-tank res_mods folder (both variants).  **Strong confirmation** -- a Tk dialog requires you to *type* the tank's xml basename to enable the Delete button.  Counts files before deleting and prunes empty parent dirs after. |

### UI group

| Button       | What it does                                                                                                                                                                    |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Grid**     | Show the ground grid.                                                                                                                                                            |
| **Axes**     | Show the world XYZ axis lines at origin.                                                                                                                                         |
| **Light**    | Show the orbit-light indicator spheres.                                                                                                                                          |
| **Orbit**    | Animate the three scene lights orbiting the model.                                                                                                                               |
| **Skybox**   | Show / hide the cubemap skybox (IBL keeps working either way).                                                                                                                   |
| **Terrain**  | Show / hide the procedural ground.  See **Terrain** below for heightmap, sand texture, and detail-displacement options.                                                          |

### Model group

Operations on the loaded mesh set rather than the viewport.

| Button       | What it does                                                                                                                                                                    |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Meshes**   | Open the per-mesh visibility window.                                                                                                                                             |
| **Flip**     | Toggle between FBX-imported and PKG-loaded mesh sets.                                                                                                                            |
| **Compare**  | Side-by-side per-part stats (FBX vs PKG).                                                                                                                                        |
| **Wireframe**| Polygon-offset wireframe overlay on top of the solid pass.                                                                                                                       |
| **Shaded**   | Render the loaded tank in cheap diffuse + bump Phong (no PBR / IBL / GMM / damage / armour tint).  Surface hard-coded to a neutral light grey (`vec3(0.22)` linear → mid grey lit, dark grey in shadow) for shape evaluation regardless of theme.  Useful as a diagnostic A/B against the full PBR look. |

## Terrain

The **Terrain** UI button toggles a 1025×1025-vertex ground mesh
spanning a 160 m × 160 m world centred under the tank.

**Macro heightmap source** (in priority order):
1. The path in `terrain_heightmap` config key (any
   Pillow-decodable grayscale image).
2. `resources/heightmap.png` if present.
3. Procedural Perlin-fBm fallback.

The image-loader path resamples to the mesh resolution (LANCZOS),
Gaussian-smooths, edge-fades the borders so the terrain ramps to
zero at the world edges, optionally curve-remaps via `terrain_curve_gamma`,
and anchors the lowest pixel at `y=0` so the tank's tracks always
meet flat ground.

**Sand-diffuse texture** (priority):
1. `terrain_sand_texture` config key.
2. `resources/sand_painted.png` (procedural -- generated by the
   painter tool below).
3. `resources/sand.png` (any user-supplied photo).

The texture is uploaded with `glGenerateMipmap` + trilinear +
16× anisotropic + `GL_REPEAT`.  Tiled at 50 m per repeat by
default (`terrain_sand_tile_size`).  The shader samples the
texture as the surface diffuse with a small UV warp to break
grid alignment, applies Lambert + sun-direction warm/cool tint,
slope desaturation toward neutral grey on cliffs, and
exponential distance fog.  No specular -- pure diffuse ground.

**Detail displacement** (priority):
1. `terrain_detail_heightmap` config key.
2. `sand_painted_height.png` next to the active sand colour
   texture (auto-paired).

Tiled at `terrain_detail_tile_size` (defaults to the sand tile
size) and added on top of the macro heights at amplitude
`terrain_detail_height_scale` (default 5 cm).  When paired with
the painted sand texture, the geometry's ripple troughs line up
with the colour texture's ripple troughs because both are derived
from the same source surface.

### Painting a procedural sand texture

```bash
python cust_tools/paint_sand_desert.py            # 8192² default
python cust_tools/paint_sand_desert.py --size 4096
python cust_tools/paint_sand_desert.py --size 16384  # GL max; ~6 GB CPU peak
python cust_tools/paint_sand_desert.py --seed 7
```

Writes `resources/sand_painted.png` (RGB colour) and
`resources/sand_painted_height.png` (grayscale displacement
companion) next to it.  All layers are FFT-domain tileable so
the texture wraps perfectly under `GL_REPEAT`.  At 8192² the run
takes ~70 s and ~1.5 GB CPU peak.

### Config keys (`tankExporterPy.json`)

| Key | Default | Purpose |
|-----|---------|---------|
| `terrain_heightmap`             | (auto)   | Path to macro heightmap image |
| `terrain_smooth_sigma`          | 1.0      | Gaussian smooth radius in pixels |
| `terrain_edge_fade`             | 0.10     | Edge falloff fraction (0 disables) |
| `terrain_curve_gamma`           | 1.0      | Power-curve remap (<1 deepens valleys, >1 flattens peaks) |
| `terrain_sand_texture`          | (auto)   | Path to sand-diffuse texture |
| `terrain_sand_tile_size`        | 50.0     | Metres per repeat of the diffuse texture |
| `terrain_detail_heightmap`      | (auto)   | Path to detail displacement heightmap |
| `terrain_detail_tile_size`      | 50.0     | Metres per repeat of the detail tile |
| `terrain_detail_height_scale`   | 0.05     | Detail displacement amplitude in metres |

### IO group

| Button        | What it does                                                                                                                                                                    |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Set Paths** | Configure WoT install paths (saved to `tankExporterPy.json`).                                                                                                                       |
| **Import**    | Read FBX / GLB / OBJ back into the viewer (Blender bridge).                                                                                                                      |
| **Export**    | Write the loaded tank as FBX / GLB / GLTF / OBJ.                                                                                                                                 |
| **Save Prim** | Write hull / chassis / turret / gun back as `.primitives_processed` into `res_mods/`.                                                                                           |
| **Language**  | Tk picker for the in-app UI language; restart-to-apply.                                                                                                                          |
| **Theme**     | Tk picker showing every preset palette as a row of swatch previews (c1 / c2 / c3 / c4 / bg).  Click a row → Set to apply live (no restart); Cancel keeps the previous theme.    |

### Tools group

| Button       | What it does                                                   |
| ------------ | -------------------------------------------------------------- |
| **ItemList** | Rebuild `TheItemList.xml` from every kept pkg.                 |
| **Pick Tri** | Toggle the off-screen triangle picker.  When ON, hover the loaded tank to see per-vertex bone indices, weights, and vertex-colour bone-tag in the console.  The hovered triangle paints in the active theme's c1 (fill) / c2 (edges) with red / green / blue vertex markers; the console lines carry colour-coded `#` markers matching the on-screen vertices. |

### Right panel -- Debug group

Smoke / fire / normals sliders + PerVtx / Debug checkboxes.
Same chevron toggle as the left-panel sections.  Collapsed:
panel shrinks to the header strip and the GL viewport reclaims
the bottom-right of the screen.

### Load dialog

A "**Load from res_mods**" checkbox at the top of the dialog
controls whether the loader walks res_mods overrides (default
checked) or reads textures + visuals straight from pkg
(unchecked).  Per-load setting; persists across sessions.
FBX-import → PKG-twin auto-loads always use pkg-only,
regardless of the checkbox.

### Camera & keys

Mouse drag is **mode-aware** — left-drag means different things in
different camera modes (cycle with `C`):

| Mode | Left-drag | Right-drag | Wheel |
|------|-----------|-----------|-------|
| 0 (orbit / free cam)        | Orbit world camera | Pan on XZ ground plane | Zoom |
| 1 (chase, default)          | Orbit chase angle in chassis-local frame (camera tracks the tank's yaw — kid-out-the-window behavior) | Pan look-at | Zoom (chassis-local distance) |
| 2 (commander, in the seat)  | Rotate the head left/right + up/down in chassis-local space (look around the tank's interior) | Pan look-at | (no zoom — fixed seat) |

| Input                       | Action                                            |
| --------------------------- | ------------------------------------------------- |
| `C`                         | Cycle camera mode (orbit → chase → commander → orbit).  Default at startup AND on every tank load = chase. |
| `R`                         | Reset camera (orbit defaults; goes back to mode-0 view next time you cycle there) |
| Middle-click hold           | Show look-at crosshair (also fires automatically on right-drag) |
| `Shift` + middle-click drag | Lift / drop the look-at point on the world Y axis (drag DOWN raises, drag UP lowers) |
| `F2`                        | Toggle wireframe overlay                          |
| `N`                         | Toggle normal map                                 |
| `W` `S`                     | Drive forward (hold)                              |
| `Z` `X`                     | Drive backward (hold)                             |
| `A` `Q`                     | Yaw left                                          |
| `D` `E`                     | Yaw right                                         |
| `0` – `9`                   | Speed step (0 = stopped, 1 = top speed, 9 = creep) |
| `O`                         | Toggle auto-circle drive                          |
| `F11`                       | Toggle maximized ↔ windowed                       |
| `ESC`                       | Quit                                              |

A **pale-pink crosshair** (10 units total per axis: X / Y / Z)
draws at the look-at point during right-button-drag (pan) and
middle-button-hold (Y-lift), so the camera pivot is always visible
during a move.  Hidden otherwise.  Depth-tested — when the look-
at point sits inside geometry, only the parts that emerge to the
surface are visible.

When the cursor enters the TEPY window, focus is automatically
transferred so the next click registers as a UI action --
without the standard Windows "first click eaten by the OS"
double-tap.  Focus is handed back to the previous foreground
window when the cursor leaves.

---

## Requirements

- **Python 3.10+** (3.13 used in development; 3.10 is the floor).
  - **Tkinter MUST be installed.**  See the
    [Tkinter is required](#tkinter-is-required) note below before
    you install Python -- the python.org installer makes it
    *optional* and TEPY's Set Paths / Import / Export / Language
    pickers all break without it.
- **Windows 10 / 11** for the .bat launchers and the taskbar
  AppUserModelID; the Python code itself runs on macOS / Linux too,
  there's just no first-class launcher.
- **A WoT install** -- TEPY reads from `<wot>/res/packages/*.pkg`
  and `<wot>/res_mods/<version>/`.
- **Blender 3.x or 4.x** (optional) -- only needed for FBX / GLB /
  GLTF / OBJ Import / Export.  Save Prim works without Blender.

Python packages (handled automatically by `go.bat`):

```
pygame >= 2.5
PyOpenGL >= 3.1
numpy >= 1.24
Pillow >= 10.0
```

### Tkinter is required

TEPY uses Python's stdlib `tkinter` for every file / folder picker:
**Set Paths**, **Import**, **Export**, **Save Prim**'s component
picker, the **Language** dropdown, the **FBX upgrade** popup, all
of it.  Without it, those dialogs silently fail to open and a
tester sees "the buttons don't do anything."

Tkinter is part of the Python standard library, **but the
python.org Windows installer makes it optional**.  The
**Customize installation -> Optional Features** screen has a
checkbox labelled **"tcl/tk and IDLE"** -- if it's unchecked at
install time, you end up with a Python that lacks tkinter.

**Verify** which Python TEPY runs against and whether tkinter is
present:

```cmd
py -3 -c "import sys; print(sys.executable)"
py -3 -c "import tkinter; print('OK', tkinter.TkVersion)"
```

The first prints the Python install path; the second prints
`OK 8.6` (or similar) if tkinter works, or an `ImportError` if
not.

**If tkinter is missing**, repair the install in place:

1. **Settings -> Apps -> Installed apps**
2. Find your Python entry -> **... -> Modify**
3. Click **Modify** in the installer
4. Under **Optional Features**, check :ballot_box_with_check:
   **"tcl/tk and IDLE"**
5. Click **Next -> Install**

That repairs in place; no need to uninstall first.  After it
finishes, all TEPY file dialogs work.

TEPY also detects the missing-tkinter case at startup and posts a
clear error in the in-app console pane with the same repair
steps -- so a tester running the binary doesn't have to ask you.

---

## Buy a Coffee for Coffee_

If TEPY is useful to you, consider supporting development:

[![Donate via PayPal](https://img.shields.io/badge/Donate-PayPal-blue?logo=paypal&logoColor=white)](https://www.paypal.com/donate/?hosted_button_id=HVRUCWXVKRJ26)

<https://www.paypal.com/donate/?hosted_button_id=HVRUCWXVKRJ26>

---

## Project layout

```
tankExporterPy.py              entry point
tankExporterPy/                 main package (loaders, writers, UI, render)
cust_tools/                 diagnostic + asset-generation scripts
shaders/                    GLSL 330 shader sources
resources/                  splash, banner, icons, environment maps
                            (fire/ and smoke/ are auto-extracted on
                            first launch from your WoT install -- the
                            repo never carries Wargaming's textures)
reference/                  legacy VB code kept as ground truth
ARCHITECTURE.md             per-module reference
CHANGELOG.md                per-day work log (newest first)
COORDINATE_SYSTEMS.md       Y-up vs Z-up swizzle reference
VISUAL_PROCESSED_FORMAT.md  on-disk byte spec (read AND write)
CLAUDE.md                   AI session orientation file
```

---

## Credits

Original VB.NET Tank Exporter by **mikeoverbay** ("Coffee_").
Python rewrite ongoing.

*World of Tanks* © Wargaming.net.
