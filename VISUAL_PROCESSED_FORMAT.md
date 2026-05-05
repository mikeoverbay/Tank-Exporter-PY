# `.primitives_processed` File Format Reverse Engineering

## Overview
The `.primitives_processed` file is a binary mesh/geometry container used in World of Tanks to store tank component models (hull, turret, gun, chassis, etc.). The format is parsed by `ModTankLoader.vb:build_primitive_data()`.

---

## File Structure

### High-Level Layout
```
[Header (4 bytes)]
[Sections Data...]
...
[Section Table]
[Section Table Offset (4 bytes at EOF)]
```

---

## Parsing Algorithm

### 1. **Locate Section Table**
```
Seek to: FileLength - 4
Read: section_table_start (Int32)
      — This is an OFFSET from EOF, not absolute position
Seek to: FileLength - 4 - section_table_start
```

### 2. **Skip Header**
```
Seek to: 0
Read: dummy (UInt32)  ← 4 bytes of "special tag characters" (ignored)
```

---

## Section Table Structure

The section table comes LAST in the file and describes all sections.

### Per Section Entry (Variable Length):
```
Offset          Type       Description
──────────────────────────────────────────────────────────────
+0              UInt32     Section size (bytes, excluding padding)
+4              UInt32[4]  16 bytes of unused junk (always read 4×UInt32)
+20             UInt32     Section name length (L)
+24             Char[L]    Section name (null-terminated ASCII)
+24+L           Char[pad]  Padding to 4-byte boundary: (4 - L mod 4) bytes if L mod 4 ≠ 0
```

### Location Tracking:
```vb
location = 4  ' Start after header
For each section:
    section_locations(i) = location
    section_sizes(i) = ReadUInt32()
    location += section_sizes(i)
    location += location mod 4  ' Account for alignment padding
```

---

## Section Types

Sections are identified by name suffix in the section table.  Single-group
files (Hull) use the bare name without a `<group>.` prefix, so the matcher
needs to handle both `<group>.vertices` and bare `vertices` as the same kind.

| Suffix                                                                     | Kind          | Purpose                                       |
|----------------------------------------------------------------------------|---------------|-----------------------------------------------|
| `.vertices`                                                                | vertex data   | position, normal, UVs, bone idx/weight, tb    |
| `.indices`                                                                 | index data    | triangle indices + primitive-group metadata   |
| `.uv2` (also seen as `.uvs2` / `.uv1` / `.uvs1` / `.uvb` / `.uvsb` / `.uv` / `.uvs` historically) | sidecar UV2 | second UV channel — lightmap or detail        |
| `.colour` (also `.colours` / `.color` / `.colors`)                         | sidecar RGBA  | per-vertex BGRA `uint8` colour                |

The sidecar UV2 / colour sections are PARALLEL to a primitive group's
`.vertices` section — same vertex count, same ordering — so they do **not**
extend the per-vertex stride.  A primitive group either ships a 2nd UV
inside its vertex stream (`xyznuvuv*` formats below) **or** in a sidecar
section, never both.

---

## Vertices Section Format

### Header (64 bytes):
```
Offset    Type      Description
────────────────────────────────────────
+0–63     Char[64]  Format string (e.g., "xyznuv", "BPVTxyznuviiiwwtb", etc.)
```

### Format Strings & Strides:

`realNormals` is true ONLY for the exact string `"xyznuv"`; every other
format uses the 4-byte packed normal.  `BPVT_mode` is true whenever the
format string contains the substring `"BPVT"` and bumps the section
preamble from 64 to 132 bytes.

#### Single-UV formats

| Format String          | Stride | realNormals | BPVT | Notes                                |
|------------------------|--------|-------------|------|--------------------------------------|
| `"xyznuv"`             | 32     | ✓           | ✗    | Position + real normal + UV          |
| `"BPVTxyznuv"`         | 24     | ✗           | ✓    | BPVT packed normal                   |
| `"BPVTxyznuviiiww"`    | 32     | ✗           | ✓    | + bone indices / weights             |
| `"BPVTxyznuviiiwwtb"`  | 40     | ✗           | ✓    | + tangent / binormal pair            |
| `"xyznuvtb"`           | 40     | ✓           | ✗    | Real normals + tangent / binormal    |
| `"BPVTxyznuvtb"`       | 32     | ✗           | ✓    | BPVT + tangent / binormal            |
| `"xyznuviiiwwtb"`      | 48     | ✗           | ✗    | Real normals + bones + tangents      |

