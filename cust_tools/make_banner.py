"""Generate `resources/tepy_banner.png` -- the README hero banner.

Draws "TEPY -- Tank Exporter in PYthon" plus a tagline line directly
over a copy of `resources/splash.png` so the README has a real
text-on-image banner.  GitHub-rendered Markdown can't overlay text on
images natively, so baking the title into the PNG is the only reliable
way to get this effect across every Markdown viewer (GitHub, VS Code,
PyPI, Sourceforge, etc.).

Run from the project root:

    python cust_tools/make_banner.py

Layout: text sits centered at the BOTTOM of the splash with the
glyphs weathered (per-pixel alpha jitter + slight blur) so the
lettering reads like aged stencil paint rather than a sharp modern
overlay.  No drop shadow -- the weathering does the visual heavy
lifting.

Re-run after editing the constants below to regenerate.

Pillow + numpy only -- both already in `requirements.txt`.
"""

import os
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ---------------------------------------------------------------------------
# Banner content

TITLE    = 'TEPY'
SUBTITLE = 'Tank Exporter in PYthon'

# Layout / typography ------------------------------------------------------

# Title font size scales with the splash image's height so smaller
# splashes don't get a giant title slab and 4K splashes don't get a
# pin-prick.  Subtitle is roughly a third of the title.
# Bottom of the splash has a usable strip ~10% of image H tall
# below the seated figure and crates -- enough to fit two centred
# lines if we keep the text disciplined.  Subtitle bumped to ~55%
# of the title's cap height (was 42%) so the tagline carries
# weight comparable to the lettering on a 19th-century stage-coach
# postcard, where the descriptive line was nearly as prominent as
# the title slug.
TITLE_HEIGHT_FRAC    = 0.065   # title cap-height as a fraction of image H
SUBTITLE_HEIGHT_FRAC = 0.036   # subtitle cap-height likewise
BOTTOM_PAD_FRAC      = 0.012   # gap from bottom edge up to subtitle baseline
GAP_BETWEEN_FRAC     = 0.006   # vertical gap between title and subtitle

# Edge-only weathering -- the per-pixel alpha jitter is gated by an
# "edge mask" derived from the blurred alpha, so the glyph BODY
# stays at full opacity (readable) while only the antialiased
# transition zone around each glyph is eroded into a worn,
# stencil-paint look.  The user-facing complaint that drove this
# rework: "weather the edges, no overlap, text not visible enough".
EDGE_LOW              = 0.05   # blurred-alpha BELOW this is background
                                # (always 0; never weathered)
EDGE_HIGH             = 0.85   # blurred-alpha ABOVE this is body
                                # (always full; never weathered)
                                # values in between get jittered.
WEATHER_FADE_BAND     = 0.55   # within the edge zone, how aggressively
                                # noise drives alpha drops (0..1 -- 1
                                # means "full noise driven")
WEATHER_PUNCH_RATE    = 0.012  # fraction of EDGE pixels punched fully
                                # out (independent of the noise field)
WEATHER_BLUR_RADIUS   = 0.7    # Gaussian blur on the alpha mask, in px
WEATHER_NOISE_SCALE_FRAC = 0.0050  # noise pattern scale relative to text H
                                    # -- higher = chunkier worn patches
WEATHER_RNG_SEED      = 0xC0FFEE   # frozen seed so two regen runs give the
                                    # same banner (handy for git diffs)

# Colours -- "1876 postcard" palette.  Bright burnt orange for the
# title (the WoT-stencil colour the rest of the UI uses, pushed up
# in saturation and brightness so it carries through the
# dark-dirt background) plus a warm cream-gold subtitle that
# matches the canvas tones in the splash's tepees.  Earlier we
# tried a brown-on-brown muted orange which vanished into the
# ground -- the shift here is "dial up the chroma until it reads
# as paint on a stagecoach side-panel".
#
# No shadow / outline -- the edge-only alpha weathering integrates
# the lettering with the substrate, like ink that's bled slightly
# into porous paper.
COLOR_TITLE    = (228, 132,  64, 255)   # vivid burnt orange (postcard ink)
COLOR_SUBTITLE = (232, 200, 142, 245)   # warm cream-gold (canvas tone)


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


