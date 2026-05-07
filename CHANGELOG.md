# Changelog — Tank Exporter PY

A Python/PyOpenGL rewrite of the original VB.NET Tank Exporter for World of Tanks.
Entries are ordered newest-first.  Dates are best-effort: today's session is exact;
earlier entries are grouped by the milestone they belong to (no git history was
available at the time this file was written).

---

## 2026-05-06

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