#### Dual-UV formats (UV1 interleaved AFTER UV0)

These layouts pack a second UV channel directly into the per-vertex
stream — no sidecar `.uv2` section needed.  Detect via
`format_str.count("uv") >= 2`.  All assume packed normals (4 bytes); WoT
does not ship a real-normal + UV2 combination in any tank we've seen.

| Format String              | Stride | BPVT | Notes                                  |
|----------------------------|--------|------|----------------------------------------|
| `"xyznuvuv"`               | 32     | ✗    | + 2nd UV pair (8 bytes) right after UV0 |
| `"BPVTxyznuvuv"`           | 32     | ✓    |                                         |
| `"xyznuvuvtb"`             | 40     | ✗    | + tangent / binormal                    |
| `"BPVTxyznuvuvtb"`         | 40     | ✓    |                                         |
| `"xyznuvuviiiww"`          | 40     | ✗    | + bone idx / weight                     |
| `"BPVTxyznuvuviiiww"`      | 40     | ✓    |                                         |
| `"xyznuvuviiiwwtb"`        | 48     | ✗    | full skinned + UV2 + tb                 |
| `"BPVTxyznuvuviiiwwtb"`    | 48     | ✓    |                                         |

#### Dynamic stride for unknown formats

Anything not in the static table is parsed by walking the format string
left-to-right with these byte sizes per token:

| Token | Bytes                              |
|-------|------------------------------------|
| `xyz` | 12 (3 × `float32`)                  |
| `n`   | 12 if real-normal mode, else 4      |
| `uv`  | 8 each (per occurrence)             |
| `iii` | 4 (4 × `uint8` bone indices)        |
| `ww`  | 4 (4 × `uint8` bone weights)        |
| `tb`  | 8 each (4-byte tangent + 4-byte binormal) |

This keeps unknown variants parseable instead of falling back to a
hardcoded 32-byte stride that misaligns the entire stream.

### BPVT Offset:
If `BPVT_mode = true`:
```vb
stream.Position = header_start + 132  ' Skip 64-byte name + 68 padding
```

### Vertex Count:
```
After format string (or after BPVT skip):
Read: nVertices_ (UInt32)
```

### Per-Vertex Data Structure:

#### Base (all formats):
```
Offset    Type      Description
────────────────────────────────────
+0        Single    x (position)
+4        Single    y (position)
+8        Single    z (position)
+12       Varies    Normal (see below)
+16       Single    u (UV 0)
+20       Single    v (UV 0)
```

#### If `realNormals = true` (stride 32, 37, 40):
```
+12       Single    nx
+16       Single    ny
+20       Single    nz
+24       Single    u
+28       Single    v
```

#### If `realNormals = false` (stride 24):
```
+12       UInt32    n  (packed normal — unpack with unpackNormal() or unpackNormal_8_8_8())
+16       Single    u
+20       Single    v
```

#### Interleaved UV1 (only for `xyznuvuv*` formats)

When the format string contains a second `uv` token, the UV1 pair is
read **immediately after UV0** — before any bone or tangent data:

```
+24 (real normals path)  Single  u1
+28                       Single  v1

+16 (packed normals path) Single  u1   ← actually offset depends on n size
+20                       Single  v1
```

Concrete example for `BPVTxyznuvuv`:
```
+0..11   xyz   (3 floats)
+12..15  n     (1 packed UInt32)
+16..23  uv0   (2 floats)
+24..31  uv1   (2 floats)        ← stride 32
```

Concrete example for `BPVTxyznuvuvtb`:
```
+0..11   xyz
+12..15  n
+16..23  uv0
+24..31  uv1
+32..39  tb     (1 packed tangent + 1 packed binormal)        ← stride 40
```

#### If stride ≥ 37 (bone data present):
```
+24 (or +28)    Byte    index_1   (bone index 0)
+25 (or +29)    Byte    index_2   (bone index 1)
+26 (or +30)    Byte    index_3   (bone index 2)
+27 (or +31)    Byte    index_4   (bone index 3)
+28 (or +32)    Byte    weight_1  (bone weight 0)
+29 (or +33)    Byte    weight_2  (bone weight 1)
+30 (or +34)    Byte    weight_3  (bone weight 2)
+31 (or +35)    Byte    weight_4  (bone weight 3)
```

#### If stride ≥ 37 and not "BPVTxyznuviiiww" (tangent data):
```
+32 (or +36)    UInt32    t  (packed tangent — unpack with unpackNormal())
+36 (or +40)    UInt32    bn (packed binormal — unpack with unpackNormal())
```

