"""Diagnostic plot of the Quest-spider leg geometry + IK bones.

Builds a `Spider` with default parameters, runs the IK at several
body Y offsets (rest, crouched, mid-air apex), and renders:

  - panel A: side view (YZ) of one representative leg through
    the jump cycle, showing hip / knee / mid / foot positions
    and segment lines.
  - panel B: top-down view (XZ) of all 4 legs at rest, marking
    the body sphere and hip-cluster at the north pole.
  - panel C: per-leg segment lengths + 2-link IK split
    (L_A = L1, L_B = L2 + L3) annotated.

Output: math_images/spider_legs.png.
"""

from __future__ import annotations

import math
import os
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle

THIS_DIR     = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(THIS_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tankExporterPy.spider import Spider


def main():
    # No GL context -- skip the VAO uploads.
    spider = Spider(world_pos=(0.0, 0.0, 0.0), build_gl=False)

    # Five poses across the new behaviour cycle:
    #   (label, body_y_off, foot_xz_scale, foot_y_off, anchored,
    #    color)
    poses = [
        ("stand",    *Spider.POSE_STAND,  True,  "C0"),
        ("crouched", *Spider.POSE_CROUCH, True,  "C3"),
        ("apex",     +1.5, 1.0, -1.5, False, "C2"),
        ("sleep",    *Spider.POSE_SLEEP,  False, "C4"),
        ("squat",    *Spider.POSE_SQUAT,  True,  "C1"),
    ]

    fig = plt.figure(figsize=(14, 10))
    axA = fig.add_subplot(2, 2, 1)     # YZ side
    axB = fig.add_subplot(2, 2, 2)     # XZ top-down
    axC = fig.add_subplot(2, 1, 2)     # per-leg lengths table

    LEG_IDX = 0   # which leg to feature in panel A

    # ---- Panel A: side view (Y-up, Z right) of leg 0 across poses ----
    for label, body_y, fxz, fy, anchored, color in poses:
        spider._update_leg_ik(body_y, fxz, fy, anchored)
        hip, knee, mid, foot = spider._legs[LEG_IDX]
        zs = [hip[2], knee[2], mid[2], foot[2]]
        # Express in WORLD coords (= add body_y to hip + knee + mid
        # since they're spider-local and the body has moved by
        # body_y).  Foot's Y already accounts for body offset via
        # the anchored branch in IK.
        ys = [hip[1] + body_y,
              knee[1] + body_y,
              mid[1] + body_y,
              foot[1] + body_y]
        axA.plot(zs, ys, '-o', color=color,
                  label=f"{label}  Δy={body_y:+.2f}  fxz={fxz:.2f}",
                  markersize=6, linewidth=2)
        # Annotate joints
        for j_label, jz, jy in zip(("hip", "knee", "mid", "foot"),
                                     zs, ys):
            axA.annotate(j_label, (jz, jy), textcoords="offset points",
                          xytext=(4, 4), fontsize=8, color=color)
    # Body sphere outline (rest)
    axA.add_patch(Circle((0, 0), spider.BODY_RADIUS,
                          fill=False, edgecolor="0.3", lw=1.5,
                          label="body sphere"))
    axA.axhline(spider._legs_rest[LEG_IDX][3][1],
                 color="k", lw=0.5, ls="--", alpha=0.4,
                 label="ground")
    axA.set_aspect("equal", adjustable="box")
    axA.set_xlabel("Z (chord/forward) [m]")
    axA.set_ylabel("Y (up) [m]")
    axA.set_title(f"Side view (leg {LEG_IDX}) — IK across jump poses")
    axA.legend(fontsize=8, loc="upper left")
    axA.grid(True, alpha=0.3)

    # ---- Panel B: top-down view (XZ) at rest ----
    # Body sphere top-down
    axB.add_patch(Circle((0, 0), spider.BODY_RADIUS,
                          fill=False, edgecolor="0.3", lw=1.5))
    spider._update_leg_ik(*Spider.POSE_STAND, True)
    for i, (hip, knee, mid, foot) in enumerate(spider._legs):
        xs = [hip[0], knee[0], mid[0], foot[0]]
        zs = [hip[2], knee[2], mid[2], foot[2]]
        axB.plot(xs, zs, '-o', color=f"C{i}", markersize=5,
                  linewidth=1.5, label=f"leg {i}")
        # mark foot with bigger marker
        axB.plot(foot[0], foot[2], 's', color=f"C{i}", markersize=10,
                  alpha=0.5)
    axB.set_aspect("equal", adjustable="box")
    axB.set_xlabel("X [m]")
    axB.set_ylabel("Z [m]")
    axB.set_title("Top-down view (XZ) at rest — 4 legs from above")
    axB.legend(fontsize=8, loc="upper right")
    axB.grid(True, alpha=0.3)

    # ---- Panel C: lengths + IK structure table ----
    axC.axis("off")
    spider._update_leg_ik(*Spider.POSE_STAND, True)
    text_lines = []
    text_lines.append("Per-leg geometry (spider-local meters)")
    text_lines.append("=" * 78)
    header = (f"{'leg':<4} "
              f"{'L1 (hip→knee)':>14} "
              f"{'L2 (knee→mid)':>14} "
              f"{'L3 (mid→foot)':>14} "
              f"{'|L|':>8} "
              f"{'L_A (IK)':>9} "
              f"{'L_B (IK)':>9}")
    text_lines.append(header)
    text_lines.append("-" * 78)
    for i, (L1, L2, L3) in enumerate(spider._leg_seg_lens):
        Ltot = L1 + L2 + L3
        # 2-link IK split used in `_update_leg_ik`:
        #   bone A = L1 (hip→knee), bone B = L2 + L3 (knee→foot via mid)
        text_lines.append(
            f"{i:<4} "
            f"{L1:>14.4f} "
            f"{L2:>14.4f} "
            f"{L3:>14.4f} "
            f"{Ltot:>8.4f} "
            f"{L1:>9.4f} "
            f"{L2+L3:>9.4f}")
    text_lines.append("")
    text_lines.append(
        "2-link IK per leg:  knee = hip + proj·along + h·perp_out")
    text_lines.append(
        "  along    = (foot - hip).normalized()")
    text_lines.append(
        "  perp_out = leg's XZ outward dir, orthogonalised vs along")
    text_lines.append(
        "  proj     = (L_A² + d² − L_B²) / (2 d)        (cosine rule)")
    text_lines.append(
        "  h        = sqrt(L_A² − proj²)")
    text_lines.append(
        "  mid      = knee + (foot − knee).unit · L2   (L2 preserved)")
    text_lines.append("")
    text_lines.append(
        "Overstretch (d > L_A + L_B): foot pulls back along ray to "
        "|hip-foot| = L_A + L_B.")
    axC.text(0.02, 0.98, "\n".join(text_lines),
              family="monospace", fontsize=9, va="top")

    out_path = os.path.join(PROJECT_ROOT, "math_images",
                              "spider_legs.png")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    print(f"[out] {out_path}")


if __name__ == "__main__":
    main()
