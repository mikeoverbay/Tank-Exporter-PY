"""Extract WoT's painted fire/smoke flipbooks from the master particle atlas.

WoT packs every particle texture into one 4096x4096 master atlas at
`particles/content_deferred/PFX_textures/eff_tex.dds`.  The atlas is
organised into ~16 sub-quadrants and each sub-quadrant hosts one or
more flipbook grids -- regular MxN tilings of same-sized animation
frames.  This script catalogues every grid we've identified by
visual inspection and slices each into its own subfolder under
`resources/fire_sets/<name>/`.

After running:

    resources/fire_sets/
        fire_BIG/                 64 frames -- orange-bg main fire
        fire_small/               128 frames -- small orange fire
        fireball_blast/           32 frames -- black-bg fireballs
        flame_columns_black/      32 frames -- black-bg flame columns
        flame_columns_light/      32 frames -- light-bg flame columns
        smoke_white/              64 frames -- white smoke clouds
        smoke_dark/               64 frames -- dark grey smoke
        smoke_cream/              64 frames -- cream/warm smoke
        smoke_dirt/               32 frames -- dirt/ground impact
        ...

The runtime FlipbookTexture loads any `*.png` in alphabetical order
from a single folder, so to pick a set, copy / point its frames at
`resources/fire/`.  The viewer's load path is folder-name driven --
all you'd change is which subfolder feeds it.

Why we slice manually (no .vfx parsing):

The .vfxbin format is binary and undocumented.  The named regions
INSIDE the .vfx (`fire_locked`, `fire_flame`, `fire_anim_BIG_2`,
etc.) are stored as integer atlas-table indices, not as float UV
rects -- a brute-force probe near each region name turned up
particle simulation parameters (rotation in radians, lifetime
fractions, scale curves) but no atlas-rect coordinates.

Visual inspection of the atlas reveals regular grid layouts that
are easy to identify by eye, so we just hardcode each grid below.
Tweak the rectangle / cols / rows / tile_size if a particular set
slices off-frame -- re-run to regenerate.

Pillow only.  Run from the project root:

    python cust_tools/extract_wot_fire_atlas.py
"""

import os
import sys
import zipfile
from PIL import Image

# ---------------------------------------------------------------------------
# Source

_PKG_CANDIDATES = (
    r'C:\Games\World_of_Tanks_NA\res\packages\particles.pkg',
    r'C:\Games\World_of_Tanks_EU\res\packages\particles.pkg',
    r'C:\Games\World_of_Tanks_RU\res\packages\particles.pkg',
    r'C:\Games\World_of_Tanks\res\packages\particles.pkg',
)
_INTERNAL = 'particles/content_deferred/PFX_textures/eff_tex.dds'

# ---------------------------------------------------------------------------
# Grid catalogue.  Each entry: name -> dict with the atlas rect,
# tile layout, and a short description.  Names are also used as
# subfolder names under resources/fire_sets/.

# rect is (x0, y0, x1, y1) in atlas pixels.  cols/rows is the tile
# count.  frame_size is the per-tile pixel size (always square in
# this atlas).  cols * rows == total frames in that set.

