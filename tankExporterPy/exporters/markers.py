"""
Synthetic marker meshes shipped alongside the real tank geometry.

Purpose: a modder editing the exported FBX/GLB in Blender needs to SEE
(and later MOVE) the WoT wheel locations.  The viewer knows those
locations from `Chassis.visual_processed` (bone positions) + the
chassis XML (`wheel_roles` classification); neither survives a plain
mesh export.  So we bake small synthetic meshes into the payload:

  * Wheel PINS  -- thin cylindrical rods, one per wheel bone, axis
    along the wheel axle (chassis-local +X).  The rod COLOR encodes
    the wheel ROLE so a modder can tell a drive sprocket from a road
    wheel at a glance:

        road wheel     -> green
        drive sprocket -> red
        idler          -> blue
        return roller  -> yellow

    (grey is reserved for the bone marker balls.)

  * Bone BALLS -- small grey spheres, one per animation bone (added in
    a follow-up task).

Reserved mesh names -- the importer keys off these prefixes and the
Save-Prim writer must NEVER treat them as real WoT sections:

    WoTPin_<roleToken>_<boneName>     roleToken has no underscores, so
                                      the bone name is everything after
                                      the second '_'.
    WoTBall_<boneName>

Coordinate frames: `viewer._track_chassis_bones_bind` comes from
`parse_chassis_bone_world_positions`, which returns Z with the
OPPOSITE sign to the chassis mesh-vertex frame (the loader keeps
skinned chassis verts in BigWorld native frame, no flip).  Markers are
emitted in the chassis mesh-vertex frame so they ride the same
`model_matrix` as the chassis meshes -- hence the `-z` below, same
rule the extra-rotating-wheel wiring in viewer.py applies.
"""

import math

import numpy as np


# Marker-name prefixes.  Import side + Save-Prim writer both key off
# these; keep them in one place.
PIN_PREFIX  = 'WoTPin_'
BALL_PREFIX = 'WoTBall_'

# role-dict key (minus the _L/_R suffix) -> (roleToken, RGBA color).
# roleToken deliberately has NO underscores so an importer can split
# 'WoTPin_<roleToken>_<boneName>' unambiguously.
PIN_ROLE_STYLES = {
    'road_wheels':     ('roadwheel', (0.10, 0.85, 0.10, 1.0)),   # green
    'drive_sprockets': ('sprocket',  (0.90, 0.10, 0.10, 1.0)),   # red
    'idlers':          ('idler',     (0.15, 0.35, 0.95, 1.0)),   # blue
    'return_rollers':  ('roller',    (0.95, 0.85, 0.10, 1.0)),   # yellow
}

# Pin geometry: a real cylinder the user can SEE.  The original spec
# said ~1 mm diameter, but per Coffee 2026-10-19 ("i cant see the
# pins") that was invisible next to a ~1.5 m wheel -- 2 cm diameter
# reads clearly in Blender's viewport while still being obviously a
# marker, not tank geometry.  Long enough to poke out of both faces of
# the widest WoT wheel (~0.5 m).
PIN_RADIUS   = 0.01      # metres (2 cm diameter)
PIN_LENGTH   = 0.6       # metres, centred on the hub
PIN_SEGMENTS = 12

# Bone-ball geometry: all balls GREY per the task spec -- the color
# channel is already spent on the pins' wheel-role coding, and a modder
# only needs to see WHERE the animation bones sit.
BALL_RADIUS = 0.01       # metres (2 cm diameter)
BALL_COLOR  = (0.55, 0.55, 0.55, 1.0)
BALL_SEGS   = 8          # longitude steps
BALL_RINGS  = 6          # latitude steps


def marker_color_for_name(name):
    """RGBA for a marker mesh, derived purely from its reserved NAME
    (`WoTPin_<roleToken>_...` / `WoTBall_...`).  Lets the import side
    restore the role color even when the file format dropped the flat
    material (FBX/OBJ round trips keep names far more reliably than
    untextured material colors).  None for non-marker names."""
    n = (name or '').strip()
    if n.startswith(BALL_PREFIX):
        return tuple(BALL_COLOR)
    if n.startswith(PIN_PREFIX):
        token = n[len(PIN_PREFIX):].split('_', 1)[0].split('.')[0]
        for _role, (tok, color) in PIN_ROLE_STYLES.items():
            if tok == token:
                return tuple(color)
        return (0.85, 0.20, 0.85, 1.0)   # unknown role: magenta
    return None


def is_marker_name(name):
    """True when `name` is one of our reserved synthetic marker meshes
    (wheel pin or bone ball).  Case-insensitive, tolerates Blender's
    '.001' collision suffixes."""
    n = (name or '').strip()
    return (n.startswith(PIN_PREFIX) or n.startswith(BALL_PREFIX))


