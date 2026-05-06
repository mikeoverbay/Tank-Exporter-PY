"""
Catalog of every Blender-supported mesh interchange format.

Used by the Import / Export buttons to drive the format-picker form
and to drive the file-dialog filetype filter.  Each entry carries:

    ext             file extension WITHOUT the dot (e.g. 'fbx')
    name            human label shown in the picker + filedialog
                    (e.g. 'FBX (Filmbox)')
    export          True when the EXPORT path is wired up end-to-end
                    (collect_payload -> bridge -> Blender writer).
                    False = the checkbox shows up disabled with
                    "(not yet)" appended; selecting it does nothing.
    import_         True when the IMPORT path is wired up end-to-end
                    (bridge -> Blender reader -> load_imported_payload).

Today only FBX / glTF / OBJ are fully wired -- everything else is a
placeholder so the UI shows the full menu Blender supports without
forcing us to implement them all up-front.
"""

# Each format is a small dict so callers can iterate and read fields by
# name.  Order here is the order shown in the picker dialog -- supported
# formats first, then the placeholders the user can see but not yet
# select.
FORMATS = [
    # ---- supported (full round-trip via the Blender bridge) ----------
    {'ext': 'fbx',  'name': 'FBX (Filmbox)',           'export': True,  'import_': True},
    {'ext': 'glb',  'name': 'glTF binary',             'export': True,  'import_': True},
    {'ext': 'gltf', 'name': 'glTF separate (.gltf+.bin)', 'export': True, 'import_': True},
    {'ext': 'obj',  'name': 'Wavefront OBJ',           'export': True,  'import_': True},
    # ---- placeholders (Blender CAN do these; we haven't wired them) --
    {'ext': 'dae',  'name': 'COLLADA',                 'export': False, 'import_': False},
    {'ext': 'usd',  'name': 'USD (Universal Scene Description)',
                                                       'export': False, 'import_': False},
    {'ext': 'usda', 'name': 'USD ASCII',               'export': False, 'import_': False},
    {'ext': 'usdc', 'name': 'USD Crate (binary)',      'export': False, 'import_': False},
    {'ext': 'usdz', 'name': 'USDZ (zipped)',           'export': False, 'import_': False},
    {'ext': 'stl',  'name': 'STL',                     'export': False, 'import_': False},
    {'ext': 'ply',  'name': 'PLY (Stanford)',          'export': False, 'import_': False},
    {'ext': 'x3d',  'name': 'X3D Extensible 3D',       'export': False, 'import_': False},
    {'ext': 'abc',  'name': 'Alembic',                 'export': False, 'import_': False},
]


def supported_extensions(direction):
    """Return [ext, ...] of formats currently wired for `direction`.

    Args:
        direction (str): 'export' or 'import'

    Returns:
        list[str] of bare extensions (no leading dot).
    """
    key = 'export' if direction == 'export' else 'import_'
    return [f['ext'] for f in FORMATS if f[key]]


def supported_filedialog_filters(direction):
    """Return a list of (label, '*.ext') tuples for tkinter's filedialog.

    Only includes formats currently wired for the given direction so the
    file picker doesn't dangle .stl / .dae / etc. that we can't actually
    process yet.
    """
    key = 'export' if direction == 'export' else 'import_'
    out = [(f['name'], f"*.{f['ext']}") for f in FORMATS if f[key]]
    out.append(('All files', '*.*'))
    return out


def lookup(ext):
    """Find the catalog entry for an extension (case-insensitive, dot-tolerant).

    Returns the entry dict or None.
    """
    if not ext:
        return None
    e = ext.lower().lstrip('.')
    for f in FORMATS:
        if f['ext'] == e:
            return f
    return None
