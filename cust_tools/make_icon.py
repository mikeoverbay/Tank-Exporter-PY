"""Generate `resources/tepy.ico` -- the TEPY tepee window icon.

Procedurally renders a tepee (American Plains tipi) silhouette at
every size Windows expects in a multi-resolution .ico file:
    16x16, 24x24, 32x32, 48x48, 64x64, 128x128, 256x256

Run from the project root:

    python cust_tools/make_icon.py

Re-run after tweaking the constants below to regenerate the file.

Why a generator instead of a hand-painted .ico?
* Reproducible -- we can regenerate from this script alone, no
  binary asset to lose.
* Tweakable -- the colors below match the burnt-orange / dark-frame
  palette the rest of the UI uses; bump them in one place to
  re-skin every icon size in one go.
* No external dependency on an .ico authoring tool.

Pillow is the only requirement -- already in `requirements.txt` for
the runtime.
"""

import os
import math
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Palette -- match the in-app burnt-orange / dark-frame scheme

COLOR_HIDE_LIGHT  = (212, 116,  52, 255)   # body, sun-side
COLOR_HIDE_SHADE  = (158,  82,  32, 255)   # body, shadow side (right edge)
COLOR_POLE        = ( 50,  35,  25, 255)   # crossing poles + outline
COLOR_DOOR        = ( 28,  18,  12, 255)   # interior darkness through the door
COLOR_BAND        = (240, 195,  85, 255)   # decorative stripe -- only at 32px+
COLOR_OUTLINE     = ( 32,  20,  12, 255)   # crisp 1px silhouette outline

# Sizes the .ico will carry.  Windows picks the closest match for any
# given UI surface (taskbar, file-explorer, alt-tab, title-bar).
SIZES = (16, 24, 32, 48, 64, 128, 256)


# ---------------------------------------------------------------------------
# Drawing


