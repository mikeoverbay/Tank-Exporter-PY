"""Track-pad mesh load using the standard primitive pipeline
(Phase C step 2).

Pads load like any other tank primitive: parse the
`.primitives_processed` for geometry, parse the sibling
`.visual_processed` for material / texture refs, build one `Mesh`
object per sub-mesh with full PBR-shader-compatible state
(diffuse / normal / AO / GMM textures, alpha test, double-sided
flag).  The result is a `list[Mesh]` that can be appended to
`Viewer.meshes` and renders through the existing mesh-draw loop
unchanged.

This is the inline mirror of viewer.py's per-component mesh-build
block (around line 9000); we don't try to refactor that code
into a shared helper yet because the per-component path has a
lot of PBR-specific knobs (engine smoke hardpoints, GMM channel
routing, crash damage layer) that pads don't need and shouldn't
inherit.  Once step 3 (instanced render with a dedicated
`pad.vert` / `pad.frag`) lands, this file becomes the loader
behind `TrackPadRenderer` and the per-Mesh-object draw path
goes away.

Author: Coffee + Claude, 2026-05-09.
"""
from __future__ import annotations

import ctypes
import os
from typing import List, Optional

import numpy as np
from OpenGL.GL import (
    glGenBuffers, glBindBuffer, glBufferData, glBufferSubData,
    glEnableVertexAttribArray, glVertexAttribPointer,
    glVertexAttribDivisor, glBindVertexArray,
    glDrawElementsInstanced, glDeleteBuffers,
    glActiveTexture, glBindTexture,
    GL_ARRAY_BUFFER, GL_DYNAMIC_DRAW,
    GL_FLOAT, GL_FALSE, GL_TRIANGLES, GL_UNSIGNED_INT,
    GL_TEXTURE0, GL_TEXTURE1, GL_TEXTURE2, GL_TEXTURE3,
    GL_TEXTURE_2D,
)

from .loaders import (
    MeshParser, VisualLoader, TextureLoader, PkgExtractor,
)
from .mesh import Mesh


# Per-instance attribute layout for `pad.vert`.  A mat4 takes 4
# attribute locations (one per row), starting at 9.  Each row is
# a vec4 (4 floats, stride = 16 bytes).
_PAD_XFORM_ATTR_BASE = 9
_PAD_XFORM_ROW_SIZE  = 4 * 4    # bytes per row vec4
_PAD_XFORM_SIZE      = 16 * 4   # bytes per mat4


