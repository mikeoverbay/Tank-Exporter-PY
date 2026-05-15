"""
Extract WoT's `maps/decals_pbs/PBS_ShellHole_*_AM.dds` files from
the user's local pkg install and decode them to PNGs the TEPY
runtime `Decals` projector can sample.

WoT ships PBR-triplet decals for shell holes:

  AM  -- albedo / colour map (sRGB, BC3/DXT5)
  NM  -- normal map (2-channel reconstructed Z)
  GMM -- gloss / metallic / mask map (PBR aux)

The TEPY decal projector is forward-rendered and only needs the
albedo for the first pass.  Normal-mapped decals are deferred --
NM + GMM extraction is intentionally OFF below; flip the
`_VARIANT_FILTER` regex when we wire up the proper PBR decal
shader.

Source pkgs that ship the shellhole assets (user-observed
2026-05-14, may vary by install / region):

  shared_content-part1.pkg
  shared_content-part2.pkg
  shared_content-part3.pkg
  shared_content_sandbox-part1.pkg
  shared_content_sandbox-part2.pkg
  120_graf_zeppelin_scc.pkg
  38_mannerheim_line.pkg

We scan EVERY pkg under the configured pkg dir at extract time
rather than hardcoding the list -- WG can move assets between
pkgs at any patch.  Match-pattern is fixed
(`maps/decals_pbs/PBS_ShellHole_*_AM.dds`) so map-specific pkgs
that re-ship variants are picked up automatically.

Like the fire-atlas extractor, this file is:

  * git-safe -- writes only to `resources/decals_pbs/` which is
    gitignored, so Wargaming pixels never enter the repo.
  * idempotent -- skips already-extracted PNGs unless `--force`.
  * usable from CLI (this script as __main__) AND from the
    viewer startup path (`ensure_runtime_decals(...)`).

Run from the repo root:

  python cust_tools/extract_wot_shellhole_decals.py
  python cust_tools/extract_wot_shellhole_decals.py --force
  python cust_tools/extract_wot_shellhole_decals.py --pkg-dir \
       C:/Games/World_of_Tanks_NA/res/packages

Author: Coffee + Claude, 2026-05-14.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import zipfile

try:
    from PIL import Image
except ImportError:
    Image = None


# Match `maps/decals_pbs/PBS_ShellHole_<token>_(AM|NM|GMM).dds`.
# Per Coffee 2026-05-15 (full-PBR decals): broadened from
# albedo-only to the full PBR triplet so the runtime decal
# projector can do normal-map perturbation + Cook-Torrance
# specular instead of flat alpha-blended sampling.
# Case-insensitive so a future capitalisation tweak by WG doesn't
# break us silently.
_DECAL_RE = re.compile(
    r'^maps/decals_pbs/PBS_ShellHole_(.+?)_(AM|NM|GMM)\.dds$',
    re.IGNORECASE)

# Filename glob applied to candidate pkgs in pkg_dir.  Empty
# string would match every pkg which is wasteful (60+ map pkgs
# we don't care about); the regex below filters to a known good
# set the user has actually observed shellholes in.  We still
# scan EVERY name in the matched pkgs -- the filter only
# eliminates obviously-irrelevant pkgs (numbered map pkgs that
# don't carry shellholes go straight to the skip pile).
_PKG_NAME_RE = re.compile(
    r'^(?:shared_content(?:_sandbox)?-part\d+|'
    r'\d+_graf_zeppelin_scc|'
    r'\d+_mannerheim_line)\.pkg$',
    re.IGNORECASE)


# ---------------------------------------------------------------------------
def _scan_pkg_for_decals(pkg_path):
    """Open one pkg and return [(internal_name, basename_without_ext)]
    for every shellhole AM/NM/GMM file inside.  Empty list if none.
    """
    out = []
    try:
        with zipfile.ZipFile(pkg_path) as zf:
            for name in zf.namelist():
                m = _DECAL_RE.match(name)
                if m:
                    base = (f'PBS_ShellHole_{m.group(1)}'
                            f'_{m.group(2).upper()}')
                    out.append((name, base))
    except (zipfile.BadZipFile, FileNotFoundError, OSError) as exc:
        # Silent on bad pkgs -- the user might have a partial mirror.
        # Log at high verbosity for the CLI; the runtime wrapper
        # suppresses everything.
        if os.environ.get('TEPY_DEBUG_DECAL_EXTRACT'):
            print(f"[shellhole-extract] skip {pkg_path}: {exc}")
    return out


def _locate_pkg_dir(extra_pkg_paths=()):
    """Find the user's WoT pkg directory.  Looks at, in order:

      * `extra_pkg_paths` (typically the user's configured pkg_dir
        from `tankExporterPy.json`)
      * common Windows install locations

    Returns the first existing directory, or None if nothing
    matches.  Same probe order as `extract_wot_fire_atlas._locate_pkg`
    so a single configured `pkg_dir` covers both extractors.
    """
    candidates = list(extra_pkg_paths)
    candidates.extend([
        r'C:\Games\World_of_Tanks_NA\res\packages',
        r'C:\Games\World_of_Tanks\res\packages',
        r'C:\Games\World_of_Tanks_RU\res\packages',
        r'C:\Games\World_of_Tanks_EU\res\packages',
    ])
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    return None


def _scan_pkg_dir(pkg_dir):
    """Walk pkg_dir for shellhole-bearing pkgs.  Returns a dict
    mapping `<basename>` (no extension) -> (pkg_path, internal_name)
    for every UNIQUE shellhole found.  When the same basename
    appears in multiple pkgs (rare; usually means a map pkg
    overrides a shared pkg) we keep the FIRST one found, scanned
    in alphabetical order so the result is deterministic across
    runs.
    """
    found = {}
    try:
        names = sorted(os.listdir(pkg_dir))
    except OSError:
        return found
    for fname in names:
        if not _PKG_NAME_RE.match(fname):
            continue
        full = os.path.join(pkg_dir, fname)
        for internal, base in _scan_pkg_for_decals(full):
            if base not in found:
                found[base] = (full, internal)
    return found


# ---------------------------------------------------------------------------
def _decode_dds_to_png(dds_bytes, png_path):
    """Decode a DXT5 DDS blob and write it as a 32-bit RGBA PNG.

    Pillow's DDS plugin handles BC1 / BC2 / BC3 (DXT1/3/5) out of
    the box -- the shellhole AM files are BC3 (DXT5) per the
    header `fourcc` field.  PNG keeps the image lossless +
    16x bigger than the DDS, which is fine for one-time-per-
    install scratch; the runtime memory cost is the same either
    way (GL uncompresses BC3 on upload too).
    """
    if Image is None:
        raise RuntimeError(
            "Pillow required to decode DDS (pip install pillow)")
    import io
    img = Image.open(io.BytesIO(dds_bytes))
    img = img.convert('RGBA')
    # Force RGBA in case the DDS decoder returned an indexed /
    # palette mode.  PNG-save accepts both but the runtime
    # FlipbookTexture / GL upload path expects 4-channel pixels.
    img.save(png_path, 'PNG', optimize=False)


def extract_one(pkg_path, internal_name, dest_path):
    """Extract one shellhole DDS from `pkg_path` -> `dest_path`
    PNG.  Returns True on success, False on any failure (logged
    at high verbosity).  Skips silently if `dest_path` already
    exists -- caller should pre-clear if it wants a fresh
    extract.
    """
    if os.path.isfile(dest_path):
        return True
    try:
        with zipfile.ZipFile(pkg_path) as zf:
            with zf.open(internal_name) as src:
                blob = src.read()
        _decode_dds_to_png(blob, dest_path)
        return True
    except Exception as exc:
        print(f"[shellhole-extract] {os.path.basename(dest_path)}: "
              f"{exc}")
        return False


def ensure_runtime_decals(resources_dir, pkg_dir=None,
                          extra_pkg_paths=(), force=False):
    """Make sure `resources/decals_pbs/` is populated with PNG
    shellhole albedos.  Called from `Viewer.__init__` at startup
    so first-launch already has decals available without the
    user having to run the CLI manually.

    Args:
        resources_dir (str)    : project resources/ folder
        pkg_dir       (str|None): explicit pkg directory (skips probe)
        extra_pkg_paths (iter) : extra search paths (typically the
                                  user's configured pkg_dir)
        force         (bool)   : re-extract every shellhole even
                                  when the PNG already exists

    Returns:
        dict: {basename: png_path} of files written this call.
              Empty when nothing needed doing or no pkg was found.
              Never raises.
    """
    out_dir = os.path.join(resources_dir, 'decals_pbs')
    os.makedirs(out_dir, exist_ok=True)
    pdir = pkg_dir if (pkg_dir and os.path.isdir(pkg_dir)) \
        else _locate_pkg_dir(extra_pkg_paths)
    if not pdir:
        print("[ensure_runtime_decals] no WoT pkg dir found -- "
              "shellhole decals unavailable until pkg_dir is set")
        return {}

    found = _scan_pkg_dir(pdir)
    if not found:
        print("[ensure_runtime_decals] no shellhole AMs in pkg dir "
              f"({pdir}) -- skipping")
        return {}

    written = {}
    for base, (pkg_path, internal) in sorted(found.items()):
        dest = os.path.join(out_dir, base + '.png')
        if force and os.path.isfile(dest):
            try:
                os.remove(dest)
            except OSError:
                pass
        if extract_one(pkg_path, internal, dest):
            written[base] = dest

    if written:
        new_writes = [b for b, p in written.items()
                       if os.path.getsize(p) > 0]
        print(f"[ensure_runtime_decals] {len(new_writes)} shellhole "
              f"PNGs in {out_dir}")
    return written


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resources', default='resources',
                        help='resources/ folder (default: ./resources)')
    parser.add_argument('--pkg-dir', default=None,
                        help='WoT res/packages dir (autodetected if omitted)')
    parser.add_argument('--force', action='store_true',
                        help='re-extract even when output PNG already exists')
    args = parser.parse_args()

    written = ensure_runtime_decals(
        args.resources, pkg_dir=args.pkg_dir, force=args.force)
    if not written:
        print("nothing extracted")
        return 1
    for base, path in sorted(written.items()):
        print(f"  {base}  ->  {path}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