def build_marker_meshes(viewer):
    """Return a list of payload-mesh dicts (same schema as
    `common.collect_payload` emits) holding the synthetic markers for
    the currently loaded tank.  Empty list when the viewer has no
    chassis-bone data (standalone prim load, very old tank, etc.).
    """
    out = []
    out.extend(_build_wheel_pins(viewer))
    out.extend(_build_bone_balls(viewer))
    return out


# ---------------------------------------------------------------------------
# Wheel pins
# ---------------------------------------------------------------------------

def _build_wheel_pins(viewer):
    bones = getattr(viewer, '_track_chassis_bones_bind', None) or {}
    ci    = getattr(viewer, '_pending_chassis_info', None) or {}
    roles = ci.get('wheel_roles') or {}
    if not bones or not roles:
        return []

    model_matrix = _chassis_model_matrix(viewer)

    pins = []
    seen = set()
    for role_base, (token, color) in PIN_ROLE_STYLES.items():
        material = {
            'name':         f'WoTPin_{token}',
            'diffuse':      None,
            'normal':       None,
            'ao':           None,
            'gmm':          None,
            'double_sided': False,
            'alpha_test':   False,
            'base_color':   list(color),
        }
        for side in ('L', 'R'):
            for nm in roles.get(f'{role_base}_{side}', []):
                if nm in seen:
                    continue
                pos = bones.get(nm)
                if pos is None:
                    pos = bones.get(nm + '_BlendBone')
                if pos is None:
                    continue
                seen.add(nm)
                # parse_chassis_bone_world_positions Z is opposite the
                # chassis mesh-vertex frame -- negate (see module doc).
                hub = (float(pos[0]), float(pos[1]), -float(pos[2]))
                positions, normals, indices = _cylinder_x(
                    hub, PIN_RADIUS, PIN_LENGTH, PIN_SEGMENTS)
                pins.append({
                    'name':         f'{PIN_PREFIX}{token}_{nm}',
                    'positions':    positions,
                    'normals':      normals,
                    'tangents':     None,
                    'binormals':    None,
                    'uvs':          [(0.0, 0.0)] * len(positions),
                    'uvs2':         None,
                    'indices':      indices,
                    'model_matrix': model_matrix,
                    'material':     dict(material),
                    'bone_indices': None,
                    'bone_weights': None,
                })
    return pins


# ---------------------------------------------------------------------------
# Bone balls
# ---------------------------------------------------------------------------

def _build_bone_balls(viewer):
    """One small grey sphere per chassis animation bone.

    Every named node `parse_chassis_bone_world_positions` found gets a
    ball -- wheel bones included (their pin marks the ROLE, the ball
    marks the BONE; they overlap by design).  Bare / `_BlendBone` name
    pairs that sit at the SAME position collapse to one ball (the
    `_BlendBone` form, since that's the skin-palette name) so we don't
    ship coincident duplicate spheres.
    """
    bones = getattr(viewer, '_track_chassis_bones_bind', None) or {}
    if not bones:
        return []

    model_matrix = _chassis_model_matrix(viewer)
    material = {
        'name':         'WoTBall_bone',
        'diffuse':      None,
        'normal':       None,
        'ao':           None,
        'gmm':          None,
        'double_sided': False,
        'alpha_test':   False,
        'base_color':   list(BALL_COLOR),
    }

    balls = []
    for nm in sorted(bones.keys()):
        if not nm.endswith('_BlendBone'):
            twin = bones.get(nm + '_BlendBone')
            if twin is not None and np.allclose(
                    np.asarray(bones[nm], dtype=np.float64),
                    np.asarray(twin, dtype=np.float64), atol=1e-6):
                continue    # _BlendBone twin at same spot wins
        pos = bones[nm]
        # Same frame rule as the pins: negate parse-Z into the chassis
        # mesh-vertex frame.
        centre = (float(pos[0]), float(pos[1]), -float(pos[2]))
        positions, normals, indices = _uv_sphere(
            centre, BALL_RADIUS, BALL_SEGS, BALL_RINGS)
        balls.append({
            'name':         f'{BALL_PREFIX}{nm}',
            'positions':    positions,
            'normals':      normals,
            'tangents':     None,
            'binormals':    None,
            'uvs':          [(0.0, 0.0)] * len(positions),
            'uvs2':         None,
            'indices':      indices,
            'model_matrix': model_matrix,
            'material':     dict(material),
            'bone_indices': None,
            'bone_weights': None,
        })
    return balls


