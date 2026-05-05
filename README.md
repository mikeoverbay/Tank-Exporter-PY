![TEPY banner](resources/tepy_banner.png)

# TEPY -- Tank Exporter in PYthon

A Python/PyOpenGL rewrite of the original VB.NET Tank Exporter for
*World of Tanks*.  Loads tanks straight from the WoT install, renders
them with PBR + IBL lighting, and round-trips back out as
`.primitives_processed` files the game accepts.

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
  with adjustable start size, end size, speed, and fade.

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

---

## Install

### Windows -- just double-click

```
go.bat
```

The launcher checks for Python and the four runtime packages
(`pygame`, `PyOpenGL`, `numpy`, `Pillow`).  If any are missing it
installs them, makes a permanent backup of the requirements at
`resources/requirements_backup/`, deletes the live `requirements/`
folder, and launches the viewer.  Subsequent runs skip the install
step entirely and go straight to launch.

Companion bats:
- `start.bat` -- minimal launcher (no install dance, just launch).
- `uninstall.bat` -- pip-uninstalls the four runtime packages.
- `reinstall.bat` -- restores `requirements/` from the backup and
  force-reinstalls everything.

### Manual install

```bash
python -m pip install pygame PyOpenGL numpy Pillow
python tank_viewer.py
```

### First-time setup

Point TEPY at your WoT install once (paths get saved to
`tankviewer.json` and reused forever):

```
python tank_viewer.py --pkg-dir  "C:\Games\World_of_Tanks_NA\res\packages"
python tank_viewer.py --res-mods "C:\Games\World_of_Tanks_NA\res_mods\<version>"
```

Or use the **Set Paths** button in the IO group of the left panel.

---

## Usage

After launch, pick a tank from the right-hand tier-filtered tree,
confirm in the load dialog, and the viewer assembles the full vehicle
(hull + chassis + best turret + top gun) with correct offsets.

### Top buttons -- the IO group

| Button       | What it does                                                   |
| ------------ | -------------------------------------------------------------- |
| **Set Paths**| Configure WoT install paths (saved to `tankviewer.json`).      |
| **Import**   | Read FBX / GLB / OBJ back into the viewer (Blender bridge).    |
| **Export**   | Write the loaded tank as FBX / GLB / GLTF / OBJ.               |
| **Save Prim**| Write hull / chassis / turret / gun back as `.primitives_processed` into `res_mods/`. |

### UI group

| Button       | What it does                                                   |
| ------------ | -------------------------------------------------------------- |
| **Meshes**   | Open the per-mesh visibility window.                           |
| **Flip**     | Toggle between FBX-imported and PKG-loaded mesh sets.          |
| **Compare**  | Side-by-side per-part stats (FBX vs PKG).                      |

### Tools group

| Button       | What it does                                                   |
| ------------ | -------------------------------------------------------------- |
| **ItemList** | Rebuild `TheItemList.xml` from every kept pkg.                 |

### Camera & keys

| Input                  | Action                                            |
| ---------------------- | ------------------------------------------------- |
| Right-click drag       | Orbit                                             |
| Middle-click drag      | Pan                                               |
| Scroll                 | Zoom (or scroll the tree when cursor is over it)  |
| `W`                    | Toggle wireframe                                  |
| `N`                    | Toggle normal map                                 |
| `R`                    | Reset camera                                      |
| `ESC`                  | Quit                                              |

---

## Requirements

- **Python 3.10+** (3.13 used in development; 3.10 is the floor).
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

Tkinter (used for file dialogs and the picker forms) ships with the
standard Python installer, no separate install needed.

---

## Buy a Coffee for Coffee_

If TEPY is useful to you, consider supporting development:

[![Donate via PayPal](https://img.shields.io/badge/Donate-PayPal-blue?logo=paypal&logoColor=white)](https://www.paypal.com/donate/?hosted_button_id=HVRUCWXVKRJ26)

<https://www.paypal.com/donate/?hosted_button_id=HVRUCWXVKRJ26>

---

## Project layout

```
tank_viewer.py              entry point
tankviewer/                 main package (loaders, writers, UI, render)
cust_tools/                 diagnostic + asset-generation scripts
shaders/                    GLSL 330 shader sources
resources/                  splash, banner, icons, environment maps
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
