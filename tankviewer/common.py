"""
Common utilities used across the tank viewer.

Functions:
    unpack_normal(packed)          : decode a 32-bit packed normal (10:10:10 format).
    unpack_normal_bpvt(packed)     : decode a BPVT-mode 32-bit packed normal (8:8:8 format).
    read_c_string(data, offset, n) : read null-terminated ASCII string from bytes.
    load_shader_file(path)         : read a shader source file relative to CWD or script.
    decode_bwxml(data)             : decode a BigWorld packed-XML binary to an XML string.
    is_bwxml(data)                 : return True if data starts with the BWXML magic.
"""

import io
import os
import struct
import xml.etree.ElementTree as ET

import numpy as np

# ──────────────────────────────────────────────────────────────────────────────
# BigWorld packed-XML decoder
# ──────────────────────────────────────────────────────────────────────────────
# Based on vis_main.vb / packed_section.vb from Tank Exporter.
#
# File layout (after the 4-byte magic 0x62A14E45):
#   1 byte  : padding / skip
#   N bytes : null-terminated string dictionary (key names), ends with empty string
#   element : recursive element block (see _bw_element below)
#
# Element block:
#   uint16  : number of child elements
#   uint32  : self data descriptor  (top 4 bits = type, low 28 bits = cumulative end)
#   N x (uint16 nameIdx + uint32 descriptor) : child descriptors
#   data    : self data, then children data, all contiguous; `end` values are
#             cumulative byte offsets within this data region.
#
# Data types:
#   0x0  sub-element  -> recurse
#   0x1  string       -> raw bytes
#   0x2  integer      -> 1/2/4/8 bytes, signed
#   0x3  floats       -> N x float32; 12 floats -> 4x3 matrix with <row0..3> children
#   0x4  boolean      -> length 0 = false, length 1 = read byte (must be 1) = true
#   0x5  base64 blob  -> raw bytes encoded as base64
# ──────────────────────────────────────────────────────────────────────────────

BWXML_MAGIC  = 0x62A14E45   # 'EN\xa1b' little-endian
BINARY_MAGIC = 0x42A14E65   # alternate header (not decodeable)


def is_bwxml(data: bytes) -> bool:
    """Return True if *data* begins with the BigWorld packed-XML magic."""
    return len(data) >= 4 and struct.unpack('<I', data[:4])[0] == BWXML_MAGIC


def decode_bwxml(data: bytes, root_tag: str = 'root') -> str:
    """Decode a BigWorld packed-XML binary blob and return an XML string.

    Args:
        data     : raw bytes of the packed file (magic header inclusive)
        root_tag : tag name for the synthetic root element (default 'root')

    Returns:
        UTF-8 XML string

    Raises:
        ValueError if the magic header is wrong.
    """
    if not is_bwxml(data):
        raise ValueError(f'Not BWXML (got {data[:4].hex() if len(data) >= 4 else repr(data)})')

    f = io.BytesIO(data)
    f.read(4)   # consume magic
    f.read(1)   # skip padding byte (matches reader.ReadSByte() in DecodePackedFile)

    # Read the string dictionary (null-terminated strings, ends with empty string)
    dictionary = []
    while True:
        s = _bw_read_cstr(f)
        if not s:
            break
        dictionary.append(s)

    root = ET.Element(root_tag)
    _bw_element(f, root, dictionary)
    return ET.tostring(root, encoding='unicode')


# ── internal helpers ──────────────────────────────────────────────────────────

def _bw_read_cstr(f: io.BytesIO) -> str:
    """Read a null-terminated Latin-1 string from *f*."""
    buf = bytearray()
    while True:
        b = f.read(1)
        if not b or b == b'\x00':
            break
        buf.extend(b)
    return buf.decode('latin-1')


def _bw_desc(val: int):
    """Split a 32-bit descriptor into (cumulative_end, data_type)."""
    return val & 0x0FFFFFFF, val >> 28


