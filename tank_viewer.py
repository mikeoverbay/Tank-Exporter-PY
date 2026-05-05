#!/usr/bin/env python3
"""
Tank Mesh Viewer — entry point.

Usage:
    python tank_viewer.py <file> [options]

Positional:
    file                  .primitives_processed or vehicle .xml to load

Options:
    --pkg-dir  <path>     Path to WoT res/packages/ folder (saved to config)
    --res-mods <path>     Path to res_mods/<version>/ folder (saved to config)

    Supplied paths are written to tankviewer.json so you only need to set
    them once; subsequent runs use them automatically.

Controls:
    Right-click drag  — orbit camera
    Middle-click drag — pan camera
    Mouse scroll      — zoom in / out
    W                 — toggle wireframe
    N                 — toggle normal map
    R                 — reset camera
    ESC               — quit

All application logic lives in the tankviewer/ package.
"""

import argparse
import os
import sys


def main():
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
        help='Path to WoT res/packages/ folder (persisted to tankviewer.json)',
    )
    parser.add_argument(
        '--res-mods',
        metavar='DIR',
        default=None,
        help='Path to res_mods/<version>/ folder (persisted to tankviewer.json)',
    )
    parser.add_argument(
        '--lookup-xml',
        metavar='FILE',
        default=None,
        help='Path to TheItemList.xml pkg lookup file (persisted to tankviewer.json)',
    )
    args = parser.parse_args()

    if args.filepath is not None and not os.path.exists(args.filepath):
        print(f"Error: file not found: {args.filepath}")
        sys.exit(1)

    # Load config and apply any CLI overrides (then persist)
    from tankviewer import config
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

    from tankviewer.viewer import Viewer
    Viewer(args.filepath, cfg).run()


if __name__ == '__main__':
    main()
