"""Compile every `.po` file under `tankExporterPy/locale/` to `.mo`.

`.po` files are human-editable plain text; `.mo` files are the
binary catalog format Python's `gettext` module reads at runtime.
Translators edit `.po`; the runtime needs `.mo`.  This tool keeps
both in sync without depending on the system's `msgfmt` binary
(which isn't always installed on Windows).

Format of `.mo` (GNU gettext, little-endian):

    uint32 magic        = 0x950412de
    uint32 revision     = 0
    uint32 N            = number of strings
    uint32 O_msgids     = offset of msgid table
    uint32 O_msgstrs    = offset of msgstr table
    uint32 hash_size    = 0  (we skip the hash table entirely;
                              it's optional, gettext falls back
                              to linear scan)
    uint32 hash_offset  = 0

    msgid table  : N entries of (length, offset)  -> 8 bytes each
    msgstr table : N entries of (length, offset)  -> 8 bytes each
    msgid blob   : null-terminated UTF-8 strings, sorted ASCII-ascending
    msgstr blob  : matching translations

Usage
-----
    python cust_tools/build_locale_mo.py
        # walks tankExporterPy/locale/, compiles every .po next
        # to its sibling .mo

    python cust_tools/build_locale_mo.py path/to/file.po
        # compiles a single file

The compiler is forgiving: empty msgstrs map to empty strings
(gettext echoes the msgid in that case, which is what we want as
the English fallback).  Multi-line `msgid "..."  "..."` pairs are
joined.  `\\n`, `\\t`, `\\"`, `\\\\` are decoded; everything else
is left as-is.
"""

import os
import re
import struct
import sys


_HERE         = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
_LOCALE_ROOT  = os.path.join(_PROJECT_ROOT, 'tankExporterPy', 'locale')

_MO_MAGIC = 0x950412de


# ---------------------------------------------------------------------------

def _decode_po_string(raw):
    """Decode a `.po` string-literal segment (without the surrounding quotes).

    Handles `\\n`, `\\t`, `\\r`, `\\"`, `\\\\`.  Other backslash
    sequences pass through unchanged (rare in our content).
    """
    out = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == '\\' and i + 1 < len(raw):
            nxt = raw[i + 1]
            if   nxt == 'n' : out.append('\n'); i += 2
            elif nxt == 't' : out.append('\t'); i += 2
            elif nxt == 'r' : out.append('\r'); i += 2
            elif nxt == '"' : out.append('"');  i += 2
            elif nxt == '\\': out.append('\\'); i += 2
            else:
                out.append(c); i += 1
        else:
            out.append(c); i += 1
    return ''.join(out)


def parse_po(path):
    """Parse a `.po` file -> list of (msgid, msgstr) tuples.

    Skips entries whose msgstr is empty (gettext would echo the
    msgid for those anyway, and including them just bloats the
    `.mo`).  The header entry (msgid "") is preserved if present
    -- it carries Content-Type / Plural-Forms metadata that
    Python's `gettext` reads.
    """
    entries = []
    msgid   = None
    msgstr  = None
    last    = None     # 'msgid' or 'msgstr' -- which line type the
                       # last `"..."` continuation belongs to.

    with open(path, 'r', encoding='utf-8') as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith('#'):
                # End of an entry; flush.
                if msgid is not None and msgstr is not None:
                    if msgid == '' or msgstr != '':
                        entries.append((msgid, msgstr))
                    msgid = msgstr = last = None
                continue

            m = re.match(r'^msgid\s+"(.*)"$', line)
            if m:
                # Flush previous entry first
                if msgid is not None and msgstr is not None:
                    if msgid == '' or msgstr != '':
                        entries.append((msgid, msgstr))
                msgid  = _decode_po_string(m.group(1))
                msgstr = None
                last   = 'msgid'
                continue

            m = re.match(r'^msgstr\s+"(.*)"$', line)
            if m:
                msgstr = _decode_po_string(m.group(1))
                last   = 'msgstr'
                continue

            m = re.match(r'^"(.*)"$', line)
            if m:
                # Continuation line: append to whichever section is open.
                seg = _decode_po_string(m.group(1))
                if last == 'msgid' and msgid is not None:
                    msgid += seg
                elif last == 'msgstr' and msgstr is not None:
                    msgstr += seg
                continue

    # Final flush at EOF
    if msgid is not None and msgstr is not None:
        if msgid == '' or msgstr != '':
            entries.append((msgid, msgstr))

    return entries


def compile_mo(entries, dest):
    """Write `entries` (list of (msgid, msgstr)) as a `.mo` file."""
    # Sort by msgid for the binary catalog (gettext requires it).
    entries = sorted(entries, key=lambda kv: kv[0].encode('utf-8'))

    n        = len(entries)
    keys     = [k.encode('utf-8') for k, _v in entries]
    values   = [v.encode('utf-8') for _k, v in entries]

    header_size       = 7 * 4   # magic, rev, N, O_msgids, O_msgstrs, h_size, h_off
    table_size        = n * 8   # msgid table (length+offset per entry)
    msgid_table_off   = header_size
    msgstr_table_off  = msgid_table_off + table_size
    body_offset       = msgstr_table_off + table_size

    # Layout the string blobs and remember (length, offset) for each.
    msgid_meta  = []
    msgstr_meta = []
    blob = bytearray()

    cursor = body_offset
    for k in keys:
        msgid_meta.append((len(k), cursor))
        blob.extend(k); blob.append(0)
        cursor += len(k) + 1
    for v in values:
        msgstr_meta.append((len(v), cursor))
        blob.extend(v); blob.append(0)
        cursor += len(v) + 1

    out = bytearray()
    # Header
    out.extend(struct.pack(
        '<IIIIIII',
        _MO_MAGIC, 0, n,
        msgid_table_off, msgstr_table_off,
        0, 0))   # no hash table

    # Msgid table
    for length, offset in msgid_meta:
        out.extend(struct.pack('<II', length, offset))
    # Msgstr table
    for length, offset in msgstr_meta:
        out.extend(struct.pack('<II', length, offset))
    # Blob
    out.extend(blob)

    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    tmp = dest + '.tmp'
    with open(tmp, 'wb') as fh:
        fh.write(out)
    os.replace(tmp, dest)


# ---------------------------------------------------------------------------

def compile_one(po_path):
    """Compile one .po -> sibling .mo (same name, .mo extension)."""
    entries = parse_po(po_path)
    mo_path = os.path.splitext(po_path)[0] + '.mo'
    compile_mo(entries, mo_path)
    return mo_path, len(entries)


def compile_tree(root=None):
    """Walk `root` (default: tankExporterPy/locale/) and compile every .po."""
    if root is None:
        root = _LOCALE_ROOT
    if not os.path.isdir(root):
        print(f"locale root not found: {root}")
        return 0
    n_files = 0
    for cur, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.endswith('.po'):
                continue
            po_path = os.path.join(cur, fn)
            try:
                mo_path, n_entries = compile_one(po_path)
            except Exception as exc:
                print(f"  FAIL  {po_path}: {exc}")
                continue
            rel = os.path.relpath(mo_path, root)
            print(f"  ok    {rel}  ({n_entries} entries)")
            n_files += 1
    print(f"-- compiled {n_files} catalog(s)")
    return n_files


def main():
    if len(sys.argv) > 1:
        path = sys.argv[1]
        if os.path.isfile(path) and path.endswith('.po'):
            mo, n = compile_one(path)
            print(f"  ok    {mo}  ({n} entries)")
        else:
            sys.exit(f"argument must be a .po file path: {path}")
    else:
        compile_tree()


if __name__ == '__main__':
    main()