---

## Indices Section Format

### Header (64 bytes):
```
Offset    Type      Description
────────────────────────────────
+0–63     Char[64]  Format string (e.g., "list16", "list32")
```

### Index Header:
```
Offset    Type      Description
────────────────────────────────
+64       UInt32    nIndices_ (total index count)
+68       UInt16    nInd_groups (number of primitive groups)
```

### Index Data:
```
Offset    Type      Description
────────────────────────────────
+72       [varies]  Indices (16-bit or 32-bit depending on format string)
          UInt16    if "list16"
          UInt32    if "list32"
```

### Primitive Groups (after indices):
Located at offset: `+72 + (nIndices_ × stride_in_bytes)`

Per group:
```
Offset    Type      Description
────────────────────────────────
+0        UInt32    startIndex_    (offset into index array)
+4        UInt32    nPrimitives_   (triangle count)
+8        UInt32    startVertex_   (offset into vertex array)
+12       UInt32    nVertices_     (vertex count for this group)
```

**Example:** For 100 triangles (300 indices) in 16-bit format:
```
Offset of primitive groups = 72 + (300 × 2) = 672
```

---

## UV2 Sidecar Section Format (Optional)

A primitive group named `tank_hull_01` may ship a parallel section called
`tank_hull_01.uv2` carrying the second UV channel.  Vertex count and
ordering match the matching `.vertices` section exactly — this is a
parallel buffer, not a per-vertex stride extension.

### Preamble — TWO layouts

Which preamble appears depends on whether the matching vertex section
was BPVT.  In practice we **probe** both candidate body offsets and
pick whichever produces a body whose `(size − offset) / 8` equals the
expected vertex count, because the in-section count field is unreliable
(see "Quirks" below).

#### Non-BPVT layout (68-byte preamble)
```
Offset    Type      Description
────────────────────────────────
+0..63    Char[64]  Format string (often "uv2", but the contents are
                    not load-bearing -- we don't depend on the text)
+64..67   UInt32    uv2_count   (NOT TRUSTED -- see Quirks)
+68..end  Single[]  Pairs (u, v), 8 bytes each
```

#### BPVT layout (136-byte preamble) — used when the matching
`.vertices` section's format string starts with `BPVT`

Mirrors the matching `.vertices` section's preamble (primary +
secondary format string + uint32 count) exactly:

```
Offset    Type      Description
────────────────────────────────
+0..67    Char[68]  Primary format string ('BPVSuv2', 64 bytes
                    of string + 4 bytes of zero pad)
+68..131  Char[64]  Secondary format string ('set3/uv2pc')
+132..135 UInt32    uv2_count   (NOT TRUSTED)
+136..end Single[]  Pairs (u, v), 8 bytes each
```

> **Off-by-4 trap.**  An older draft of this doc described the BPVT
> preamble as 132 bytes (count at offset 128, body at 132).  That was
> wrong: live tank data has count at 132 and body at 136.  Probing
> 132 first looked safe because integer truncation
> `(section_size - 132) // 8` rounded `1638.5` down to `1638` and
> matched `expected_count` by coincidence -- but it shifted the body
> read by 4 bytes, so `uv1[0]` came back as the count uint32
> reinterpreted as a float (`2.295e-42`, a denormal).  The loader's
> probe list was changed to `(136, 68)` and the divisibility check
> tightened to require `(size - offset) % 8 == 0` to prevent the
> false positive from coming back.  See CHANGELOG 2026-05-05.

### Per-entry payload (8 bytes)
```
+0        Single    u (second UV coordinate)
+4        Single    v (second UV coordinate)
```

### V-flip
The viewer applies the same `v ← 1 − v` flip to UV1 as to UV0 so the
second-UV-bound textures sample with the same orientation convention as
the diffuse.

### Quirks (observed on live tanks)