def _draw_tepee(size):
    """Return an RGBA Image of a tepee at `size` x `size`.

    Layout (proportions are size-relative so every resolution looks
    consistent):
        * Outer 1/16 of every edge stays transparent for icon padding.
        * Body is an isosceles triangle from apex (just below the top
          padding) down to a base sitting one inset above the bottom.
        * Body is split vertically with a darker shade on the right
          third to suggest a single light source from the upper-left.
        * Two crossing poles peek up above the apex.
        * A small dark trapezoid door sits centred on the base.
        * Above ~32px we add a pale horizontal band a third of the
          way up (the painted-stripe motif on real tipis).
    """
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)

    pad = max(1, size // 16)

    # --- Triangular body --------------------------------------------------
    apex_y     = pad + max(1, size // 14)
    base_y     = size - pad - max(1, size // 16)
    half_base  = (size - 2 * pad) // 2 - max(1, size // 14)
    apex       = (size // 2,                  apex_y)
    base_left  = (size // 2 - half_base,      base_y)
    base_right = (size // 2 + half_base,      base_y)

    # Light-side: full triangle in the lighter hide tone.
    d.polygon([apex, base_right, base_left], fill=COLOR_HIDE_LIGHT)

    # Shade-side: paint the right-of-centre wedge a darker tone to
    # cheat a sun-from-upper-left feel on a flat icon.
    shade_split_x = size // 2 + max(1, size // 32)
    d.polygon(
        [apex, base_right, (shade_split_x, base_y)],
        fill=COLOR_HIDE_SHADE,
    )

    # --- Decorative stripe (only when there's room for it) ----------------
    # A real tipi often has a painted band a third of the way up the
    # body; below 32 px the band collapses to a hairline that just
    # muddies the silhouette, so we skip it.
    if size >= 32:
        stripe_y_centre = base_y - (base_y - apex_y) // 3
        stripe_h        = max(2, size // 24)
        stripe_top      = stripe_y_centre - stripe_h // 2
        stripe_bot      = stripe_y_centre + stripe_h // 2 + 1

        # Compute the body width at the stripe's vertical band by
        # interpolating between the apex and base on each side -- a
        # full-width rectangle would punch outside the silhouette.
        def _body_x_at(y, side):
            """Interpolate the triangle's left/right edge x at row y."""
            t = (y - apex_y) / float(base_y - apex_y)
            if side == 'left':
                return apex[0] + (base_left[0]  - apex[0]) * t
            return  apex[0] + (base_right[0] - apex[0]) * t

            # NOTE: `apex` is shared by both sides -- the only thing
            # changing per side is the base x; the t-blend handles it.

        sx0 = int(round(_body_x_at(stripe_y_centre, 'left')))  + 1
        sx1 = int(round(_body_x_at(stripe_y_centre, 'right'))) - 1
        if sx1 > sx0:
            d.rectangle([sx0, stripe_top, sx1, stripe_bot],
                        fill=COLOR_BAND)

    # --- Crossing poles peeking out at the top ---------------------------
    # Two short bars that cross above the apex.  The line width scales
    # with size so they stay visible at 16x16 (where everything is 1 px)
    # without dominating at 256x256.
    pole_w   = max(1, size // 32)
    pole_h   = max(2, size // 8)
    cross_cx = size // 2
    cross_cy = apex_y - pole_h // 2
    spread   = max(1, size // 18)

    d.line(
        [(cross_cx - spread, cross_cy - pole_h // 2),
         (cross_cx + spread, cross_cy + pole_h // 2 + 1)],
        fill=COLOR_POLE,
        width=pole_w,
    )
    d.line(
        [(cross_cx + spread, cross_cy - pole_h // 2),
         (cross_cx - spread, cross_cy + pole_h // 2 + 1)],
        fill=COLOR_POLE,
        width=pole_w,
    )

    # --- Doorway --------------------------------------------------------
    # Trapezoid: narrower at the top, full-width at the bottom.  Solid
    # near-black so it reads as the dark interior peeking through.
    door_h     = max(3, size // 4)
    door_w_top = max(2, size // 12)
    door_w_bot = max(3, size // 8)
    door_top_y = base_y - door_h
    door_top_l = (cross_cx - door_w_top // 2, door_top_y)
    door_top_r = (cross_cx + door_w_top // 2, door_top_y)
    door_bot_l = (cross_cx - door_w_bot // 2, base_y)
    door_bot_r = (cross_cx + door_w_bot // 2, base_y)
    d.polygon([door_top_l, door_top_r, door_bot_r, door_bot_l],
              fill=COLOR_DOOR)

    # --- Crisp silhouette outline ---------------------------------------
    # Helps the icon stay legible against any taskbar / titlebar
    # background colour by surrounding the body with a 1-px dark line.
    outline_w = 1 if size <= 32 else max(1, size // 64)
    d.line([apex, base_left,  apex], fill=COLOR_OUTLINE, width=outline_w)
    d.line([apex, base_right, apex], fill=COLOR_OUTLINE, width=outline_w)
    d.line([base_left, base_right],  fill=COLOR_OUTLINE, width=outline_w)

    return img


# ---------------------------------------------------------------------------
# Assemble multi-resolution .ico


def main():
    here   = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(os.path.dirname(here), 'resources')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'tepy.ico')

    # Render each size separately -- a single resize chain gives sharper
    # detail at small sizes than a downsample of the largest because
    # _draw_tepee uses size-aware proportions (line widths, pad,
    # stripe-on/off).
    images = [_draw_tepee(s) for s in SIZES]

    # Pillow's .ico writer takes the LARGEST image as the base and
    # bundles the requested `sizes=` list in.  We've already produced
    # those at native scale; pass them via append_images so each one
    # lands in the .ico without further resampling.
    biggest = max(SIZES)
    base    = next(img for img, s in zip(images, SIZES) if s == biggest)
    rest    = [img for img, s in zip(images, SIZES) if s != biggest]

    base.save(
        out_path,
        format='ICO',
        sizes=[(s, s) for s in SIZES],
        append_images=rest,
    )

    sz_kb = os.path.getsize(out_path) / 1024.0
    print(f"wrote {out_path}  ({sz_kb:.1f} KB, "
          f"{len(SIZES)} sizes: {', '.join(str(s) for s in SIZES)})")

    # ---- Pygame-friendly PNG sidecars ----------------------------------
    # SDL_image's .ico support picks an arbitrary frame (often the
    # smallest), which makes for a blocky title-bar icon at 32 / 48 px.
    # We write dedicated PNG sidecars at the sizes Windows actually
    # asks for so the viewer can hand pygame.display.set_icon a
    # properly-sized surface every time:
    #
    #   tepy_icon_24.png  -- title bar (Windows asks for 16, supplies
    #                        24 on hi-DPI; 24 downsamples to 16 with
    #                        less artifacting than 32 -> 16).
    #   tepy_icon_32.png  -- alt-tab / window-switcher slot, plus
    #                        fallback for systems where the title bar
    #                        wants 32 (most modern Windows builds).
    #   tepy_icon_48.png  -- taskbar at standard DPI.
    #   tepy_icon_256.png -- jump-list / file-explorer thumbnails.
    #
    # The viewer's icon-load path tries them in priority order and
    # uses the first match.
    PNG_EXPORT_SIZES = (24, 32, 48, 256)
    for psz in PNG_EXPORT_SIZES:
        png_path = os.path.join(out_dir, f'tepy_icon_{psz}.png')
        png_img  = next(img for img, s in zip(images, SIZES) if s == psz)
        png_img.save(png_path, format='PNG')
        print(f"wrote {png_path}")


if __name__ == '__main__':
    main()
