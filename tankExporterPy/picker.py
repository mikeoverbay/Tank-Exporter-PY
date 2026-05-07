"""Off-screen triangle / mesh picker for the tank viewer.

Renders the loaded tank's geometry into a hidden RGBA8 framebuffer
each frame the picker is active, encoding (mesh_id, primitive_id)
into the pixel colour, then reads one pixel under the mouse to
recover "which triangle of which mesh did the user hover over".

Two output paths:

  1.  Console dump.  When the hovered triangle CHANGES, the
      picker clears the console and writes three colour-coded
      lines (one per vertex) showing that vertex's bone indices
      and weights.  The vertex colour matches the on-screen
      vertex marker so users can connect the dots between the
      data and the geometry visually.

  2.  3-D overlay.  After the main mesh pass, the picker draws:
        - the picked triangle filled in `theme.c1` with alpha
          (the underlying material still shows through);
        - the three triangle edges as line strips in `theme.c2`;
        - three points at the vertices, vertex 0 = red,
          vertex 1 = green, vertex 2 = blue.

The whole picker lives behind the `Pick Tri` button in the Tools
group; when off, no FBO bind / readback / overlay happens at all.

Architecture
------------

`TrianglePicker` owns:

  * a self-resizing FBO (color + depth renderbuffer) sized to the
    scene viewport;
  * the picking shader program (`shaders/picking.{vert,frag}`);
  * the overlay shader program (`shaders/overlay_solid.{vert,frag}`);
  * a small dynamic VAO/VBO for the 3-vertex highlighted-triangle
    geometry (re-uploaded each frame the hovered triangle changes).

The viewer calls:

  - `picker.update_pass(meshes, view, proj, mouse_xy, viewport)`
    each frame BEFORE the main mesh pass; it fills the FBO and
    reads back the hit.

  - `picker.draw_overlay(view, proj)` AFTER the main mesh pass;
    it paints the highlight on the visible scene.

The hover-changed event is the trigger for the console dump.
The picker exposes `picker.last_hit` (`(mesh_idx, tri_idx)` or
`None`) so external code (UI, exporters) can read the current
selection without re-running the pass.
"""

import os
import struct

import numpy as np
from OpenGL.GL import *

from .shaders import _compile_program, load_shader_file


# ---------------------------------------------------------------------------
# Picking + overlay shader programs.  Tiny, hard-coded uniform set --
# we don't need the heavy ShaderProgram wrapper from shaders.py.
# ---------------------------------------------------------------------------

class _PickingShader:
    """Wraps the compiled picking program + cached uniform locations."""
    def __init__(self):
        vs = load_shader_file('shaders/picking.vert')
        fs = load_shader_file('shaders/picking.frag')
        self.program = _compile_program(vs, fs, 'Picking shader')
        self.loc_view     = glGetUniformLocation(self.program, 'u_view')
        self.loc_proj     = glGetUniformLocation(self.program, 'u_proj')
        self.loc_model    = glGetUniformLocation(self.program, 'u_model')
        self.loc_mesh_id  = glGetUniformLocation(self.program, 'u_mesh_id')

    def use(self):
        glUseProgram(self.program)

    def set_view_proj(self, view, proj):
        glUniformMatrix4fv(self.loc_view, 1, GL_TRUE, view)
        glUniformMatrix4fv(self.loc_proj, 1, GL_TRUE, proj)

    def set_model(self, model):
        glUniformMatrix4fv(self.loc_model, 1, GL_TRUE, model)

    def set_mesh_id(self, mesh_id):
        glUniform1i(self.loc_mesh_id, int(mesh_id))


class _OverlayShader:
    """Wraps the compiled overlay-solid program + cached uniforms."""
    def __init__(self):
        vs = load_shader_file('shaders/overlay_solid.vert')
        fs = load_shader_file('shaders/overlay_solid.frag')
        self.program = _compile_program(vs, fs, 'Overlay-solid shader')
        self.loc_view  = glGetUniformLocation(self.program, 'u_view')
        self.loc_proj  = glGetUniformLocation(self.program, 'u_proj')
        self.loc_model = glGetUniformLocation(self.program, 'u_model')
        self.loc_color = glGetUniformLocation(self.program, 'u_color')

    def use(self):
        glUseProgram(self.program)

    def set_view_proj(self, view, proj):
        glUniformMatrix4fv(self.loc_view, 1, GL_TRUE, view)
        glUniformMatrix4fv(self.loc_proj, 1, GL_TRUE, proj)

    def set_model(self, model):
        glUniformMatrix4fv(self.loc_model, 1, GL_TRUE, model)

    def set_color(self, r, g, b, a=1.0):
        glUniform4f(self.loc_color,
                    float(r), float(g), float(b), float(a))


