"""
Bump tankExporterPy/__init__.py's __version__ in place.

The version follows the project's own convention (NOT strict SemVer):
    MAJOR.MINOR.PATCH
    | 1st: incompatible / breaking change
    | 2nd: every "major shit" addition (new button, new dialog,
    |      new file-format support, new tool, new perf feature)
    | 3rd: bug fix only

Usage:
    python cust_tools/bump_version.py minor   # 1.14.0  -> 1.15.0
    python cust_tools/bump_version.py patch   # 1.14.0  -> 1.14.1
    python cust_tools/bump_version.py major   # 1.14.0  -> 2.0.0

Also accepts:
    python cust_tools/bump_version.py --show              # print current version
    python cust_tools/bump_version.py --set 1.20.3        # set explicitly
    python cust_tools/bump_version.py minor --dry-run     # preview without writing

Lower-digit values reset to 0 on a higher-digit bump (semver-style):
    1.14.7  --> minor --> 1.15.0
    1.14.7  --> major --> 2.0.0
"""

from __future__ import annotations

import argparse
import os
import re
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INIT_PATH    = os.path.join(PROJECT_ROOT, 'tankExporterPy', '__init__.py')

# Match  __version__ = "MAJOR.MINOR.PATCH"  (single OR double quotes,
# whitespace-tolerant).  Captures the three integers individually so we
# can reconstruct the line preserving the original quoting style.
_VERSION_RE = re.compile(
    r'(?P<prefix>__version__\s*=\s*)'
    r'(?P<quote>["\'])'
    r'(?P<maj>\d+)\.(?P<min>\d+)\.(?P<patch>\d+)'
    r'(?P=quote)',
    re.MULTILINE,
)


def read_version():
    """Return ((maj, min, patch), full_file_text, match_object).

    Raises SystemExit on parse failure -- this script is not useful
    when it can't find the version line.
    """
    if not os.path.isfile(INIT_PATH):
        raise SystemExit(f"tankExporterPy/__init__.py not found at {INIT_PATH}")
    with open(INIT_PATH, 'r', encoding='utf-8') as fh:
        text = fh.read()
    m = _VERSION_RE.search(text)
    if m is None:
        raise SystemExit(f"could not find __version__ line in {INIT_PATH}")
    return (
        (int(m.group('maj')), int(m.group('min')), int(m.group('patch'))),
        text,
        m,
    )


def write_version(text, match, new_triple):
    """Splice the new (maj, min, patch) into `text` at the location of
    `match` and write the file back.  Preserves the surrounding quote
    style and the rest of the file verbatim.
    """
    maj, mn, patch = new_triple
    new_line = (f"{match.group('prefix')}"
                f"{match.group('quote')}{maj}.{mn}.{patch}{match.group('quote')}")
    new_text = text[:match.start()] + new_line + text[match.end():]
    with open(INIT_PATH, 'w', encoding='utf-8') as fh:
        fh.write(new_text)


def bump(triple, kind):
    """Apply one of 'major' / 'minor' / 'patch' to a (maj, mn, patch)
    triple and return the new triple.  Lower digits reset to 0 on a
    higher-digit bump.
    """
    maj, mn, patch = triple
    if kind == 'major':
        return (maj + 1, 0, 0)
    if kind == 'minor':
        return (maj, mn + 1, 0)
    if kind == 'patch':
        return (maj, mn, patch + 1)
    raise ValueError(f"unknown bump kind: {kind}")


def parse_explicit(s):
    """Parse a 'X.Y.Z' string the user passed via --set."""
    m = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', s.strip())
    if not m:
        raise SystemExit(f"--set value must be MAJOR.MINOR.PATCH, got: {s!r}")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def fmt(triple):
    return f"{triple[0]}.{triple[1]}.{triple[2]}"


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('kind', nargs='?', choices=('major', 'minor', 'patch'),
                   help="which digit to bump (default: minor)")
    p.add_argument('--show', action='store_true',
                   help='print current version and exit')
    p.add_argument('--set', dest='set_to', metavar='X.Y.Z',
                   help='set the version to an explicit X.Y.Z value')
    p.add_argument('--dry-run', action='store_true',
                   help="show what would change but don't write the file")
    args = p.parse_args()

    cur, text, m = read_version()

    if args.show:
        print(fmt(cur))
        return 0

    if args.set_to:
        new_triple = parse_explicit(args.set_to)
    else:
        kind = args.kind or 'minor'
        new_triple = bump(cur, kind)

    if new_triple == cur:
        print(f"version unchanged: {fmt(cur)}")
        return 0

    print(f"{fmt(cur)}  ->  {fmt(new_triple)}")
    if args.dry_run:
        print("(dry-run; not writing the file)")
        return 0

    write_version(text, m, new_triple)
    print(f"wrote {INIT_PATH}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