* **Header count is sometimes 0** — at least one A38_T110E4-class tank
  ships its `track_L_Shape.uv2` section with the `count` field zeroed
  while the body still carries the correct number of (u, v) pairs.
  Don't trust the count; derive it from `(section_size − body_offset)
  / 8` and verify it equals the matching `.vertices` count.
* **Probe order matters** — try the BPVT 132-byte preamble first when
  the matching vertex format string contains `"BPVT"`, otherwise try
  68 first.  Reversing the order risks picking the wrong layout when
  one happens to also produce a clean integer count.

---

## Vertex Colour Sidecar Section Format (Optional)

A primitive group may ship a parallel `<group>.colour` section carrying
per-vertex BGRA `uint8` data.  The preamble follows the same two-layout
pattern as the UV2 section (probe both 68-byte and 132-byte preambles).

### Preamble — same probe rules as `.uv2`

```
Offset    Type      Description
────────────────────────────────
+0..63    Char[64]  Format string
+(probe)  UInt32    colour_count   (NOT TRUSTED -- derive from size)
+(body)   Byte[4]×N BGRA per vertex
```

### Per-entry payload (4 bytes — BGRA, not RGBA)

```
+0        Byte      B (0–255)
+1        Byte      G (0–255)
+2        Byte      R (0–255)
+3        Byte      A (0–255)
```

### BGRA → RGBA conversion

BigWorld stores the channels in BGRA byte order.  The loader swizzles
to RGBA float [0, 1] on the way in so downstream code (shaders,
compositors) sees the standard layout:

```
rgba.r = bgra[2] / 255.0
rgba.g = bgra[1] / 255.0
rgba.b = bgra[0] / 255.0
rgba.a = bgra[3] / 255.0
```

### Status

Vertex colour data is **read-only** in the current implementation:
parsed and surfaced in the Compare dialog (`Col` column), but not yet
round-tripped through the FBX / glTF / OBJ Blender bridge and not yet
written back to `.primitives_processed`.

---

## Normal Packing

### Standard Unpacking (`unpackNormal`):
Converts a 32-bit packed value back to a 3D vector.
```vb
' Pseudo-code
Dim x As Single = ((n & 0x000000FF) / 255.0) × 2 - 1
Dim y As Single = (((n >> 8) & 0x000000FF) / 255.0) × 2 - 1
Dim z As Single = sqrt(1 - x² - y²)
```

### BPVT Unpacking (`unpackNormal_8_8_8`):
Different scale/offset for BPVT format.

### Special Case:
If mesh name contains `"!"` marker:
```vb
nz = -nz  ' Flip Z component
```

---

## Winding Order

**Hull/Turret/Primitives:**
```vb
If filename contains "hull" Or "turret" Or PRIMITIVES_MODE Then
    v1_final = p2
    v2_final = p1
    v3_final = p3
Else
    v1_final = p1
    v2_final = p2
    v3_final = p3
End If
```

This is then offset:
```vb
indices(i) = (final_value - startVertex_)
```

---

## Index Scale Detection

```vb
If format_string contains "list32" Then
    ind_scale = 4  ' UInt32 indices
Else
    ind_scale = 2  ' UInt16 indices (default)
End If
```

---

## File Size Estimation

```
Total ≈ 4 (header)
      + SUM(section_size + (4 - section_size mod 4))  [for each data section]
      + [section_table entries] × [entry_size]
      + 4 (section_table_offset at EOF)
```

---

## Example: Reading a Single Mesh

```vb
' 1. Read section table
seek(FileLength - 4)
section_table_offset = read_int32()
seek(FileLength - 4 - section_table_offset)

' 2. Parse sections
For each section_name in section_table:
    If section_name contains "vertices":
        seek(section_location)
        format_string = read_char[64]
        vertex_count = read_uint32()
        For i = 0 to vertex_count - 1:
            x = read_single()
            y = read_single()
            z = read_single()
            [normal data based on format]
            u = read_single()
            v = read_single()
            [optional: bone/tangent data]

' 3. Read indices
If section_name contains "indices":
    seek(section_location)
    format_string = read_char[64]
    index_count = read_uint32()
    group_count = read_uint16()
    For i = 0 to index_count - 1:
        idx = read_uint16() or read_uint32()  [based on format]
    
    ' Primitive group metadata
    For i = 0 to group_count - 1:
        start_index = read_uint32()
        prim_count = read_uint32()
        start_vertex = read_uint32()
        vert_count = read_uint32()
