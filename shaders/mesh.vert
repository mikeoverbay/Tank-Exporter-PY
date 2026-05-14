#version 330 core
//
// mesh.vert -- standard PBR mesh vertex shader with optional GPU skinning.
//
// Skinning path (gated by `u_skinned`)
// ------------------------------------
// When `u_skinned == 1`, the vertex is transformed by a per-vertex skin
// matrix BEFORE the model transform.  The skin matrix is a weighted sum
// of bone matrices indexed via the WoT `iii` byte stream:
//
//     skin = sum_i ww[i] * u_bones[ iii[i] / 3 ]
//
// The `/ 3` is the SC_UBYTE4_REVERSE_PADDED convention every Bigworld /
// WoT shader uses: the engine binds a `vec4 bones[N*3]` uniform array
// where each bone occupies 3 vec4 rows, and the iii byte indexes
// directly into that flat array.  We use a `mat4 u_bones[N]` instead
// (cleaner GLSL) and divide the byte by 3 to recover the bone palette
// index.
//
// `position`, `normal`, `tangent`, `binormal` are all skinned -- the
// TBN basis must rotate with the bones too or normal-mapped surfaces
// look wrong on deflected geometry.
//
// When `u_skinned == 0` the path collapses to the identity skin
// matrix, so non-skinned meshes (hull / turret / gun on most tanks,
// every imported FBX) draw exactly as they did before this shader
// was extended.
//
// `iii` and `ww` attribs are only consumed by the skinning path; the
// VAO uploader leaves them disabled for non-skinned meshes and the
// shader's `u_skinned == 0` branch never reads them.
//

layout (location = 0) in vec3  position;
layout (location = 1) in vec3  normal;
layout (location = 2) in vec3  tangent;
layout (location = 3) in vec3  binormal;
layout (location = 4) in vec2  uv0;
// Bone byte stream from the WoT `iii` vertex format.  Raw uint8x4 in
// the source; we read it as uvec4 so the divide-by-3 produces an
// integer (no float rounding surprises).  Unused when u_skinned == 0.
layout (location = 5) in uvec4 iii;
// Per-slot weights, normalised to sum 1.0.  Float32x4.  Unused when
// u_skinned == 0.
layout (location = 6) in vec4  ww;

out VS_OUT {
    vec3 position;
    vec3 normal;
    mat3 TBN;
    vec2 uv0;
} vs_out;

// Per-vertex wheel state, propagated to the fragment shader.
//   0 = none / not a wheel bone
//   1 = CONTACT      (touching ground, plane fit anchor) -- paint red
//   2 = HANGING      (terrain too far below, drooped at extension cap)
//                       -- paint green
//   3 = OVER_COMPRESSED (bottomed-out into hull) -- treated as CONTACT
//                       in the fragment for now (keeps the highlight
//                       red since it IS still touching).
// `flat` because each wheel mesh is single-bone-bound -- all three
// verts of a wheel triangle share the same dominant bone, so the
// provoking-vertex flat interpolation is exactly the "all 3 verts
// share a state" check we want.
flat out int v_wheel_state;

uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;

// Skinning uniforms.  Only consulted when u_skinned == 1.
//
// Matrix array size 64 -- comfortably above the largest WoT chassis
// renderSet we've measured (T92's exportChassL1_Shape carries 23
// bones; AMX 50B and Object 268/4 sit around 17-25).  Bumping past
// 64 would push us into the GLSL min-uniform-component territory on
// older drivers, so anything bigger should drop to a UBO instead.
const int MAX_BONES = 64;
uniform mat4 u_bones[MAX_BONES];
// 0 = no skinning (identity), 1 = apply u_bones via iii / ww.  Int
// rather than bool because some drivers handle bool uniforms
// inconsistently across versions.
uniform int  u_skinned;

