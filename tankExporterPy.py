#!/usr/bin/env python3
"""
TEPY (Tank Exporter PY) — entry point.

Usage:
    python tankExporterPy.py <file> [options]

Positional:
    file                  .primitives_processed or vehicle .xml to load

Options:
    --pkg-dir  <path>     Path to WoT res/packages/ folder (saved to config)
    --res-mods <path>     Path to res_mods/<version>/ folder (saved to config)

    Supplied paths are written to tankExporterPy.json so you only need to set
    them once; subsequent runs use them automatically.

Controls:
    Right-click drag  — orbit camera
    Middle-click drag — pan camera
    Mouse scroll      — zoom in / out
    W                 — toggle wireframe
    N                 — toggle normal map
    R                 — reset camera
    ESC               — quit

All application logic lives in the tankExporterPy/ package.

History: this file was previously named `tank_viewer.py`; renamed to
`tankExporterPy.py` so the entry-point matches the GitHub repo name
(`Tank-Exporter-PY`) and user-facing brand (TEPY).  Both go.bat and
launch_skip_deps.bat (formerly start.bat -- v1.67.3) invoke the new
name; older docs referencing `tank_viewer.py` are stale.
"""

import argparse
import os
import sys


def main():
    # Minimise the cmd window we were launched from before pygame
    # creates the GL window -- otherwise the user briefly sees both
    # stacked, which reads as "did something go wrong".  Stdout
    # still goes to the console; click its taskbar entry to bring
    # it back if you want to read the startup log.  No-op on
    # non-Windows / pythonw.exe / IDE runs (no attached console).
    try:
        from tankExporterPy.win_console import minimize_console
        minimize_console()
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description='WoT Tank Mesh Viewer',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        'filepath',
        nargs='?',
        default=None,
        help='Optional .primitives_processed or vehicle .xml file to load. '
             'Omit to start with an empty scene and pick a tank from the '
             'tree panel.',
    )
    parser.add_argument(
        '--pkg-dir',
        metavar='DIR',
        default=None,
        help='Path to WoT res/packages/ folder (persisted to tankExporterPy.json)',
    )
    parser.add_argument(
        '--res-mods',
        metavar='DIR',
        default=None,
        help='Path to res_mods/<version>/ folder (persisted to tankExporterPy.json)',
    )
    parser.add_argument(
        '--lookup-xml',
        metavar='FILE',
        default=None,
        help='Path to TheItemList.xml pkg lookup file (persisted to tankExporterPy.json)',
    )
    args = parser.parse_args()

    if args.filepath is not None and not os.path.exists(args.filepath):
        print(f"Error: file not found: {args.filepath}")
        sys.exit(1)

    # Load config and apply any CLI overrides (then persist)
    from tankExporterPy import config
    cfg = config.load()
    changed = False

    if args.pkg_dir is not None:
        cfg['pkg_dir'] = os.path.abspath(args.pkg_dir)
        changed = True
    if args.res_mods is not None:
        cfg['res_mods'] = os.path.abspath(args.res_mods)
        changed = True
    if args.lookup_xml is not None:
        cfg['lookup_xml'] = os.path.abspath(args.lookup_xml)
        changed = True

    if changed:
        config.save(cfg)

    # Print active paths so the user knows what's being used
    print(f"[config] settings : {config.config_path()}")
    print(f"[config] pkg_dir  : {cfg['pkg_dir']  or '(auto-detect)'}")
    print(f"[config] res_mods : {cfg['res_mods'] or '(auto-detect)'}")

    from tankExporterPy.viewer import Viewer
    Viewer(args.filepath, cfg).run()


if __name__ == '__main__':
    main()
