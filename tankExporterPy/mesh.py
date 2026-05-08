"""
Mesh class -- GPU-side representation of one primitive group.

Classes:
    Mesh : holds positions/normals/tangents/binormals/uv0/indices, material
           texture IDs and flags, and the VAO/VBOs.
           Methods: build_vao(), render(shader), cleanup(), _compute_tangents().
"""

import ctypes

import numpy as np
from OpenGL.GL import *


class Mesh:
    """OpenGL mesh representation for one WoT primitive group.

    After construction call build_vao() to upload data to GPU.
    Texture IDs and material flags should be set before the first render().

    Attributes set from parsed_group:
        name          (str)          : group name from section table
        positions     (N,3 float32)  : vertex positions
        normals       (N,3 float32)  : vertex normals
        tangents      (N,3 float32)  : vertex tangents (computed if absent)
        binormals     (N,3 float32)  : vertex binormals (computed if absent)
        uv0           (N,2 float32)  : primary UV set
        indices       (M,  uint32)   : triangle indices

    Material attributes (set by Viewer after visual_processed parse):
        diffuse_tex_id      (GLuint | None)
        normal_tex_id       (GLuint | None)
        ao_tex_id           (GLuint | None)
        gmm_tex_id          (GLuint | None)
        alpha_reference     (int 0-255)  - alphaReference / 255 sent as float to shader
        alpha_test_enable   (bool)
        double_sided        (bool)       - disables GL_CULL_FACE when True.
                                           Auto-promoted to True whenever
                                           alpha_test_enable is True (set
                                           by Viewer when the mesh is built).
        identifier          (str)        - material identifier from visual
        alpha_in_normal_red (bool)       - True = skinned: alpha from ANM.R, AO from AM.A
                                           False = non-skinned: alpha from AM.A, AO from ao_map.G
    """

    def __init__(self, parsed_group):
        self.name      = parsed_group['name']
        # Vertex format string from the source data.  For WoT pkg loads
        # this is the raw .primitives_processed format ('xyznuv',
        # 'xyznuviiiwwtb', 'set3/iiiwwtbBPVT', ...) which encodes the
        # vertex stride, UV-set count, bone presence, etc.  For FBX
        # imports it's the literal 'imported' (or '' on legacy paths)
        # since the FBX bridge bakes everything into named vertex-color
        # attributes -- the format isn't reconstructible.  Read by the
        # Compare dialog to surface UV2-presence and vertex layout
        # side-by-side.
        self.format    = parsed_group.get('format', '')
        self.positions = parsed_group['vertices']['positions']
        self.normals   = parsed_group['vertices']['normals']
        self.tangents  = parsed_group['vertices']['tangents']
        self.binormals = parsed_group['vertices']['binormals']
        self.uv0       = parsed_group['vertices']['uv0']
        # Optional second UV channel (lightmap / detail-routing).  Only
        # set for WoT formats that contain 'uvuv' (e.g. xyznuvuv,
        # xyznuvuvtb, xyznuvuviiiwwtb) OR groups that ship a sidecar
        # '.uv2' section in the primitives_processed file.  None when
        # absent so consumers can branch with `if mesh.uv1 is not None`.
        # Read by the Compare dialog and exposed to exporters for
        # round-trip via the Blender bridge's UVMap2 layer.
        self.uv1       = parsed_group['vertices'].get('uv1')
        # Optional per-vertex colour set (RGBA float [0,1]).  Sourced
        # from a sidecar '.colour' section -- BigWorld stored it BGRA
        # uint8 but the loader has already swizzled + normalised so
        # downstream consumers see RGBA float without conversion.
        # None when the source had no colour section.
        self.colour    = parsed_group['vertices'].get('colour')
        self.indices   = parsed_group['indices']

        # Per-vertex bone influences (None for non-skinned meshes -- the
        # static hull/turret/gun typically lack them; chassis tracks and
        # certain equipment carry them).  Captured by MeshParser straight
        # from the .primitives_processed vertex stream.  Available to
        # exporters (FBX skin clusters / glTF joints) without a re-parse.
        verts = parsed_group['vertices']
        self.bone_indices = verts.get('bone_indices')   # uint8  (N, 4) | None
        self.bone_weights = verts.get('bone_weights')   # float32(N, 4) | None

        # Material texture IDs
        self.diffuse_tex_id = None
        self.normal_tex_id  = None
        self.ao_tex_id      = None
        self.gmm_tex_id     = None
        # Texture file paths on the local disk (set by Viewer at load
        # time, alongside the GL texture upload).  Used by the FBX /
        # glTF / OBJ exporters to reference the source DDS / PNG without
        # re-resolving via PkgExtractor.  None when the material has
        # no such map.
        self.diffuse_path   = None
        self.normal_path    = None
        self.ao_path        = None
        self.gmm_path       = None
        # Shared "scratch" detail noise texture (metallicDetailMap from the
        # visual_processed file -- almost always Tank_detail/Details_map.dds).
        # Drives the sSpec Phong highlight intensity in mesh.frag.
        self.detail_tex_id  = None
        self.detail_tiling  = (1.0, 1.0)   # from g_detailUVTiling.xy

        # Damage layer (PBS_tank_crash.fx).  Only populated when the
        # source visual_processed lives under /crash/ AND its <fx>
        # node points at the crash shader -- some /crash/ visuals
        # (tracks, gun barrels) reuse the standard PBS_tank.fx and
        # have no damage layer.
        #
        # crash_tile_tex_id : GLuint | None -- single shared damage
        #                     tile (`vehicles/russian/Tank_detail/
        #                     crash_tile.dds`) holding scorched
        #                     metal / dirt / exposed steel in RGB
        #                     and the blend mask in A.  Renderer
        #                     samples this at uv * crash_uv_tiling.xy
        #                     + crash_uv_tiling.zw.
        # crash_tile_path   : str | None -- local disk path the GL
        #                     texture was uploaded from.  Lets the
        #                     FBX exporter reference the file
        #                     without re-resolving via PkgExtractor.
        # crash_uv_tiling   : (xMul, yMul, xOff, yOff)  from
        #                     g_crashUVTiling.  Default (1,1,0,0)
        #                     so an absent property still tiles 1:1.
        # crash_coefficient : float in [0, 1] -- "how damaged" mask
        #                     scaler.  Default 1.0 (full coverage)
        #                     when the property is missing.  The
        #                     renderer multiplies the tile alpha by
        #                     this before mixing into the base.
        self.crash_tile_tex_id = None
        self.crash_tile_path   = None
        self.crash_uv_tiling   = (1.0, 1.0, 0.0, 0.0)
        self.crash_coefficient = 1.0
        # Source shader filename ('shaders/std_effects/PBS_tank_crash.fx'
        # for damage-layer materials; 'PBS_tank.fx' / 'PBS_tank_skinned*.fx'
        # for everything else).  Empty for FBX imports / standalone
        # primitive loads.  The renderer uses this to drive the
        # damage-layer pass; we don't try to emulate the dozen+
        # other shader variants WoT ships, just the PBS_tank vs
        # PBS_tank_crash split.
        self.fx                = ''

        # Per-material flags from visual_processed
        self.alpha_reference     = 0
        self.alpha_test_enable   = False
        self.double_sided        = False
        self.identifier          = ''
        self.alpha_in_normal_red = False   # alpha-test mask in ANM.R
        self.ao_in_diffuse_alpha = False   # ambient occlusion in AM.A

        # Render-time toggle.  Viewer skips draw when False so the user
        # can hide individual sub-meshes (e.g. hide the camo-net layer
        # to inspect the body underneath).
        self.visible             = True

        # WoT vehicle component this mesh belongs to.  Set by
        # Viewer.load_vehicle to one of 'hull' / 'chassis' / 'turret' /
        # 'gun' so the .primitives_processed writer can group meshes
        # back into their per-component output files.  Stays '' for
        # standalone load_mesh calls and FBX/GLB/OBJ imports (no WoT
        # component context to assign).
        self.component           = ''
        # Canonical pkg-relative path of the source .primitives_processed
        # file (forward slashes, no leading 'res/'), e.g.
        # 'vehicles/usa/A14_T30/normal/lod0/Hull.primitives_processed'.
        # Set by Viewer.load_vehicle from the component dict.  Used by
        # the .primitives_processed writer to compose the destination
        # under res_mods/<version>/ -- mirrors the original pkg layout
        # so the game picks up the modified file as an override.
        # Empty string for FBX imports / standalone primitive loads.
        self.primitives_zip      = ''

        # Per-component world-space translation (set by vehicle loader)
        self.model_matrix = np.eye(4, dtype=np.float32)

        # GL objects
        self.vao         = None
        self.vbos        = {}
        self.ebo         = None
        self.index_count = len(self.indices)

        # Ensure tangent/binormal data exists
        if self.tangents is None or len(self.tangents) == 0:
            self._compute_tangents()

    # ------------------------------------------------------------------
    def _compute_tangents(self):
        """Fallback: generate tangents orthogonal to each vertex normal."""
        n = len(self.positions)
        tangents  = np.zeros((n, 3), dtype=np.float32)
        binormals = np.zeros((n, 3), dtype=np.float32)

        for i in range(n):
            normal = self.normals[i]
            ref    = np.array([1.0, 0.0, 0.0]) if abs(normal[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
            t      = np.cross(normal, ref)
            t     /= (np.linalg.norm(t) + 1e-6)
            b      = np.cross(normal, t)
            tangents[i]  = t
            binormals[i] = b

        self.tangents  = tangents
        self.binormals = binormals

    # ------------------------------------------------------------------
    def build_vao(self):
        """Upload vertex data to GPU and create VAO.

        Attribute layout:
            0 - position      (vec3)
            1 - normal        (vec3)
            2 - tangent       (vec3)
            3 - binormal      (vec3)
            4 - uv0           (vec2)
            5 - iii           (uvec4 -- 4 raw bone bytes, skinned only)
            6 - ww            (vec4  -- 4 normalised weights, skinned only)

        The bone attribs (5 + 6) are only uploaded when both
        `bone_indices` and `bone_weights` are populated -- i.e. the
        source primitive group was a skinned format
        (`xyznuviiiwwtb`, `BPVTxyznuviiiwwtb`, ...).  Non-skinned
        meshes leave those locations DISABLED so the shader sees
        whatever the driver default is when it tries to read them
        (typically zero); the matching `mesh.vert` short-circuits
        the skin maths via `u_skinned == 0` so the disabled attribs
        are never actually consulted.
        """
        self.vao = glGenVertexArrays(1)
        glBindVertexArray(self.vao)

        def _upload(attrib, data, components):
            buf = glGenBuffers(1)
            glBindBuffer(GL_ARRAY_BUFFER, buf)
            glBufferData(GL_ARRAY_BUFFER, data.nbytes, data, GL_STATIC_DRAW)
            stride = components * 4   # sizeof(float)
            glVertexAttribPointer(attrib, components, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(0))
            glEnableVertexAttribArray(attrib)
            return buf

        self.vbos['position'] = _upload(0, self.positions, 3)
        self.vbos['normal']   = _upload(1, self.normals,   3)
        self.vbos['tangent']  = _upload(2, self.tangents,  3)
        self.vbos['binormal'] = _upload(3, self.binormals, 3)
        self.vbos['uv0']      = _upload(4, self.uv0,       2)

        # ---- Skinning attribs (locations 5 + 6) -------------------
        # Uploaded only when the source mesh was skinned.  We use
        # glVertexAttribIPointer for `iii` so the bytes arrive as
        # uvec4 in the shader (no implicit normalisation, no
        # float-cast surprises).  Weights go through the standard
        # FLOAT path with `normalized=GL_FALSE` since they're
        # already in [0,1] floats on the CPU side.
        if self.bone_indices is not None and self.bone_weights is not None:
            n = len(self.positions)
            # Coerce to contiguous uint8 / float32 arrays of shape
            # (n, 4).  Source data MAY arrive with fewer than 4
            # slots populated; pad with zero (bone 0 / weight 0)
            # which the shader's weighted sum then ignores.
            bi = np.ascontiguousarray(self.bone_indices, dtype=np.uint8)
            bw = np.ascontiguousarray(self.bone_weights, dtype=np.float32)
            if bi.ndim == 1:
                bi = bi.reshape(n, -1)
            if bw.ndim == 1:
                bw = bw.reshape(n, -1)
            if bi.shape[1] < 4:
                pad = np.zeros((n, 4 - bi.shape[1]), dtype=np.uint8)
                bi  = np.hstack([bi, pad])
            if bw.shape[1] < 4:
                pad = np.zeros((n, 4 - bw.shape[1]), dtype=np.float32)
                bw  = np.hstack([bw, pad])

            buf_iii = glGenBuffers(1)
            glBindBuffer(GL_ARRAY_BUFFER, buf_iii)
            glBufferData(GL_ARRAY_BUFFER, bi.nbytes, bi, GL_STATIC_DRAW)
            glVertexAttribIPointer(5, 4, GL_UNSIGNED_BYTE, 4,
                                   ctypes.c_void_p(0))
            glEnableVertexAttribArray(5)
            self.vbos['bone_idx'] = buf_iii

            buf_ww = glGenBuffers(1)
            glBindBuffer(GL_ARRAY_BUFFER, buf_ww)
            glBufferData(GL_ARRAY_BUFFER, bw.nbytes, bw, GL_STATIC_DRAW)
            glVertexAttribPointer(6, 4, GL_FLOAT, GL_FALSE, 16,
                                  ctypes.c_void_p(0))
            glEnableVertexAttribArray(6)
            self.vbos['bone_w']   = buf_ww

        self.ebo = glGenBuffers(1)
        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, self.ebo)
        glBufferData(GL_ELEMENT_ARRAY_BUFFER, self.indices.nbytes, self.indices, GL_STATIC_DRAW)

        glBindVertexArray(0)

    # ------------------------------------------------------------------
    def render(self, shader):
        """Bind textures and draw the mesh.

        Args:
            shader (ShaderProgram): must already be in use (shader.use() called
                by the caller to set shared uniforms before iterating meshes).
        """
        if self.vao is None:
            self.build_vao()

        if self.diffuse_tex_id:
            glActiveTexture(GL_TEXTURE0)
            glBindTexture(GL_TEXTURE_2D, self.diffuse_tex_id)
            glUniform1i(shader.get_uniform('diffuse_map'), 0)

        if self.normal_tex_id:
            glActiveTexture(GL_TEXTURE1)
            glBindTexture(GL_TEXTURE_2D, self.normal_tex_id)
            glUniform1i(shader.get_uniform('normal_map'), 1)

        if self.ao_tex_id:
            glActiveTexture(GL_TEXTURE2)
            glBindTexture(GL_TEXTURE_2D, self.ao_tex_id)
            glUniform1i(shader.get_uniform('ao_map'), 2)

        # GMM map: G=glossiness (->roughness), B=metallic
        if self.gmm_tex_id:
            glActiveTexture(GL_TEXTURE3)
            glBindTexture(GL_TEXTURE_2D, self.gmm_tex_id)
            glUniform1i(shader.get_uniform('gmm_map'), 3)
            glUniform1i(shader.get_uniform('has_gmm_map'), 1)
        else:
            glUniform1i(shader.get_uniform('has_gmm_map'), 0)

        # Detail map: tiled scratch noise -- drives sSpec strength.
        # Units 4/5/6 are globally reserved for the IBL maps
        # (irradiance / brdf_lut / prefiltered), so detail gets unit 7.
        if self.detail_tex_id:
            glActiveTexture(GL_TEXTURE7)
            glBindTexture(GL_TEXTURE_2D, self.detail_tex_id)
            glUniform1i(shader.get_uniform('detail_map'),     7)
            glUniform1i(shader.get_uniform('has_detail_map'), 1)
            glUniform2f(shader.get_uniform('detail_tiling'),
                        self.detail_tiling[0], self.detail_tiling[1])
        else:
            glUniform1i(shader.get_uniform('has_detail_map'), 0)

        # Crash tile (PBS_tank_crash.fx damage layer).  Unit 8.
        # Bound only when the source visual_processed wired up
        # crashTileMap -- otherwise has_crash_tile=0 short-circuits
        # the damage blend in the fragment shader.
        if self.crash_tile_tex_id:
            glActiveTexture(GL_TEXTURE8)
            glBindTexture(GL_TEXTURE_2D, self.crash_tile_tex_id)
            glUniform1i(shader.get_uniform('crash_tile_map'),  8)
            glUniform1i(shader.get_uniform('has_crash_tile'),  1)
            glUniform4f(shader.get_uniform('crash_uv_tiling'),
                        self.crash_uv_tiling[0],
                        self.crash_uv_tiling[1],
                        self.crash_uv_tiling[2],
                        self.crash_uv_tiling[3])
            glUniform1f(shader.get_uniform('crash_coefficient'),
                        float(self.crash_coefficient))
        else:
            glUniform1i(shader.get_uniform('has_crash_tile'), 0)

        glBindVertexArray(self.vao)
        glDrawElements(GL_TRIANGLES, self.index_count, GL_UNSIGNED_INT, None)
        glBindVertexArray(0)

    # ------------------------------------------------------------------
    def cleanup(self):
        """Delete every GPU object owned by this mesh: VBOs, EBO, VAO, and
        all four material textures (diffuse / normal / AO / GMM).

        Textures are NOT shared across meshes (TextureLoader.load_texture
        builds a fresh GL object per call) so freeing them per-mesh is
        safe.  Idempotent: nulls each handle after deletion so a second
        cleanup() call is a no-op.
        """
        for buf in self.vbos.values():
            glDeleteBuffers(1, [buf])
        self.vbos = {}

        if self.ebo:
            glDeleteBuffers(1, [self.ebo])
            self.ebo = None
        if self.vao:
            glDeleteVertexArrays(1, [self.vao])
            self.vao = None

        # NOTE: detail_tex_id and crash_tile_tex_id are intentionally
        # NOT freed here -- both are shared across every sub-mesh of
        # the vehicle (and across vehicles within a session) via
        # Viewer._shared_tex_cache.  Freeing them per-mesh would leave
        # dangling handles on the other meshes.  The viewer owns the
        # lifetime and frees them in its own cleanup().
        for attr in ('diffuse_tex_id', 'normal_tex_id',
                     'ao_tex_id',      'gmm_tex_id'):
            tex = getattr(self, attr, None)
            if tex:
                glDeleteTextures(1, [tex])
                setattr(self, attr, None)
        self.detail_tex_id     = None   # drop the reference, don't delete
        self.crash_tile_tex_id = None   # ditto -- viewer owns the GL id
