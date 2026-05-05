"""Compare section tables of an on-disk .primitives_processed against
its source counterpart inside a pkg.

Walks the res_mods/.../<part>.primitives_processed files we wrote, finds the
matching original entry in the WoT packages folder, and prints the
section table side by side.

Usage:
    python compare_sections.py <res_mods_tank_dir> <wot_packages_root>

Example:
    python compare_sections.py \
        "C:\\Games\\World_of_Tanks_NA\\res_mods\\2.2.1.2\\vehicles\\german\\G102_Pz_III" \
        "C:\\Games\\World_of_Tanks_NA\\res\\packages"
"""

import os
import sys
import struct
import zipfile


def read_section_table(data):
    """Walk the trailing offset and return a list of (size, name)."""
    file_len = len(data)
    if file_len < 8:
        return []
    table_offset = struct.unpack('<I', data[-4:])[0]
    pos = file_len - 4 - table_offset
    out = []
    while pos < file_len - 4:
        if pos + 24 > file_len - 4:
            break
        size = struct.unpack('<I', data[pos:pos + 4])[0]
        pos += 4 + 16
        nlen = struct.unpack('<I', data[pos:pos + 4])[0]
        pos += 4
        nm = data[pos:pos + nlen].decode('ascii', errors='replace')
        pos += nlen + ((-pos - nlen) & 3)
        out.append((size, nm))
        if len(out) > 200:
            break
    return out


def index_packages(pkg_root):
    """Build {internal_path -> (pkg_path, internal_path)} for every
    .primitives_processed in every .pkg under `pkg_root`.  Skips map
    pkgs (numeric-prefixed) since they don't carry tank assets."""
    idx = {}
    if not os.path.isdir(pkg_root):
        return idx
    for name in sorted(os.listdir(pkg_root)):
        if not name.endswith('.pkg'):
            continue
        path = os.path.join(pkg_root, name)
        try:
            with zipfile.ZipFile(path) as zf:
                for entry in zf.namelist():
                    if entry.endswith('.primitives_processed'):
                        idx.setdefault(entry, (path, entry))
        except Exception:
            pass
    return idx


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    res_dir, pkg_root = sys.argv[1], sys.argv[2]

    pkg_idx = index_packages(pkg_root)

    # Collect res_mods primitives, derive each one's pkg-relative path
    for root, _, files in os.walk(res_dir):
        for f in files:
            if not f.endswith('.primitives_processed'):
                continue
            local_path = os.path.join(root, f)
            # Derive the pkg-relative path: everything after 'vehicles/'
            rel = local_path.replace('\\', '/')
            if 'vehicles/' not in rel:
                continue
            pkg_internal = 'vehicles/' + rel.split('vehicles/', 1)[1]

            print(f"\n{'=' * 70}")
            print(f"part: {pkg_internal}")
            print('=' * 70)

            with open(local_path, 'rb') as fh:
                ours = fh.read()
            ours_table = read_section_table(ours)

            # Match against pkg
            src_entry = pkg_idx.get(pkg_internal)
            if src_entry is None:
                print("  (no matching entry in any .pkg)")
                continue
            src_pkg, src_internal = src_entry
            with zipfile.ZipFile(src_pkg) as zf:
                src = zf.read(src_internal)
            src_table = read_section_table(src)

            print(f"  ORIGINAL ({len(src):>10} bytes, {os.path.basename(src_pkg)})")
            print(f"  OURS     ({len(ours):>10} bytes)")
            print(f"  {'name':<40} {'orig size':>10}  {'our size':>10}  diff")
            names = [nm for _sz, nm in src_table]
            our_by_name = dict(((nm, sz) for sz, nm in ours_table))
            for sz, nm in src_table:
                ours_sz = our_by_name.get(nm)
                if ours_sz is None:
                    print(f"  {nm:<40} {sz:>10}  {'(missing)':>10}")
                else:
                    diff = ours_sz - sz
                    flag = '' if diff == 0 else f' ({diff:+d})'
                    print(f"  {nm:<40} {sz:>10}  {ours_sz:>10}{flag}")
            extra = [nm for sz, nm in ours_table if nm not in names]
            if extra:
                print(f"  EXTRA in our file: {extra}")


if __name__ == '__main__':
    main()
