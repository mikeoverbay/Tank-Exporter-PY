"""
Extract and disassemble compiled WoT shaders.

A WoT shader on disk is layered:

    res/packages/shaders.pkg                       <- standard ZIP
        shaders/std_effects/PBS_tiled_atlas.11.dx11.fxo  <- another ZIP
            depends N        : tiny BWXML blobs naming the .fxh
                               source files the shader was built from
            effect           : "ARIEDX11" container holding 1+ DXBC
                               blobs (vertex / pixel / hull / domain
                               / geometry / compute stages)
            hash             : 32-byte content hash
            version          : "wgFX compiler X.Y.Z" text

Each DXBC blob is the standard DirectX bytecode container:

    'DXBC' + md5(16) + version(4) + totalSize(4) + chunkCount(4)
        + chunkOffsets[chunkCount]
        chunks: RDEF (resource bindings), ISGN (input sig),
                OSGN (output sig), SHDR (bytecode), STAT (stats)

This script unwraps both layers, writes each DXBC blob to disk, and
optionally invokes fxc.exe / dxc.exe to produce a human-readable
disassembly listing (HLSL-equivalent assembly + reflection metadata).

Usage:
    python tools/extract_shader.py <shader_name>            # DX11
    python tools/extract_shader.py PBS_tiled_atlas
    python tools/extract_shader.py shaders/std_effects/PBS_tiled_atlas
    python tools/extract_shader.py --list                   # browse
    python tools/extract_shader.py --dependencies <name>    # show .fxh
                                                              source list
    python tools/extract_shader.py --no-disasm <name>       # raw .dxbc
                                                              only
    python tools/extract_shader.py --pkg <path> <name>      # alt install

Output goes to extracted_shaders/<shader_name>/ next to this script's
project root.  Each shader is split into shader_00.dxbc through
shader_NN.dxbc plus a parallel set of .disasm.txt files when fxc is
on $PATH.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import struct
import subprocess
import sys
import zipfile
from typing import Iterable


# ---------------------------------------------------------------------------
# Defaults / discovery

DEFAULT_PKG_CANDIDATES = [
    r'C:\Games\World_of_Tanks_NA\res\packages\shaders.pkg',
    r'C:\Games\World_of_Tanks_EU\res\packages\shaders.pkg',
    r'C:\Games\World_of_Tanks\res\packages\shaders.pkg',
]

# Prefer the newest Windows SDK we can find.  Either fxc.exe or dxc.exe
# accepts -dumpbin -Fc on a DXBC file; we use the one we find.
DISASM_CANDIDATES = [
    r'C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\fxc.exe',
    r'C:\Program Files (x86)\Windows Kits\10\bin\10.0.22621.0\x64\fxc.exe',
    r'C:\Program Files (x86)\Windows Kits\10\bin\10.0.19041.0\x64\fxc.exe',
    r'C:\Program Files (x86)\Windows Kits\10\bin\x64\fxc.exe',
    r'C:\Program Files (x86)\Microsoft DirectX SDK (June 2010)\Utilities\bin\x86\fxc.exe',
]

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT_ROOT = os.path.join(PROJECT_ROOT, 'extracted_shaders')


def find_pkg(explicit_path: str | None) -> str:
    if explicit_path:
        if not os.path.isfile(explicit_path):
            raise SystemExit(f'pkg not found: {explicit_path}')
        return explicit_path
    for c in DEFAULT_PKG_CANDIDATES:
        if os.path.isfile(c):
            return c
    raise SystemExit(
        'shaders.pkg not found.  Pass --pkg <path>.  Tried:\n'
        + '\n'.join('  ' + c for c in DEFAULT_PKG_CANDIDATES))


def find_disassembler() -> str | None:
    for c in DISASM_CANDIDATES:
        if os.path.isfile(c):
            return c
    return None


# ---------------------------------------------------------------------------
# Shader-name resolution

def resolve_shader_path(z: zipfile.ZipFile, name: str) -> list[str]:
    """Return the matching `.fxo` entries inside the pkg for a user-supplied
    shader name.  Accepts:

        PBS_tiled_atlas               -- bare stem, both 10 and 11 returned
        PBS_tiled_atlas.11            -- specific shader-model variant
        shaders/std_effects/PBS_tiled_atlas
                                       -- path prefix without .NN.dx11.fxo
        shaders/std_effects/PBS_tiled_atlas.11.dx11.fxo
                                       -- exact match
    """
    name = name.strip()
    all_fxo = [n for n in z.namelist() if n.endswith('.fxo')]
    # Exact match wins
    if name in all_fxo:
        return [name]
    # Path-prefixed lookup
    if '/' in name:
        prefix = name
        # Strip a trailing .fxo if any
        if prefix.endswith('.fxo'):
            return [prefix]
        return sorted(n for n in all_fxo
                      if n.startswith(prefix + '.') or n == prefix + '.fxo')
    # Stem match -- "PBS_tiled_atlas" or "PBS_tiled_atlas.11"
    pat = re.compile(r'(?:^|/)' + re.escape(name) + r'(?:\.\d+)?\.dx11\.fxo$')
    return sorted(n for n in all_fxo if pat.search(n))


# ---------------------------------------------------------------------------
# .fxo unwrap

def unwrap_fxo(blob: bytes) -> dict[str, bytes]:
    """The `.fxo` is itself a ZIP.  Returns {filename: data} of all the
    inner entries (effect, depends N, hash, version)."""
    out: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for info in z.infolist():
            out[info.filename] = z.read(info.filename)
    return out


def parse_dependency(blob: bytes) -> tuple[str, str]:
    """Each `depends N` entry is a tiny BWXML-style blob with a leading
    'eN.B' magic (0x65 0x4e 0xa1 0x42) followed by:

        <filename> NUL [pad NULs] <hash-string> NUL [pad NULs]

    Some entries embed extra padding nulls between the filename and
    hash, and some have only the filename.  Skip any run of nulls
    between the two strings so we recover the real hash even when
    its position varies.
    """
    if len(blob) < 8 or blob[:4] != b'\x65\x4e\xa1\x42':
        return ('?', '?')
    p = 4
    end1 = blob.find(b'\x00', p)
    if end1 < 0:
        return ('?', '?')
    name = blob[p:end1].decode('latin-1', errors='replace')
    # Skip any run of pad nulls before the hash starts
    p = end1 + 1
    while p < len(blob) and blob[p] == 0:
        p += 1
    end2 = blob.find(b'\x00', p)
    if end2 < 0:
        end2 = len(blob)
    h = blob[p:end2].decode('latin-1', errors='replace')
    # Hash is 32 hex chars (an MD5).  Anything longer is the next
    # padding region bleeding in -- truncate to be safe.
    if len(h) > 32 and re.match(r'^[0-9A-F]{32}', h):
        h = h[:32]
    return (name, h)


# ---------------------------------------------------------------------------
# DXBC extraction from the `effect` blob

def iter_dxbc(buf: bytes) -> Iterable[tuple[int, bytes]]:
    """Yield (source_offset, dxbc_blob) for every DXBC container in `buf`.

    The "ARIEDX11" wrapper around them is a custom WG container we
    don't fully decode -- but we don't need to: each embedded DXBC is
    self-describing (header carries totalSize), so a magic-string
    scan + size-aware extraction works.
    """
    pos = 0
    while True:
        i = buf.find(b'DXBC', pos)
        if i < 0:
            return
        if i + 32 > len(buf):
            return
        version, total_size, chunk_count = struct.unpack_from(
            '<III', buf, i + 20)
        ok = (version == 1
              and 100 < total_size < (len(buf) - i + 1)
              and 0 < chunk_count < 30)
        if ok:
            yield (i, buf[i:i + total_size])
            pos = i + total_size
        else:
            pos = i + 1


# ---------------------------------------------------------------------------
# Stage classification (vertex / pixel / etc.) from the SHDR chunk

_STAGE_TAGS = {
    0xFFFE: 'vs',   # vertex
    0xFFFF: 'ps',   # pixel
    0x4853: 'ps',   # 'SH' newer
    0x4753: 'gs',   # geometry
    0x4853: 'cs',
    0x4453: 'ds',   # domain
    0x4853: 'hs',   # hull (overlap is fine -- best-effort)
}


def classify_stage(dxbc: bytes) -> str:
    """Return 'vs' / 'ps' / 'gs' / 'hs' / 'ds' / 'cs' / 'unk'.

    The first 16 bits of the SHDR chunk's body encode the shader type
    in the top byte (D3D's DXBC layout: bit 31..16 = type, 15..0 =
    minor.major version).  We sniff the chunk table and read it.

    Returns a filename-safe string -- never embeds the raw type code
    because some invalid SHDR headers produce values that would
    contain '?' or other path-illegal characters.
    """
    if len(dxbc) < 32 or dxbc[:4] != b'DXBC':
        return 'unk'
    chunk_count = struct.unpack_from('<I', dxbc, 28)[0]
    if not (0 < chunk_count < 30):
        return 'unk'
    chunk_offsets = struct.unpack_from(f'<{chunk_count}I', dxbc, 32)
    for co in chunk_offsets:
        tag = dxbc[co:co + 4]
        if tag in (b'SHDR', b'SHEX'):
            # body[0..3] = version dword: bits 31..16 type, 15..0 ver
            ver = struct.unpack_from('<I', dxbc, co + 8)[0]
            t = (ver >> 16) & 0xffff
            # Two known encodings: D3D9-style (0xFFFE/0xFFFF in the
            # type field) and the SM5 style where SHDR/SHEX uses 0
            # for vs and 1..5 for the other stages.  Cover both.
            tag_d3d = {0xFFFE: 'vs', 0xFFFF: 'ps',
                       0x4753: 'gs', 0x4853: 'hs',
                       0x4453: 'ds', 0x4353: 'cs'}.get(t)
            if tag_d3d:
                return tag_d3d
            tag_sm5 = {0: 'ps', 1: 'vs', 2: 'gs',
                       3: 'hs', 4: 'ds', 5: 'cs'}.get(t)
            if tag_sm5:
                return tag_sm5
            return 'unk'
    return 'unk'


# ---------------------------------------------------------------------------
# Driver

def process_one_fxo(z: zipfile.ZipFile, fxo_path: str, out_root: str,
                    disassembler: str | None,
                    show_dependencies: bool) -> None:
    print(f'\n=== {fxo_path} ===')
    fxo_blob = z.read(fxo_path)
    inner = unwrap_fxo(fxo_blob)
    print(f'  outer .fxo: {len(fxo_blob)} bytes, {len(inner)} inner entries')

    # Source dependencies (the .fxh files this shader was compiled from)
    deps = sorted(((k, parse_dependency(v))
                   for k, v in inner.items()
                   if k.startswith('depends ')),
                  key=lambda kv: int(kv[0].split()[1]))
    if show_dependencies:
        print('  Source dependencies (.fxh files):')
        for _key, (nm, _h) in deps:
            print(f'    {nm}')

    eff = inner.get('effect')
    if not eff:
        print('  WARNING: no `effect` entry inside .fxo -- nothing to extract')
        return

    # Per-shader output directory under extracted_shaders/.  Normalise
    # the in-pkg path to use the platform separator before joining so
    # mixed-slash paths don't trip the OS open() call on Windows.
    rel = fxo_path.replace('\\', '/').replace('/', os.sep)
    if rel.endswith('.dx11.fxo'):
        rel = rel[:-len('.dx11.fxo')]
    if rel.endswith('.fxo'):
        rel = rel[:-4]
    out_dir = os.path.join(out_root, rel)
    os.makedirs(out_dir, exist_ok=True)

    # Save the dependency list to disk too -- handy as a record of which
    # source files we'd want to recover if we ever need to rebuild the
    # shader from scratch.
    with open(os.path.join(out_dir, 'dependencies.txt'), 'w',
              encoding='utf-8') as f:
        f.write('# Source .fxh dependencies (build-time only -- not\n'
                '# shipped in the WoT pkg)\n')
        for _key, (nm, h) in deps:
            f.write(f'{nm:60s} {h}\n')

    # Extract every DXBC blob
    extracted = []
    for idx, (at, blob) in enumerate(iter_dxbc(eff)):
        stage = classify_stage(blob)
        fname = f'shader_{idx:02d}_{stage}.dxbc'
        full  = os.path.join(out_dir, fname)
        with open(full, 'wb') as f:
            f.write(blob)
        extracted.append((fname, full, stage, len(blob), at))

    print(f'  extracted {len(extracted)} DXBC blob(s) to {out_dir}')
    for fname, _full, stage, sz, at in extracted:
        print(f'    {fname:32s}  stage={stage}  '
              f'{sz} bytes  src offset 0x{at:x}')

    # Optional disassembly via fxc.exe
    if disassembler is None:
        print('  (no disassembler found -- raw .dxbc only.  '
              'install Windows SDK for fxc.exe.)')
        return

    for fname, full, _stage, _sz, _at in extracted:
        out_txt = full[:-len('.dxbc')] + '.disasm.txt'
        try:
            r = subprocess.run(
                [disassembler, '-dumpbin', '-nologo', '-Fc', out_txt, full],
                check=False, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                print(f'    disasm -> {os.path.basename(out_txt)}')
            else:
                err = (r.stderr or r.stdout or '').strip().splitlines()[:2]
                print(f'    disasm FAIL on {fname}: {" / ".join(err)}')
        except Exception as exc:
            print(f'    disasm CRASH on {fname}: {exc}')


def list_shaders(z: zipfile.ZipFile, filter_substring: str | None) -> None:
    fxo = sorted(n for n in z.namelist() if n.endswith('.fxo'))
    if filter_substring:
        f = filter_substring.lower()
        fxo = [n for n in fxo if f in n.lower()]
    for n in fxo:
        print(n)
    print(f'\n{len(fxo)} entries')


# ---------------------------------------------------------------------------
# CLI

def main():
    ap = argparse.ArgumentParser(
        description='Extract & disassemble WoT compiled shaders.')
    ap.add_argument('shader', nargs='?',
                    help='Shader stem (e.g. PBS_tiled_atlas) or full path '
                         'inside shaders.pkg.  Required unless --list.')
    ap.add_argument('--pkg', default=None,
                    help='Path to shaders.pkg (auto-detected by default)')
    ap.add_argument('--out', default=DEFAULT_OUT_ROOT,
                    help=f'Output root (default: {DEFAULT_OUT_ROOT})')
    ap.add_argument('--list', action='store_true',
                    help='List every shader in the pkg.  Combine with '
                         'a positional substring to filter.')
    ap.add_argument('--dependencies', action='store_true',
                    help='Also print the .fxh source-file dependency list.')
    ap.add_argument('--no-disasm', action='store_true',
                    help='Only write raw .dxbc blobs; skip fxc disassembly.')
    args = ap.parse_args()

    pkg_path = find_pkg(args.pkg)
    print(f'pkg: {pkg_path}')

    with zipfile.ZipFile(pkg_path) as z:
        if args.list:
            list_shaders(z, args.shader)
            return

        if not args.shader:
            ap.error('shader name required (or pass --list to browse)')

        matches = resolve_shader_path(z, args.shader)
        if not matches:
            print(f'No shader matched "{args.shader}".  '
                  f'Try: --list "{args.shader}"')
            sys.exit(1)

        disassembler = None if args.no_disasm else find_disassembler()
        if disassembler:
            print(f'disasm: {disassembler}')
        elif not args.no_disasm:
            print('disasm: (none -- only raw .dxbc will be written)')

        for fxo_path in matches:
            process_one_fxo(z, fxo_path, args.out,
                            disassembler, args.dependencies)


if __name__ == '__main__':
    main()
