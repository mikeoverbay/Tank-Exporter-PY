"""
Generate a procedural shell-impact scorch decal texture.

Output: `resources/scorch.png` -- a 512 x 512 RGBA image consumed by
the runtime `Decals` projector (see `tankExporterPy/particles.py`).

The texture is designed to:

* Read as "burnt earth + soot" on a tan / brown terrain --
  near-black inner crater, dark-brown peripheral scorch ring,
  fading to fully transparent at the edges so the quad has no
  visible boundary.

* Tile / repeat poorly (intentional -- decals are per-impact
  one-offs, not a repeating pattern), but be radially symmetric
  enough that the user can't tell the orientation of the
  projector.  Per-impact world-Y rotation jitter at render time
  breaks the few asymmetries that survive.

* Carry alpha that drops to ZERO at the boundary so adjacent
  decals composite without seams.

Run:
    python cust_tools/make_scorch_texture.py [--size 512] [--seed 7]

Author: Coffee + Claude, 2026-05-14.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

try:
    from PIL import Image
except ImportError as exc:
    print(f"Pillow required: {exc}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
def _smoothstep(edge0, edge1, x):
    """GLSL smoothstep -- 0 below edge0, 1 above edge1, smooth between."""
    t = np.clip((x - edge0) / max(edge1 - edge0, 1e-9), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _radial_alpha(size):
    """Build the master alpha mask: full at center, fading to 0 at edges.

    Two-stage falloff:
      * inner disc (0..0.45 of radius) stays fully opaque -- the
        crater core.
      * outer ring (0.45..0.95) ramps down via smoothstep so the
        edge of the decal is invisible.  Anything past 0.95 is
        zero to give a hard cutoff (no fringe).
    """
    cx = cy = (size - 1) * 0.5
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    dx = (x - cx) / (size * 0.5)
    dy = (y - cy) / (size * 0.5)
    r  = np.sqrt(dx * dx + dy * dy)
    # 1.0 inside r<0.45, smoothstep down to 0 at r=0.95.
    return 1.0 - _smoothstep(0.45, 0.95, r)


def _ring_darkness(size):
    """A SECOND darker ring around the crater proper -- "scorch
    that hasn't been blown away".  Adds tonal variation between the
    blackened core and the transparent rim."""
    cx = cy = (size - 1) * 0.5
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    dx = (x - cx) / (size * 0.5)
    dy = (y - cy) / (size * 0.5)
    r  = np.sqrt(dx * dx + dy * dy)
    # Peak intensity at r ~ 0.55 (just outside the crater proper),
    # fading to 0 at the outer rim.  Bell curve shape.
    ring = np.exp(-((r - 0.55) ** 2) / 0.04)
    ring *= 1.0 - _smoothstep(0.85, 1.0, r)
    return np.clip(ring, 0.0, 1.0)


def _ground_noise(size, seed):
    """Low-amplitude noise for organic edge irregularity.  Built
    via FFT low-pass on white noise -- same recipe as the tileable
    sand painter.  We DON'T need this to tile (one-off decal), but
    the FFT path is faster than authoring multi-octave Perlin from
    scratch."""
    rng = np.random.default_rng(seed)
    base = rng.standard_normal((size, size)).astype(np.float32)
    f    = np.fft.fft2(base)
    # Low-pass: zero everything above cutoff frequency.
    kx = np.fft.fftfreq(size).reshape(1, -1)
    ky = np.fft.fftfreq(size).reshape(-1, 1)
    kmag = np.sqrt(kx * kx + ky * ky)
    cutoff = 0.06
    mask = np.exp(-(kmag / cutoff) ** 2)
    f *= mask
    spatial = np.real(np.fft.ifft2(f))
    # Normalise to [0, 1].
    lo, hi = spatial.min(), spatial.max()
    if hi > lo:
        spatial = (spatial - lo) / (hi - lo)
    return spatial.astype(np.float32)


def _radial_streaks(size, seed):
    """Faint radial streak pattern emanating from center -- gives
    the eye a "blast pattern" cue without being obvious.  Built
    from sin(angle * N) modulated by 1/r, smoothed by a low-pass
    sweep."""
    cx = cy = (size - 1) * 0.5
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    dx = x - cx
    dy = y - cy
    r  = np.sqrt(dx * dx + dy * dy)
    a  = np.arctan2(dy, dx)
    rng = np.random.default_rng(seed + 1)
    # 17 streaks at random phase; multiplied amplitude tapers off
    # with radius so streaks fade outward.
    phase   = rng.uniform(0.0, 2.0 * np.pi)
    streaks = 0.5 + 0.5 * np.sin(17.0 * a + phase)
    # Amplitude profile: peaks just outside the crater, fades to
    # zero at the rim.
    amp = np.exp(-((r / (size * 0.5) - 0.6) ** 2) / 0.08)
    return np.clip(streaks * amp, 0.0, 1.0)


# ---------------------------------------------------------------------------
def build(size=512, seed=7):
    """Compose RGBA scorch texture.  Returns a uint8 array shape
    (size, size, 4)."""
    alpha_mask = _radial_alpha(size)
    ring       = _ring_darkness(size)
    noise      = _ground_noise(size, seed)
    streaks    = _radial_streaks(size, seed)

    # Color: very dark brown core (so it reads black on terrain
    # but with a HINT of warm undertone), nudged a touch warmer
    # in the ring (incomplete-combustion soot).
    core_color = np.array([0.06, 0.05, 0.04], dtype=np.float32)
    ring_color = np.array([0.16, 0.10, 0.06], dtype=np.float32)
    rgb = (core_color[None, None, :] * (1.0 - ring[..., None])
           + ring_color[None, None, :] * ring[..., None])
    # Subtle noise modulation (+/- 10 %) so the surface isn't a
    # flat gradient.
    rgb *= 0.9 + 0.2 * noise[..., None]
    # Streaks darken (subtract a small amount).
    rgb *= 1.0 - 0.25 * streaks[..., None]

    rgb = np.clip(rgb, 0.0, 1.0)

    # Alpha: master radial * (1 + ring boost) clipped, then
    # roughened by the noise so the edge isn't perfectly circular.
    alpha = alpha_mask * (0.6 + 0.4 * ring + 0.4 * (1.0 - ring))
    alpha *= 0.85 + 0.30 * noise
    # Subtract a small streak-driven alpha bump so streaks read
    # as cracks rather than just dark stripes.
    alpha = np.clip(alpha - 0.10 * streaks * alpha_mask, 0.0, 1.0)
    # Hard zero outside the master mask so the rim is invisible.
    alpha = np.where(alpha_mask > 1e-3, alpha, 0.0)

    rgba = np.empty((size, size, 4), dtype=np.float32)
    rgba[..., :3] = rgb
    rgba[..., 3]  = alpha
    return (rgba * 255.0 + 0.5).astype(np.uint8)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--size', type=int, default=512)
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--out', type=str,
                        default=os.path.join('resources', 'scorch.png'))
    args = parser.parse_args()

    img_arr = build(size=args.size, seed=args.seed)
    out = args.out
    out_dir = os.path.dirname(out)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    Image.fromarray(img_arr, 'RGBA').save(out)
    print(f"wrote {out}  ({args.size}x{args.size}, seed={args.seed})")


if __name__ == '__main__':
    main()