# ---------------------------------------------------------------------------
# Triangle picker
# ---------------------------------------------------------------------------

class TrianglePicker:
    """Off-screen picker + overlay renderer for the tank viewer.

    `enabled` toggles the whole feature.  When False:
      * `update_pass` is a no-op;
      * `draw_overlay` is a no-op;
      * `last_hit` stays at its last value (so the console doesn't
        get cleared just because the user toggled the picker off).

    `update_pass` runs the picking render + readback each frame.  It
    detects the hover-change edge internally and calls back into the
    `viewer` to print the per-vertex bone data when the hit changes.
    """

    # Per-vertex marker colours, in (R, G, B) 0..255 order so they
    # double as console-line colours.  Order matters: vertex 0 of
    # the picked triangle = vertex_colors[0] = red, etc.
    VERTEX_COLORS = (
        (220,  60,  60),    # vertex 0 -- red
        ( 60, 220,  60),    # vertex 1 -- green
        ( 80, 130, 255),    # vertex 2 -- blue
    )

    def __init__(self):
        self.enabled = False

        # FBO + attachments.  Created lazily on the first update_pass
        # call so we can do GL ops in the right order (after the
        # context is current).  Re-allocated when scene viewport
        # dimensions change.
        self._fbo        = 0
        self._color_tex  = 0
        self._depth_rbo  = 0
        self._fbo_w      = 0
        self._fbo_h      = 0

        # Shader programs; built lazily.
        self._pick_sh    = None
        self._over_sh    = None

        # Highlighted-triangle dynamic VAO (3 vertices).  Position-
        # only, re-uploaded each time the hover changes.
        self._tri_vao    = 0
        self._tri_vbo    = 0
        self._tri_verts  = None    # (3, 3) float32, last upload
        # Cached model matrix of the picked mesh; the overlay needs
        # it so the highlight transforms with the same model
        # rotation/translation as the surface it sits on (turret
        # yaw, gun pitch, etc).  4x4 identity when nothing is picked.
        self._tri_model  = np.eye(4, dtype=np.float32)

        # Last successful hit -- (mesh_idx, tri_idx) or None.  Used
        # to detect hover-change for the console-clear-and-redraw.
        self.last_hit    = None
        # Last cursor position used for the readback -- exposed for
        # debugging.
        self.last_mouse  = (-1, -1)

    # ------------------------------------------------------------------
    def cleanup(self):
        """Release every GL resource we own.  Idempotent -- safe to
        call multiple times.  Triggered from Viewer.cleanup at app
        shutdown."""
        try:
            if self._tri_vbo:
                glDeleteBuffers(1, [int(self._tri_vbo)])
            if self._tri_vao:
                glDeleteVertexArrays(1, [int(self._tri_vao)])
            if self._color_tex:
                glDeleteTextures([int(self._color_tex)])
            if self._depth_rbo:
                glDeleteRenderbuffers(1, [int(self._depth_rbo)])
            if self._fbo:
                glDeleteFramebuffers(1, [int(self._fbo)])
        except Exception:
            pass
        self._tri_vbo   = 0
        self._tri_vao   = 0
        self._color_tex = 0
        self._depth_rbo = 0
        self._fbo       = 0
        self._fbo_w     = self._fbo_h = 0
        self._pick_sh   = None
        self._over_sh   = None
        self.last_hit   = None

    # ------------------------------------------------------------------
    def _ensure_shaders(self):
        if self._pick_sh is None:
            self._pick_sh = _PickingShader()
        if self._over_sh is None:
            self._over_sh = _OverlayShader()

    def _ensure_fbo(self, width, height):
        """(Re)allocate the FBO when the scene viewport size changes."""
        w = max(1, int(width))
        h = max(1, int(height))
        if self._fbo and w == self._fbo_w and h == self._fbo_h:
            return
        # Tear down whatever we had before resizing.
        if self._color_tex:
            glDeleteTextures([int(self._color_tex)])
        if self._depth_rbo:
            glDeleteRenderbuffers(1, [int(self._depth_rbo)])
        if self._fbo:
            glDeleteFramebuffers(1, [int(self._fbo)])

        self._color_tex = int(glGenTextures(1))
        glBindTexture(GL_TEXTURE_2D, self._color_tex)
        # RGBA8, no mips, NEAREST filtering (we only ever read 1
        # pixel back -- no interpolation needed).
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0,
                     GL_RGBA, GL_UNSIGNED_BYTE, None)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
        glBindTexture(GL_TEXTURE_2D, 0)

        self._depth_rbo = int(glGenRenderbuffers(1))
        glBindRenderbuffer(GL_RENDERBUFFER, self._depth_rbo)
        glRenderbufferStorage(GL_RENDERBUFFER, GL_DEPTH_COMPONENT24, w, h)
        glBindRenderbuffer(GL_RENDERBUFFER, 0)

        self._fbo = int(glGenFramebuffers(1))
        glBindFramebuffer(GL_FRAMEBUFFER, self._fbo)
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                               GL_TEXTURE_2D, self._color_tex, 0)
        glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT,
                                  GL_RENDERBUFFER, self._depth_rbo)
        if (glCheckFramebufferStatus(GL_FRAMEBUFFER)
                != GL_FRAMEBUFFER_COMPLETE):
            print("[picker] WARN: FBO incomplete -- picker disabled "
                  "for this resize")
            self._fbo = 0
        glBindFramebuffer(GL_FRAMEBUFFER, 0)

        self._fbo_w = w
        self._fbo_h = h

    def _ensure_tri_vao(self):
        if self._tri_vao:
            return
        self._tri_vao = int(glGenVertexArrays(1))
        self._tri_vbo = int(glGenBuffers(1))
        glBindVertexArray(self._tri_vao)
        glBindBuffer(GL_ARRAY_BUFFER, self._tri_vbo)
        # Allocate space for 3 vec3 positions; re-uploaded per hover.
        glBufferData(GL_ARRAY_BUFFER, 3 * 3 * 4, None, GL_DYNAMIC_DRAW)
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 0, None)
        glEnableVertexAttribArray(0)
        glBindVertexArray(0)

    # ------------------------------------------------------------------
    def update_pass(self, meshes, view, proj,
                    mouse_xy, window_h, viewport,
                    on_hit_change=None):
        """Run the picking render pass + readback.

        Args:
            meshes        (list[Mesh])     : the loaded sub-meshes,
                in the same order their VAOs are bound during the
                main render -- determines the encoded mesh_id.
            view, proj    (np.ndarray 4x4) : the same matrices the
                main pass uses, so the picker geometry lines up
                pixel-perfectly.
            mouse_xy      ((mx, my))       : current mouse position
                in PYGAME window pixels (top-left origin).
            window_h      (int)            : full window height in
                pixels.  Needed to flip pygame Y to GL Y.
            viewport      ((vx, vy, vw, vh)) : the scene viewport
                in OpenGL window-coords (bottom-left origin) --
                exactly the values the caller passed to
                glViewport(vx, vy, vw, vh) before its main draw.
            on_hit_change (callable | None)  : called as
                `cb(mesh_idx, tri_idx)` when the hovered triangle
                changes between frames.  `mesh_idx` and `tri_idx`
                are -1, -1 when the user moves off the geometry.
                None disables the callback.
        """
        if not self.enabled:
            return
        if not meshes:
            self._invalidate_hit(on_hit_change)
            return

        self._ensure_shaders()
        vx, vy, vw, vh = viewport
        self._ensure_fbo(vw, vh)
        if not self._fbo:
            return

        # ---- Render every mesh into the FBO with the picking shader.
        #
        # Defensive state setup -- the main render path leaves a few
        # depth / blend / polygon-offset flags in whatever shape its
        # last pass needed, and inheriting them silently produces
        # wrong-face picks:
        #
        #   * `glDepthFunc` -- skybox sets GL_LEQUAL; if we picked
        #     up that state, coplanar triangles would tie and
        #     "later draw wins", giving random face selection per
        #     pixel.  Force GL_LESS so the FIRST closest writer
        #     wins deterministically.
        #
        #   * `glPolygonOffset` -- enabled when the user has
        #     Wireframe on; pushes filled tris backward in depth.
        #     The picker doesn't want any z-bias.  Disable.
        #
        #   * `glDepthMask` -- could be GL_FALSE from a transparent
        #     overlay pass.  Force GL_TRUE so depth writes land.
        #
        # Per-mesh culling is matched to the main render's
        # convention (`mesh.double_sided` -> disable, else enable
        # back-face cull) so back faces can't accidentally "win"
        # depth on a curved surface and produce the wrong pick.
        glBindFramebuffer(GL_FRAMEBUFFER, self._fbo)
        glViewport(0, 0, self._fbo_w, self._fbo_h)
        glClearColor(0.0, 0.0, 0.0, 0.0)    # mesh_id=0 -> "no hit"
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glEnable(GL_DEPTH_TEST)
        glDepthFunc(GL_LESS)
        glDepthMask(GL_TRUE)
        glDisable(GL_BLEND)
        glDisable(GL_POLYGON_OFFSET_FILL)
        glPolygonOffset(0.0, 0.0)

        self._pick_sh.use()
        self._pick_sh.set_view_proj(view, proj)

        # mesh_id 0 is reserved as "no hit"; offset by +1 in the
        # shader.  Visible meshes only -- the user has hidden some
        # via the mesh-window, those should be transparent to the
        # pick.  The per-mesh model matrix is uploaded each
        # iteration so the picker FBO matches the world-space
        # positions of the visible scene; without that, hovering
        # over the (correctly-positioned) tank would land the
        # cursor on a screen pixel where the (model-space)
        # picker has no triangle, and we'd report a hit somewhere
        # else on the screen instead -- typically "under the tank
        # in space".
        for i, mesh in enumerate(meshes):
            if not getattr(mesh, 'visible', True):
                continue
            if mesh.vao is None:
                continue
            # Match the main render's culling convention so the
            # picker sees the same surface the user does.
            if getattr(mesh, 'double_sided', False):
                glDisable(GL_CULL_FACE)
            else:
                glEnable(GL_CULL_FACE)
                glCullFace(GL_BACK)
            self._pick_sh.set_mesh_id(i)
            self._pick_sh.set_model(self._mesh_model(mesh))
            glBindVertexArray(mesh.vao)
            glDrawElements(GL_TRIANGLES, mesh.index_count,
                           GL_UNSIGNED_INT, None)
        glBindVertexArray(0)
        # Leave culling in a known state for the caller's main pass
        # below -- it'll override per-mesh anyway, but the explicit
        # enable here means we don't accidentally hand back a "no
        # culling at all" state if the loop happened to terminate
        # on a double-sided mesh.
        glEnable(GL_CULL_FACE)

        # ---- Read back the single pixel under the mouse.
        # Pygame y is measured DOWN from the top of the window;
        # GL y is measured UP from the bottom.  Flip first, then
        # subtract the viewport origin to land in FBO-local coords.
        mx, my = int(mouse_xy[0]), int(mouse_xy[1])
        gl_y_window = int(window_h) - 1 - my
        fbo_x       = mx - int(vx)
        fbo_y       = gl_y_window - int(vy)
        if (0 <= fbo_x < self._fbo_w and 0 <= fbo_y < self._fbo_h):
            data = glReadPixels(fbo_x, fbo_y, 1, 1,
                                GL_RGBA, GL_UNSIGNED_BYTE)
            r, g, b, a = struct.unpack_from('BBBB', bytes(data), 0)
        else:
            r = g = b = a = 0
        self.last_mouse = (mx, my)

        glBindFramebuffer(GL_FRAMEBUFFER, 0)
        # Caller will reset their own glViewport before drawing the
        # main scene; we don't restore here because the picker
        # always runs BEFORE the main pass.

        # ---- Decode + dispatch hit-change events.
        if r == 0:
            self._invalidate_hit(on_hit_change)
            return
        mesh_idx = r - 1
        tri_idx  = g | (b << 8) | (a << 16)
        if mesh_idx < 0 or mesh_idx >= len(meshes):
            self._invalidate_hit(on_hit_change)
            return
        new_hit = (mesh_idx, tri_idx)
        if new_hit != self.last_hit:
            self.last_hit = new_hit
            self._upload_triangle_geometry(meshes[mesh_idx], tri_idx)
            if on_hit_change:
                on_hit_change(mesh_idx, tri_idx)

    # ------------------------------------------------------------------
    def _invalidate_hit(self, on_hit_change):
        if self.last_hit is not None:
            self.last_hit = None
            if on_hit_change:
                on_hit_change(-1, -1)

    def _upload_triangle_geometry(self, mesh, tri_idx):
        """Pull the 3 vertex positions for this triangle from the
        mesh's CPU-side buffers and re-upload to our dynamic VAO.
        Also caches the mesh's model matrix so the overlay draw
        transforms the highlight with the same world placement
        the main render gave the surface."""
        self._ensure_tri_vao()
        try:
            base = tri_idx * 3
            i0 = int(mesh.indices[base    ])
            i1 = int(mesh.indices[base + 1])
            i2 = int(mesh.indices[base + 2])
        except (IndexError, TypeError):
            self._tri_verts = None
            return
        try:
            p0 = mesh.positions[i0]
            p1 = mesh.positions[i1]
            p2 = mesh.positions[i2]
        except (IndexError, TypeError):
            self._tri_verts = None
            return
        verts = np.asarray([p0, p1, p2], dtype=np.float32)
        self._tri_verts = verts
        self._tri_model = self._mesh_model(mesh)
        glBindBuffer(GL_ARRAY_BUFFER, self._tri_vbo)
        glBufferSubData(GL_ARRAY_BUFFER, 0, verts.nbytes, verts)
        glBindBuffer(GL_ARRAY_BUFFER, 0)

    @staticmethod
    def _mesh_model(mesh):
        """Return the mesh's world transform as a 4x4 float32 matrix.
        Defaults to identity when the mesh doesn't carry one (some
        unit-test / FBX-import paths leave it unset)."""
        m = getattr(mesh, 'model_matrix', None)
        if m is None:
            return np.eye(4, dtype=np.float32)
        return np.asarray(m, dtype=np.float32)

    # ------------------------------------------------------------------
    def draw_overlay(self, view, proj, c1, c2):
        """Render the highlighted triangle, edge lines, and vertex
        markers.  Caller must have already restored the scene
        viewport + bound the default framebuffer.  Depth test is
        kept enabled but with `glPolygonOffset` so the highlight
        doesn't z-fight the underlying tank surface.

        State contract
        --------------
        On entry we assume the main mesh pass just finished, so the
        usual "scene drawing" state holds: GL_DEPTH_TEST enabled,
        GL_CULL_FACE enabled, GL_BLEND off, scene viewport bound,
        default framebuffer bound.

        On exit we GUARANTEE the same state is restored, so the UI
        2-D pass that follows doesn't have to clean up after us:

            GL_DEPTH_TEST          -> enabled (re-enabled after pass 3)
            GL_CULL_FACE           -> enabled (re-enabled after pass 1)
            GL_POLYGON_OFFSET_FILL -> disabled, glPolygonOffset(0, 0)
            GL_BLEND               -> disabled
            glLineWidth            -> 1.0
            glPointSize            -> 1.0

        The shader program is left bound to our overlay program;
        the next caller (UI render) re-binds its own program, so
        that's safe.

        Args:
            view, proj (np.ndarray 4x4): same matrices the main
                pass used.
            c1 ((r,g,b,a) 0..1): theme.c1 -- highlighted-triangle fill.
            c2 ((r,g,b,a) 0..1): theme.c2 -- edge-line colour.
        """
        if not self.enabled or self._tri_verts is None:
            return
        self._ensure_shaders()
        self._over_sh.use()
        self._over_sh.set_view_proj(view, proj)
        # Picked mesh's world transform -- without this, the
        # highlight renders at the triangle's MODEL-space position
        # while the visible surface sits at the WORLD position
        # determined by mesh.model_matrix (turret rotation, gun
        # pitch, etc).  They have to match.
        self._over_sh.set_model(self._tri_model)

        glBindVertexArray(self._tri_vao)

        # ---- Pass 1: filled triangle, alpha-blended on top of the
        # already-rendered tank.  Polygon offset pulls the highlight
        # slightly TOWARD the camera so it wins z-fights against the
        # mesh underneath.
        glEnable(GL_POLYGON_OFFSET_FILL)
        glPolygonOffset(-1.0, -1.0)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glDisable(GL_CULL_FACE)
        # 0.45 alpha -- bright enough to read the pick clearly,
        # transparent enough that the underlying material is still
        # visible (so we can correlate texture seams with bones).
        self._over_sh.set_color(c1[0], c1[1], c1[2], 0.45)
        glDrawArrays(GL_TRIANGLES, 0, 3)
        # Restore polygon-offset state *fully* -- not just the enable
        # flag.  Leaving glPolygonOffset(-1, -1) live means any later
        # code that turns POLYGON_OFFSET_FILL back on without setting
        # its own offset would inherit our bias.
        glDisable(GL_POLYGON_OFFSET_FILL)
        glPolygonOffset(0.0, 0.0)
        glDisable(GL_BLEND)
        # CULL_FACE was disabled for the filled-triangle pass so the
        # back side of a thin-shell triangle still reads the highlight
        # at grazing angles.  Re-enable it now so we hand back a
        # known-good state to the caller.
        glEnable(GL_CULL_FACE)

        # ---- Pass 2: edges as a closed line loop in c2.
        glLineWidth(2.0)
        self._over_sh.set_color(c2[0], c2[1], c2[2], 1.0)
        glDrawArrays(GL_LINE_LOOP, 0, 3)
        glLineWidth(1.0)

        # ---- Pass 3: vertex markers, one point per vertex in red /
        # green / blue.  Drawn with depth test disabled so the
        # points are always visible regardless of where the camera
        # is -- otherwise they vanish behind the tank surface from
        # most angles.
        glDisable(GL_DEPTH_TEST)
        glPointSize(8.0)
        for i, (r, g, b) in enumerate(self.VERTEX_COLORS):
            self._over_sh.set_color(r / 255.0, g / 255.0, b / 255.0, 1.0)
            glDrawArrays(GL_POINTS, i, 1)
        glPointSize(1.0)
        glEnable(GL_DEPTH_TEST)

        glBindVertexArray(0)


