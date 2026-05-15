"""
Extract WoT's Karelia skydome panorama from `01_karelia.pkg` and
decode it to a PNG the TEPY runtime `SkyDome` can sample.

Source path:
    01_karelia.pkg :: maps/skyboxes/01_Karelia_sky/skydome/
                      sky_karelia_forward.dds

The DDS is a 4096 x 1024 BC1 (DXT1) panorama -- a horizon band
designed for equirectangular sphere mapping.  We decode it once
to PNG so the runtime can use the same Pillow + GL upload path
the rest of the texture loaders use.

Same redistribution rule as the fire / smoke atlases and the
shellhole decals: WoT pixels never enter the repo.  Output is
gitignored; the extractor pulls on first launch.

Run:
    python cust_tools/extract_wot_karelia_sky.py
    python cust_tools/extract_wot_karelia_sky.py --force

Author: Coffee + Claude, 2026-05-15.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import zipfile

try:
    from PIL import Image
except ImportError:
    Image = None


_PKG_NAME = '01_karelia.pkg'
_INTERNAL = ('maps/skyboxes/01_Karelia_sky/skydome/'
             'sky_karelia_forward.dds')
_OUT_REL  = ('skyboxes/01_Karelia_sky/skydome/'
             'sky_karelia_forward.png')


def _locate_pkg(extra_pkg_paths=()):
    """Find a `01_karelia.pkg` under any provided pkg_dir.  Same
    probe order the other extractors use so a single configured
    pkg_dir covers every WoT pull."""
    candidates = list(extra_pkg_paths)
    candidates.extend([
        r'C:\Games\World_of_Tanks_NA\res\packages',
        r'C:\Games\World_of_Tanks\res\packages',
        r'C:\Games\World_of_Tanks_RU\res\packages',
        r'C:\Games\World_of_Tanks_EU\res\packages',
    ])
    for c in candidates:
        if c and os.path.isdir(c):
            full = os.path.join(c, _PKG_NAME)
            if os.path.isfile(full):
                return full
    return None


def ensure_karelia_sky(resources_dir, pkg_dir=None,
                      extra_pkg_paths=(), force=False):
    """Make sure `resources/skyboxes/01_Karelia_sky/skydome/
    sky_karelia_forward.png` exists.  Returns its absolute path
    or None if the source pkg couldn't be located / decode failed.

    Idempotent: skips work when the PNG is already on disk
    (unless `force`).  Designed to be called from
    `Viewer.__init__` on every launch.

    Args:
        resources_dir (str)    : project resources/ folder
        pkg_dir       (str|None): explicit `01_karelia.pkg`
                                  parent directory
        extra_pkg_paths (iter) : extra search paths (typically
                                  the user's configured pkg_dir)
        force         (bool)   : re-extract even if the PNG
                                  already exists
    """
    out_path = os.path.join(resources_dir, _OUT_REL)
    if os.path.isfile(out_path) and not force:
        return out_path
    if Image is None:
        print("[ensure_karelia_sky] Pillow not installed -- "
              "sky disabled")
        return None

    if pkg_dir and os.path.isdir(pkg_dir):
        pkg_path = os.path.join(pkg_dir, _PKG_NAME)
        if not os.path.isfile(pkg_path):
            pkg_path = _locate_pkg(extra_pkg_paths)
    else:
        pkg_path = _locate_pkg(extra_pkg_paths)
    if not pkg_path:
        print("[ensure_karelia_sky] 01_karelia.pkg not found -- "
              "sky disabled")
        return None

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    try:
        with zipfile.ZipFile(pkg_path) as zf:
            with zf.open(_INTERNAL) as src:
                blob = src.read()
        img = Image.open(io.BytesIO(blob)).convert('RGBA')
        img.save(out_path, 'PNG', optimize=False)
        print(f"[ensure_karelia_sky] {os.path.basename(out_path)} "
              f"({img.size[0]}x{img.size[1]})  ok")
        return out_path
    except Exception as exc:
        print(f"[ensure_karelia_sky] decode failed: {exc}")
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resources', default='resources')
    parser.add_argument('--pkg-dir', default=None)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()

    out = ensure_karelia_sky(
        args.resources, pkg_dir=args.pkg_dir, force=args.force)
    if not out:
        print("nothing extracted")
        return 1
    print(out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