```

---

## Key Implementation Notes

1. **Alignment:** All sections are padded to 4-byte boundaries
2. **Byte Order:** Little-endian (standard Intel x86)
3. **Encoding:** ASCII strings (section names)
4. **Dynamic:** Format varies per mesh (auto-detected via header string)
5. **Multi-Part:** Single file can contain multiple meshes (sub_groups loop)
6. **Offset Arithmetic:** All indices into vertices are pre-adjusted by `startVertex_`

---

## References in Code

### Original VB.NET (Tank Exporter)

- **Load entry point:** `ModTankLoader.vb:build_primitive_data()` (line 495)
- **Section table parsing:** Lines 652–700
- **Vertex parsing:** Lines 914–1175
- **Index parsing:** Lines 855–911
- **Primitive group metadata:** Lines 894–911

### Current Python rewrite (Tank Exporter PY)

- **Load entry point:** `tankviewer/loaders.py :: MeshParser.parse_primitives_processed()`
- **Section grouping & sidecar discovery:** the inner `_strip_known_suffix`
  helper claims `.vertices` / `.indices` / `.uv2` (with spelling
  variants) / `.colour` (with spelling variants).
- **Vertex parsing + UV2-interleaved support:** `MeshParser._parse_vertices()`
  (static stride map + `_compute_stride()` dynamic fallback).
- **Sidecar UV2 parsing:** `MeshParser._parse_uv2_section()` — probes
  68- and 132-byte preambles, derives count from body size (header
  count is unreliable on live data).
- **Sidecar colour parsing:** `MeshParser._parse_colour_section()` —
  same probe logic, BGRA `uint8` → RGBA float [0, 1] swizzle.
- **Index parsing:** `MeshParser._parse_indices()`.

### Per-file diagnostic logging

Every primitive group prints a flag line of the form
`[+v +i +u2 -c] tank_hull_01` in the load log so a glance at the
console reveals which sidecar sections each group has.  At end-of-file
the parser emits a `UV2 SUMMARY:` and (when applicable) a
`COLOUR SUMMARY:` rollup plus a `Distinct vertex formats in this file:`
list.  These are read-time-only — they have no effect on parsed data.

---

## Official Format Definitions

WoT ships authoritative element-type descriptions for every vertex
layout inside `res/packages/shaders.pkg` under
`shaders/formats/`.  Each is a tiny BWXML file describing the
element list, type tags, and stream / semanticIndex assignments.
Decoding a few key ones gives us the canonical truth about how to
read each token in a format string.

### Stream assignments (parallel buffers)

WoT uses multi-stream vertex buffers.  Streams that show up in tank
data (via the same section table parsed above):

| Stream | Section name in `.primitives_processed` | Format file                  |
|--------|------------------------------------------|------------------------------|
| 0      | `<group>.vertices`                       | one of the `set1/*` / `set3/*` per-vertex layouts |
| 10     | `<group>.uv2`                            | `shaders/formats/uv2.xml`     |
| 11     | `<group>.colour` (semanticIndex 0)       | `shaders/formats/colour.xml`  |
| 11     | `<group>.colour2` (semanticIndex 1)      | `shaders/formats/colour2.xml` |

### Element types (from the format XMLs)

| Type tag                              | Bytes | Used for                           |
|---------------------------------------|-------|------------------------------------|
| `POSITION` (implicit `FLOAT3`)        | 12    | `xyz`                              |
| `UBYTE4_NORMAL_8_8_8`                 | 4     | packed `n`, `t`, `b`               |
| `FLOAT3` real normal                  | 12    | `n` for the legacy `xyznuv` exact-match format only |
| `FLOAT2`                              | 8     | UV in `set3/*` and in non-BPVT live data |
| `SHORT2`                              | 4     | UV in `set1/*` (live tanks haven't shipped this; documented for completeness) |
| `COLOR`                               | 4     | BGRA `uint8` (vertex colour streams) |
| `SC_UBYTE4_MULT3_REVERSE_PADDED_IIIP` | 4     | `iii` skinned bone indices (3 indices + 1 pad, byte-reversed, scaled ×3 for matrix offset) |
| `SC_UBYTE4_REVERSE_PADDED_PWWP`       | 4     | `ww` skinned bone weights (1 pad + 2 weights + 1 pad, byte-reversed) |
| `FLOAT1`                              | 4     | `i` -- single bone index (used by tracks / fauna) |
| `SC_FLOAT1_MULT3_I`                   | 4     | `i` -- single bone index, ×3 scaled |

### Live-data caveat -- `BPVT` prefix vs `set3/` prefix

Real tank `.primitives_processed` files store format strings starting
with `BPVT` (e.g. `BPVTxyznuviiiwwtb`).  The shaders.pkg names are
prefixed with `set3/` instead (e.g. `set3/xyznuviiiwwtbpc`).  The two
naming conventions describe the **same** underlying byte layout in
practice -- both use `UBYTE4_NORMAL_8_8_8` for normals and `FLOAT2`
for UVs.  We currently key off the `BPVT` substring because that's
what shows up in actual tank meshes; the `set3/` form may surface in
non-tank assets we haven't audited.

### Sidecar UV2 -- size depends on the SET

`shaders/formats/set1/uv2pc.xml` defines the UV2 sidecar as
`SHORT2` (4 bytes per vertex), while
`shaders/formats/set3/uv2pc.xml` defines it as `FLOAT2` (8 bytes per
vertex).  Every live tank we've inspected uses the set-3 / FLOAT2
form, so the sidecar parser only probes 8-byte entries today.  If
a SHORT2 set ever turns up in the wild, the probe would need a
second pass with `entry_size = 4` and a `int16` → float
de-quantisation step.

### Bone data quirks per the official XML

The skinned `iii` / `ww` tokens carry **byte-reversed, padded** data
according to the official type tags
(`SC_UBYTE4_MULT3_REVERSE_PADDED_IIIP` /
`SC_UBYTE4_REVERSE_PADDED_PWWP`).  Three indices + one pad byte for
indices; one pad + two weights + one pad for weights.  The current
viewer reads them as plain `4 × uint8` blocks because all we do
with them today is round-trip through Blender's `WoTBoneIdx` /
`WoTBoneWeight` color attributes.  The reverse-padding will need to
be respected if we ever drive a Blender skin cluster from this data.

---

## Writing the format

The encoder lives at `tankviewer/writers/_primitive_encoder.py`; the
public entry point is `encode_file(meshes, want_uv2=True)` and the
public wrapper that drops it on disk atomically is
`tankviewer.writers.write_primitives()`.

### Two layouts the encoder must support

The encoder picks one of two layouts per file based on `mesh.name`
(the section base name preserved by the loader from the source file's
section table).  These are the same two layouts the parser groups
sections into, just viewed from the writer side:

| Layout              | Detected by                  | Section names emitted                            | Visual reference style |
| ------------------- | ---------------------------- | ------------------------------------------------ | ---------------------- |
| **bare-shared**     | every `mesh.name == ''`      | one global `indices` + `vertices` (+ `uv2`)      | by index               |
| **named-per-mesh**  | every `mesh.name != ''`      | `<base>.indices` + `<base>.vertices` (+ `<base>.uv2`) per mesh | by name |

Mixed input (some named, some empty) raises `ValueError`.

In bare-shared layout the `indices` section header reports `N`
primitive groups + `N` per-group metadata entries (one per mesh, with
running `start_index` / `start_vertex` offsets).  Indices are
re-based into a single shared vertex buffer.

In named-per-mesh layout each section's `group_count = 1` and the
single primitive-group entry covers the whole local buffer
(`start_index = 0`, `start_vertex = 0`).  No re-basing across meshes.

### What never to invent on the writer side

`mesh.identifier` (the WoT *visual-side* material label like
`tank_chassis_01_skinned`) is **not** a section name.  Substituting it
when `mesh.name` is empty would make the engine look for sections that
don't exist anywhere in the file.  If `mesh.name` is empty, write the
bare-shared layout; if it's populated, use it verbatim.

### Section bodies that the engine ignores

Real pkg `.primitives_processed` files have non-zero data in bytes
8..63 of each section (after the 4-byte format-string head): pointer-
shaped values (`0x74EA9A87`, `0xD6EA50D0`), sentinels
(`0xFFFFFFFFFFFFFFFE` = `-2` int64), small counts (`0x07FE` = 2046).
This is metadata BigWorld writes during compilation -- runtime
pointers / cache state / build counters captured at the moment the
file was last serialised.  The engine does **not** validate this
region at load time; our writer leaves it zero-filled and the game
loads our output cleanly.  No need to chase these bytes for
round-trip fidelity.

### What the writer doesn't yet handle

* `.colour` / `.colour2` sidecar write-back (parsed read-only today).
* The visual-rewrite pass that splices new `<PG_ID>` blocks into the
  matching `.visual_processed` when the user has *added* meshes.
  Will land alongside "add new mesh to a tank".
* `BSP2` collision data -- old format, not used by live tanks.

---

## See Also

* `CHANGELOG.md` — per-day work log for the Python rewrite (UV2
  sidecar support landed 2026-05-04).
* `ARCHITECTURE.md` — per-module / per-class reference.
* `README_TANK_VIEWER.md` — user-facing feature list and controls.
* `shaders/formats/` inside `res/packages/shaders.pkg` — authoritative
  WoT format definitions (BWXML; decode with `tankviewer.common.decode_bwxml`).