def _bw_element(f: io.BytesIO, el: ET.Element, dictionary: list):
    """Decode one element block into *el* (recursive)."""
    num_children = struct.unpack('<H', f.read(2))[0]
    self_end, self_type = _bw_desc(struct.unpack('<I', f.read(4))[0])

    # Collect child descriptors (nameIdx + descriptor each = 6 bytes)
    children = []
    for _ in range(num_children):
        name_idx        = struct.unpack('<H', f.read(2))[0]
        child_end, ctype = _bw_desc(struct.unpack('<I', f.read(4))[0])
        children.append((name_idx, child_end, ctype))

    # Read data: self first, then each child in order
    offset = _bw_data(f, el, dictionary, 0, self_end, self_type)
    for name_idx, child_end, ctype in children:
        tag  = dictionary[name_idx] if name_idx < len(dictionary) else f'unk{name_idx}'
        child = ET.SubElement(el, tag)
        offset = _bw_data(f, child, dictionary, offset, child_end, ctype)


def _bw_data(f: io.BytesIO, el: ET.Element, dictionary: list,
             offset: int, end: int, dtype: int) -> int:
    """Read `end - offset` bytes of typed data into *el*.  Returns new offset."""
    length = end - offset

    if dtype == 0x0:        # sub-element (recurse; length bytes are the sub-element)
        _bw_element(f, el, dictionary)

    elif dtype == 0x1:      # string
        if length > 0:
            el.text = f.read(length).decode('utf-8', errors='replace')

    elif dtype == 0x2:      # signed integer
        if length == 1:
            el.text = str(struct.unpack('b', f.read(1))[0])
        elif length == 2:
            el.text = str(struct.unpack('<h', f.read(2))[0])
        elif length == 4:
            el.text = str(struct.unpack('<i', f.read(4))[0])
        elif length == 8:
            el.text = str(struct.unpack('<q', f.read(8))[0])
        elif length > 0:
            f.read(length)

    elif dtype == 0x3:      # floats
        n = length // 4
        if n > 0:
            floats = struct.unpack(f'<{n}f', f.read(n * 4))
            if n == 12:     # 4x3 matrix -> row0..row3 child elements
                for ri in range(4):
                    row = ET.SubElement(el, f'row{ri}')
                    row.text = ' '.join(f'{floats[ri*3+ci]:.6f}' for ci in range(3))
            else:
                el.text = ' '.join(f'{v:.6f}' for v in floats)

    elif dtype == 0x4:      # boolean
        if length == 1:
            el.text = 'true' if f.read(1) == b'\x01' else 'false'
        else:
            el.text = 'false'

    elif dtype == 0x5:      # base64 blob
        import base64
        raw = f.read(length) if length > 0 else b''
        el.text = base64.b64encode(raw).decode('ascii')

    else:
        if length > 0:
            f.read(length)  # skip unknown type

    return end


def unpack_normal(packed):
    """Unpack a 32-bit packed normal to (x, y, z) using the 10:10:10 layout
    used by WoT's primitives_processed for non-BPVT vertices.

    Bit layout:
        X: bits 0-10  (10 bits, signed, /511)
        Y: bits 11-20 (10 bits, signed, /511)
        Z: bits 22-31 (10 bits, signed, /511)
    """
    pkx = packed & 0x7FF
    pky = (packed & 0x3FF800)
    pkz = (packed & 0xFFC00000)

    x = int(pkx) if pkx < 512 else int(pkx) - 1024
    y = int(pky >> 11) if (pky >> 11) < 512 else int(pky >> 11) - 1024
    z = int(pkz >> 22) if (pkz >> 22) < 512 else int(pkz >> 22) - 1024

    x /= 511.0
    y /= 511.0
    z /= 511.0

    length = np.sqrt(x*x + y*y + z*z)
    if length < 0.000001:
        length = 1.0
    return np.array([x / length, y / length, z / length], dtype=np.float32)


def unpack_normal_bpvt(packed):
    """Unpack BPVT-mode packed normal (8:8:8 format)."""
    bx = float(packed & 0xFF)
    by = float((packed >> 8) & 0xFF)
    bz = float((packed >> 16) & 0xFF)
    x = (bx / 127.5) - 1.0
    y = (by / 127.5) - 1.0
    z = (bz / 127.5) - 1.0
    length = np.sqrt(x*x + y*y + z*z)
    if length < 0.000001:
        length = 1.0
    return np.array([x / length, y / length, z / length], dtype=np.float32)