class TrackPadRenderer:
    """One pattern's worth of instanced track-pad rendering.

    Owns:
        * a reference to the source `Mesh` (positions / normals /
          uv0 / indices already on the GPU via `Mesh.build_vao`),
        * an instance VBO holding `(N_pads, 4, 4)` float32 mat4
          transforms in row-major order (chassis-local placement
          for each pad).

    Per frame:
        1. Caller computes a fresh `(N, 4, 4)` array (translation
           per pad in chassis-local space; rotation lands later).
        2. `update_transforms(arr)` uploads it via `glBufferSubData`.
        3. `render(shader, chassis_pose, view, proj)` issues a
           single `glDrawElementsInstanced` per Mesh in the
           pattern.  4 draw calls total per tank when both
           segment + segment2 patterns are present (segment-L,
           segment-R, segment2-L, segment2-R).

    Lifecycle:
        * Build at tank load (when pad meshes + spline data are
          ready).  Reuses the source `Mesh.vao`; this class just
          adds the per-instance VBO + the four divisor-1 attribute
          bindings on top of the existing VAO.
        * Reupload transforms each frame (cheap, 16-32 KB at
          most for tracked tanks).
        * Free the instance VBO + remove the divisor bindings on
          tank reload via `cleanup`.
    """

    def __init__(self, source_mesh: Mesh, max_instances: int):
        """Args:
            source_mesh: Already-built `Mesh` -- must have a valid
                `.vao`.  Its position / normal / uv0 / index
                attribs are reused; we add 4 divisor-1 attribs
                on top.
            max_instances: Upper bound on how many pad instances
                this renderer can draw in one call.  We allocate
                the instance VBO at this size and reupload only
                the active prefix per frame.  Pre-allocate so we
                never have to re-`glBufferData` mid-session.
        """
        self.mesh           = source_mesh
        self.max_instances  = int(max_instances)
        self.live_instances = 0     # set by `update_transforms`
        # Pad-local AABB half-extents -- needed so a per-pad
        # terrain-floor clamp can compute the lowest corner world
        # Y as `pad_center_y - (ex|R10| + ey|R11| + ez|R12|)`.
        # Computed once from the source mesh's positions so the
        # render path doesn't have to re-derive it.
        if (source_mesh.positions is not None
                and len(source_mesh.positions) > 0):
            pos = np.asarray(source_mesh.positions, dtype=np.float32)
            bb_min = pos.min(axis=0)
            bb_max = pos.max(axis=0)
            half = 0.5 * (bb_max - bb_min).astype(np.float32)
            # Centre the half-extent around the bbox CENTRE rather
            # than pad-local origin, but for the worst-case lowest
            # corner that's the same value because we use absolute
            # rotation columns below.  Stash the half extent for
            # the projection.
            self.bbox_half_extent = half
            # Per Coffee 2026-05-10 ("we need to find the actual
            # pivot point in the segments"): stash bbox_min /
            # bbox_max and the geometric centre so the render
            # side can detect whether the pad mesh's local
            # origin is at the bbox centre (= placement by
            # centre) or shifted toward one face (= placement
            # by hinge pin at a specific edge).  One-shot dump
            # below makes this visible per loaded pattern.
            self.bbox_min     = bb_min.astype(np.float32)
            self.bbox_max     = bb_max.astype(np.float32)
            self.bbox_center  = (0.5 * (bb_min + bb_max)
                                  ).astype(np.float32)
            try:
                mesh_name = getattr(source_mesh, 'name',
                                     '<unnamed pad mesh>')
            except Exception:
                mesh_name = '<unnamed pad mesh>'
            print(
                f"[pad-pivot] mesh='{mesh_name}'  "
                f"bbox_min=({bb_min[0]:+.4f}, {bb_min[1]:+.4f}, "
                f"{bb_min[2]:+.4f})  "
                f"bbox_max=({bb_max[0]:+.4f}, {bb_max[1]:+.4f}, "
                f"{bb_max[2]:+.4f})  "
                f"center=({self.bbox_center[0]:+.4f}, "
                f"{self.bbox_center[1]:+.4f}, "
                f"{self.bbox_center[2]:+.4f})  "
                f"origin_offset_from_center="
                f"({-self.bbox_center[0]:+.4f}, "
                f"{-self.bbox_center[1]:+.4f}, "
                f"{-self.bbox_center[2]:+.4f})")
        else:
            self.bbox_half_extent = np.array([0.0, 0.0, 0.0],
                                              dtype=np.float32)
            self.bbox_min    = np.array([0.0, 0.0, 0.0],
                                         dtype=np.float32)
            self.bbox_max    = np.array([0.0, 0.0, 0.0],
                                         dtype=np.float32)
            self.bbox_center = np.array([0.0, 0.0, 0.0],
                                         dtype=np.float32)

        # Allocate the per-instance VBO -- one mat4 per instance.
        self.instance_vbo = int(glGenBuffers(1))
        glBindBuffer(GL_ARRAY_BUFFER, self.instance_vbo)
        glBufferData(GL_ARRAY_BUFFER,
                     self.max_instances * _PAD_XFORM_SIZE,
                     None, GL_DYNAMIC_DRAW)

        # Wire the four divisor-1 mat4-row attributes onto the
        # source mesh's VAO.  This binds them once at construction
        # so subsequent frames just need a `glBindBuffer` +
        # `glBufferSubData` to update.
        glBindVertexArray(self.mesh.vao)
        for row in range(4):
            loc = _PAD_XFORM_ATTR_BASE + row
            glEnableVertexAttribArray(loc)
            glVertexAttribPointer(
                loc, 4, GL_FLOAT, GL_FALSE,
                _PAD_XFORM_SIZE,
                ctypes.c_void_p(row * _PAD_XFORM_ROW_SIZE))
            glVertexAttribDivisor(loc, 1)
        glBindVertexArray(0)

    def update_transforms(self, mat4_array: np.ndarray):
        """Upload a fresh `(N, 4, 4)` row-major float32 array as
        the per-instance transforms.  N must be <= max_instances.

        The shader reads each row as a vec4; transposing back to
        column-major happens inside `pad.vert` (`transpose()` on
        the reconstructed mat4) so the row-major numpy convention
        carries through unchanged.
        """
        arr = np.ascontiguousarray(mat4_array, dtype=np.float32)
        n = arr.shape[0] if arr.ndim == 3 else 0
        if n > self.max_instances:
            n = self.max_instances
            arr = arr[:n]
        if n == 0:
            self.live_instances = 0
            return
        glBindBuffer(GL_ARRAY_BUFFER, self.instance_vbo)
        glBufferSubData(GL_ARRAY_BUFFER, 0,
                         n * _PAD_XFORM_SIZE, arr)
        self.live_instances = n

    def render(self, shader, chassis_pose, view, projection,
               color=(0.55, 0.55, 0.60, 1.0)):
        """Issue ONE instanced draw covering `live_instances` copies
        of the source pad mesh.

        Args:
            shader      : `PadShader` instance.  Caller has already
                          bound the global per-frame PBR state
                          (light_pos, view_pos, IBL textures,
                          sliders) -- see
                          `Viewer._render_track_pad_body`.
            chassis_pose: 4x4 row-major float32 matrix -- the
                          tank's render-pose `chassis_matrix()`.
            view        : 4x4 view matrix.
            projection  : 4x4 projection matrix.
            color       : kept for backwards-compat signature, no
                          longer consumed by the PBR pad shader.
        """
        if not self.live_instances:
            return
        if self.mesh.vao is None or not self.mesh.index_count:
            return
        shader.use()
        shader.set_mat4('u_chassis_pose', chassis_pose)
        shader.set_mat4('u_view',         view)
        shader.set_mat4('u_proj',         projection)
        # Backwards-compat: original flat-color shader read u_color.
        # The PBR shader no longer uses it; the call is a silent
        # no-op when the uniform is optimised out of the program.
        shader.set_vec4('u_color', *color)

        # ---- Per-mesh material textures (units 0/1/2/3) --------------
        # Pads' diffuse / normal / AO / GMM tex IDs are loaded once
        # at chassis load by track_pads.py's PadVisualLoader (around
        # line 616-649); this is the per-frame bind site.  Each
        # call rebinds the unit -> tex association so the shader
        # picks up THIS pad mesh's textures rather than whatever
        # the previous instanced draw left on the unit.  Lines
        # match mesh.py:299's per-mesh bind dance exactly so the
        # behaviour is consistent across the main mesh pass and
        # the instanced pad pass.
        mesh = self.mesh
        if mesh.diffuse_tex_id:
            glActiveTexture(GL_TEXTURE0)
            glBindTexture(GL_TEXTURE_2D, mesh.diffuse_tex_id)
            shader.set_int('diffuse_map', 0)
        if mesh.normal_tex_id:
            glActiveTexture(GL_TEXTURE1)
            glBindTexture(GL_TEXTURE_2D, mesh.normal_tex_id)
            shader.set_int('normal_map', 1)
        if getattr(mesh, 'ao_tex_id', None):
            glActiveTexture(GL_TEXTURE2)
            glBindTexture(GL_TEXTURE_2D, mesh.ao_tex_id)
            shader.set_int('ao_map', 2)
            shader.set_int('has_ao_map', 1)
        else:
            shader.set_int('has_ao_map', 0)
        if getattr(mesh, 'gmm_tex_id', None):
            glActiveTexture(GL_TEXTURE3)
            glBindTexture(GL_TEXTURE_2D, mesh.gmm_tex_id)
            shader.set_int('gmm_map', 3)
            shader.set_int('has_gmm_map', 1)
        else:
            shader.set_int('has_gmm_map', 0)

        glBindVertexArray(self.mesh.vao)
        glDrawElementsInstanced(
            GL_TRIANGLES, self.mesh.index_count,
            GL_UNSIGNED_INT, None, self.live_instances)
        glBindVertexArray(0)

    def cleanup(self):
        """Free the per-instance VBO.  The source `Mesh` itself is
        owned by `Viewer.meshes` (or the pad-mesh dict) and freed
        elsewhere -- we just release our own buffer.
        """
        if self.instance_vbo:
            try:
                glDeleteBuffers(1, [self.instance_vbo])
            except Exception:
                pass
            self.instance_vbo = 0
        self.live_instances = 0


