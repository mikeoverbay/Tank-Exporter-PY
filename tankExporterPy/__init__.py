"""Tank Exporter PY - modular OpenGL viewer + exporter for WoT mesh files.

The Python rewrite of Tank Exporter.  Original VB.NET version (1.0.0.x)
provided the FBX export of WoT primitives_processed assets; this package
replaces it with a cross-platform PyOpenGL renderer and a Blender-bridge
exporter capable of FBX / GLB / GLTF / OBJ.

Version
-------
__version__ follows MAJOR.MINOR.PATCH ("1.14.0", "0.5.2", etc.).  Bump:
    MAJOR (1st): incompatible / breaking change (config schema bump,
                 file format change, complete rewrite of a subsystem).
    MINOR (2nd): every "major shit" addition -- new button, new dialog,
                 new file-format support, new tool, new perf feature.
                 This is the digit you tick most often.
    PATCH (3rd): bug fix, comment fix, no new feature.

Use cust_tools/bump_version.py to bump and persist:
    python cust_tools/bump_version.py minor   # 1.14.0 -> 1.15.0
    python cust_tools/bump_version.py patch   # 1.14.0 -> 1.14.1
    python cust_tools/bump_version.py major   # 1.14.0 -> 2.0.0
"""

__version__ = "1.231.67"