def _uv_sphere(center, radius, segs, rings):
    """UV sphere centred on `center`.  Returns (positions, normals,
    indices) as plain Python lists, outward-CCW winding, smooth radial
    normals.  Poles are single verts; `rings` counts latitude STEPS
    (so rings-1 interior rings of `segs` verts each).
    """
    cx, cy, cz = center
    positions = [(cx, cy + radius, cz)]        # north pole (index 0)
    normals   = [(0.0, 1.0, 0.0)]
    for r in range(1, rings):
        phi = math.pi * r / rings              # 0=north pole
        y   = math.cos(phi)
        s   = math.sin(phi)
        for q in range(segs):
            th = 2.0 * math.pi * q / segs
            nx, ny, nz = s * math.cos(th), y, s * math.sin(th)
            positions.append((cx + radius * nx,
                              cy + radius * ny,
                              cz + radius * nz))
            normals.append((nx, ny, nz))
    south = len(positions)
    positions.append((cx, cy - radius, cz))
    normals.append((0.0, -1.0, 0.0))

    def ring_idx(r, q):
        return 1 + (r - 1) * segs + (q % segs)

    indices = []
    # North cap: pole -> first ring.  theta runs +X toward +Z, which is
    # CLOCKWISE seen from outside above the pole in this Y-up frame, so
    # outward-CCW needs the (pole, q+1, q) order.
    for q in range(segs):
        indices.extend((0, ring_idx(1, q + 1), ring_idx(1, q)))
    # Bands
    for r in range(1, rings - 1):
        for q in range(segs):
            a0 = ring_idx(r,     q)
            a1 = ring_idx(r,     q + 1)
            b0 = ring_idx(r + 1, q)
            b1 = ring_idx(r + 1, q + 1)
            indices.extend((a0, a1, b1))
            indices.extend((a0, b1, b0))
    # South cap
    for q in range(segs):
        indices.extend((south, ring_idx(rings - 1, q),
                        ring_idx(rings - 1, q + 1)))
    return positions, normals, indices


def _chassis_model_matrix(viewer):
    """Flat 16-float bind model matrix of the chassis component, so
    markers land in the same world spot as the chassis meshes they
    annotate.  Falls back to the PKG twin set (markers spawned onto an
    imported FBX scene whose meshes carry no component tags), then to
    identity (standalone-prim loads)."""
    pkg_set = getattr(viewer, '_pkg_set', None)
    mesh_lists = [getattr(viewer, 'meshes', None) or []]
    if pkg_set is not None:
        mesh_lists.append(getattr(pkg_set, 'meshes', None) or [])
    for meshes in mesh_lists:
        for mesh in meshes:
            if (getattr(mesh, 'component', '') or '').lower() != 'chassis':
                continue
            mm = getattr(mesh, 'bind_model_matrix', None)
            if mm is None:
                mm = getattr(mesh, 'model_matrix', None)
            if mm is not None:
                return [float(v) for v in np.asarray(mm).reshape(-1)]
    return [float(v) for v in np.eye(4, dtype=np.float32).reshape(-1)]


def _cylinder_x(center, radius, length, segments):
    """Closed cylinder whose axis runs along +X, centred on `center`.

    Returns (positions, normals, indices) as plain Python lists --
    the payload is JSON-serialised so numpy types must not leak out.
    Winding is CCW viewed from outside (matches the viewer/exporter
    front-face convention after collect_payload; these verts never
    pass through the loader's CW->CCW flip, so we author CCW here).
    """
    cx, cy, cz = center
    x0 = cx - length * 0.5
    x1 = cx + length * 0.5

    positions = []
    normals   = []
    indices   = []

    # Side wall: two rings of `segments` verts with radial normals.
    for xi, x in enumerate((x0, x1)):
        for s in range(segments):
            a = 2.0 * math.pi * s / segments
            ny, nz = math.cos(a), math.sin(a)
            positions.append((x, cy + radius * ny, cz + radius * nz))
            normals.append((0.0, ny, nz))
    for s in range(segments):
        s2 = (s + 1) % segments
        a0, a1 = s, s2                       # ring at x0
        b0, b1 = segments + s, segments + s2  # ring at x1
        indices.extend((a0, b1, b0))
        indices.extend((a0, a1, b1))

    # End caps: centre vert + reuse of ring angles with axial normals.
    for xi, (x, nx) in enumerate(((x0, -1.0), (x1, 1.0))):
        c_idx = len(positions)
        positions.append((x, cy, cz))
        normals.append((nx, 0.0, 0.0))
        ring = len(positions)
        for s in range(segments):
            a = 2.0 * math.pi * s / segments
            positions.append((x, cy + radius * math.cos(a),
                              cz + radius * math.sin(a)))
            normals.append((nx, 0.0, 0.0))
        for s in range(segments):
            s2 = (s + 1) % segments
            if nx > 0:
                indices.extend((c_idx, ring + s, ring + s2))
            else:
                indices.extend((c_idx, ring + s2, ring + s))

    return positions, normals, indices