GRID_DEFS = {
    # ---- FIRE GRIDS --------------------------------------------------------

    # Orange-bg main fire animation.  Maps to `fire_anim_BIG_*` in the
    # burning-tank .vfx files.  Cleanest "visible flame" set.
    'fire_BIG': dict(
        rect=(3072, 2048, 4096, 3072),
        cols=8, rows=8, frame_size=128,
        note='orange-bg main fire animation (64 frames @ 128px)',
    ),

    # Smaller orange fire frames -- 16 cols x 8 rows of 64x64 in
    # the 1024 x 1024 sector at (3072, 1024).  More frames at lower
    # resolution -- useful for a denser fire of small flickers.
    'fire_small': dict(
        rect=(3072, 1024, 4096, 2048),
        cols=16, rows=16, frame_size=64,
        note='small orange fire animation (256 frames @ 64px)',
    ),

    # Black-bg fireball BLAST -- more "ammo cookoff" than continuous
    # burn.  Good for a BURNUP transient; not ideal for steady fire.
    'fireball_blast': dict(
        rect=(3072, 3072, 4096, 3584),
        cols=8, rows=4, frame_size=128,
        note='black-bg fireball blast (32 frames @ 128px)',
    ),

    # Black-bg flame columns -- vertical flames on transparent /
    # black background.  Good source if you want a "torch" look
    # rather than a spreading fire.
    'flame_columns_black': dict(
        rect=(2048, 3584, 3072, 4096),
        cols=8, rows=4, frame_size=128,
        note='black-bg vertical flame columns (32 frames @ 128px)',
    ),

    # Light-bg flame columns -- same idea but on a light background.
    # Use when the destination scene is bright; the alpha fights
    # less with light backgrounds.
    'flame_columns_light': dict(
        rect=(2048, 3072, 3072, 3584),
        cols=8, rows=4, frame_size=128,
        note='light-bg vertical flame columns (32 frames @ 128px)',
    ),

    # ---- SMOKE GRIDS -------------------------------------------------------

    # White / pale smoke cloud grid -- generic smoke, reads well
    # against dark dirt or environments.
    'smoke_white': dict(
        rect=(1024, 0, 2048, 1024),
        cols=8, rows=8, frame_size=128,
        note='white / pale smoke clouds (64 frames @ 128px)',
    ),

    # Dark grey smoke -- "burning oil" / "gun powder" style.
    'smoke_dark': dict(
        rect=(1024, 1024, 2048, 2048),
        cols=8, rows=8, frame_size=128,
        note='dark grey smoke (64 frames @ 128px)',
    ),

    # Cream / warm smoke -- middle-tone, slightly tan; matches the
    # sepia palette of the splash scene.
    'smoke_cream': dict(
        rect=(2048, 1024, 3072, 2048),
        cols=8, rows=8, frame_size=128,
        note='cream / warm smoke (64 frames @ 128px)',
    ),

    # Dirt / ground impact dust clouds.
    'smoke_dirt': dict(
        rect=(0, 3072, 1024, 4096),
        cols=8, rows=8, frame_size=128,
        note='dirt / ground impact dust (64 frames @ 128px)',
    ),

    # Smoke dome / under-smoke (low-lying) -- the kind that pools
    # under a destroyed tank.
    'smoke_under': dict(
        rect=(2048, 2048, 3072, 3072),
        cols=8, rows=8, frame_size=128,
        note='under-smoke dome (64 frames @ 128px)',
    ),
}

# Default `single-set` mode (overwrites resources/fire/) -- use the
# main BIG fire grid for the burning-damaged-tanks effect.
DEFAULT_RUNTIME_PICK = 'fire_BIG'


# ---------------------------------------------------------------------------


def _locate_pkg():
    for p in _PKG_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def _slice_one(grid_image, grid_def, out_dir, prefix):
    """Slice `grid_image` into per-tile PNGs in `out_dir`.

    Standard flipbook convention: frame 0 is the TOP-LEFT tile,
    sweep RIGHT across the row (increasing column), then DOWN to
    the next row.  Top-left first, bottom-right last -- that's
    what WoT's atlas uses too, despite an early misread that had
    us reversing both axes.
    """
    cols, rows = grid_def['cols'], grid_def['rows']
    fs = grid_def['frame_size']
    n = 0
    for r in range(rows):
        for c in range(cols):
            x0 = c * fs
            y0 = r * fs
            tile = grid_image.crop((x0, y0, x0 + fs, y0 + fs))
            out = os.path.join(out_dir, f'{prefix}_{n:04d}.png')
            tile.save(out, format='PNG', optimize=False)
            n += 1
    return n


def _wipe_pngs(folder):
    """Remove every *.png from `folder` so a fresh slice doesn't
    interleave with leftover frames from a prior cut."""
    if not os.path.isdir(folder):
        return 0
    cleared = 0
    for fn in os.listdir(folder):
        full = os.path.join(folder, fn)
        if os.path.isfile(full) and fn.lower().endswith('.png'):
            os.unlink(full)
            cleared += 1
    return cleared


