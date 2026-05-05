"""Generate `resources/fire/fire_NNNN.png` -- a 91-frame fire
flipbook for the burning-tank ParticleSystem.

Replaces the legacy `explosion 1_rgb*.png` set (which was a single
explosion blast, not a continuous flame).  We render each frame
procedurally so the look is reproducible from this script alone --
no external animation source to track.

Algorithm per frame
-------------------
1. A vertical gradient + flame-envelope mask defines WHERE fire
   exists in the frame.  The envelope is teardrop-shaped: wider
   at the base, narrowing toward the top, with a small lateral
   wobble that varies frame-to-frame so the flame looks alive.
2. A pair of low-frequency noise fields at different scales (one
   chunky, one fine) is sampled at frame-dependent vertical
   offsets so the noise scrolls UPWARD each frame -- the visual
   convection of a real flame.  Intensity = mask * noise.
3. The intensity field is mapped through a fire palette
   (black -> dark red -> orange -> yellow -> white-hot) so the
   hottest pixels (highest intensity, near the base) come out
   white-yellow and the wispy edges stay deep red.
4. The frame's overall brightness follows a life-cycle envelope:
   ramp up over the first ~15 % of frames (ignition), full
   strength through the middle, ramp down to nothing over the
   last ~30 % (dissipation).  The ParticleSystem already maps
   particle age to flipbook frame, so this gives every spawned
   particle a complete birth->burn->die arc without any code
   changes on the engine side.

Run from the project root:

    python cust_tools/make_fire_frames.py

By default it writes to `resources/fire/` next to the existing
explosion frames.  The viewer's `FlipbookTexture` walks the folder
and uploads ALL `.png` files there in sorted order, so to switch
to the new set rename the old `explosion*.png` files to a
non-matching extension (e.g. `.png.legacy`) or move them aside.

Numbered names use a fresh `fire_NNNN.png` prefix that sorts
BEFORE `explosion 1_rgb*.png`, so a folder containing both will
play this set first.

Pillow + numpy only.
"""

import os
import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Constants -- tweak and re-run

FRAME_COUNT      = 91          # match the existing flipbook length
SIZE             = 256         # square frames; matches legacy explosion set
RNG_SEED         = 0xF1CE      # frozen so two runs produce identical output

# Lifetime shape: ignition / mature / dissipation phases as
# fractions of FRAME_COUNT.  Sum can be < 1.0; remainder counts as
# mature.  Dissipation kept short so a late-life particle still
# shows visible flame -- the existing per-particle alpha-fade in
# ParticleSystem (smoke_fade_start_frame / end) handles the
# tail-end opacity ramp.
PHASE_IGNITION   = 0.12
PHASE_DISSIPATE  = 0.20

# Flame envelope shape.  All values are in [0, 1] image space
# (0 = bottom edge, 1 = top edge of the frame).
ENV_BASE_Y       = 0.05        # bottom of the flame
ENV_PEAK_Y       = 0.92        # top of the flame at peak life
ENV_BASE_HALF_W  = 0.30        # half-width at the base
ENV_PEAK_HALF_W  = 0.04        # half-width at the tip
ENV_WOBBLE_AMP   = 0.025       # horizontal wobble amplitude (per-frame x shift)

# Turbulence -- two octaves of low-frequency noise upscaled to the
# full frame.  Lower SCALE_PX = chunkier features.
COARSE_SCALE_PX  = 12
FINE_SCALE_PX    = 5
COARSE_WEIGHT    = 0.6
FINE_WEIGHT      = 0.4

# How far the noise scrolls upward over the full sequence, in
# image heights.  > 1.0 means features wrap around more than once.
NOISE_SCROLL     = 2.5

# Fire palette: list of (intensity, r, g, b) waypoints, 0..1 each.
# Intensity 0 is fully transparent; everything in between gets
# alpha = intensity directly so wisps fade naturally.
#
# Top of the ramp deliberately stops at warm yellow rather than
# pushing through to white -- a pure-white tip reads as steam or
# a pyro flash, not a continuous flame.  Real fire's hottest
# visible region is yellow-to-light-orange unless you're staring
# into a propane jet.
FIRE_PALETTE = [
    (0.00, 0.00, 0.00, 0.00),     # nothing
    (0.18, 0.50, 0.06, 0.00),     # dark ember red
    (0.40, 0.95, 0.25, 0.04),     # red-orange
    (0.62, 1.00, 0.55, 0.10),     # orange
    (0.82, 1.00, 0.80, 0.25),     # bright yellow
    (1.00, 1.00, 0.92, 0.45),     # warm yellow core (NOT white)
]


