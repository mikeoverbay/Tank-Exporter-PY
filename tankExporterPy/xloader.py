"""
Loader for text-format DirectX .x mesh files.

Only the text (ASCII) variant of the format is supported -- the format tag in
the header must start with ``xof 0302txt`` or ``xof 0303txt``.

The parser extracts the first ``Mesh`` block it finds and returns a dict
compatible with the rest of the tankExporterPy pipeline:

    {
        'positions' : np.ndarray  shape (N, 3)  float32
        'normals'   : np.ndarray  shape (N, 3)  float32   (or None)
        'uv0'       : np.ndarray  shape (N, 2)  float32   (or None)
        'indices'   : np.ndarray  shape (M,)    uint32
        'materials' : list[str]   texture filename(s) referenced by the mesh
    }

Vertices, normals, and UVs are stored per-vertex (indexed the same way), so
the data can be fed directly into a VAO without further expansion.

Public API
----------
    load_x(filepath)  ->  dict   (raises ValueError on parse failure)
"""

import re
import numpy as np


# ---------------------------------------------------------------------------
# Tokeniser helpers
# ---------------------------------------------------------------------------

def _strip_comments(text):
    """Remove // line comments and C-style /* */ block comments."""
    text = re.sub(r'//[^\n]*', '', text)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    return text


