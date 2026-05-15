"""
Regenerate `resources/Cursor.png` -- the projected aim crosshair
reticle.

Per Coffee 2026-05-15 ("can you remake my curser file so the
headings initials are not upside down or backwards?"): the
original nuTerra reticle has S at top, N upside-down at bottom,
and E / W rotated 90 deg so they read from outside the ring.
This version places every letter in the standard compass
position AND right-side-up:

    N  -- top    (12 o'clock)
    E  -- right  (3 o'clock)
    S  -- bottom (6 o'clock)
    W  -- left   (9 o'clock)

Style preserved: double black-and-white outer ring, central
small ring, horizontal + vertical crosshair lines with tick
marks, green compass letters.  Transparent background.

Run:
    python cust_tools/make_cursor.py

Author: Coffee + Claude, 2026-05-15.
"""
from __future__ import annotations

import os
import sys

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:
    print(f"Pillow required: {exc}", file=sys.stderr)
    sys.exit(1)


# Image dimensions.  Square.  Match-ish the original (494x495).
SIZE = 512

# Colours.
TRANSPARENT = (0, 0, 0, 0)
BLACK       = (0, 0, 0, 255)
WHITE       = (255, 255, 255, 255)
GREEN       = (75, 255, 75, 255)


def _try_font(size):
    """Return a TrueType font at `size` px, or None if no font lookup
    works on this host.  Trying a couple of common Windows fonts then
    Pillow's default."""
    for name in ('arialbd.ttf', 'arial.ttf', 'segoeuib.ttf',
                 'DejaVuSans-Bold.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def build_cursor():
    """Construct and return the cursor PIL Image (RGBA)."""
    img = Image.new('RGBA', (SIZE, SIZE), TRANSPARENT)
    d   = ImageDraw.Draw(img)

    cx = cy = (SIZE - 1) / 2.0

    # ---- Outer double ring ----------------------------------------
    # Visual reads as a thick black band with a thin white inner ring
    # plus a thin white outer ring -- gives the reticle a strong
    # silhouette against any terrain colour.  Matches the original
    # nuTerra reticle's "double outline" feel.
    R_outer       = SIZE * 0.49
    R_outer_inner = SIZE * 0.46
    R_ring_inner  = SIZE * 0.435
    # White outer rim.
    d.ellipse(
        (cx - R_outer, cy - R_outer, cx + R_outer, cy + R_outer),
        outline=WHITE, width=4)
    # Black thick band.
    d.ellipse(
        (cx - R_outer_inner, cy - R_outer_inner,
         cx + R_outer_inner, cy + R_outer_inner),
        outline=BLACK, width=10)
    # White inner rim.
    d.ellipse(
        (cx - R_ring_inner, cy - R_ring_inner,
         cx + R_ring_inner, cy + R_ring_inner),
        outline=WHITE, width=4)

    # ---- Crosshair lines ------------------------------------------
    # Span from the inner edge of the centre circle out to the inner
    # rim of the band.  Drawn in black with a 6 px stroke.
    R_centre  = SIZE * 0.10
    R_arm_max = SIZE * 0.42
    arm_stroke = 6
    # Horizontal: x in [-R_arm_max, -R_centre] U [+R_centre, +R_arm_max]
    d.line(
        ((cx - R_arm_max, cy), (cx - R_centre, cy)),
        fill=BLACK, width=arm_stroke)
    d.line(
        ((cx + R_centre, cy), (cx + R_arm_max, cy)),
        fill=BLACK, width=arm_stroke)
    # Vertical.
    d.line(
        ((cx, cy - R_arm_max), (cx, cy - R_centre)),
        fill=BLACK, width=arm_stroke)
    d.line(
        ((cx, cy + R_centre), (cx, cy + R_arm_max)),
        fill=BLACK, width=arm_stroke)

    # ---- Tick marks -----------------------------------------------
    # 10 ticks per arm at uniform spacing along the line.  Tick
    # length grows slightly at every 5th tick so the eye can count
    # without straining.
    N_TICKS = 10
    tick_len_small = 12
    tick_len_big   = 22
    tick_stroke    = 4
    span = R_arm_max - R_centre
    for i in range(1, N_TICKS + 1):
        frac = i / N_TICKS
        r = R_centre + frac * span
        big = (i % 5 == 0)
        tl = tick_len_big if big else tick_len_small
        # Right arm: vertical tick.
        d.line(((cx + r, cy - tl), (cx + r, cy + tl)),
               fill=BLACK, width=tick_stroke)
        # Left arm.
        d.line(((cx - r, cy - tl), (cx - r, cy + tl)),
               fill=BLACK, width=tick_stroke)
        # Top arm: horizontal tick.
        d.line(((cx - tl, cy - r), (cx + tl, cy - r)),
               fill=BLACK, width=tick_stroke)
        # Bottom arm.
        d.line(((cx - tl, cy + r), (cx + tl, cy + r)),
               fill=BLACK, width=tick_stroke)

    # ---- Centre circle --------------------------------------------
    # Small ring with a centre dot so the user can see the EXACT aim
    # point through the ring.
    d.ellipse(
        (cx - R_centre, cy - R_centre,
         cx + R_centre, cy + R_centre),
        outline=BLACK, width=4)
    d.ellipse(
        (cx - 4, cy - 4, cx + 4, cy + 4),
        fill=BLACK)

    # ---- Compass letters ------------------------------------------
    # Placed just OUTSIDE the inner band on each axis.  All four
    # letters drawn at the canonical orientation (NOT rotated) so
    # they read right-side-up regardless of the reticle's on-screen
    # rotation.  The aim renderer is responsible for keeping the
    # reticle aligned -- see `AimCrosshair.rotation_z_deg`.
    letter_font_px = int(SIZE * 0.105)
    font = _try_font(letter_font_px)
    # Inset from the outer ring inward so letters don't overlap the
    # band.
    R_letter = SIZE * 0.395

    def _draw_letter(letter, world_pos):
        """Centre-anchor draw of `letter` at image-space `world_pos`."""
        x, y = world_pos
        try:
            bbox = d.textbbox((0, 0), letter, font=font)
            tw   = bbox[2] - bbox[0]
            th   = bbox[3] - bbox[1]
            ox   = bbox[0]
            oy   = bbox[1]
            d.text((x - tw / 2.0 - ox, y - th / 2.0 - oy),
                   letter, fill=GREEN, font=font)
        except Exception:
            # Fallback for environments where textbbox isn't
            # available -- approximate by font size.
            tw = th = letter_font_px
            d.text((x - tw / 2.0, y - th / 2.0),
                   letter, fill=GREEN, font=font)

    _draw_letter('N', (cx,             cy - R_letter))
    _draw_letter('E', (cx + R_letter,  cy))
    _draw_letter('S', (cx,             cy + R_letter))
    _draw_letter('W', (cx - R_letter,  cy))

    return img


def main():
    out_path = os.path.join('resources', 'Cursor.png')
    img = build_cursor()
    # Per Coffee 2026-05-15 ("the cursor needs up down flipped" +
    # "Just flip the image you made"): the screen-space decal
    # projector (which renders the cursor on terrain hits)
    # computes UVs from world-space reconstruction in the
    # fragment shader, so per-vertex UV transforms on the CPU
    # are ignored.  Easiest fix is to bake the up-down flip
    # into the source PNG itself.  After this flip:
    #
    #     N at bottom of image  -> appears at world +Z (forward)
    #     S at top of image     -> appears at world -Z (back)
    #     E at right            -> stays at world +X (right)
    #     W at left             -> stays at world -X (left)
    #
    # The decal projector's local +X = world +X and local +Z =
    # world +Z, so the screen-space `local.xz + 0.5` UV mapping
    # reads our (flipped) PNG correctly: bottom of image (N)
    # lands at world +Z which is "forward" in WoT convention.
    img = img.transpose(Image.FLIP_TOP_BOTTOM)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path, 'PNG')
    print(f"wrote {out_path}  ({SIZE}x{SIZE}, flipped vertically)")


if __name__ == '__main__':
    main()
