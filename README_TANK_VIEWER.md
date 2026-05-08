# Tank Mesh Viewer - OpenGL .primitives_processed Viewer

A Python-based OpenGL viewer for World of Tanks `.primitives_processed`
meshes and complete vehicle XMLs.  Includes a built-in tank browser
sourced from the WoT install and Tank Exporter's tank list.

## Features

✓ **Binary parser** — `.primitives_processed` mesh format (reverse-engineered from Tank Exporter)
✓ **Vehicle XML loader** — assembles hull + chassis + best turret + top gun with correct world-space offsets, with optional **destroyed/crashed** variant
✓ **`.pkg` extractor** — pulls assets straight out of WoT's `res/packages/*.pkg` archives via `TheItemList.xml` index, no manual extraction needed
✓ **Tank browser tree** — right-hand panel listing every active tank (per-nation, tiered) sourced from each `list.xml` and filtered by Tank Exporter's `tanks.txt`; click → load-confirm dialog with a "load crashed" checkbox
✓ **Hover thumbnails** — PNG preview + Tank Exporter display name swap on hover, persist on the loaded tank
✓ **PBR rendering** — GGX direct lighting + split-sum IBL (Lambertian irradiance, GGX prefiltered specular, BRDF LUT), all baked from the skybox cubemap on startup; tuned to WoT's `tank_fragment.glsl` formulas (gloss-gated specular kills rubber reflections, etc.)
✓ **Per-nation armor color** — linear sRGB tint of the diffuse, picked from the XML path
✓ **Texture / AO / GMM channel handling** — auto-detects skinned vs non-skinned alpha & AO routing
✓ **Interactive camera** — right-click orbit, middle-click pan, scroll-wheel zoom
✓ **2-D overlay** — toggle bar for Grid / Axes / Light / Skybox / Wireframe, sliders for Light & Ambient, NMap / AO check-boxes
✓ **Persistent config** — CLI overrides for `--pkg-dir` / `--res-mods` / `--lookup-xml` are saved to `tankExporterPy.json`
✓ **Per-wheel suspension physics** — auto-extracts the wheel rig from any tank's chassis bones, samples terrain Y under each wheel, classifies each as CONTACT / HANGING / OVER_COMP with hysteresis, plane-fits the chassis tilt through contacting wheels, force-balances chassis Y so each wheel rests at its rest compression, and propagates per-wheel residuals through GPU skinning so the track + wheel geometry deforms with the terrain.  Toggle via the **Susp** checkbox.
✓ **Three camera modes** — orbit (default, mouse-driven), driver-side chase (free orbit but locked look-at on the driver position, anchored to chassis pose), commander POV (turret-base view forward, hides turret + gun for clear visibility).  Cycle with **C**.
✓ **Stepped drive speed** — keys `0..9` select speed.  `1` is hard-capped at the per-tank `<speedLimits><forward>` value from the gameplay XML; `9` is creep (0.5 kph); `0` is stopped.  Arrow keys drive, Q/E yaw.  Press **O** for hands-free auto-circle drive (steady 25 m radius arc at the current step).
✓ **Debug overlay** — pink stars at each wheel's terrain contact point, cyan stars at the wheel-centre target, yellow suspension shafts, blue X at the physics-computed hit location, red/green wheel-color highlight (CONTACT vs HANGING), orange hull bounding box, live per-bone state dump to the launching terminal.  All gated by the **Debug** checkbox.

## Requirements

```
Python 3.7+
pygame 2.x
PyOpenGL 3.x
Pillow (PIL)
NumPy
```

**Install dependencies:**
```bash
python -m pip install pygame PyOpenGL pillow numpy
```

## Usage

```bash
python tankExporterPy.py [<file>] [options]
```

`<file>` is optional.  Omit it to start with an empty scene and pick a
tank from the right-hand tree panel; otherwise pass either:

* a `.primitives_processed` (single-mesh load), or
* a vehicle `.xml` (full vehicle load with hull + chassis + best
  turret + top gun).

**First-time setup** — point the viewer at your WoT install once:

```bash
python tankExporterPy.py --pkg-dir  "C:\Games\World_of_Tanks_NA\res\packages"
python tankExporterPy.py --res-mods "C:\Games\World_of_Tanks_NA\res_mods\<version>"
```

The paths are written to `tankExporterPy.json` (next to `tankExporterPy.py`)
and reused on every subsequent run.

**Examples:**
```bash
# Empty start, pick from the tree:
python tankExporterPy.py

# Direct mesh:
python tankExporterPy.py "C:\path\Hull.primitives_processed"

# Direct vehicle XML (loads crashed-variant if you tick the dialog box
# when launching from the tree):
python tankExporterPy.py "C:\path\A14_T30.xml"
```