def build_position_only_transforms(pad_positions: np.ndarray) -> np.ndarray:
    """Build an `(N, 4, 4)` row-major mat4 array that just
    TRANSLATES each pad to its chassis-local pad_pos -- no
    rotation, no scale.  Used by commit-1 of the instanced
    render.  Kept around for A/B debugging when the oriented
    version misbehaves.
    """
    n = len(pad_positions)
    out = np.tile(np.eye(4, dtype=np.float32), (n, 1, 1))
    if n:
        out[:, 0, 3] = pad_positions[:, 0].astype(np.float32)
        out[:, 1, 3] = pad_positions[:, 1].astype(np.float32)
        out[:, 2, 3] = pad_positions[:, 2].astype(np.float32)
    return out


def build_oriented_transforms(pad_positions: np.ndarray,
                               pad_tangents:  np.ndarray,
                               pad_forward_axis: str = '-Z',
                               ) -> np.ndarray:
    """Build an `(N, 4, 4)` row-major mat4 array placing each pad
    at its `pad_pos` and rotating its local frame so the
    `pad_forward_axis` aligns with the spline tangent AND the
    pad's local "up" points OUTWARD from the loop centre.

    Per-pad up (per Coffee 2026-05-09 "below axle centerline
    flips it"):
        loop_centroid = mean(pad_pos)           # close to wheel-axle hub
        up_per_pad    = normalize(pad_pos - centroid, projected onto YZ)
    Top-run pads have up ~ +Y; bottom-run pads have up ~ -Y;
    wraparound pads transition smoothly through the loop's
    front / rear curves.  X is zeroed before normalising because
    the wheel axle runs along chassis-local X -- pads orbit the
    axle in the YZ plane.

    Frame construction (right-handed):
        forward = `pad_tan`
        right   = normalize(forward x up_per_pad)
        up      = right x forward       (re-orthogonalised)

    Resulting rotation maps:
        pad-local +X  -> right
        pad-local +Y  -> up_per_pad      (outward from axle)
        pad-local +Z  -> +/- forward    (sign per `pad_forward_axis`)

    Args:
        pad_positions    : `(N, 3)` chassis-local XYZ.
        pad_tangents     : `(N, 3)` unit tangents along the spline.
        pad_forward_axis : '+Z' or '-Z'.  Default '-Z' to match
                           the load-time Z-flip we apply to pad
                           geometry.  Flip if pads face backwards.

    Numerical stability: tangents parallel to up_per_pad yield a
    zero `right` vector; we clamp the normalisation denominators
    to 1e-9 so degenerate frames stay finite (the pad ends up at
    a plausible-if-arbitrary orientation rather than NaN).
    """
    n = len(pad_positions)
    out = np.tile(np.eye(4, dtype=np.float32), (n, 1, 1))
    if n == 0:
        return out
    pos = np.asarray(pad_positions, dtype=np.float32)
    fwd = np.asarray(pad_tangents,  dtype=np.float32).copy()
    fwd /= np.maximum(np.linalg.norm(fwd, axis=1, keepdims=True),
                       1e-9)

    # Per-pad outward (= local +Y).  Zero out X (axle direction)
    # so the loop is treated as a 2-D ring in YZ; remaining Y/Z
    # offset from the centroid IS the outward radial direction.
    centroid = pos.mean(axis=0)
    radial = pos - centroid
    radial[:, 0] = 0.0
    rnrm = np.linalg.norm(radial, axis=1, keepdims=True)
    up_per_pad = radial / np.maximum(rnrm, 1e-9)

    right = np.cross(fwd, up_per_pad)
    rrnm  = np.linalg.norm(right, axis=1, keepdims=True)
    right = right / np.maximum(rrnm, 1e-9)
    up_o  = np.cross(right, fwd)
    z_axis = (-fwd) if pad_forward_axis == '-Z' else fwd

    # Row-major mat4 -- column j of the GL matrix is numpy column j.
    out[:, 0, 0] = right[:, 0]
    out[:, 1, 0] = right[:, 1]
    out[:, 2, 0] = right[:, 2]
    out[:, 0, 1] = up_o[:, 0]
    out[:, 1, 1] = up_o[:, 1]
    out[:, 2, 1] = up_o[:, 2]
    out[:, 0, 2] = z_axis[:, 0]
    out[:, 1, 2] = z_axis[:, 1]
    out[:, 2, 2] = z_axis[:, 2]
    out[:, 0, 3] = pos[:, 0]
    out[:, 1, 3] = pos[:, 1]
    out[:, 2, 3] = pos[:, 2]
    return out