// Wheel-state highlight uniforms.  `u_contact_mode == 1` enables
// the fragment-shader colour overlay; `u_wheel_state[i]` carries the
// state for palette index `i` (one of 0/1/2/3 -- see v_wheel_state
// declaration above).  Indexed by the dominant-bone palette index
// (NOT raw byte; already divided by 3 by the caller).  Bones that
// aren't wheels (V_, Track_, WD_ on tracked tanks, etc.) get state
// 0 and don't paint.
uniform int  u_contact_mode;
uniform int  u_wheel_state[MAX_BONES];

// Gun-recoil byte filter (= the iii byte value that identifies a
// recoil vert; ALWAYS 3 per Coffee 2026-05-14 "FORGET ABOUT BONE
// IDs.. IT IS ALWAYS INDX 0 and 1 Red and green").  -1 = disabled
// (chassis meshes, gun off).  Set per-mesh by viewer.py
// `_upload_skinning`.
uniform int  u_gun_recoil_byte;

// Gun-recoil translation in mesh-local space.  When set on a gun
// mesh and the vertex matches the (R,G) recoil pattern, this
// vec3 gets ADDED to the position directly -- bypassing the bone
// palette entirely so it doesn't matter which palette index the
// recoil bone happens to live at on this tank.  Set per-frame by
// viewer.py from `gun_recoil.offset_m`.
uniform vec3 u_gun_recoil_translation;