## Controls

| Input | Action |
|-------|--------|
| **Right-click drag** | Orbit camera |
| **Middle-click drag** | Pan camera |
| **Scroll wheel** | Zoom in / out (or scroll the tree when the cursor is over it) |
| **Left-click (tree)** | Expand a nation, or open the load dialog for a tank |
| **W** | Toggle wireframe |
| **N** | Toggle normal map |
| **R** | Reset camera to fit the loaded mesh |
| **C** | Cycle camera mode: orbit / driver-side chase / commander-POV |
| **H** | Toggle wheel-state highlight (red contact / green hanging; gated by Debug) |
| **O** | Toggle auto-circle drive (tank pulls a steady arc at current speed step) |
| **0..9** | Speed step: `0` = stopped, `1` = per-tank max kph from gameplay XML, `9` = creep (0.5 kph) |
| **Arrow keys** | Drive the tank (when Susp is enabled and auto-circle is off) |
| **Q / E** | Yaw the chassis left / right |
| **Shift + drag** | Lift / drop the look-at point |
| **ESC** | Quit |

The top bar's **Light** and **Ambient** sliders scale direct + IBL
specular and the flat ambient fill respectively.  **NMap** / **AO**
checkboxes disable the corresponding texture sample without unloading it.

## Asset Resolution

When loading a vehicle XML the viewer resolves each component in this order:

1. **`res_mods/<version>/`** — your modded copy (HD `_hd.dds` first, then SD)
2. **WoT `.pkg` archives** — extracted on demand via `PkgExtractor`,
   indexed by `TheItemList.xml` for O(1) lookup
3. **Placeholder** — solid neutral if everything failed

Per-tank thumbnails come from `thumb_nails/<xml_basename>.png` (988
bundled).  XML basenames that don't have an exact match fall back to a
progressive `_TOKEN` trim heuristic (e.g. `Ch19_121_IGR.xml` →
`Ch19_121.png`), which lifts coverage to 100 % across the
Tank-Exporter active-tank list.

## File Format Support

- **.primitives_processed** - WoT tank component mesh format
- **Vertex Formats Supported:**
  - `xyznuv` (position + normal + UV)
  - `BPVTxyznuv` (packed normals, BPVT mode)
  - `xyznuvtb` / `BPVTxyznuvtb` (real / packed normal + tangent + binormal)
  - `xyznuviiiwwtb` / `BPVTxyznuviiiwwtb` (with tangent/binormal + bone data)
  - **Dual-UV variants:** `xyznuvuv*`, `BPVTxyznuvuv*` (with optional `iiiww` and `tb`)
  - **Dynamic stride fallback** for unknown formats (walks the format string)
- **Sidecar sections:** `.vertices`, `.indices`, `.uv2` (lightmap / detail UV),
  `.colour` (per-vertex BGRA → RGBA float)
- **Index Formats:** 16-bit and 32-bit indices

## Architecture

See **`ARCHITECTURE.md`** for a per-module / per-class reference.  At a glance:

| Module | What lives there |
|--------|------------------|
| `tankExporterPy/loaders.py` | `MeshParser`, `VisualLoader`, `TextureLoader`, `PkgExtractor`, `VehicleXMLLoader` |
| `tankExporterPy/mesh.py` | `Mesh` (VAO + 4 material textures, idempotent `cleanup()`) |
| `tankExporterPy/scene.py` | `Camera`, `Grid`, `Axes`, `Sphere` (orbit-light indicator) |
| `tankExporterPy/shaders.py` | `ShaderProgram` (PBR), `UIShader`, `SimpleColorShader`, `SkyboxShader`, `IBLPrefilterShader` |
| `tankExporterPy/skybox.py` | `Skybox` — also bakes `irradiance_id` / `prefiltered_id` / `brdf_lut_id` for IBL |
| `tankExporterPy/ui.py` | `UIButton`, `UISlider`, `UICheckbox`, `UITreeView` / `UITreeNode`, `UIConfirmDialog`, `UIManager` |
| `tankExporterPy/viewer.py` | `Viewer` (event loop, scene lifecycle, tank-browser wiring) |
| `tankExporterPy/common.py` | bit-packed normal decoders, BWXML decoder, shader-source loader |
| `tankExporterPy/config.py` | persistent JSON config (`pkg_dir`, `res_mods`, `lookup_xml`) |
| `tankExporterPy/xloader.py` | text-format DirectX `.x` parser (skybox cube only) |

## Technical Details

### Binary Format

The `.primitives_processed` format contains:

1. **4-byte header** (ignored)
2. **Section data** (vertices, indices, UV2, colors)
3. **Section table** (metadata for each section)
4. **Section table offset** (4 bytes at EOF)