# ---------------------------------------------------------------------------
# Helpers


def _life_envelope(t):
    """Lifecycle multiplier 0..1 for normalised lifetime t in [0, 1]."""
    if t < PHASE_IGNITION:
        return t / max(PHASE_IGNITION, 1e-6)
    diss_start = 1.0 - PHASE_DISSIPATE
    if t > diss_start:
        return max(0.0, 1.0 - (t - diss_start) / PHASE_DISSIPATE)
    return 1.0


def _flame_mask(W, H, life, frame_x_wobble):
    """Per-pixel alpha mask shaped like a flame teardrop.

    Returns a (H, W) float32 array in [0, 1].

    Args:
        W, H        : image dimensions
        life        : 0..1 lifecycle multiplier; scales the flame
                      height (a dying flame is a short flame).
        frame_x_wobble : horizontal centerline shift this frame, in
                         normalised image units; gives the flame
                         that lateral lick-of-tongue look without
                         needing per-row noise.
    """
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    yy /= max(H - 1, 1)
    xx /= max(W - 1, 1)
    yc_from_bottom = 1.0 - yy           # 0 at bottom, 1 at top

    # Effective top of the flame at this lifecycle phase.
    peak_y = ENV_BASE_Y + (ENV_PEAK_Y - ENV_BASE_Y) * life

    # Vertical position WITHIN the flame, 0..1 (clipped outside).
    yt = np.clip(
        (yc_from_bottom - ENV_BASE_Y) / max(peak_y - ENV_BASE_Y, 1e-3),
        0.0, 1.0,
    )

    # Half-width tapers from base to peak.  Slight nonlinearity
    # (sqrt) makes the base feel rounded and the tip pointed.
    half_w = (ENV_BASE_HALF_W * (1.0 - np.sqrt(yt))
              + ENV_PEAK_HALF_W * np.sqrt(yt))

    # Distance from the wobbling centerline, normalised by half_w.
    cx = 0.5 + frame_x_wobble
    dist = np.where(half_w > 1e-4,
                    np.abs(xx - cx) / half_w,
                    99.0)

    # Inside-flame mask: smoothstep so the edge isn't a hard cut.
    body = np.clip(1.0 - dist, 0.0, 1.0)
    body = body * body * (3.0 - 2.0 * body)   # smoothstep

    # Vertical brightness falloff: bright at base, fading at top.
    # Parabolic so the fade accelerates near the tip.
    y_falloff = 1.0 - yt * yt

    # Above the flame top: zero (the clip on yt above already
    # capped it at 1, so y_falloff handles it; explicit safety
    # for the band yc > peak_y is body == 0 already).
    return (body * y_falloff).astype(np.float32)


