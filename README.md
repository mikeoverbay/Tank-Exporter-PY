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
  with adjustable start size, end size, speed, and fade.  Smoke and
  fire scale **per WoT engine class** (`gas_small`, `gas_medium`,
  `gas_large`, `diesel_small`, `diesel_medium`, `diesel_large`,
  `diesel_strv`) -- the loaded tank's `<exhaust><pixie>` value
  auto-wires which slot the sliders read/write, so a tier-1 light
  doesn't puff Maus-scale clouds.  Fire billboards are anchored
  bottom-center on each `HP_Fire_*` hardpoint so flames rise up out
  of the deck rather than sinking through it.

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
- `start.bat` -- minimal launcher (no install dance, just launch).
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

### Top buttons -- the IO group

| Button       | What it does                                                   |
| ------------ | -------------------------------------------------------------- |
| **Set Paths**| Configure WoT install paths (saved to `tankExporterPy.json`).      |
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
