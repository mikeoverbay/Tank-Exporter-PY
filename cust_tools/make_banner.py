"""Generate `resources/tepy_banner.png` -- the README hero banner.

Draws "TEPY -- Tank Exporter in PYthon" plus a tagline line directly
over a copy of `resources/splash.png` so the README has a real
text-on-image banner.  GitHub-rendered Markdown can't overlay text on
images natively, so baking the title into the PNG is the only reliable
way to get this effect across every Markdown viewer (GitHub, VS Code,
PyPI, Sourceforge, etc.).

Run from the project root:

    python cust_tools/make_banner.py

Re-run after editing the constants below to regenerate.

Pillow only -- already in `requirements.txt`.
"""

import os
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Banner content

TITLE    = 'TEPY'
SUBTITLE = 'Tank Exporter in PYthon'

# Layout / typography ------------------------------------------------------

# Title font size scales with the splash image's height so smaller
# splashes don't get a giant title slab and 4K splashes don't get a
# pin-prick.  Subtitle is roughly a third of the title.
TITLE_HEIGHT_FRAC    = 0.16    # title cap-height as a fraction of image H
SUBTITLE_HEIGHT_FRAC = 0.055   # subtitle cap-height likewise
TOP_PAD_FRAC         = 0.06    # gap between top edge and title baseline area
GAP_BETWEEN_FRAC     = 0.012   # vertical gap between title and subtitle

# Colours -- aged burnt orange, tone-shifted to match the sepia
# splash so the title reads as integrated old-timey signage instead
# of a bright modern overlay.  Compared to the in-app PBR-render
# burnt orange (212, 116, 52) we drop a notch in saturation and
# brightness, then push slightly toward amber:
#
#   in-app burnt orange : (212, 116,  52)   vivid, tepee body tone
#   banner aged orange  : (188, 110,  58)   muted, sepia-friendly
#
# Subtitle + outline are warm browns (not pure black) so the whole
# title reads as one continuous earth-tone graphic.
COLOR_TITLE    = (188, 110,  58, 255)   # aged burnt orange
COLOR_SUBTITLE = (210, 178, 130, 235)   # weathered cream
COLOR_SHADOW   = ( 18,  10,   4, 220)   # warm dark drop shadow
SHADOW_OFFSET  = (3, 4)                  # +x, +y in pixels at 1080p
SHADOW_BLUR    = 0                       # set >0 for a soft shadow
                                         # (requires PIL.ImageFilter)

# Optional thin outline drawn around every glyph at 0..3 px so the
# title pops against any patch of the splash.  0 disables.
OUTLINE_PX     = 2
COLOR_OUTLINE  = ( 40,  22,  10, 255)   # warm-dark, not pure black


# Font search order -- first installed face wins.  All are bundled
# with Windows; on other platforms PIL's default font is the fallback.
FONT_CANDIDATES = (
    'georgiab.ttf',     # Georgia Bold -- serif, good "old-timey" weight
    'georgia.ttf',
    'constanb.ttf',     # Constantia Bold
    'cambriab.ttf',     # Cambria Bold
    'calibrib.ttf',     # Calibri Bold (last resort -- sans-serif)
)


# ---------------------------------------------------------------------------
# Helpers


def _load_font(size):
    """Try every candidate face in FONT_CANDIDATES at `size`; return
    the first that loads, or PIL's default if none do."""
    for name in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, IOError):
            continue
    print("[make_banner] WARN: no candidate font available, "
          "using default (small)")
    return ImageFont.load_default()


def _text_size(draw, text, font):
    """PIL deprecated `font.getsize` in 10.0; bbox is the modern path."""
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    return (r - l, b - t)


def _draw_text_with_effects(draw, xy, text, font, fill,
                             shadow_color=COLOR_SHADOW,
                             shadow_offset=SHADOW_OFFSET,
                             outline_px=OUTLINE_PX,
                             outline_color=COLOR_OUTLINE):
    """Draw `text` at `xy` with shadow + outline, then the glyph fill.

    Order matters: shadow first (lowest layer), then outline, then
    fill on top -- otherwise the shadow paints over the glyphs.
    """
    x, y = xy

    # Shadow: simple offset copy in semi-transparent black.
    sx, sy = shadow_offset
    draw.text((x + sx, y + sy), text, font=font, fill=shadow_color)

    # Outline: 8-direction nudge to fake a stroke without needing
    # PIL's stroke_width (some older Pillows lack it).  Skipped at 0.
    if outline_px > 0:
        for dx in range(-outline_px, outline_px + 1):
            for dy in range(-outline_px, outline_px + 1):
                if dx == 0 and dy == 0:
                    continue
                draw.text((x + dx, y + dy), text,
                          font=font, fill=outline_color)

    # Fill on top.
    draw.text((x, y), text, font=font, fill=fill)


# ---------------------------------------------------------------------------
# Main


def main():
    here     = os.path.dirname(os.path.abspath(__file__))
    res_dir  = os.path.join(os.path.dirname(here), 'resources')
    src_path = os.path.join(res_dir, 'splash.png')
    out_path = os.path.join(res_dir, 'tepy_banner.png')

    if not os.path.isfile(src_path):
        raise SystemExit(f"splash.png not found at {src_path}")

    # Load splash, force RGBA so transparent overlay text composes
    # cleanly even on older Pillows that default to RGB for JPEGs.
    img = Image.open(src_path).convert('RGBA')
    W, H = img.size

    # An overlay layer keeps the original splash pixels untouched
    # underneath; we only replace alpha-mixed pixels where the text
    # lives.  Final composite at the end.
    overlay = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    title_size    = max(20, int(round(H * TITLE_HEIGHT_FRAC)))
    subtitle_size = max(12, int(round(H * SUBTITLE_HEIGHT_FRAC)))
    title_font    = _load_font(title_size)
    subtitle_font = _load_font(subtitle_size)

    title_w,    title_h    = _text_size(draw, TITLE,    title_font)
    subtitle_w, subtitle_h = _text_size(draw, SUBTITLE, subtitle_font)

    title_x = (W - title_w) // 2
    title_y = int(round(H * TOP_PAD_FRAC))
    sub_x   = (W - subtitle_w) // 2
    sub_y   = title_y + title_h + int(round(H * GAP_BETWEEN_FRAC))

    _draw_text_with_effects(draw, (title_x, title_y),
                             TITLE, title_font,
                             fill=COLOR_TITLE)
    _draw_text_with_effects(draw, (sub_x, sub_y),
                             SUBTITLE, subtitle_font,
                             fill=COLOR_SUBTITLE,
                             outline_px=max(1, OUTLINE_PX - 1))

    # Composite overlay onto splash and save.
    out = Image.alpha_composite(img, overlay)
    out.save(out_path, format='PNG', optimize=True)

    sz_kb = os.path.getsize(out_path) / 1024.0
    print(f"wrote {out_path}  ({W}x{H}, {sz_kb:.1f} KB)")
    print(f"  title    : {TITLE!r:>28}  font={title_size}px")
    print(f"  subtitle : {SUBTITLE!r:>28}  font={subtitle_size}px")


if __name__ == '__main__':
    main()
