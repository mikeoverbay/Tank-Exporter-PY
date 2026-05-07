"""Sanity demo for the terrain-Y sampler + four-corner-wheel plane fit.

What this does
--------------

1. Builds the macro heightmap + tiled sand-detail displacement
   exactly the way `Terrain.__init__` does (calling the same
   helpers `_heightmap_from_image` / `_make_heightmap` /
   `_detail_displacement`) BUT skipping the GL upload, so this
   tool runs headless / in CI / on a developer box without an
   OpenGL context.
2. Calls `bilinear_sample_height` (the shared helper that
   `Terrain.sample_height` delegates to) at a handful of known
   points -- centre / corners / a couple of random offsets --
   to verify it returns sane values.
3. Drops a virtual T110E4 chassis at a configurable world
   (X, Z) origin / yaw and samples the terrain Y under each
   of the FOUR CORNER wheels (front-left, front-right, rear-left,
   rear-right -- ground-contact points = wheel-Z plus the wheel
   radius below the bone pivot).
4. Adds the wheel radius back to recover each wheel's CENTER Y,
   then fits a plane through the four wheel-centre points to
   give the chassis tilt -- pitch (forward/back lean), roll
   (side-to-side lean), and the plane normal in world coords.

The chassis layout numbers come from the dump we already did
on T110E4 (`hand_off/TRACK_SKINNING_T110E4.md`):

    front  Z = +1.979   (wheel L5 / R5)
    rear   Z = -1.991   (wheel L0 / R0)
    side   X = +/- 1.511
    wheel radius = 0.32952  (W_L0..L5 group -- main road wheels)

Usage
-----

    python cust_tools/demo_terrain_corners.py
    python cust_tools/demo_terrain_corners.py --x 12.0 --z -5.0
    python cust_tools/demo_terrain_corners.py --yaw 30
"""

import argparse
import math
import os
import sys

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tankExporterPy.terrain import (
    _make_heightmap,
    _heightmap_from_image,
    _detail_displacement,
    bilinear_sample_height,
    bilinear_sample_heights,
)


# ---------------------------------------------------------------------------
# Build the same heightmap Terrain.__init__ would build, without GL.
# ---------------------------------------------------------------------------
def build_terrain_heightmap(size=1025, world_size=160.0,
                            height_scale=3.0,
                            heightmap_path=None,
                            detail_path=None,
                            detail_tile_size=50.0,
                            detail_height_scale=0.05):
    """Return the (size, size) float32 grid Terrain would have
    baked into `self._heightmap`."""
    if heightmap_path and os.path.isfile(heightmap_path):
        heights = _heightmap_from_image(
            path=heightmap_path, size=size,
            height_scale=height_scale)
    else:
        # Procedural Perlin fallback.  Same default seed.
        heights = _make_heightmap(
            seed=0, size=size, world_size=world_size,
            octaves=5, persistence=0.5, lacunarity=2.0,
            height_scale=height_scale)
    if detail_path and os.path.isfile(detail_path):
        detail = _detail_displacement(
            detail_path=detail_path, world_size=world_size,
            mesh_size=size, tile_meters=detail_tile_size,
            height_scale=detail_height_scale)
        heights = heights + detail
    return heights


# ---------------------------------------------------------------------------
# Plane fit through 3+ points -- least-squares.  Returns
# (normal, point_on_plane) where normal is unit-length.
# ---------------------------------------------------------------------------
def fit_plane(pts):
    pts  = np.asarray(pts, dtype=np.float64)
    cen  = pts.mean(axis=0)
    cov  = np.cov((pts - cen).T)
    # Eigvec of smallest eigenvalue is the plane normal.
    w, v = np.linalg.eigh(cov)
    n    = v[:, 0]
    # Force +Y up so the tilt sign is intuitive (n.y > 0).
    if n[1] < 0:
        n = -n
    return n / np.linalg.norm(n), cen


# ---------------------------------------------------------------------------
# Convert a plane normal to pitch / roll Euler angles in degrees.
# Pitch = rotation around X (forward/back lean, +ve = nose up).
# Roll  = rotation around Z (side-to-side, +ve = right side up).
# ---------------------------------------------------------------------------
def normal_to_pitch_roll(n):
    nx, ny, nz = float(n[0]), float(n[1]), float(n[2])
    # Pitch: how much the plane tilts forward/back along Z.
    # If the normal leans toward +Z, the surface tilts so the
    # FRONT (+Z) is LOWER -- nose-down.  We sign so nose-up = +.
    pitch_rad = math.atan2(-nz, ny)
    # Roll: how much the plane tilts left/right along X.  +X
    # normal lean means the right side is HIGHER (rolling left).
    # Convention: roll positive when right side goes UP.
    roll_rad  = math.atan2(nx, ny)
    return math.degrees(pitch_rad), math.degrees(roll_rad)


# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Sanity-check the terrain Y sampler + "
                    "compute four-corner-wheel chassis tilt for "
                    "a virtual T110E4 placed on the terrain.")
    parser.add_argument('--x',   type=float, default=0.0,
        help="world X to place the tank centre at (default 0).")
    parser.add_argument('--z',   type=float, default=0.0,
        help="world Z to place the tank centre at (default 0).")
    parser.add_argument('--yaw', type=float, default=0.0,
        help="tank yaw in degrees (rotation around Y).  Default 0 "
             "= +Z is the front of the tank.")
    parser.add_argument('--size',       type=int,   default=1025)
    parser.add_argument('--world-size', type=float, default=160.0)
    parser.add_argument('--height-scale', type=float, default=3.0)
    parser.add_argument('--detail-scale', type=float, default=0.05)
    args = parser.parse_args(argv)

    # ---- Build the heightmap ----------------------------------------
    res = os.path.join(ROOT, 'resources')
    hm_path  = os.path.join(res, 'heightmap.png')
    det_path = os.path.join(res, 'sand_painted_height.png')
    if not os.path.isfile(hm_path):  hm_path  = None
    if not os.path.isfile(det_path): det_path = None

    print(f"-- Building heightmap "
          f"(size={args.size}, world={args.world_size} m, "
          f"height_scale={args.height_scale} m)")
    print(f"   macro heightmap : {hm_path or '(perlin fallback)'}")
    print(f"   detail tiled    : "
          f"{det_path or '(none)'} "
          f"x{args.detail_scale} m amplitude")
    heights = build_terrain_heightmap(
        size               = args.size,
        world_size         = args.world_size,
        height_scale       = args.height_scale,
        heightmap_path     = hm_path,
        detail_path        = det_path,
        detail_tile_size   = 50.0,
        detail_height_scale= args.detail_scale)
    print(f"   heightmap shape: {heights.shape}, "
          f"min={heights.min():+.3f}, max={heights.max():+.3f} m")

    # ---- 1. Sanity samples ------------------------------------------
    print("\n-- Sanity samples (terrain Y at known points)")
    pts = [
        ('center',          0.0,  0.0),
        ('left edge',     -79.0,  0.0),
        ('right edge',    +79.0,  0.0),
        ('back edge',       0.0,-79.0),
        ('front edge',      0.0,+79.0),
        ('out-of-bounds', 200.0,200.0),     # should clamp to base_y
        ('arbitrary 1',   -7.5, 12.3),
        ('arbitrary 2',   24.0, -8.0),
    ]
    for name, x, z in pts:
        y = bilinear_sample_height(heights, args.world_size, x, z)
        print(f"   {name:<16s}  ({x:+7.2f}, {z:+7.2f}) -> y = {y:+.3f}")

    # ---- 2. Four-corner wheel plane fit -----------------------------
    # T110E4 mesh-local wheel positions (from
    # hand_off/TRACK_SKINNING_T110E4.md vertex centroids).  The
    # main-road-wheel group radius is in
    # scripts/item_defs/vehicles/usa/A83_T110E4.xml -> wheelGroups.
    WHEEL_RADIUS = 0.32952        # main road wheel
    # Local wheel CENTROID (xyz) -- that's the track-vert centroid
    # of the wheel's bone group.  The wheel CENTER above is the
    # contact-patch Y plus the radius:
    LOCAL_FL = np.array([-1.511, 0.07, +1.979], dtype=np.float64)  # front-left  W_L5
    LOCAL_FR = np.array([+1.511, 0.07, +1.979], dtype=np.float64)  # front-right W_R5
    LOCAL_RL = np.array([-1.511, 0.09, -1.991], dtype=np.float64)  # rear-left   W_L0
    LOCAL_RR = np.array([+1.511, 0.09, -1.991], dtype=np.float64)  # rear-right  W_R0

    yaw_rad = math.radians(args.yaw)
    cy, sy  = math.cos(yaw_rad), math.sin(yaw_rad)
    def world_xz(local):
        # Rotate around Y by `yaw_rad`, then translate to (args.x, args.z).
        wx = cy * local[0] + sy * local[2] + args.x
        wz = -sy * local[0] + cy * local[2] + args.z
        return wx, wz

    print(f"\n-- Tank placement: world ({args.x:+.2f}, _, {args.z:+.2f}), "
          f"yaw {args.yaw:+.1f} deg")
    print(f"   wheel radius (main road): {WHEEL_RADIUS:.4f} m")
    print(f"   wheel mesh-local Z range: -1.991 .. +1.979 (3.97 m wheelbase)")
    print(f"   wheel mesh-local X range: -1.511 .. +1.511 (3.02 m track width)")

    print("\n-- Four-corner ground samples + wheel-centre Y")
    print(f"   {'corner':<12s}  {'world (x, z)':<22s}  "
          f"{'terrain_y':>9s}  {'wheel_center_y':>15s}")
    corners = [('FL', LOCAL_FL), ('FR', LOCAL_FR),
               ('RL', LOCAL_RL), ('RR', LOCAL_RR)]
    centers = []
    for label, local in corners:
        wx, wz = world_xz(local)
        ty     = bilinear_sample_height(heights, args.world_size, wx, wz)
        wy     = ty + WHEEL_RADIUS
        centers.append((wx, wy, wz))
        print(f"   {label:<12s}  ({wx:+7.2f}, {wz:+7.2f})    "
              f"{ty:+8.3f}     {wy:+8.3f}")

    # ---- 3. Plane fit + tilt ----------------------------------------
    centers = np.asarray(centers, dtype=np.float64)
    n, p0   = fit_plane(centers)
    pitch, roll = normal_to_pitch_roll(n)
    print(f"\n-- Plane fit through 4 wheel centres")
    print(f"   centroid:      ({p0[0]:+.3f}, {p0[1]:+.3f}, {p0[2]:+.3f})")
    print(f"   normal (unit): ({n[0]:+.4f}, {n[1]:+.4f}, {n[2]:+.4f})")
    print(f"   pitch (nose-up +): {pitch:+6.2f} deg")
    print(f"   roll  (right-up +): {roll:+6.2f} deg")
    # Per-wheel residual (deviation from the fitted plane along its normal).
    resid = (centers - p0) @ n
    print(f"   per-wheel deviation from plane (m):")
    for (label, _), r in zip(corners, resid):
        print(f"     {label}: {r:+.4f}")
    rms = float(np.sqrt(np.mean(resid * resid)))
    print(f"   RMS residual: {rms:.4f} m")

    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