### Parsing Flow

```
Read section table offset from EOF-4
  ↓
Parse section table (sizes, names, offsets)
  ↓
Locate "vertices" and "indices" sections
  ↓
Read vertex format string (e.g., "xyznuviiiwwtb")
  ↓
Determine stride from format and BPVT flag
  ↓
Unpack vertices (position, normal, UV, tangent, bone data)
  ↓
Read indices (16 or 32-bit)
  ↓
Extract primitive group metadata
  ↓
Create OpenGL vertex arrays and upload to GPU
```

### Normal Map Handling

- Normals stored as either:
  - 3× float (if format string contains "xyznuv")
  - Packed UInt32 (bit-shifted 8:8:8 format)
- TBN matrix constructed from tangent + binormal + normal
- Normal map unpacked from [0,1] to [-1,1] in shader
- Applied in tangent space for per-pixel detail

### Camera System

Trackball orbit camera implementation:
- Yaw + pitch angles + distance from center
- Auto-computed projection based on viewport aspect ratio
- Mouse position delta → rotation angles
- Scroll wheel → distance adjustment

## Example Workflow

1. Export a tank component using Tank Exporter:
   ```
   Export → FBX → creates primitives_processed files
   ```

2. Copy textures alongside the mesh files:
   ```
   mesh_file.primitives_processed
   diffuse.png
   normal.png
   ```

3. Launch viewer:
   ```bash
   python tankExporterPy.py mesh_file.primitives_processed
   ```

4. Interact with 3D model:
   - Drag mouse to rotate
   - Scroll to zoom
   - Press W for wireframe
   - Press N to toggle normal mapping

## Troubleshooting

**"Module not found" errors:**
```bash
python -m pip install --upgrade pygame PyOpenGL pillow numpy
```

**Textures not loading:**
- Verify files are named exactly: `diffuse.png`, `normal.png`
- Place in same directory as .primitives_processed
- Check file permissions (must be readable)

**White/blank model:**
- Model may have very large coordinates
- Camera auto-fits but may need adjustment
- Try pressing R to reset camera

**Performance issues:**
- Large meshes (>1M triangles) may be slow
- Disable normal mapping (press N) to test vertex bottleneck
- Try wireframe mode (press W) to isolate rendering

## Implementation Notes

- **Single-pass rendering** — no deferred rendering or post-processing
- **PBR with split-sum IBL** — irradiance, prefiltered specular, and BRDF LUT
  are all baked from the skybox cubemap on startup via the `ibl_prefilter`
  shader (GL-3.3 port of the Khronos sample renderer).  Lighting equations
  follow WoT's `tank_fragment.glsl` (gloss-gated specular, NdotV × gloss IBL
  attenuation, ACES filmic tonemap).
- **No skeletal animation** — bone weights are read but not used
- **UV2 supported** — both interleaved (`xyznuvuv*` formats) and sidecar
  (`.uv2` parallel section) UV2 sources land on `mesh.uv1`; round-trips
  through the Blender FBX bridge as a second `UVMap2` layer
- **Vertex colour read-only** — sidecar `.colour` sections are parsed
  (BGRA → RGBA float) and surfaced in the Compare dialog; not yet
  round-tripped through Blender or written back to `.primitives_processed`
- **Per-load GPU cleanup** — every `load_mesh` / `load_vehicle` calls
  `Viewer._clear_scene()` first, freeing the previous tank's VBOs / EBOs /
  VAOs and all four per-mesh material textures, plus the loaded-thumbnail
  texture; no leaks across loads

## Future Enhancements

- Camo/customization texture pass (per-nation paint masks via `GMM.b`)
- Class icons in the tree (vclass is already saved in each leaf's payload:
  `lightTank` / `mediumTank` / `heavyTank` / `AT-SPG` / `SPG`)
- Skeletal animation (bone deformation)
- Shadow mapping
- Bloom / post-processing
- Save to FBX / glTF
- Texture cache shared across meshes (currently each mesh owns its copies)

## References

**Tank Exporter Source:**
- `ModTankLoader.vb:495-1587` - Binary format specification
- `ModTankLoader.vb:2225-2750` - Texture path extraction
- `Shaders/AtlasPBR_vertex.glsl` - TBN matrix construction
- `Shaders/AtlasPBR_fragment.glsl` - PBR principles

**Binary Format Reverse Engineering:**
- Format document: `VISUAL_PROCESSED_FORMAT.md`

---

**Created:** 2025-05-01  •  **Last updated:** 2026-05-04
See `CHANGELOG.md` for the full per-day log.
**Language:** Python 3.7+
**License:** Educational/Research Use