def _numbers(text):
    """Return all numeric tokens from *text* as a flat list of strings."""
    return re.findall(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', text)


# ---------------------------------------------------------------------------
# Block extractor
# ---------------------------------------------------------------------------

def _find_block(text, keyword, start=0):
    """Find the outer braces of the first *keyword* block at or after *start*.

    Returns ``(inner_text, end_pos)`` where *inner_text* is the content
    between the outermost ``{`` and ``}`` and *end_pos* is the index just
    past the closing ``}``.

    Raises ``ValueError`` if the block is not found or braces are unbalanced.
    """
    # Find the keyword
    pat = re.compile(r'\b' + re.escape(keyword) + r'\b')
    m = pat.search(text, start)
    if m is None:
        raise ValueError(f"Block '{keyword}' not found")

    # Advance to opening brace
    brace = text.find('{', m.end())
    if brace == -1:
        raise ValueError(f"No opening brace for block '{keyword}'")

    depth = 0
    i = brace
    while i < len(text):
        c = text[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return text[brace + 1:i], i + 1
        i += 1

    raise ValueError(f"Unbalanced braces in block '{keyword}'")


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------

def _parse_vertex_list(text):
    """Parse ``count; x;y;z;, ... x;y;z;;`` into an (N,3) float32 array."""
    nums = _numbers(text)
    if not nums:
        raise ValueError("Empty vertex list")
    count = int(nums[0])
    coords = [float(v) for v in nums[1:1 + count * 3]]
    if len(coords) < count * 3:
        raise ValueError(f"Expected {count*3} floats for {count} vertices, got {len(coords)}")
    return np.array(coords, dtype=np.float32).reshape(count, 3)


def _parse_face_list(text):
    """Parse ``count; n;i,i,i;, ... ;; `` into a flat uint32 index array.

    Only triangles (n==3) and quads (n==4, split into two triangles)
    are handled.  Quads are split as (0,1,2) and (0,2,3).
    """
    nums = _numbers(text)
    if not nums:
        raise ValueError("Empty face list")

    face_count = int(nums[0])
    idx = 1
    triangles = []

    for _ in range(face_count):
        n = int(nums[idx]);  idx += 1
        verts = [int(nums[idx + k]) for k in range(n)];  idx += n

        if n == 3:
            triangles.extend(verts)
        elif n == 4:
            triangles.extend([verts[0], verts[1], verts[2],
                               verts[0], verts[2], verts[3]])
        else:
            raise ValueError(f"Unsupported polygon with {n} vertices")

    return np.array(triangles, dtype=np.uint32)


def _parse_uv_list(text):
    """Parse ``MeshTextureCoords`` inner text into an (N,2) float32 array."""
    nums = _numbers(text)
    count = int(nums[0])
    vals = [float(v) for v in nums[1:1 + count * 2]]
    return np.array(vals, dtype=np.float32).reshape(count, 2)


def _parse_normal_section(text):
    """Parse ``MeshNormals`` inner text.

    Returns an (N,3) float32 array of per-vertex normals re-indexed so they
    align with the positions array (using the normal-face index list).
    The caller already holds the position index list; normals get their own
    face list and may be in a different order.
    """
    # Split at the two numeric blocks: normal vectors, then face list
    # Strategy: parse first count, collect that many vec3s, then ignore face list
    # (for rendering we expand normals per-position-vertex below)
    nums = _numbers(text)
    ncount = int(nums[0])
    nvals  = [float(v) for v in nums[1:1 + ncount * 3]]
    normals_raw = np.array(nvals, dtype=np.float32).reshape(ncount, 3)

    # Normal face list starts after ncount*3 + 1 numbers
    offset = 1 + ncount * 3
    fcount = int(nums[offset]);  offset += 1
    normal_indices = []
    for _ in range(fcount):
        n = int(nums[offset]);  offset += 1
        normal_indices.extend(int(nums[offset + k]) for k in range(n))
        offset += n

    # Build per-position-vertex normals array using the normal index list
    # (same length as the flattened triangle index list)
    return normals_raw, np.array(normal_indices, dtype=np.uint32)


def _parse_material_list(block_inner, defined_materials):
    """Return texture filename(s) used by this mesh."""
    # Find material reference names:  { MaterialName }
    refs = re.findall(r'\{\s*(\w+)\s*\}', block_inner)
    textures = []
    for ref in refs:
        if ref in defined_materials:
            tex = defined_materials[ref]
            if tex:
                textures.append(tex)
    return textures


# ---------------------------------------------------------------------------
# Top-level material scanner
# ---------------------------------------------------------------------------

def _scan_materials(text):
    """Return {name: texture_filename} for every top-level Material block."""
    materials = {}
    pat = re.compile(r'\bMaterial\s+(\w+)\s*\{')
    for m in pat.finditer(text):
        name = m.group(1)
        brace = m.end() - 1   # points at '{'
        depth = 0
        i = brace
        inner_start = brace + 1   # character after opening '{'
        while i < len(text):
            c = text[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        inner = text[inner_start:i]   # preserves all chars including nested braces

        # TextureFileName { "filename"; }
        tf = re.search(r'TextureFileName\s*\{\s*"([^"]+)"', inner)
        materials[name] = tf.group(1) if tf else None

    return materials


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_x(filepath):
    """Parse a text-format DirectX .x file and return mesh data.

    The first ``Mesh`` block found in the file is returned.  Vertices,
    normals, and UVs are re-indexed to align with the position index list so
    the arrays can be uploaded directly as interleaved or separate VBOs.

    Args:
        filepath (str): path to the .x file

    Returns:
        dict with keys:
            ``positions`` (N,3 float32),
            ``normals``   (N,3 float32 or None),
            ``uv0``       (N,2 float32 or None),
            ``indices``   (M,  uint32),
            ``materials`` (list[str]  texture filenames)

    Raises:
        ValueError: if the file is not a text-format .x file or is malformed
        FileNotFoundError: if the file does not exist
    """
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as fh:
        raw = fh.read()

    # Validate header
    header = raw[:16].lower()
    if not header.startswith('xof') or 'txt' not in header:
        raise ValueError(f"Not a text-format .x file: {filepath!r}")

    text = _strip_comments(raw)

    # --- Top-level materials -------------------------------------------------
    defined_materials = _scan_materials(text)

    # --- Mesh block ----------------------------------------------------------
    mesh_inner, _ = _find_block(text, 'Mesh')

    # Positions -- everything up to the first nested block
    # The positions section is at the very top of the Mesh block
    first_brace = mesh_inner.find('{')
    pos_section = mesh_inner[:first_brace] if first_brace != -1 else mesh_inner
    positions = _parse_vertex_list(pos_section)
    N = len(positions)

    # Faces -- second numeric run inside Mesh (after positions)
    # Find where the face count number starts (right after positions data ends)
    # Strategy: consume the position count + N*3 numbers, rest is faces up to
    # the first nested block
    all_top_nums = _numbers(pos_section)  # already used for positions
    # The face list text is between the last position number and the first '{'
    # Use the raw block up to first_brace and skip past position floats
    pos_num_count = 1 + N * 3              # count + N x (x y z)
    face_section_start = 0
    found = 0
    # Scan character-by-character to find the start of the face block
    num_pat = re.compile(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?')
    pos_iter = num_pat.finditer(pos_section)
    last_m = None
    for _ in range(pos_num_count):
        last_m = next(pos_iter, None)
    face_text = pos_section[last_m.end():] if last_m else ''
    indices = _parse_face_list(face_text)

    # --- Sub-blocks (UVs, normals, material list) ----------------------------
    normals_aligned  = None
    uv0_aligned      = None
    material_textures = []

    # UVs
    try:
        uv_inner, _ = _find_block(mesh_inner, 'MeshTextureCoords')
        uv_raw = _parse_uv_list(uv_inner)     # one UV per position vertex
        if len(uv_raw) == N:
            uv0_aligned = uv_raw
        else:
            print(f"[xloader] UV count {len(uv_raw)} != vertex count {N}; UVs ignored")
    except ValueError:
        pass

    # Normals
    try:
        nrm_inner, _ = _find_block(mesh_inner, 'MeshNormals')
        normals_raw, nrm_indices = _parse_normal_section(nrm_inner)

        # Re-index normals to match position vertices
        # nrm_indices is the flat triangle list for normals
        # indices is the flat triangle list for positions
        # If they have the same length we can build a per-position normal map
        aligned = np.zeros((N, 3), dtype=np.float32)
        counts  = np.zeros(N, dtype=np.int32)
        if len(nrm_indices) == len(indices):
            for pos_vi, nrm_vi in zip(indices, nrm_indices):
                aligned[pos_vi] += normals_raw[nrm_vi]
                counts[pos_vi]  += 1
            mask = counts > 0
            aligned[mask] /= counts[mask, None]
            # Renormalise
            lengths = np.linalg.norm(aligned, axis=1, keepdims=True)
            lengths[lengths < 1e-6] = 1.0
            normals_aligned = (aligned / lengths).astype(np.float32)
        else:
            print("[xloader] Normal/position index count mismatch; normals ignored")
    except ValueError:
        pass

    # Material list
    try:
        mat_inner, _ = _find_block(mesh_inner, 'MeshMaterialList')
        material_textures = _parse_material_list(mat_inner, defined_materials)
    except ValueError:
        pass

    print(f"[xloader] {filepath}")
    print(f"  vertices={N}  triangles={len(indices)//3}"
          f"  normals={'yes' if normals_aligned is not None else 'no'}"
          f"  uvs={'yes' if uv0_aligned is not None else 'no'}"
          f"  materials={material_textures}")

    return {
        'positions' : positions,
        'normals'   : normals_aligned,
        'uv0'       : uv0_aligned,
        'indices'   : indices,
        'materials' : material_textures,
    }
