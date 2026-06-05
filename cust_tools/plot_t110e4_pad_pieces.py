"""Plot T110E4 pad pieces (segment1 + segment2) in YZ to figure out
the offset semantics.

Loads `vehicles/american/A83_T110E4/track/segment{1,2}.primitives_processed`
via the project's MeshParser, reads `segmentOffset` and
`segment2Offset` from the per-tank vehicle XML, and draws each
piece in the YZ plane:

  - mesh in its native (un-shifted) coordinates
  - mesh shifted along Z by its XML offset
  - world origin marker

Per the v1.231.x discussions:
  * `segmentLength`     = 0.172
  * `segmentOffset`     = 0.258
  * `segment2Offset`    = 0.09
  * BigWorld native frame is `-Z = forward`; the runtime
    renderer flips Z so `+Z = forward`.  We plot with Z flipped
    (renderer convention) but also flag where the un-flipped
    positions land for sanity.

Usage:
    python cust_tools/plot_t110e4_pad_pieces.py
                                     [--wot C:/Games/World_of_Tanks_NA]

Output: PNG at math_images/t110e4_pad_pieces.png .
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Make `tankExporterPy` importable when this script is run from
# either checkout root.
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(THIS_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tankExporterPy.loaders import (
    PkgExtractor, MeshParser, decode_bwxml, is_bwxml)


T110E4_DIR    = "vehicles/american/A83_T110E4"
SEG1_REL_PATH = f"{T110E4_DIR}/track/segment1.primitives_processed"
SEG2_REL_PATH = f"{T110E4_DIR}/track/segment2.primitives_processed"
TANK_XML_REL  = "scripts/item_defs/vehicles/usa/A83_T110E4.xml"


def _extract_text_xml(pkg, rel_path):
    """Pull a vehicle / chassis XML out of a pkg, decode if BWXML."""
    local = pkg.extract(rel_path)
    if not local or not os.path.isfile(local):
        return None
    with open(local, "rb") as fh:
        raw = fh.read()
    if is_bwxml(raw):
        return decode_bwxml(raw)
    return raw.decode("utf-8", errors="replace")


def _read_offsets_from_xml(pkg):
    """Pull segmentLength / segmentOffset / segment2Offset out of
    the A83_T110E4 vehicle def XML.  Falls back to known T110E4
    values when the field is missing."""
    text = _extract_text_xml(pkg, TANK_XML_REL)
    seg_len = 0.172
    seg_off = 0.258
    seg2_off = 0.09
    if text:
        import re
        m_len = re.search(r"<segmentLength>([^<]+)</segmentLength>",
                          text)
        m_off = re.search(r"<segmentOffset>([^<]+)</segmentOffset>",
                          text)
        m_off2 = re.search(r"<segment2Offset>([^<]+)</segment2Offset>",
                           text)
        if m_len:
            seg_len = float(m_len.group(1).strip())
        if m_off:
            seg_off = float(m_off.group(1).strip())
        if m_off2:
            seg2_off = float(m_off2.group(1).strip())
    return seg_len, seg_off, seg2_off


def _load_mesh_yz(pkg, rel_path):
    """Extract & parse a primitives_processed; return (N, 3) positions
    array stacked across every primitive group inside.  Coordinates
    are returned in NATIVE BigWorld frame (no Z flip yet)."""
    local = pkg.extract(rel_path)
    if not local or not os.path.isfile(local):
        raise RuntimeError(f"PkgExtractor returned no file for {rel_path}")
    pgs = MeshParser.parse_primitives_processed(local)
    chunks = []
    for pg in pgs:
        v = pg["vertices"]
        pos = np.asarray(v["positions"], dtype=np.float32)
        if pos.size:
            chunks.append(pos)
    if not chunks:
        raise RuntimeError(f"No vertex positions found in {rel_path}")
    return np.concatenate(chunks, axis=0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--wot",
        default="C:/Games/World_of_Tanks_NA",
        help="WoT install root (contains res/packages/).")
    ap.add_argument(
        "--out",
        default=os.path.join(
            PROJECT_ROOT, "math_images", "t110e4_pad_pieces.png"),
        help="Output PNG path.")
    args = ap.parse_args()

    pkg = PkgExtractor(args.wot)

    seg_len, seg_off, seg2_off = _read_offsets_from_xml(pkg)
    print(f"[xml] segmentLength={seg_len}  "
          f"segmentOffset={seg_off}  segment2Offset={seg2_off}")

    seg1_pos = _load_mesh_yz(pkg, SEG1_REL_PATH)
    seg2_pos = _load_mesh_yz(pkg, SEG2_REL_PATH)
    print(f"[mesh] segment1 verts: {len(seg1_pos)}  "
          f"bbox YZ "
          f"y=[{seg1_pos[:,1].min():+.4f}, {seg1_pos[:,1].max():+.4f}]  "
          f"z=[{seg1_pos[:,2].min():+.4f}, {seg1_pos[:,2].max():+.4f}]")
    print(f"[mesh] segment2 verts: {len(seg2_pos)}  "
          f"bbox YZ "
          f"y=[{seg2_pos[:,1].min():+.4f}, {seg2_pos[:,1].max():+.4f}]  "
          f"z=[{seg2_pos[:,2].min():+.4f}, {seg2_pos[:,2].max():+.4f}]")

    # Renderer convention: +Z forward (flipped from BigWorld native
    # which is -Z forward).  We plot with Z flipped so "forward" is
    # to the right of the figure -- matches the side-view convention
    # used in math_images plots elsewhere.
    def yz(pos, dz=0.0):
        return pos[:, 1], -pos[:, 2] + dz

    fig, axes = plt.subplots(2, 2, figsize=(14, 12),
                              sharex=True, sharey=True)

    titles_and_offsets = [
        ("seg1 @ origin (no shift)",   seg1_pos,  0.0),
        ("seg2 @ origin (no shift)",   seg2_pos,  0.0),
        ("seg1 shifted +segmentOffset along forward (Z-flipped)",
                                       seg1_pos,  seg_off),
        ("seg2 shifted +segment2Offset along forward (Z-flipped)",
                                       seg2_pos,  seg2_off),
    ]

    for ax, (title, pos, dz) in zip(axes.flat, titles_and_offsets):
        ys, zs = yz(pos, dz=dz)
        ax.scatter(zs, ys, s=2, alpha=0.4)
        ax.scatter([0], [0], s=80, c="red", marker="x",
                   label="model world zero (0,0)", zorder=10)
        # Mark the offset shift target as well.
        if dz != 0.0:
            ax.axvline(dz, ls="--", lw=0.8, c="orange",
                       label=f"forward shift Δ = {dz}")
        ax.set_title(title)
        ax.set_xlabel("forward (renderer +Z, BW -Z) [m]")
        ax.set_ylabel("up (Y) [m]")
        ax.set_aspect("equal", adjustable="box")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best", fontsize=8)

    # 5th plot: BOTH pieces overlaid, each at its OWN offset.
    fig2, ax2 = plt.subplots(figsize=(11, 8))
    y1, z1 = yz(seg1_pos, dz=seg_off)
    y2, z2 = yz(seg2_pos, dz=seg2_off)
    ax2.scatter(z1, y1, s=3, alpha=0.45,
                label=f"segment1 shifted by segmentOffset={seg_off}",
                c="C0")
    ax2.scatter(z2, y2, s=3, alpha=0.45,
                label=f"segment2 shifted by segment2Offset={seg2_off}",
                c="C3")
    ax2.scatter([0], [0], s=120, c="red", marker="x",
                label="world zero (chain anchor)", zorder=10)
    ax2.axvline(seg_off, ls="--", lw=0.8, c="C0", alpha=0.6)
    ax2.axvline(seg2_off, ls="--", lw=0.8, c="C3", alpha=0.6)
    ax2.set_title("T110E4 pad pieces: each placed at its XML offset "
                  "(YZ, Z-flipped to renderer convention)")
    ax2.set_xlabel("forward (renderer +Z, BW -Z) [m]")
    ax2.set_ylabel("up (Y) [m]")
    ax2.set_aspect("equal", adjustable="datalim")
    ax2.grid(True, alpha=0.3)
    ax2.legend(loc="best", fontsize=10)

    out_path  = args.out
    out_path2 = out_path.replace(".png", "_overlay.png")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    fig2.savefig(out_path2, dpi=120, bbox_inches="tight")
    plt.close(fig)
    plt.close(fig2)
    print(f"[out] {out_path}")
    print(f"[out] {out_path2}")


if __name__ == "__main__":
    main()