def _noise_field(W, H, scale_px, scroll_y_px, rng_seed_extra=0):
    """Generate a low-frequency noise field upscaled to (H, W).

    Independent RNG seeded by RNG_SEED + scale + offset so the
    pattern is deterministic AND distinguishable across calls
    (caller passes a different `rng_seed_extra` for different
    octaves).

    `scroll_y_px` shifts the noise tile vertically before
    upsampling, so animating it across frames produces an
    upward-flowing pattern.
    """
    rng = np.random.default_rng(RNG_SEED + scale_px + rng_seed_extra)

    # Generate enough rows to allow scrolling without revealing the
    # tile boundary -- use a tall noise tile and crop with offset.
    nh = max(2, H // scale_px + 4)
    nw = max(2, W // scale_px + 2)
    # Tall buffer for vertical scrolling
    buf_h = nh * 3
    small = rng.random((buf_h, nw)).astype(np.float32)

    # Scroll: pick the row offset based on scroll_y_px.  This is
    # the integer-pixel cheap version; bilinear upsample below
    # smooths out the resulting steps.
    row_off = int(round(scroll_y_px / scale_px))
    row_off %= buf_h
    rolled = np.roll(small, -row_off, axis=0)[:nh]

    # Upsample with PIL bilinear -- much cleaner than nearest, no
    # extra dep over what we already use.
    img = Image.fromarray((rolled * 255.0).astype(np.uint8))
    img = img.resize((W, H), Image.BILINEAR)
    return np.array(img, dtype=np.float32) / 255.0


def _palette_lookup(intensity):
    """Map an (H, W) intensity field to (H, W, 4) uint8 RGBA via
    the FIRE_PALETTE waypoints, linearly interpolated between
    waypoints.
    """
    pts = sorted(FIRE_PALETTE, key=lambda p: p[0])
    H, W = intensity.shape
    out = np.zeros((H, W, 4), dtype=np.float32)

    for i in range(len(pts) - 1):
        t0, r0, g0, b0 = pts[i]
        t1, r1, g1, b1 = pts[i + 1]
        mask = (intensity >= t0) & (intensity <= t1)
        if not np.any(mask):
            continue
        denom = max(t1 - t0, 1e-6)
        u = (intensity[mask] - t0) / denom
        out[mask, 0] = r0 + (r1 - r0) * u
        out[mask, 1] = g0 + (g1 - g0) * u
        out[mask, 2] = b0 + (b1 - b0) * u
        # Alpha equals the underlying intensity so wisps fade
        # naturally; the palette's alpha component is unused for
        # this generator but kept in the table for future tweaks.
        out[mask, 3] = intensity[mask]

    return (np.clip(out, 0.0, 1.0) * 255.0).astype(np.uint8)


# ---------------------------------------------------------------------------
# Driver


def main():
    here     = os.path.dirname(os.path.abspath(__file__))
    fire_dir = os.path.join(os.path.dirname(here), 'resources', 'fire')
    os.makedirs(fire_dir, exist_ok=True)

    print(f"writing {FRAME_COUNT} frames @ {SIZE}x{SIZE} -> {fire_dir}")

    rng = np.random.default_rng(RNG_SEED + 0xBEEF)

    for i in range(FRAME_COUNT):
        t        = i / max(FRAME_COUNT - 1, 1)
        life     = _life_envelope(t)
        wobble   = (np.sin(t * 11.0) * 0.5
                    + np.sin(t * 7.3 + 1.7) * 0.5) * ENV_WOBBLE_AMP

        mask = _flame_mask(SIZE, SIZE, life, wobble)

        # Two octaves of upward-scrolling noise.  Scroll is in pixels
        # along the ORIGINAL frame; the noise generator handles the
        # division by scale internally.
        scroll_px = t * SIZE * NOISE_SCROLL
        coarse = _noise_field(SIZE, SIZE,
                              COARSE_SCALE_PX, scroll_px,
                              rng_seed_extra=0)
        fine   = _noise_field(SIZE, SIZE,
                              FINE_SCALE_PX,   scroll_px * 1.4,
                              rng_seed_extra=11)
        turb = COARSE_WEIGHT * coarse + FINE_WEIGHT * fine
        # Re-centre so values straddle 0.5 not 0.0/1.0 with bias
        turb = np.clip(turb, 0.0, 1.0)

        # Combine: envelope * (mask * (0.55 + 0.45 * turb))
        # The 0.55 floor inside the parentheses keeps the body of
        # the flame solid even when the noise dips low; without it
        # the core would flicker with holes.
        intensity = mask * (0.55 + 0.45 * turb) * life

        # Final intensity gets a faint global flicker so even the
        # mature-phase frames don't all look identical.
        flicker = 0.85 + 0.15 * rng.random()
        intensity = intensity * flicker

        rgba = _palette_lookup(intensity)
        img  = Image.fromarray(rgba, mode='RGBA')
        out  = os.path.join(fire_dir, f"fire_{i:04d}.png")
        img.save(out, format='PNG', optimize=False)

    print(f"  done -- {FRAME_COUNT} frames written")
    print(f"  the viewer's FlipbookTexture sorts the folder, so")
    print(f"  fire_*.png plays before any explosion*.png that may")
    print(f"  still live in {fire_dir}.  Move/rename the legacy")
    print(f"  explosion frames if you want them gone.")


if __name__ == '__main__':
    main()