# ---------------------------------------------------------------------------
# Inverse pack functions -- used by the .primitives_processed writer
# (tankviewer/writers/primitives_writer.py).  Each pack_* is the
# inverse of the matching unpack_* above so a write -> read round-trip
# yields the same uint32 byte-for-byte (modulo the rounding step).

def pack_normal(x, y, z):
    """Inverse of unpack_normal: pack a unit vector into a 32-bit
    integer using the 10:10:10 signed-fixed-point layout.  Returns
    a Python int (uint32-shaped); caller writes it as '<I' bytes.

    Bit layout (matches unpack_normal):
        X: bits  0..10  (10-bit signed, /511)
        Y: bits 11..20  (10-bit signed, /511)
        Z: bits 22..31  (10-bit signed, /511)

    Each component is clamped to [-1, 1] then quantised to a signed
    10-bit value via round(c * 511), in [-511, +511].  Negative
    values are stored in two's-complement-on-1024 form -- the same
    form unpack_normal undoes via the `if pk < 512: pk - 1024` branch.
    """
    def _q(c):
        i = int(round(max(-1.0, min(1.0, float(c))) * 511.0))
        if i < -512:
            i = -512
        elif i > 511:
            i = 511
        return (i + 1024) & 0x3FF if i < 0 else i & 0x3FF
    ix = _q(x)
    iy = _q(y)
    iz = _q(z)
    return (ix | (iy << 11) | (iz << 22)) & 0xFFFFFFFF


def pack_normal_bpvt(x, y, z):
    """Pack a unit vector into a 32-bit integer using WoT's BPVT
    8:8:8 normal format (the one the legacy VB Tank Exporter writes
    via packnormalFBX888_writePrimitive_NEWMODEL + s_to_int).

    The format is NOT the simple `(c + 1) * 127.5` you'd expect.  It
    layers three operations (each component independent):

        signed   = round(-c * 127)        # negate + scale to int8
        byte     = signed & 0xFF          # two's-complement wrap to uint8
        stored   = byte XOR 127           # final on-disk byte

    XOR-127 flips the low 7 bits, leaving the sign bit alone; this
    means c=0 maps to byte 127 (NOT 128, which is what a naive
    midpoint pack would produce).  The matching unpack is
    `unpackNormal_8_8_8` in modPrimWriter.vb -- see
    VISUAL_PROCESSED_FORMAT.md for the round-trip discussion.

    Bit layout in the returned uint32:
        bits  0..7   x byte
        bits  8..15  y byte
        bits 16..23  z byte
        bits 24..31  reserved (left at 0)
    """
    def _q(c):
        signed = int(round(-max(-1.0, min(1.0, float(c))) * 127.0))
        if signed < -128:
            signed = -128
        elif signed > 127:
            signed = 127
        byte = signed & 0xFF       # two's-complement wrap
        return byte ^ 0x7F          # final XOR-127 step
    bx = _q(x)
    by = _q(y)
    bz = _q(z)
    return (bx | (by << 8) | (bz << 16)) & 0xFFFFFFFF


def read_c_string(data, offset, max_len=64):
    """Read a null-terminated ASCII string from `data` starting at `offset`,
    capped at `max_len` bytes. Stops at first null byte."""
    end = offset
    while end < min(offset + max_len, len(data)) and data[end] != 0:
        end += 1
    return data[offset:end].decode('ascii', errors='ignore')


def load_shader_file(path):
    """Read a shader source file from a relative path. Tries the current
    working directory first, then the directory containing the package."""
    if not os.path.exists(path):
        # Look next to the package itself
        pkg_dir = os.path.dirname(os.path.abspath(__file__))
        # Then try one level up (project root)
        project_dir = os.path.dirname(pkg_dir)
        for candidate in (os.path.join(pkg_dir, path), os.path.join(project_dir, path)):
            if os.path.exists(candidate):
                path = candidate
                break
    with open(path, 'r') as f:
        return f.read()