def main():
    here     = os.path.dirname(os.path.abspath(__file__))
    res_dir  = os.path.join(os.path.dirname(here), 'resources')
    sets_dir = os.path.join(res_dir, 'fire_sets')
    os.makedirs(sets_dir, exist_ok=True)

    # 1) Source pkg + atlas extraction (cached on disk so re-runs
    #    are fast).
    pkg = _locate_pkg()
    if pkg is None:
        sys.exit("ERROR: particles.pkg not found in any of:\n  "
                 + "\n  ".join(_PKG_CANDIDATES))

    atlas_local = os.path.join(res_dir, '_wot_eff_tex.dds')
    if not os.path.isfile(atlas_local):
        with zipfile.ZipFile(pkg) as zf:
            with zf.open(_INTERNAL) as src, open(atlas_local, 'wb') as dst:
                dst.write(src.read())
        print(f"  extracted {_INTERNAL}")
        print(f"    -> {atlas_local}")
    else:
        print(f"  using cached atlas: {atlas_local}")

    src = Image.open(atlas_local).convert('RGBA')
    sw, sh = src.size
    print(f"  atlas: {sw}x{sh}\n")

    # 2) Slice every cataloged grid into its own subfolder.
    #    Filenames inside use the SET name as prefix so dragging
    #    files between folders doesn't lose provenance.
    print(f"  writing {len(GRID_DEFS)} sets to {sets_dir}\\")
    print(f"  ----------------------------------------------------------")

    manifest_lines = ['# WoT fire/smoke flipbook sets extracted from '
                      'eff_tex.dds atlas.\n#\n'
                      '# Each subfolder is a complete flipbook ready '
                      'for FlipbookTexture\n# consumption.  Copy any '
                      'set into resources/fire/ to wire it up to\n# '
                      'the burning-tank ParticleSystem at runtime.\n#\n']
    for name, gdef in GRID_DEFS.items():
        out_dir = os.path.join(sets_dir, name)
        os.makedirs(out_dir, exist_ok=True)
        cleared = _wipe_pngs(out_dir)

        rx0, ry0, rx1, ry1 = gdef['rect']
        grid = src.crop((rx0, ry0, rx1, ry1))
        n = _slice_one(grid, gdef, out_dir, prefix=name)

        line = (f"  {name:<22} {gdef['cols']:>2}x{gdef['rows']:<2} "
                f"@ {gdef['frame_size']:>3}px  "
                f"= {n:>3} frames   "
                f"-- {gdef['note']}")
        print(line)
        manifest_lines.append(
            f"{name}/  ({n} frames @ {gdef['frame_size']}px,  "
            f"rect=({rx0},{ry0},{rx1},{ry1}))\n"
            f"    {gdef['note']}\n")

    # 3) Drop a manifest file so the user can read the catalogue
    #    without re-running the script.
    manifest_path = os.path.join(sets_dir, 'README.txt')
    with open(manifest_path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(manifest_lines))
        fh.write('\n----------------------------------------------------\n'
                 'Source atlas: particles.pkg/'
                 + _INTERNAL
                 + '\nSlice script: cust_tools/extract_wot_fire_atlas.py\n')
    print(f"\n  manifest -> {manifest_path}")

    # 4) Wire DEFAULT_RUNTIME_PICK into resources/fire/ so the
    #    viewer's burning-tank effect picks up the WoT fire on the
    #    next launch without manual file shuffling.
    runtime_dir = os.path.join(res_dir, 'fire')
    os.makedirs(runtime_dir, exist_ok=True)
    cleared = _wipe_pngs(runtime_dir)
    pick_dir = os.path.join(sets_dir, DEFAULT_RUNTIME_PICK)
    n_copied = 0
    for fn in sorted(os.listdir(pick_dir)):
        if not fn.lower().endswith('.png'):
            continue
        with open(os.path.join(pick_dir, fn), 'rb') as fsrc, \
             open(os.path.join(runtime_dir, fn), 'wb') as fdst:
            fdst.write(fsrc.read())
        n_copied += 1
    print(f"\n  runtime: copied {n_copied} frame(s) from "
          f"{DEFAULT_RUNTIME_PICK}/ -> resources/fire/")
    print(f"  cleared {cleared} prior frame(s) before copy.")
    print(f"\n  to switch to a different set later, replace the contents")
    print(f"  of resources/fire/ with any other resources/fire_sets/<name>/")


if __name__ == '__main__':
    main()