void main() {
    // ---- Skinning matrix ---------------------------------------------
    // When skinned, build the weighted sum of bone matrices indexed
    // by iii/3.  When not skinned, the matrix collapses to the
    // identity so the rest of this shader sees a no-op skin step.
    // Gun-recoil part identifier.  Per Coffee 2026-05-14 ("change
    // gun rule.. red = 0 = not recoil.. red != 0 = recoil") plus
    // ("add color rule.  red = 6 = no fire" -- WoT outsourced
    // some tank models and the studios partially lost the colour-ID
    // convention, leaving byte 6 in slot 0 on verts that should
    // NOT recoil):
    //
    //   iii.x == 0  -> rigid / mantlet / cloth (bound to gun root).
    //                  Does NOT recoil.
    //   iii.x == 6  -> outsourced-model anchor, also rigid.
    //                  Does NOT recoil.
    //   iii.x != 0 AND iii.x != 6  -> recoil.
    //
    // Universal across nations regardless of palette ordering --
    // the iii.x byte value alone determines recoil membership.
    bool gr_force_recoil = (u_gun_recoil_byte >= 0
                            && int(iii.x) != 0
                            && int(iii.x) != 6);

    mat4 skin = mat4(1.0);
    if (u_skinned == 1) {
        // Per-slot bone matrices.  Default to the byte-decoded
        // `u_bones[iii/3]` for each slot; the gun-recoil veto below
        // may rewrite some of these to identity before the weighted
        // sum.
        mat4 m0 = u_bones[int(iii.x) / 3];
        mat4 m1 = u_bones[int(iii.y) / 3];
        mat4 m2 = u_bones[int(iii.z) / 3];
        mat4 m3 = u_bones[int(iii.w) / 3];

        // Per Coffee 2026-05-14 ("FORGET ABOUT BONE IDs"): no
        // veto needed.  The recoil translation now comes from a
        // dedicated uniform (`u_gun_recoil_translation`) instead
        // of being parked at a slot in the bone palette, so there's
        // nothing to leak through the weighted sum on non-recoil
        // verts.  iii.x == 0 verts skin normally via u_bones;
        // iii.x != 0 verts hit the force-recoil branch below.

        if (gr_force_recoil) {
            // Recoil verts: apply translation DIRECTLY via the
            // dedicated uniform, bypassing the bone palette
            // entirely.  Build the translation as an identity-
            // plus-translate matrix.
            skin = mat4(1.0);
            skin[3] = vec4(u_gun_recoil_translation, 1.0);
        } else {
            skin = m0 * ww.x + m1 * ww.y + m2 * ww.z + m3 * ww.w;
        // Per Coffee 2026-05-13 (track_mat_L_skinned X-shift):
        // BigWorld packs the per-vertex weights as 4 uint8s that
        // can round down to a sum < 255 (= < 1.0 after the
        // loader's /255 scale).  G78 has 240 ribbon verts with
        // sum ~ 0.91, which compresses their X by ~9 % toward
        // origin via `skin * vec4(pos, 1)`.  Re-normalise the
        // weighted-sum matrix by the actual weight sum so the
        // skin acts like a true convex combination of the bone
        // matrices regardless of the source rounding.
            float w_sum = ww.x + ww.y + ww.z + ww.w;
            if (w_sum > 1e-4) {
                skin /= w_sum;
            }
        }
    }

    // ---- Skinned attributes ------------------------------------------
    // Apply skin BEFORE the model transform so per-bone deflections
    // happen in mesh-local space and the model matrix still places
    // the whole component in world space.
    vec4 pos_local  = skin * vec4(position, 1.0);
    vec3 norm_local = mat3(skin) * normal;
    vec3 tan_local  = mat3(skin) * tangent;
    vec3 bin_local  = mat3(skin) * binormal;

    // ---- World-space transform (unchanged from non-skinned path) -----
    vs_out.position = vec3(model * pos_local);
    vs_out.normal   = normalize(mat3(transpose(inverse(model))) * norm_local);

    vec3 T = normalize(mat3(model) * tan_local);
    vec3 B = normalize(mat3(model) * bin_local);
    vec3 N = vs_out.normal;

    float invmax = inversesqrt(max(dot(T, T), dot(B, B)));
    vs_out.TBN = mat3(T * invmax, B * invmax, N * invmax);

    vs_out.uv0  = uv0;
    gl_Position = projection * view * vec4(vs_out.position, 1.0);

    // ---- Contact-wheel membership -----------------------------------
    // Decide whether THIS vertex is bound to one of the four contact
    // wheel bones.
    //
    // The WoT vertex format does NOT guarantee a particular slot
    // ordering for `iii` / `ww` -- it's NOT descending-weight-first.
    // So we can't just take iii.x as "the dominant bone"; an early
    // version of this shader did, and the result was that 1-bone-
    // bound wheel verts where the writer happened to put the bone
    // in slot 1, 2, or 3 went un-highlighted (only verts with the
    // bone landing in slot 0 painted red -- visible as a single
    // wheel lighting up and the others staying dim).
    //
    // Robust fix: find the highest-weight slot via four compares,
    // then take iii at that slot.  Equivalent to numpy's
    // `int(bi[v][int(np.argmax(bw[v]))])` -- the same dominant-bone
    // recipe `from_chassis_meshes` uses on the CPU side.
    int   dom_slot = 0;
    float dom_w    = ww.x;
    if (ww.y > dom_w) { dom_w = ww.y; dom_slot = 1; }
    if (ww.z > dom_w) { dom_w = ww.z; dom_slot = 2; }
    if (ww.w > dom_w) { dom_w = ww.w; dom_slot = 3; }
    int dom_byte = (dom_slot == 0) ? int(iii.x) :
                   (dom_slot == 1) ? int(iii.y) :
                   (dom_slot == 2) ? int(iii.z) :
                                     int(iii.w);
    int dom_idx  = dom_byte / 3;

    int state = 0;
    if (u_contact_mode == 1
            && dom_w > 0.0
            && dom_idx >= 0
            && dom_idx < MAX_BONES) {
        state = u_wheel_state[dom_idx];
    }

    // Debug overlay: paint red on every recoil vert (iii.x != 0).
    // Per Coffee 2026-05-14 ("red != 0 = recoil") -- the single
    // iii.x byte is now the discriminator, no secondary slots
    // involved.
    if (u_contact_mode == 1 && u_gun_recoil_byte >= 0
            && gr_force_recoil) {
        state = 1;
    }
    v_wheel_state = state;
}