def _render_weathered_text(text, font, fill, rng):
    """Render `text` to a transparent RGBA tile with stencil-style
    weathering applied to the alpha channel.

    Pipeline:
      1. Render the glyphs at full opacity into a padded tile.
      2. Slight Gaussian blur on the alpha mask -- softens the
         crisp anti-aliased edges so the glyphs look painted on
         rather than vector-rendered.
      3. Per-pixel alpha jitter via low-frequency upscaled noise:
         most pixels keep most of their alpha, a band of pixels
         get partial alpha drops (the "faded paint" feel).
      4. A small fraction of pixels are punched fully out (the
         "missing fleck" feel).
      5. Pixels with alpha == 0 originally stay 0 -- weathering
         only affects the glyph interiors, not the transparent
         background.

    Args:
        text  (str)
        font  (ImageFont): pre-loaded TTF
        fill  (rgba 0-255 tuple): glyph colour
        rng   (np.random.Generator): driving the jitter pattern.
              Caller passes a seeded Generator so the output is
              reproducible across runs (helpful for git diffs of
              the banner asset).

    Returns:
        Image (RGBA): tile big enough to host the rendered text
                      with a small transparent margin around it.
    """
    # Measure glyphs on a throwaway draw context so we can size the
    # tile to fit -- bbox-based, not the deprecated getsize.
    probe = Image.new('RGBA', (4, 4), (0, 0, 0, 0))
    pd    = ImageDraw.Draw(probe)
    l, t, r, b = pd.textbbox((0, 0), text, font=font)
    text_w = r - l
    text_h = b - t

    pad   = max(8, text_h // 4)         # transparent margin for the blur
    tile_w = text_w + 2 * pad
    tile_h = text_h + 2 * pad

    tile = Image.new('RGBA', (tile_w, tile_h), (0, 0, 0, 0))
    td   = ImageDraw.Draw(tile)
    # Offset by -l/-t so the glyph bbox top-left lands at (pad, pad).
    td.text((pad - l, pad - t), text, font=font, fill=fill)

    # Pull the alpha channel out for jitter; leave RGB alone so the
    # warm fill colour stays consistent across the weathered surface.
    arr = np.array(tile, dtype=np.uint8)
    alpha = arr[:, :, 3].astype(np.float32) / 255.0

    # Gaussian-blur the alpha mask with a tiny radius -- softens the
    # crisp anti-aliased edges enough to suggest "painted on".  Done
    # via PIL because numpy/scipy convolution would add a dep.
    if WEATHER_BLUR_RADIUS > 0:
        alpha_img = Image.fromarray((alpha * 255.0).astype(np.uint8))
        alpha_img = alpha_img.filter(
            ImageFilter.GaussianBlur(radius=WEATHER_BLUR_RADIUS))
        alpha = np.array(alpha_img, dtype=np.float32) / 255.0

    # Build a low-frequency noise field by generating a small random
    # texture and bilinearly upscaling.  Higher noise scale -> chunkier
    # worn patches; lower -> finer salt-and-pepper.  We use this to
    # multiply the alpha down by varying amounts.
    scale_px = max(2, int(round(text_h * WEATHER_NOISE_SCALE_FRAC * 4)))
    small_w  = max(1, tile_w // scale_px)
    small_h  = max(1, tile_h // scale_px)
    small    = rng.random((small_h, small_w)).astype(np.float32)
    noise_img = Image.fromarray((small * 255.0).astype(np.uint8))
    noise_img = noise_img.resize((tile_w, tile_h), Image.BILINEAR)
    noise = np.array(noise_img, dtype=np.float32) / 255.0

    # ---- Edge-only weathering -------------------------------------
    # Identify the edge zone: pixels whose (blurred) alpha sits in
    # the antialiased transition band between background and body.
    # Body pixels (alpha >= EDGE_HIGH) and pure background pixels
    # (alpha <= EDGE_LOW) are NOT weathered -- only the rim of each
    # glyph is, which preserves readability while still giving the
    # painted-stencil look.
    edge_mask = (alpha > EDGE_LOW) & (alpha < EDGE_HIGH)

    # Within the edge zone, drive an alpha drop proportional to the
    # local noise value.  Outside the edge zone, leave alpha alone.
    #   noise == 1 -> keep everything
    #   noise == 0 -> drop by WEATHER_FADE_BAND of original alpha
    keep = 1.0 - (1.0 - noise) * WEATHER_FADE_BAND
    alpha = np.where(edge_mask, alpha * keep, alpha)

    # Random punch-outs for the "missing fleck" feel -- small fraction
    # of edge pixels go fully transparent.  Body pixels are still
    # protected by the edge_mask gate so the glyph silhouette
    # doesn't get holes punched through it.
    if WEATHER_PUNCH_RATE > 0:
        punch = rng.random((tile_h, tile_w)).astype(np.float32)
        punch_hits = (punch < WEATHER_PUNCH_RATE) & edge_mask
        alpha = np.where(punch_hits, 0.0, alpha)

    # Write back into the array and rebuild the PIL image.  Clamp
    # explicitly so we never overflow uint8 from float rounding.
    arr[:, :, 3] = np.clip(alpha * 255.0, 0.0, 255.0).astype(np.uint8)
    return Image.fromarray(arr)


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

    title_size    = max(20, int(round(H * TITLE_HEIGHT_FRAC)))
    subtitle_size = max(12, int(round(H * SUBTITLE_HEIGHT_FRAC)))
    title_font    = _load_font(title_size)
    subtitle_font = _load_font(subtitle_size)

    # Two seeded RNGs -- one per line of text.  Same seed across
    # runs gives the same banner so a regen can land cleanly in git
    # (no spurious diff from random noise).  Different RNGs per
    # line so the title's wear pattern doesn't perfectly mirror the
    # subtitle's, which would look unnatural.
    rng_title = np.random.default_rng(WEATHER_RNG_SEED)
    rng_sub   = np.random.default_rng(WEATHER_RNG_SEED + 1)

    title_tile = _render_weathered_text(TITLE,    title_font,
                                         COLOR_TITLE,    rng_title)
    sub_tile   = _render_weathered_text(SUBTITLE, subtitle_font,
                                         COLOR_SUBTITLE, rng_sub)
    title_w, title_h = title_tile.size
    sub_w,   sub_h   = sub_tile.size

    # Bottom-centered placement.  We anchor from the BOTTOM:
    # subtitle baseline sits BOTTOM_PAD_FRAC above the bottom edge,
    # title baseline sits a GAP above the subtitle.  The internal
    # transparent margin in each tile (`pad` inside
    # `_render_weathered_text`) means the y values place the
    # tile's top-left corner, which already includes that pad --
    # so a small visual gap stays even at zero `BOTTOM_PAD_FRAC`.
    bottom_pad  = int(round(H * BOTTOM_PAD_FRAC))
    sub_x = (W - sub_w)   // 2
    sub_y = H - bottom_pad - sub_h
    title_x = (W - title_w) // 2
    title_y = sub_y - int(round(H * GAP_BETWEEN_FRAC)) - title_h

    # Composite each weathered tile onto its own transparent layer
    # at the right (x, y), then alpha-composite both layers down
    # onto the splash.  Doing it as separate paste()s into a single
    # overlay keeps the no-overlap guarantee clean: each tile's
    # transparent margin protects the OTHER tile's pixels.
    overlay = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    overlay.paste(title_tile, (title_x, title_y), title_tile)
    overlay.paste(sub_tile,   (sub_x,   sub_y),   sub_tile)

    out = Image.alpha_composite(img, overlay)
    out.save(out_path, format='PNG', optimize=True)

    sz_kb = os.path.getsize(out_path) / 1024.0
    print(f"wrote {out_path}  ({W}x{H}, {sz_kb:.1f} KB)")
    print(f"  title    : {TITLE!r:>28}  font={title_size}px")
    print(f"  subtitle : {SUBTITLE!r:>28}  font={subtitle_size}px")


if __name__ == '__main__':
    main()
