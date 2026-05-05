# Project Orientation — Tank Exporter PY (TEPY)

You are working on **Tank Exporter PY**, a Python/PyOpenGL rewrite of the
original VB.NET TankExporter for *World of Tanks*.  This file orients you
on conventions and known pitfalls so you don't re-derive what previous
sessions already worked out.

The user (mikeoverbay / "coffee") owns the original VB project and is
porting it.  Public repo: `mikeoverbay/Tank-Exporter-PY` on GitHub
(default branch `master`).

---

## Where to look first

| Question                                               | Read                                              |
| ------------------------------------------------------ | ------------------------------------------------- |
| What's the project structure?                          | `ARCHITECTURE.md`                                 |
| User-facing features / controls                        | `README_TANK_VIEWER.md`                           |
| Recent changes / why things are the way they are       | `CHANGELOG.md` (newest entries first)             |
| Coordinate-system conventions                          | `COORDINATE_SYSTEMS.md`                           |
| `.primitives_processed` byte format (read **and** write) | `VISUAL_PROCESSED_FORMAT.md`                    |

---

## Tech stack reminders

* **PyOpenGL 3.3 core** + GLSL 330; geometry shaders for the
  surface-normal debug pass.
* **Pygame** for the window / input / UI rendering.
* **Tkinter** for modal file dialogs and the format / component pickers.
* **Blender headless** is the bridge for FBX / GLB / GLTF / OBJ I/O.
  Two runner scripts (`tankviewer/exporters/_blender_runner.py` and
  `tankviewer/importers/_blender_importer.py`) execute inside Blender's
  Python (`bpy` available, `tankviewer` package NOT) -- imports there
  must stay minimal.

---

## WoT format gotchas to keep in mind

These bit me at least once.  Don't let them bite you again.

### `.primitives_processed` has TWO section layouts

Detected on the writer side from `mesh.name` (the source section base
name preserved by the loader):

* **Bare-shared** -- one global `indices` + `vertices` (+ `uv2`)
  section pair, group_count in the indices header reports `N` primitive
  groups.  Visual references by index.  Used by hull / turret on
  G102_Pz_III.
* **Named-per-mesh** -- one `<base>.indices` + `<base>.vertices`
  (+ `<base>.uv2`) per mesh, each with `group_count = 1`.  Visual
  references by name (`<primitive>exportChassL1_Shape</primitive>`).
  Used by chassis / gun on G102.

The encoder picks the layout from `mesh.name`.  **Never** substitute
`mesh.identifier` (the visual-side material label) for the section
name -- they are different fields and the engine will fail the lookup.

### UV2 BPVT preamble is 136 bytes, not 132

Mirrors the `.vertices` preamble: 68-byte primary format string
(`BPVSuv2`) + 64-byte secondary (`set3/uv2pc`) + uint32 count + body.
A 132-byte probe matches by integer-division coincidence and silently
shifts the UV stream forward by 4 bytes -- this was a real bug we
fixed; see CHANGELOG 2026-05-05.  When probing, also require
`(size - body_offset) % 8 == 0` to kill the false positive.

### Custom split normals (FBX round-trip)

The Blender runner (export side) sets per-loop split normals via
`me.normals_split_custom_set_from_vertices`; the importer reads them
via `me.corner_normals[li].vector` (Blender 4.1+) or
`me.calc_normals_split() + me.loops[li].normal` (3.x / 4.0).  FBX
import passes `use_custom_normals=True` so split normals survive the
round trip without Blender re-computing them.

### WoT* color attributes carry the full skin / TBN data

Tangents, binormals, bone indices, and bone weights ride through FBX
/ glTF as named `FLOAT_COLOR` attributes
(`WoTTangent`, `WoTBinormal`, `WoTBoneIdx`, `WoTBoneWeight`) so the
round trip is loss-less.  Decoders are documented in
`tankviewer/importers/_blender_importer.py`.

### `.fbm` folders on FBX export are bad

If you set `path_mode='COPY'` on `bpy.ops.export_scene.fbx`, Blender
clones every referenced texture into a `<basename>.fbm/` folder next
to the FBX -- duplicating the files we already wrote to
`<basename>_textures/`.  Use `path_mode='AUTO'`.

### Pre-warming pkg ZipFile handles is the perf win

`PkgExtractor.__init__` opens every kept pkg via `_get_zip()` once at
startup and caches the handle.  That avoids per-load central-directory
re-parsing.  Skip map pkgs (regex `^\d+_`) -- they don't carry tank
assets.

### Persisted-entries cache is batched, not per-write

When PkgExtractor finds an entry via scan-fallback, it queues the
discovery.  `flush_persisted_entries()` writes them all at once at the
end of `load_vehicle`.  We previously rewrote the 15 MB
`xmlitemslist.xml` per discovery (20-30 times per fresh tank load);
batching dropped a 7.7 s first-chassis-load to ~70 ms.

---

## Build / version conventions

* Version lives in `tankviewer/__init__.py` (`__version__`).
* Bump it via `python cust_tools/bump_version.py {minor|major|patch}`
  after every meaningful change (the user wants this discipline kept).
* The "minor" digit climbs for "we added something or fixed a real
  bug"; "patch" for tiny touch-ups.  Major is reserved for the user's
  call.

---

## Diagnostic tools (under `cust_tools/`)

* `bump_version.py` -- CLI version bumper.
* `font_preview.py` -- pygame window cycling all installed fonts;
  used to pick the splash-banner cursive (`Gabriola`).
* `dump_sections.py <local_file>` or `<pkg> <internal>` -- prints the
  section table of any `.primitives_processed`.  First stop when
  debugging "why does the engine reject our file".
* `compare_sections.py <res_mods_tank_dir> <wot_packages_root>` --
  diffs every `.primitives_processed` under a res_mods tank against
  its pkg original at the section-table level.  How we verify
  round-trip correctness.

---

## What's NOT yet round-trip-safe

Don't claim these work without testing:

* **Vertex `.colour` write-back** -- parsed read-only.  Not yet emitted
  by the encoder, not yet round-tripped through Blender.
* **Visual-file rewrite for added meshes** -- adding new objects to a
  tank requires splicing fresh `<PG_ID>` blocks into
  `.visual_processed`.  Not implemented.
* **Building atlas rendering** -- atlas algorithm decoded from the
  PBS_tiled_atlas shader (see CHANGELOG 2026-05-04), but
  `BuildingLoader` not yet wired up.
* **Skinned bone-byte reverse-padding** -- `iii` / `ww` carry
  `SC_UBYTE4_*_REVERSE_PADDED_*` data per the official
  `shaders/formats/*.xml`.  We currently treat them as plain
  `4×uint8` because all we do is round-trip through Blender's
  `WoTBoneIdx` / `WoTBoneWeight` color attributes; the reverse padding
  matters only if we ever drive a real Blender skin cluster.

---

## When in doubt

* The user prefers **bumping a new version + writing real comments**
  over leaving a TODO.
* "Don't rename the parts" was a real complaint -- never invent or
  modify section / mesh names that came from a pkg.  Pass them through
  verbatim.
* The legacy VB writer at `reference/modPrimWriter.vb` is the
  ground-truth reference for the encoder logic when ours and the engine
  disagree.
* If a file format question can be answered by **looking at bytes from
  a real pkg**, do that first instead of theorising.
  `cust_tools/dump_sections.py` and ad-hoc hex dumps of pkg slices
  have ended several debugging sessions in minutes.
