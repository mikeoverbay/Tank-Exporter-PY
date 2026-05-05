"""Dump the section table of a .primitives_processed file inside a pkg.

Usage:
    python cust_tools/dump_sections.py <pkg_path> <internal_path>
or
    python cust_tools/dump_sections.py <local_file_path>

Walks the trailing section-table-offset, then prints (size, name) for
every entry.  Used to verify the on-disk section names of an original
file vs. a freshly written one.
"""

import os
import sys
import struct
import zipfile


def dump(data, label):
    file_len = len(data)
    if file_len < 8:
        print(f"{label}: too small ({file_len} bytes)")
        return
    table_offset = struct.unpack('<I', data[-4:])[0]
    table_pos = file_len - 4 - table_offset
    print(f"{label}")
    print(f"  size={file_len}  section_table_pos={table_pos}")
    pos = table_pos
    i = 0
    while pos < file_len - 4:
        if pos + 24 > file_len - 4:
            print(f"  [trailing {file_len - 4 - pos} bytes of slack/pad]")
            break
        size = struct.unpack('<I', data[pos:pos + 4])[0]
        pos += 4 + 16
        nlen = struct.unpack('<I', data[pos:pos + 4])[0]
        pos += 4
        nm = data[pos:pos + nlen].decode('ascii', errors='replace')
        pos += nlen
        pad = (-pos) & 3
        pos += pad
        print(f"  [{i}] size={size:>10}  name={nm!r}")
        i += 1
        if i > 100:
            print("  (truncated at 100 entries)")
            break


def main():
    if len(sys.argv) == 2:
        with open(sys.argv[1], 'rb') as fh:
            dump(fh.read(), sys.argv[1])
        return
    if len(sys.argv) == 3:
        pkg, internal = sys.argv[1], sys.argv[2]
        with zipfile.ZipFile(pkg) as zf:
            data = zf.read(internal)
        dump(data, f"{pkg} -> {internal}")
        return
    sys.exit("usage: dump_sections.py <local_file> | <pkg> <internal_path>")


if __name__ == '__main__':
    main()
