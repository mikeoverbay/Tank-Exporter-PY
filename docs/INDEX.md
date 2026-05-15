# TEPY architecture docs — INDEX

The single `ARCHITECTURE.md` grew past 700 lines and "where does X
live" turned into a grep+guess for both humans and Claude.  This
folder is the per-domain split.  Pick the doc, not the file.

## First step before any code grep

```
python cust_tools/arch.py search "<topic>"
python cust_tools/arch.py list                # every heading
python cust_tools/arch.py stale               # docs older than the .py they describe
```

## Topic → doc map

| When you're working on... | Read first |
|---------------------------|-----------|
| Wheel suspension, contact classification, plane fit, drive controls, settling math, total tank weight | [`PHYSICS.md`](PHYSICS.md) |
| The kinematic-bone-driven NURB track replacement (in progress) | [`TRACK_PHYSICS.md`](TRACK_PHYSICS.md) |
| Render passes, draw order, mesh-skip gates (solid / wireframe / normals / picker), camera modes, look-at crosshair, shaders chosen per source, aim cursor + shellhole decal subsystem | [`RENDERING.md`](RENDERING.md) |
| Per-module API, file map, file-by-file class / function breakdown, WoT-specific notes table | [`../ARCHITECTURE.md`](../ARCHITECTURE.md) |
| DX → GL coordinate conversion (Z flip, V flip, traversal reversal) | [`../COORDINATE_SYSTEMS.md`](../COORDINATE_SYSTEMS.md) |
| `.primitives_processed` byte format (read AND write), section layouts (bare-shared vs named-per-mesh), UV2 sidecar BPVT preamble (136 bytes!) | [`../VISUAL_PROCESSED_FORMAT.md`](../VISUAL_PROCESSED_FORMAT.md) |
| User-facing controls, panel layout, keyboard map, install / setup, language picker | [`../README.md`](../README.md) and [`../README_TANK_VIEWER.md`](../README_TANK_VIEWER.md) |
| Recent changes / why a thing is the way it is | [`../CHANGELOG.md`](../CHANGELOG.md) (newest first) |
| Claude session orientation, format gotchas, version-bump discipline | [`../CLAUDE.md`](../CLAUDE.md) |

## When to extract a new doc

Rule of thumb: when `ARCHITECTURE.md` has more than ~50 lines on
one subsystem AND you're about to add another 50, that subsystem
gets its own file.  Update this index in the same commit.

Future likely splits (extract opportunistically as we touch them):

* `LOADERS.md` — `loaders.py` (PkgExtractor, MeshParser, VisualLoader,
  VehicleXMLLoader); `mesh.py`.
* `UI.md` — `ui.py` (themes, languages, widget kinds, panel
  collapse).
* `SHADERS.md` — GLSL pipeline, IBL pre-filter, GMM channel decode,
  damage tile.
* `EXPORT_IMPORT.md` — Blender bridge runner scripts, FBX / GLB /
  GLTF / OBJ I/O, WoT* color-attribute round-trip.
* `PARTICLES.md` — `particles.py` (smoke / fire flipbooks, emitter
  position update preserving spawn accumulator).

## Doc maintenance

* When you change code, update the doc in the same commit.  The
  `arch.py stale` command will flag drift on the next run.
* Headings: prefer `## Section` for top-level domain divisions and
  `### Subsection` underneath.  `arch.py list` reads these to build
  the table-of-contents view.
* Cross-link.  Every doc that touches a concept lives elsewhere
  should link there with a relative path.
