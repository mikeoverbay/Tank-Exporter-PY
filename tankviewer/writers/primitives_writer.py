"""
Write a list of Mesh objects back out as a WoT .primitives_processed
binary file.

Inverse of `MeshParser.parse_primitives_processed()`; see
VISUAL_PROCESSED_FORMAT.md for the on-disk byte spec and
`_primitive_encoder.py` for the actual byte-level pack code.  This
module is the public-API thin wrapper -- it just calls into the
encoder and writes the result to disk atomically (write to a .tmp
sibling, then os.replace) so a half-written file never lands at the
target path.
"""

import os
from dataclasses import dataclass

from . import _primitive_encoder


@dataclass
class PrimitivesWriteOptions:
    """Knobs for write_primitives.  Every flag has a sensible default
    so callers can pass an empty options() and get a useful round-trip.

    Attributes:
        include_uv2     : when True (default) AND any mesh carries
                          mesh.uv1, emit a sidecar `.uv2` section so
                          the second UV channel survives the round
                          trip.
        include_colour  : when True AND any mesh carries mesh.colour,
                          emit a sidecar `.colour` section.  Currently
                          ignored by the encoder (no live tank uses
                          this in our test set; will land alongside
                          a tank that needs it).
        force_bpvt      : force the BPVT preamble path on the vertex
                          section even when the source format didn't
                          carry it.  Defaults to True since modern
                          live tanks all use BPVT.  No-op for now --
                          the encoder always writes BPVT-style
                          vertices since that's what live tanks ship.
        list32          : True -> emit 'list32' indices (uint32);
                          False -> 'list16' (uint16); None (default)
                          = pick automatically based on vertex count.
    """
    include_uv2:    bool = True
    include_colour: bool = True
    force_bpvt:     bool = True
    list32:         object = None    # None | True | False


def write_primitives(meshes, output_path, options=None):
    """Serialise `meshes` to one .primitives_processed file.

    Args:
        meshes (list[Mesh]): meshes to write.  All meshes destined for
            one component-level file must be passed in a single call.
            Caller is responsible for grouping (hull meshes go into
            Hull.primitives_processed, etc.).
        output_path (str): absolute path to the output file.
            Conventionally ends in '.primitives_processed'.
        options (PrimitivesWriteOptions | None): see the dataclass.
            None -> defaults.

    Returns:
        (success: bool, message: str).
    """
    if options is None:
        options = PrimitivesWriteOptions()

    n = len(meshes)
    if n == 0:
        return False, f"no meshes supplied to {output_path}"

    # Diagnostic log: helpful when debugging round-trip issues
    print(f"[prim-writer] writing {output_path}")
    print(f"[prim-writer]   meshes: {n}")
    for i, m in enumerate(meshes):
        has_uv1 = getattr(m, 'uv1', None) is not None
        has_col = getattr(m, 'colour', None) is not None
        print(f"[prim-writer]   [{i}] name={m.name!r} "
              f"identifier={m.identifier!r} "
              f"component={getattr(m, 'component', '')!r}  "
              f"verts={len(m.positions)} "
              f"inds={len(m.indices)}  "
              f"uv2={has_uv1}  colour={has_col}")

    try:
        blob = _primitive_encoder.encode_file(
            meshes,
            want_uv2=options.include_uv2,
        )
    except Exception as exc:
        return False, f"encoder error: {exc!r}"

    # Atomic-ish write: stage to .tmp sibling then rename so a
    # half-written file never appears at the target path (matters
    # because the game might be reading these out of res_mods).
    parent = os.path.dirname(os.path.abspath(output_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = output_path + '.tmp'
    try:
        with open(tmp_path, 'wb') as fh:
            fh.write(blob)
        os.replace(tmp_path, output_path)
    except Exception as exc:
        # Best-effort cleanup of the temp file
        try:
            if os.path.isfile(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        return False, f"write failed: {exc!r}"

    size_kb = len(blob) / 1024.0
    return True, f"wrote {os.path.basename(output_path)} ({size_kb:.1f} KB)"
