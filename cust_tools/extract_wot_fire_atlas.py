"""Extract WoT's painted fire/smoke flipbooks from the master particle atlas.

WoT packs every particle texture into one 4096x4096 master atlas at
`particles/content_deferred/PFX_textures/eff_tex.dds`.  The atlas is
organised into ~16 sub-quadrants and each sub-quadrant hosts one or
more flipbook grids -- regular MxN tilings of same-sized animation
frames.  This module catalogues every grid we've identified by
visual inspection and slices each into its own subfolder under
`resources/fire_sets/<name>/`.

Two modes:

1. **CLI / authoring mode** (`python cust_tools/extract_wot_fire_atlas.py`)
   slices ALL catalogued sets to `resources/fire_sets/<name>/` and
   copies the DEFAULT_RUNTIME_PICK into `resources/fire/`.  Used when
   you're swapping or comparing flipbook sets by hand.

2. **Runtime auto-extract** (`ensure_runtime_flipbooks(...)`, called
   from `Viewer.__init__`) slices ONLY the two sets the viewer
   actually consumes (fire_BIG -> resources/fire, smoke_white ->
   resources/smoke) and skips the work entirely if the target
   folders already have PNGs.  No `fire_sets/` tree is produced --
   that's authoring-only output.

Why we don't ship the WoT flipbooks: every PNG is a slice of
Wargaming's `eff_tex.dds`.  We can't redistribute their artwork.
Both modes pull from the user's local `particles.pkg` so the bytes
never leave their installation.

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
# Per Coffee 2026-05-14: WoT's muzzle-flash uses a SECOND atlas
# alongside the color one -- a 512x512 RGBA texture encoding the
# heat-haze / refraction NORMAL MAP that drives `ps_shimmer.fx`.
# Cached alongside the color atlas on first launch so the runtime
# can sample both as part of the flash shimmer pass.
_INTERNAL_DIST = (
    'particles/content_deferred/PFX_textures/eff_tex_distortion.dds')

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

    # ---- GUN-TRAIL SMOKE ---------------------------------------------------

    # Faint trail-smoke flipbook for the per-projectile trail effect.
    # Coffee 2026-05-13: "try the atlas texture at coords 5 12 x 5 12.
    # there are 4 columns of 2 rows.. 8 images".  Best guess: 128x128
    # frames in a 4x2 grid starting at atlas (512, 512), region
    # (512, 512, 1024, 768).  Adjust rect / frame_size if the
    # extracted PNGs look off-frame -- this is the first-pass
    # placement based on the spec.
    'gun_trail': dict(
        rect=(512, 512, 1024, 768),
        cols=4, rows=2, frame_size=128,
        note='gun-fire trail smoke (8 frames @ 128px, 4x2 grid)',
    ),

    # ---- MUZZLE FLASH -----------------------------------------------------

    # Coffee 2026-05-14: orange/yellow muzzle-flash plume animation.
    # Rect (1024, 2560, 1536, 3072) is a 512x512 region containing
    # 2 cols x 4 rows = 8 frames at 256x128 each (NON-SQUARE).
    # Left column = animation frames pointing right; right column =
    # the SAME frames horizontally mirrored.  Both columns share the
    # same per-row time index, so the natural decoding is column-
    # major: frames 0-3 = left col top->bottom, frames 4-7 = right
    # col top->bottom.  Use frame_w / frame_h instead of the square
    # frame_size used by the rest of the catalogue.
    'gun_flash': dict(
        rect=(1024, 2560, 1536, 3072),
        cols=2, rows=4, frame_w=256, frame_h=128,
        column_major=True,
        note='muzzle flash plume (8 frames @ 256x128, 2x4 grid; '
             'right column is left mirrored)',
    ),

    # Per Coffee 2026-05-14 (WoT shimmer pipeline dig): the
    # muzzle-flash distortion frames live in the SEPARATE
    # `eff_tex_distortion.dds` atlas (not eff_tex.dds).  They are
    # 6 normal-map puffs in a 3x2 grid at (128,0)-(512,256), each
    # 128x128 px.  Sampled by `ps_shimmer.fx` to displace the
    # back-buffer behind the flash for heat-haze refraction.
    # This is keyed under a separate `_atlas` marker so the
    # extractor knows to slice from the distortion source.
    'gun_flash_distortion': dict(
        rect=(128, 0, 512, 256),
        cols=3, rows=2, frame_size=128,
        _atlas='distortion',
        note='muzzle flash distortion normal-map (6 frames @ 128px '
             'in 3x2 grid, from eff_tex_distortion.dds)',
    ),

    # ---- SMOKE GRIDS -------------------------------------------------------

    # White / pale smoke cloud grid -- generic smoke, reads well
    # against dark dirt or environments.
    'smoke_white': dict(
        rect=(1024, 0, 2048, 1024),
        cols=8, rows=8, frame_size=128,
        note='white / pale smoke clouds (64 frames @ 128px)',
    ),

    # Per Coffee 2026-05-14 ("1024,1024.  2 sets in that area"):
    # the (1024, 1024)-(2048, 2048) quadrant is NOT generic dark
    # smoke -- it's TWO distinct explosion sequences stacked
    # vertically.  Each is 8 cols x 4 rows = 32 frames at 128 px
    # square showing a fireball -> smoke transition.  Top set is
    # the cleaner orange-bloom + black-smoke variant; bottom is
    # the dustier ground-impact variant (more brown tones,
    # rougher smoke).  Both ship at startup so the runtime can
    # pick per-impact-surface (object hit -> A, terrain hit -> B).
    'explosion_fire': dict(
        rect=(1024, 1024, 2048, 1536),
        cols=8, rows=4, frame_size=128,
        note='explosion fireball + black smoke (32 frames @ 128px)',
    ),
    'explosion_dust': dict(
        rect=(1024, 1536, 2048, 2048),
        cols=8, rows=4, frame_size=128,
        note='ground-impact dust explosion '
             '(32 frames @ 128px, brown tones)',
    ),
    # Legacy alias kept so older code referencing `smoke_dark`
    # (one big 8x8 grid) still resolves.  Same byte range, just
    # different slicing -- callers wanting the "dark smoke"
    # interpretation will still see 64 frames in row-major order
    # spanning both explosion bands.  Drop this when no callers
    # remain.
    'smoke_dark': dict(
        rect=(1024, 1024, 2048, 2048),
        cols=8, rows=8, frame_size=128,
        note='LEGACY alias -- prefer explosion_fire / '
             'explosion_dust (the same region is two 8x4 explosion '
             'sequences, not a single 8x8 smoke grid).',
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

# Runtime-only: which catalogue entry feeds which `resources/<dir>/`
# folder.  Used by `ensure_runtime_flipbooks` (called from
# Viewer.__init__) so the viewer always has fire+smoke frames to load
# even on a fresh clone where the WoT artwork was never shipped to
# git.  The grid names map back into `GRID_DEFS` above.
RUNTIME_TARGETS = {
    'fire':                 'fire_BIG',
    'smoke':                'smoke_white',
    'gun_trail':            'gun_trail',
    'gun_flash':            'gun_flash',
    'gun_flash_distortion': 'gun_flash_distortion',
    # Per Coffee 2026-05-14 (impact explosions): both variants
    # extracted at startup so the impact renderer can pick per
    # surface kind (hard hit -> fire, terrain hit -> dust).
    'explosion_fire':       'explosion_fire',
    'explosion_dust':       'explosion_dust',
}


# ---------------------------------------------------------------------------


def _locate_pkg(extra_paths=()):
    """Find particles.pkg.  Search order:

        1. Any extra paths the caller supplied (e.g. the user's
           configured `pkg_dir` from tankExporterPy.json).
        2. The hardcoded NA / EU / RU / standalone candidates.

    Returns the absolute path to the pkg, or None if no candidate
    exists.  Both individual files and a containing directory are
    accepted -- if `extra_paths` includes a folder, we look for
    `particles.pkg` inside it.
    """
    candidates = []
    for p in extra_paths:
        if not p:
            continue
        if os.path.isdir(p):
            candidates.append(os.path.join(p, 'particles.pkg'))
        else:
            candidates.append(p)
    candidates.extend(_PKG_CANDIDATES)
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def ensure_atlas_local(resources_dir, pkg_path=None, extra_pkg_paths=()):
    """Make sure `resources/_wot_eff_tex.dds` exists; extract from
    particles.pkg if not.

    Args:
        resources_dir   (str)         : project resources/ folder
        pkg_path        (str|None)    : explicit particles.pkg, or None to
                                        search via _locate_pkg
        extra_pkg_paths (iterable)    : extra search paths handed to
                                        _locate_pkg (e.g. user's pkg_dir
                                        from config)

    Returns:
        str | None: abs path to the cached `.dds`, or None if neither a
        cached copy nor a source pkg was available.
    """
    atlas_local = os.path.join(resources_dir, '_wot_eff_tex.dds')
    if os.path.isfile(atlas_local):
        return atlas_local
    src_pkg = pkg_path if (pkg_path and os.path.isfile(pkg_path)) \
        else _locate_pkg(extra_pkg_paths)
    if not src_pkg:
        return None
    os.makedirs(resources_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(src_pkg) as zf:
            with zf.open(_INTERNAL) as src, open(atlas_local, 'wb') as dst:
                dst.write(src.read())
        return atlas_local
    except Exception as exc:
        print(f"[extract_wot_fire_atlas] could not extract atlas: {exc}")
        return None


def ensure_distortion_atlas_local(resources_dir, pkg_path=None,
                                    extra_pkg_paths=()):
    """Same recipe as `ensure_atlas_local` but for the distortion
    (heat-haze) atlas at `eff_tex_distortion.dds`.  Cached at
    `resources/_wot_eff_tex_distortion.dds`.  Used by the muzzle-
    flash shimmer pipeline (Coffee 2026-05-14): the color sprite
    gives the visible flame, the distortion sprite warps the
    back-buffer through a normal-map offset producing the
    3D-looking heat haze WoT's `ps_shimmer.fx` shader provides.
    """
    atlas_local = os.path.join(
        resources_dir, '_wot_eff_tex_distortion.dds')
    if os.path.isfile(atlas_local):
        return atlas_local
    src_pkg = pkg_path if (pkg_path and os.path.isfile(pkg_path)) \
        else _locate_pkg(extra_pkg_paths)
    if not src_pkg:
        return None
    os.makedirs(resources_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(src_pkg) as zf:
            with zf.open(_INTERNAL_DIST) as src, \
                 open(atlas_local, 'wb') as dst:
                dst.write(src.read())
        return atlas_local
    except Exception as exc:
        print(f"[extract_wot_fire_atlas] could not extract distortion atlas: "
              f"{exc}")
        return None


def slice_set(set_name, target_dir, atlas_path, prefix=None,
              wipe_first=True):
    """Slice one named grid out of the atlas into target_dir as PNGs.

    Args:
        set_name    (str): catalogue key (must be in GRID_DEFS)
        target_dir  (str): output folder (created if missing)
        atlas_path  (str): path to a local copy of `eff_tex.dds`
        prefix      (str|None): per-tile filename prefix; defaults to
                                 set_name (e.g. 'fire_BIG_0000.png')
        wipe_first  (bool): empty *.png from target_dir before writing

    Returns:
        int: number of frames written.

    Raises:
        KeyError: if set_name isn't in GRID_DEFS.
    """
    gdef = GRID_DEFS[set_name]
    os.makedirs(target_dir, exist_ok=True)
    if wipe_first:
        _wipe_pngs(target_dir)
    src = Image.open(atlas_path).convert('RGBA')
    rx0, ry0, rx1, ry1 = gdef['rect']
    grid = src.crop((rx0, ry0, rx1, ry1))
    n = _slice_one(grid, gdef, target_dir, prefix=prefix or set_name)
    return n


def ensure_runtime_flipbooks(resources_dir, pkg_path=None,
                             extra_pkg_paths=(), force=False):
    """Make sure `resources/fire/` and `resources/smoke/` are populated.

    Called from `Viewer.__init__` BEFORE the FlipbookTexture loaders
    fire so the user can clone the repo, run `go.bat`, and get a
    burning-tank effect on first launch -- without the repo having
    to ship Wargaming's atlas artwork.

    Skips work entirely when both target folders already contain at
    least one PNG (steady-state cost = two os.listdir calls).  When
    one folder is empty, locates particles.pkg, caches the atlas to
    `resources/_wot_eff_tex.dds`, and slices ONLY the two grids
    listed in RUNTIME_TARGETS.

    Args:
        resources_dir   (str)      : project resources/ folder
        pkg_path        (str|None) : explicit particles.pkg
        extra_pkg_paths (iterable) : additional pkg search paths
                                     (typically the user's configured
                                     pkg_dir from tankExporterPy.json)
        force           (bool)     : re-slice even if folders are
                                     already populated

    Returns:
        dict: {target_dir_name: frames_written}.  Empty when nothing
        needed doing or when no pkg was available.  Never raises.
    """
    out = {}

    def _has_pngs(folder):
        if not os.path.isdir(folder):
            return False
        for fn in os.listdir(folder):
            if fn.lower().endswith('.png'):
                return True
        return False

    needed = []
    for target_name, set_name in RUNTIME_TARGETS.items():
        target_dir = os.path.join(resources_dir, target_name)
        if force or not _has_pngs(target_dir):
            needed.append((target_name, set_name, target_dir))

    if not needed:
        return out

    atlas = ensure_atlas_local(
        resources_dir, pkg_path=pkg_path, extra_pkg_paths=extra_pkg_paths)
    if not atlas:
        # Caller will get a clean error from FlipbookTexture; we just
        # log here so the user understands why the folders are empty.
        print(f"[ensure_runtime_flipbooks] no particles.pkg found -- "
              f"fire/smoke flipbooks unavailable until WoT path is set")
        return out
    # Per Coffee 2026-05-14 (shimmer pipeline): grids whose def
    # has `_atlas='distortion'` slice from the second WoT atlas
    # (`eff_tex_distortion.dds`) instead of the color one.  Cached
    # lazily so non-distortion-using setups don't pay the extra
    # pkg I/O.
    distortion_atlas = None

    for target_name, set_name, target_dir in needed:
        gdef = GRID_DEFS.get(set_name, {})
        atlas_kind = gdef.get('_atlas', 'color')
        if atlas_kind == 'distortion':
            if distortion_atlas is None:
                distortion_atlas = ensure_distortion_atlas_local(
                    resources_dir, pkg_path=pkg_path,
                    extra_pkg_paths=extra_pkg_paths)
            src = distortion_atlas
        else:
            src = atlas
        if not src:
            print(f"[ensure_runtime_flipbooks] skip {target_name}: "
                  f"{atlas_kind} atlas not available")
            continue
        try:
            n = slice_set(set_name, target_dir, src)
            out[target_name] = n
            print(f"[ensure_runtime_flipbooks] {target_name}/  <- "
                  f"{set_name}  ({n} frames, atlas={atlas_kind})")
        except Exception as exc:
            print(f"[ensure_runtime_flipbooks] slice {set_name} -> "
                  f"{target_name}/ failed: {exc}")
    return out


def _slice_one(grid_image, grid_def, out_dir, prefix):
    """Slice `grid_image` into per-tile PNGs in `out_dir`.

    Standard flipbook convention: frame 0 is the TOP-LEFT tile,
    sweep RIGHT across the row (increasing column), then DOWN to
    the next row.  Top-left first, bottom-right last -- that's
    what WoT's atlas uses too, despite an early misread that had
    us reversing both axes.

    Per Coffee 2026-05-14 (muzzle-flash grid):
    * `frame_w` / `frame_h` override `frame_size` for non-square
      tiles.  When only `frame_size` is set we treat it as both
      width AND height (the legacy square case).
    * `column_major=True` walks the grid down-first instead of
      right-first, so frames 0..rows-1 come from column 0,
      frames rows..2*rows-1 from column 1, etc.  Used by the
      muzzle-flash grid where the two columns are independent
      sequences (left = forward-pointing, right = mirrored).
    """
    cols, rows = grid_def['cols'], grid_def['rows']
    fw = grid_def.get('frame_w', grid_def.get('frame_size'))
    fh = grid_def.get('frame_h', grid_def.get('frame_size'))
    col_major = bool(grid_def.get('column_major', False))
    n = 0
    if col_major:
        outer, inner = cols, rows
        def coord(o, i):
            return (o, i)        # (c, r)
    else:
        outer, inner = rows, cols
        def coord(o, i):
            return (i, o)        # (c, r) -- inner is column
    for o in range(outer):
        for i in range(inner):
            c, r = coord(o, i)
            x0 = c * fw
            y0 = r * fh
            tile = grid_image.crop((x0, y0, x0 + fw, y0 + fh))
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
    #    are fast).  Re-uses ensure_atlas_local so the runtime path
    #    and the CLI path can't drift out of sync.
    atlas_local = ensure_atlas_local(res_dir)
    if atlas_local is None:
        sys.exit("ERROR: particles.pkg not found in any of:\n  "
                 + "\n  ".join(_PKG_CANDIDATES))
    print(f"  atlas: {atlas_local}")

    src = Image.open(atlas_local).convert('RGBA')
    sw, sh = src.size
    print(f"  atlas size: {sw}x{sh}\n")

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

        fw_dbg = gdef.get('frame_w', gdef.get('frame_size'))
        fh_dbg = gdef.get('frame_h', gdef.get('frame_size'))
        size_str = (f'{fw_dbg}px' if fw_dbg == fh_dbg
                    else f'{fw_dbg}x{fh_dbg}px')
        line = (f"  {name:<22} {gdef['cols']:>2}x{gdef['rows']:<2} "
                f"@ {size_str:>9}  "
                f"= {n:>3} frames   "
                f"-- {gdef['note']}")
        print(line)
        manifest_lines.append(
            f"{name}/  ({n} frames @ {size_str},  "
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