# ---------------------------------------------------------------------------
# Console-side helper: format a per-vertex bone-data line.
# Lives here (not in viewer.py) so the picker module is the single
# source of truth for the output format.
# ---------------------------------------------------------------------------

def format_vertex_line(prefix_marker, vert_idx, mesh, vert_id):
    """Produce one human-readable line describing a vertex's bone
    + colour data.  Returns a (text, color) tuple suitable for
    `console.add_line(text, color)`.

    Output covers two distinct WoT data streams that both touch
    bone-membership:

      * Skin-cluster `iii` / `ww` -- the standard 4 indices + 4
        weights (SC_UBYTE4 layout, 4th slot is usually padding).
        Drives the regular hierarchical skinning -- track segments
        following the suspension, hatch flaps, etc.

      * Vertex `colour` -- WoT abuses the per-vertex colour byte
        triplet (R/G/B, the 4th channel is alpha and unrelated)
        as a SECOND bone-tag stream for special-purpose animations
        like gun recoil.  Two adjacent vertices with the same
        skin-cluster bones can have different colour tags and
        therefore react differently to "the gun is firing".  The
        user's reference example: a vertex tagged (3, 3, 0)
        doesn't recoil, while one tagged (3, 0, 0) recoils
        straight back along the gun axis.

    Args:
        prefix_marker (str): leading glyph -- we use `#` and
            colour the line per `vert_idx`.
        vert_idx      (int): 0/1/2 -- which vertex of the picked
            triangle this is.
        mesh          (Mesh): the picked sub-mesh.
        vert_id       (int): vertex INDEX into `mesh.positions` /
            `mesh.bone_indices` / `mesh.bone_weights` /
            `mesh.colour`.

    Returns:
        (text, color) where color is a (r, g, b) 0..255 tuple.
    """
    color = TrianglePicker.VERTEX_COLORS[vert_idx % 3]

    # ---- Skin-cluster bones / weights -----------------------------
    if mesh.bone_indices is not None and mesh.bone_weights is not None:
        try:
            bi = mesh.bone_indices[vert_id]
            bw = mesh.bone_weights[vert_id]
            bi_str = ' '.join(f'{int(b):3d}' for b in bi)
            bw_str = ' '.join(f'{float(w):.2f}' for w in bw)
            bones_part = f"bones [{bi_str}]  weights [{bw_str}]"
        except IndexError:
            bones_part = "(bone idx out of range)"
    else:
        bones_part = "(unskinned)"

    # ---- Vertex colour bone-tag -----------------------------------
    # Stored as float RGBA in [0..1].  Convert to integer bytes for
    # display because that matches WoT's authoring convention --
    # users describe these as "(3, 3, 0)" / "(3, 0, 0)" / etc., not
    # as float fractions.  Alpha is shown too in case it's ever
    # used; on every tank we've seen it's 1.0 / 255 = uninformative.
    if mesh.colour is not None:
        try:
            cv = mesh.colour[vert_id]
            r = int(round(float(cv[0]) * 255.0))
            g = int(round(float(cv[1]) * 255.0))
            b = int(round(float(cv[2]) * 255.0))
            a = int(round(float(cv[3]) * 255.0)) if len(cv) > 3 else 255
            colour_part = f"colour ({r}, {g}, {b}, {a})"
        except (IndexError, TypeError):
            colour_part = "colour (idx out of range)"
    else:
        colour_part = "no colour stream"

    text = (
        f"{prefix_marker} v{vert_idx}  {bones_part}   {colour_part}"
    )
    return (text, color)