def build_wheel_aware_transforms(pad_positions: np.ndarray,
                                  pad_tangents:  np.ndarray,
                                  wheel_hubs:    np.ndarray | None,
                                  wheel_radius:  float,
                                  pad_forward_axis: str = '-Z',
                                  contact_band: float = 1.15,
                                  ) -> np.ndarray:
    """Build an `(N, 4, 4)` mat4 array per pad with WHEEL-AWARE
    orientation.

    Per Coffee 2026-05-09 "here is how you do the segs on the end
    wheels: draw a line A from wheel center to seg location on
    spline, rotate seg to be perpendicular to A":

        For each pad, find its CLOSEST wheel hub.  If the pad sits
        within `contact_band * wheel_radius` of that hub (i.e.
        the wheel-clamp would have anchored it on the wheel
        circumference), build the pad frame with

            Y_local  = normalize(pad_pos - hub)        (radial out)
            X_local  = chassis-local +X (cross-track, unchanged)
            Z_local  = sign-corrected(X_local x Y_local)

        where the sign of Z_local is chosen so it agrees with
        `pad_tan` (positive dot product).  This makes the pad
        bottom face hug the wheel circumference around the
        wraparound region instead of inheriting the steep V_loc
        wraparound chord tangent.

    Pads OUTSIDE the contact band fall back to the tangent +
    centroid-up orientation built by `build_oriented_transforms`
    -- top-run pads, free pads in the wraparound that aren't
    clamped to a wheel, etc.  The two orientations are identical
    when the closest hub is directly above the pad (= bottom-run
    middle), so the seam between modes is invisible in the
    common case.

    Args:
        pad_positions    : `(N, 3)` chassis-local XYZ pad centres.
        pad_tangents     : `(N, 3)` unit tangents along the spline.
            Used both as the fallback forward axis AND as the
            sign reference for the radial-mode `Z_local`.
        wheel_hubs       : `(W, 3)` chassis-local XYZ wheel hubs.
            None or empty -> radial mode is disabled and every
            pad falls back to `build_oriented_transforms`.
        wheel_radius     : Wheel radius in metres (one value used
            for all wheels; from `tank_physics.radius`).
        pad_forward_axis : '+Z' or '-Z'; matches the load-time
            geometry Z-flip.  Default '-Z' (current pipeline).
        contact_band     : Multiplier on `wheel_radius` setting
            the radial-mode threshold.  At 1.0 only pads clamped
            EXACTLY on the circumference qualify; at 1.15 the
            band reaches ~5 cm beyond, picking up pads in the
            transition zone.

    Returns:
        `(N, 4, 4)` row-major float32 mat4 array.  Drop-in
        replacement for `build_oriented_transforms` output.
    """
    fallback = build_oriented_transforms(
        pad_positions, pad_tangents, pad_forward_axis=pad_forward_axis)

    n = len(pad_positions)
    if (n == 0 or wheel_hubs is None or len(wheel_hubs) == 0
            or wheel_radius <= 0.0):
        return fallback

    pos  = np.asarray(pad_positions, dtype=np.float32)
    tan  = np.asarray(pad_tangents,  dtype=np.float32)
    hubs = np.asarray(wheel_hubs,    dtype=np.float32)

    # Per-pad closest hub by 3-D distance.
    delta = pos[:, np.newaxis, :] - hubs[np.newaxis, :, :]   # (N, W, 3)
    dist  = np.sqrt(np.sum(delta * delta, axis=2))           # (N, W)
    cw    = np.argmin(dist, axis=1)                          # (N,)
    idx_n = np.arange(n)
    closest_dist  = dist[idx_n, cw]
    closest_delta = delta[idx_n, cw, :]                      # (N, 3)

    threshold = float(wheel_radius) * float(contact_band)
    on_wheel  = closest_dist <= threshold

    if not np.any(on_wheel):
        return fallback

    # Radial Y_local = normalize(pad - hub).  Skip the safe-divide
    # branch where the pad sits AT the hub (essentially zero
    # gap); those pads keep the fallback frame.
    rnrm = np.linalg.norm(closest_delta, axis=1, keepdims=True)
    safe = rnrm > 1e-6
    y_local = np.zeros_like(closest_delta)
    np.divide(closest_delta, np.maximum(rnrm, 1e-9),
              out=y_local, where=safe)

    # X_local = chassis-local +X.  Constant across pads.  Per
    # Coffee 2026-05-09 the pin axis is the cross-track direction
    # so X_local doesn't depend on the pad's location.
    x_local = np.tile(np.array([1.0, 0.0, 0.0], dtype=np.float32),
                       (n, 1))

    # Z_local = X x Y, then sign-correct against the spline
    # tangent so adjacent pads stay in traversal order.
    z_local = np.cross(x_local, y_local)
    znrm = np.linalg.norm(z_local, axis=1, keepdims=True)
    z_local = z_local / np.maximum(znrm, 1e-9)
    sign = np.sign(np.einsum('ij,ij->i', z_local, tan))
    sign[sign == 0] = 1.0
    z_local = z_local * sign[:, np.newaxis]

    # Re-orthogonalise X = Y x Z so the frame stays a clean
    # rotation under the small numerical drift the cross+normalise
    # introduces.  Y stays as the canonical radial; Z is the
    # tangent we just sign-corrected.
    x_re = np.cross(y_local, z_local)
    xnrm = np.linalg.norm(x_re, axis=1, keepdims=True)
    x_re = x_re / np.maximum(xnrm, 1e-9)

    # `pad_forward_axis` flips Z so the pad's local +Z OR -Z
    # aligns with the traversal tangent.  Default '-Z' matches
    # the load-time Z-flip the pad mesh receives.
    z_axis = (-z_local) if pad_forward_axis == '-Z' else z_local

    out = fallback.copy()
    sel = on_wheel & safe.flatten()
    if np.any(sel):
        out[sel, 0, 0] = x_re[sel, 0]
        out[sel, 1, 0] = x_re[sel, 1]
        out[sel, 2, 0] = x_re[sel, 2]
        out[sel, 0, 1] = y_local[sel, 0]
        out[sel, 1, 1] = y_local[sel, 1]
        out[sel, 2, 1] = y_local[sel, 2]
        out[sel, 0, 2] = z_axis[sel, 0]
        out[sel, 1, 2] = z_axis[sel, 1]
        out[sel, 2, 2] = z_axis[sel, 2]
    return out


