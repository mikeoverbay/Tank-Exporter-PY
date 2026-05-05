"""
Native-format writers.

Today the only writer here is `primitives_writer`, which serialises a
list of Mesh objects back to WoT's `.primitives_processed` binary
format.  See VISUAL_PROCESSED_FORMAT.md at the project root for the
target byte layout.

Public API:
    write_primitives(meshes, output_path, options=None)
        -- write the given meshes as one .primitives_processed file.
           Caller groups by component (hull / chassis / turret / gun)
           upstream; this function doesn't know about that grouping.

    PrimitivesWriteOptions
        -- bag of optional flags (include_uv2, include_colour, ...)
"""

from .primitives_writer import write_primitives, PrimitivesWriteOptions

__all__ = ['write_primitives', 'PrimitivesWriteOptions']
