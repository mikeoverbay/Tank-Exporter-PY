"""Rebuild TheItemList.xml from scratch by scanning every kept pkg.

The runtime lookup table (`tankExporterPy/../TheItemList.xml`) is normally
grown incrementally -- `PkgExtractor._persist_entry` adds rows when
scan-fallback discovers a new file.  That means the table only ever
contains files we've LOOKED UP, so freshly-needed file kinds (e.g.
`scripts/item_defs/vehicles/<nation>/*.xml` tank_defs we just started
needing) never make it in until we've manually triggered a load that
forces them.

This tool does the opposite: it enumerates every member of every kept
pkg in `<wot>/res/packages` and writes a comprehensive XML.  Result is
a complete index of every game asset we COULD ever look up, so the
runtime hits the O(1) dict path on first try instead of falling back
to a multi-pkg scan.

Usage
-----

    python cust_tools/rebuild_itemlist.py
        # default WoT install autodetected, default output is the
        # project's TheItemList.xml

    python cust_tools/rebuild_itemlist.py \
        --pkg-dir "C:\\Games\\World_of_Tanks_NA\\res\\packages" \
        --out    "C:\\experiment\\TheItemList.xml"

    python cust_tools/rebuild_itemlist.py --include-all
        # skip the _is_excluded_pkg filter and walk every pkg
        # (slower; produces a larger file; useful for diagnostic runs
        # when you want to confirm an entry IS in some excluded pkg).

    python cust_tools/rebuild_itemlist.py --extensions \
        primitives_processed visual_processed xml dds model
        # restrict to a comma- or space-separated extension whitelist
        # (anything else is dropped; massively shrinks the file when
        # you only care about geometry+textures+xml lookups).

The runtime parser is permissive about file size (`ET.parse` handles
any size), but a fully-comprehensive build can land near 100 MB.
The default invocation includes everything from non-excluded pkgs.

Atomic write: result lands at `<out>.tmp` first, then `os.replace` to
the final path so a half-written file never appears at the target.
"""

import argparse
import os
import sys
import time
import zipfile
from xml.sax.saxutils import escape as xml_escape

# Project root contains TheItemList.xml; tankExporterPy package contains
# the runtime PkgExtractor we borrow the exclusion filter from.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _PROJECT_ROOT)

from tankExporterPy.loaders import PkgExtractor    # noqa: E402


# ---------------------------------------------------------------------------
# Pkg discovery


def autodetect_pkg_dir():
    """Try the common WoT install layouts in order; return the first
    `res/packages` folder that exists."""
    candidates = [
        r'C:\Games\World_of_Tanks_NA\res\packages',
        r'C:\Games\World_of_Tanks_EU\res\packages',
        r'C:\Games\World_of_Tanks_RU\res\packages',
        r'C:\Games\World_of_Tanks\res\packages',
    ]
    for cand in candidates:
        if os.path.isdir(cand):
            return cand
    return None


def list_pkgs(pkg_dir, include_all=False):
    """Return the list of pkgs we'll scan, sorted.  Same filter the
    runtime uses unless `include_all` is True."""
    out = []
    for fn in sorted(os.listdir(pkg_dir)):
        if not fn.lower().endswith('.pkg'):
            continue
        if not include_all and PkgExtractor._is_excluded_pkg(fn):
            continue
        out.append(os.path.join(pkg_dir, fn))
    return out


# ---------------------------------------------------------------------------
# Index build


def build_index(pkgs, allowed_exts=None, verbose=False):
    """Walk every pkg, accumulate {zip_path -> pkg_basename}.

    Last-write-wins on collisions so the result matches what the
    runtime sees in `_file_to_pkg` after `_load_lookup` reads the
    rebuilt file.

    Args:
        pkgs (list[str]): absolute paths of the .pkg files to scan.
        allowed_exts (set[str] | None): when not None, only entries
            whose extension (lowercased, no leading dot) is in this
            set are kept.  None means "no filter, keep everything".
        verbose (bool): per-pkg progress logging.

    Returns:
        (entries_dict, stats_dict) -- entries is the flat lookup
        table; stats covers timing / counts for the summary log.
    """
    entries = {}
    stats = {
        'pkgs_total':   len(pkgs),
        'pkgs_skipped': 0,
        'pkgs_scanned': 0,
        'entries_seen': 0,
        'entries_kept': 0,
        'duplicates':   0,
        'started':      time.perf_counter(),
    }

    for pkg_path in pkgs:
        basename = os.path.basename(pkg_path)
        try:
            zf = zipfile.ZipFile(pkg_path, 'r')
        except Exception as exc:
            stats['pkgs_skipped'] += 1
            if verbose:
                print(f"  [skip] {basename}: {exc}")
            continue
        names = zf.namelist()
        zf.close()

        kept_here = 0
        for name in names:
            stats['entries_seen'] += 1
            if allowed_exts is not None:
                # Extension test on the lowercased file name; pkgs use
                # forward slashes so basename-after-/ is enough.
                ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
                if ext not in allowed_exts:
                    continue
            if name in entries:
                stats['duplicates'] += 1
            entries[name] = basename
            stats['entries_kept'] += 1
            kept_here += 1

        stats['pkgs_scanned'] += 1
        if verbose:
            print(f"  {basename:<40} {len(names):>7} entries  "
                  f"({kept_here} kept)")

    stats['elapsed_s'] = time.perf_counter() - stats['started']
    return entries, stats