def load_pad_meshes(viewer, prim_path: str,
                    visual_path: str | None,
                    component_tag: str = 'track_pad') -> List[Mesh]:
    """Load a single track-pad `.primitives_processed` (with its
    sibling `.visual_processed`) into a list of GPU-ready `Mesh`
    objects.

    Args:
        viewer: The active `Viewer` instance.  Provides
            `_pkg_extractor`, `_cfg`, `_shared_tex_cache` and the
            res_mods root the texture resolver needs.
        prim_path: Absolute filesystem path to the
            `.primitives_processed` (already resolved by
            `Viewer._resolve_track_pad_paths`).
        visual_path: Absolute filesystem path to the matching
            `.visual_processed`, or None if it didn't resolve.
            None is OK -- meshes still load with placeholder
            grey diffuse / flat normal.
        component_tag: Value to set on `mesh.component`.  Default
            `'track_pad'` so the renderer can later filter pads
            out of the standard mesh-draw loop into the instanced
            path.

    Returns:
        list[Mesh]: one Mesh per sub-mesh in the pad's primitive
        groups, with VAOs uploaded.  Empty list on parse failure
        (logged once -- the caller doesn't need to react).

    Loaders this routine reuses verbatim (so any future fix to
    those modules applies to pads automatically):
        MeshParser.parse_primitives_processed
        VisualLoader.parse_textures
        VisualLoader.resolve_hd_path
        TextureLoader.load_texture / .create_placeholder
        Mesh                       (from .mesh)

    Pad textures live next to the .primitives_processed in the
    same `vehicles/<n>/<tank>/track/` folder; no additional path
    massaging needed beyond `resolve_hd_path` (which already
    handles _hd suffix variants and PKG fallback).
    """
    res_mods_root = (viewer._cfg.get('res_mods') or '').strip()
    pkg = getattr(viewer, '_pkg_extractor', None)

    # ---- Parse geometry ---------------------------------------
    try:
        parsed_groups = MeshParser.parse_primitives_processed(prim_path)
    except Exception as exc:
        print(f'[track_pads] parse failed: {prim_path}: '
              f'{type(exc).__name__}: {exc}')
        return []
    if not parsed_groups:
        return []
    group_names = [g['name'] for g in parsed_groups]

    # ---- DX -> GL handedness fix (per Coffee 2026-05-09) ------
    # Pads are authored skinned in the source (BigWorld bakes
    # bone weights for the original engine's track-segment
    # animation), so `MeshParser.parse_primitives_processed`
    # sets `flip_z = False` for them -- non-skinned hull /
    # turret / gun automatically get `flip_z = True` via the
    # `flip_z = not has_bones` rule.
    #
    # We don't bone-skin pads (they'll instance off a per-pad
    # transform in step-3), so the chassis-handed CW winding
    # leaks straight to GL and back-face culling drops every
    # visible triangle.  Symptom Coffee hit first time:
    # "winding order is reverse".
    #
    # Fix: post-parse, negate Z on positions / normals /
    # tangents / binormals -- exactly what `flip_z = True` does
    # for hull / turret / gun.  The geometric reflection
    # implicitly reverses triangle winding (a Z-flip swaps the
    # orientation observers see); MeshParser's index parser
    # never touches winding so we don't either.
    #
    # MeshParser's policy stays untouched -- we only override
    # at the per-mesh level so the chassis ribbon mesh, which
    # legitimately needs `flip_z = False`, still works.
    for g in parsed_groups:
        verts = g['vertices']
        for key in ('positions', 'normals', 'tangents', 'binormals'):
            arr = verts.get(key)
            if arr is None:
                continue
            arr = np.asarray(arr, dtype=np.float32)
            if arr.ndim == 2 and arr.shape[1] == 3:
                arr = arr.copy()
                arr[:, 2] *= -1.0
                verts[key] = arr

    # NOTE: an earlier attempt at a "pad-thickness lift" here
    # (shift positions up by `-bbox.min_y` so the bottom face
    # lands at pad-local Y = 0) inverted the pad orientation
    # because some pad meshes are authored with the outer
    # surface at -Y rather than +Y.  Reverted 2026-05-09 per
    # Coffee's "tracks are facing the wrong directions" report;
    # the geometric centre at pad-local origin is the safer
    # placement.  If a per-pad lift is needed, do it world-side
    # in `build_oriented_transforms` along the per-pad up
    # direction (signed knob the caller can flip per tank).

    # ---- Parse material refs from visual_processed ------------
    group_textures = {}
    if visual_path and os.path.isfile(visual_path):
        try:
            group_textures = VisualLoader.parse_textures(
                visual_path, group_names)
        except Exception as exc:
            print(f'[track_pads] visual parse failed: '
                  f'{visual_path}: {exc}')

    # ---- Build Mesh objects -----------------------------------
    # Mirror of the viewer's per-component build loop (load_mesh
    # path), simplified: pads don't carry skinning, don't need a
    # bone_palette, don't have engine-smoke hardpoints, and can
    # skip the GMM / crash-damage knobs that the PBR shader honors
    # but pads don't typically use.  If a specific tank ships pads
    # with GMM (rare), the std mesh shader will still consume the
    # texture -- we just don't go out of our way to plumb it.
    meshes: List[Mesh] = []
    for group in parsed_groups:
        group_name = group['name']
        materials  = group_textures.get(group_name, [])
        # Lift `_split_into_submeshes` out of viewer to keep this
        # module standalone-importable.  Avoids a circular
        # dependency (viewer imports track_pads at runtime).
        from .viewer import _split_into_submeshes
        for synth, tex in _split_into_submeshes(group, materials):
            mesh = Mesh(synth)
            mesh.component = component_tag

            # Diffuse
            if 'diffuse' in tex and pkg is not None:
                resolved, _ = VisualLoader.resolve_hd_path(
                    tex['diffuse'], res_mods_root, pkg)
                if resolved:
                    mesh.diffuse_tex_id = TextureLoader.load_texture(resolved)
                    mesh.diffuse_path   = resolved
                else:
                    mesh.diffuse_tex_id = TextureLoader.create_placeholder(0.7)
            else:
                mesh.diffuse_tex_id = TextureLoader.create_placeholder(0.7)

            # Normal
            if 'normal' in tex and pkg is not None:
                resolved, _ = VisualLoader.resolve_hd_path(
                    tex['normal'], res_mods_root, pkg)
                if resolved:
                    mesh.normal_tex_id = TextureLoader.load_texture(
                        resolved, is_normal=True)
                    mesh.normal_path   = resolved
                else:
                    mesh.normal_tex_id = TextureLoader.create_placeholder(0.5)
            else:
                mesh.normal_tex_id = TextureLoader.create_placeholder(0.5)

            # AO
            if 'ao' in tex and pkg is not None:
                resolved, _ = VisualLoader.resolve_hd_path(
                    tex['ao'], res_mods_root, pkg)
                if resolved:
                    mesh.ao_tex_id = TextureLoader.load_texture(resolved)
                    mesh.ao_path   = resolved

            # GMM (PBR-shader colour grading; harmless absent)
            if 'gmm' in tex and pkg is not None:
                resolved, _ = VisualLoader.resolve_hd_path(
                    tex['gmm'], res_mods_root, pkg)
                if resolved:
                    mesh.gmm_tex_id = TextureLoader.load_texture(resolved)
                    mesh.gmm_path   = resolved

            # Material flags (alpha test, double-sided, identifier)
            mesh.alpha_reference   = int(tex.get('alphaReference', 0))
            mesh.alpha_test_enable = bool(tex.get('alphaTestEnable', False))
            mesh.double_sided      = bool(tex.get('doubleSided',
                                                    mesh.alpha_test_enable))
            mesh.identifier        = str(tex.get('identifier', ''))
            mesh.alpha_in_normal_red = bool(
                tex.get('alpha_in_normal_red', False))
            mesh.ao_in_diffuse_alpha = bool(
                tex.get('ao_in_diffuse_alpha', False))
            mesh.fx                = str(tex.get('fx', ''))

            # Pad mesh sits at chassis-local origin until the
            # instanced renderer (step 3) starts placing it per-
            # pad.  Identity bind_model_matrix means
            # `chassis_pose @ bind = chassis_pose`, so a plain-
            # added Mesh will render at chassis origin.
            mesh.bind_model_matrix = np.eye(4, dtype=np.float32)
            mesh.model_matrix      = mesh.bind_model_matrix.copy()
            mesh.bone_palette      = None     # pads don't skin

            try:
                mesh.build_vao()
            except Exception as exc:
                print(f'[track_pads] build_vao failed for '
                      f'{group_name}: {exc}')
                continue
            meshes.append(mesh)

    return meshes


def load_pad_mesh_set(viewer, resolved: dict,
                       component_tag: str = 'track_pad') -> dict:
    """Load every entry in `resolved` (the dict
    `Viewer._resolve_track_pad_paths` produces) into Mesh
    objects.  Returns `{key: list[Mesh]}` for every key whose
    .primitives_processed resolved.
    """
    out: dict = {}
    for key, paths in (resolved or {}).items():
        prim = paths.get('primitives')
        vis  = paths.get('visual')
        if not prim:
            continue
        meshes = load_pad_meshes(viewer, prim, vis,
                                  component_tag=component_tag)
        if meshes:
            out[key] = meshes
            tot_v = sum(len(m.positions) for m in meshes)
            tot_t = sum(m.index_count // 3 for m in meshes)
            print(f'[track_pads] {key}: loaded -- '
                  f'{len(meshes)} mesh(es), {tot_v} verts, '
                  f'{tot_t} tris')
    return out
