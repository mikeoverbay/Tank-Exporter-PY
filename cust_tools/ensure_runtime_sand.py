"""First-launch generator for the procedural sand ground texture.

The sand textures (`resources/sand_painted.png` + its `_height`
companion) are big (60-85 MB) and trivially reproducible, so they are
gitignored and never shipped in the repo -- exactly like the
fire/smoke flipbooks.  This module regenerates them on first launch
when they are missing, so a fresh install from GitHub gets the same
textured ground the author's build has instead of an untextured
terrain.

Mirror of `extract_wot_fire_atlas.ensure_runtime_flipbooks`: cheap
no-op when the file already exists, best-effort (any failure just
leaves the terrain to fall back to its flat sand colour), callable
from `Viewer.__init__` at startup.

Added 2026-10-23 (Opus 4.8), on the owner's choice to auto-generate
sand on first launch rather than commit the big PNGs.
"""
import os


# 4096 rather than the painter's 8192 default: ~20 s and ~400 MB peak
# instead of ~70 s and ~1.5 GB, which is the right trade for a
# one-time first-launch step on an arbitrary user's machine.  At the
# standard 50 m tile that is ~12 mm/pixel -- plenty for ground tiled
# a few times across a 160 m world and viewed from tank height.
DEFAULT_SIZE = 4096
DEFAULT_TILE_METERS = 50.0
DEFAULT_SEED = 42


def ensure_runtime_sand(resources_dir, size=DEFAULT_SIZE,
                        tile_meters=DEFAULT_TILE_METERS,
                        seed=DEFAULT_SEED, on_status=None):
    """Ensure `resources/sand_painted.png` exists; paint it if not.

    Args:
        resources_dir : the project's `resources/` folder.
        size          : edge length of the generated texture.
        tile_meters   : metres per tile (matches the viewer default).
        seed          : RNG seed for the painter.
        on_status     : optional callable(str) for splash progress.

    Returns the colour-texture path if it exists or was generated,
    else None.  Never raises -- a failure just means the terrain
    renders on its flat sand colour, which is harmless.
    """
    out_path = os.path.join(resources_dir, 'sand_painted.png')
    if os.path.isfile(out_path):
        return out_path

    # sand.png (an older/user-supplied photo) is a valid fallback the
    # viewer already prefers after sand_painted.png -- if the user
    # shipped one, don't spend 20 s painting over it.
    legacy = os.path.join(resources_dir, 'sand.png')
    if os.path.isfile(legacy):
        return legacy

    try:
        if on_status:
            on_status('First launch: painting sand texture '
                      '(one-time, ~20 s)...')
        print('[ensure_runtime_sand] sand_painted.png missing -- '
              f'painting {size}x{size} once (first launch only)')
        # Imported lazily: the painter pulls in the FFT machinery and
        # is only ever needed on this one-time path.
        from cust_tools.paint_sand_desert import paint_sand
        os.makedirs(resources_dir, exist_ok=True)
        paint_sand(size=size, out_path=out_path,
                   tile_meters=tile_meters, seed=seed)
        return out_path if os.path.isfile(out_path) else None
    except Exception as exc:
        print(f'[ensure_runtime_sand] skipped ({type(exc).__name__}: '
              f'{exc}) -- terrain will use its flat sand colour')
        return None