# ---------------------------------------------------------------------------
# Output


def write_itemlist(entries, out_path, root_tag='FileList'):
    """Write `entries` to `out_path` atomically.  Stable sort on
    filename so two rebuilds produce the same file (handy for `git
    diff` of a generated artefact).
    """
    tmp_path = out_path + '.tmp'
    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    sorted_keys = sorted(entries.keys())
    # Stream straight to disk -- a 100-MB string in memory before write
    # is fine on a modern machine but unnecessary, and streaming makes
    # progress visible at the OS level.
    with open(tmp_path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('<?xml version="1.0" standalone="yes"?>\n')
        fh.write(f'<{root_tag}>\n')
        for fn in sorted_keys:
            pkg = entries[fn]
            fh.write('  <items>\n'
                     f'    <filename>{xml_escape(fn)}</filename>\n'
                     f'    <package>{xml_escape(pkg)}</package>\n'
                     '  </items>\n')
        fh.write(f'</{root_tag}>\n')

    os.replace(tmp_path, out_path)


# ---------------------------------------------------------------------------
# CLI


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--pkg-dir', default=None,
                   help="Path to <wot>/res/packages.  Autodetected when omitted.")
    p.add_argument('--out',     default=None,
                   help="Output path for TheItemList.xml.  "
                        "Defaults to the project root.")
    p.add_argument('--include-all', action='store_true',
                   help="Skip the _is_excluded_pkg filter (include map / "
                        "event / audio bundles).  Slower; bigger output.")
    p.add_argument('--extensions', nargs='*', default=None,
                   help="Optional whitelist of extensions to include "
                        "(no leading dot, case-insensitive).  Default: all.")
    p.add_argument('--quiet', action='store_true',
                   help="Suppress per-pkg progress output.")
    args = p.parse_args()

    pkg_dir = args.pkg_dir or autodetect_pkg_dir()
    if not pkg_dir or not os.path.isdir(pkg_dir):
        sys.exit(f"pkg dir not found: {pkg_dir}\n"
                 f"  pass --pkg-dir explicitly")

    out_path = args.out or os.path.join(_PROJECT_ROOT, 'TheItemList.xml')

    allowed_exts = None
    if args.extensions:
        # Accept both space-separated and comma-separated values
        flat = []
        for tok in args.extensions:
            flat.extend(t.strip().lower().lstrip('.')
                        for t in tok.split(','))
        allowed_exts = {t for t in flat if t}

    print(f"pkg_dir   : {pkg_dir}")
    print(f"output    : {out_path}")
    print(f"filter    : "
          f"{'no _is_excluded_pkg' if args.include_all else 'standard (_is_excluded_pkg)'}")
    print(f"extensions: "
          f"{','.join(sorted(allowed_exts)) if allowed_exts else 'all'}")
    print()

    pkgs = list_pkgs(pkg_dir, include_all=args.include_all)
    print(f"scanning {len(pkgs)} pkg archive(s)...")
    if not args.quiet:
        print()

    entries, stats = build_index(pkgs, allowed_exts=allowed_exts,
                                 verbose=not args.quiet)

    print()
    print(f"  pkgs scanned   : {stats['pkgs_scanned']}/{stats['pkgs_total']}"
          f"  ({stats['pkgs_skipped']} unreadable)")
    print(f"  entries seen   : {stats['entries_seen']:,}")
    print(f"  entries kept   : {stats['entries_kept']:,}")
    print(f"  unique files   : {len(entries):,}"
          f"  ({stats['duplicates']:,} cross-pkg collisions, last-wins)")
    print(f"  scan time      : {stats['elapsed_s']:.2f} s")
    print()

    print(f"writing {out_path} ...")
    write_t0 = time.perf_counter()
    write_itemlist(entries, out_path)
    write_s = time.perf_counter() - write_t0
    size_mb = os.path.getsize(out_path) / (1024.0 * 1024.0)
    print(f"  -> {size_mb:.1f} MB  in {write_s:.2f} s")


if __name__ == '__main__':
    main()
