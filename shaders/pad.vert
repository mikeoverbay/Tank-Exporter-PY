#version 330 core
//
// pad.vert -- instanced track-pad vertex shader.
//
// One draw call renders N copies of a pad mesh, each placed at a
// different chassis-local transform supplied via the per-instance
// vertex attribute `a_pad_xform` (a mat4 split across attribute
// locations 5..8 because GLSL doesn't allow a mat4 attribute in
// one slot).  See `glVertexAttribDivisor` setup in
// track_pads.TrackPadRenderer for the divisor=1 binding that
// advances the matrix once per instance.
//
// Pipeline:
//   pos_world = u_chassis_pose * a_pad_xform * vec4(position, 1)
//
// `a_pad_xform` is built CPU-side from the spline's per-pad
// position + tangent.  In commit 1 we only translate (no
// rotation), so each pad lands at its spline pad_pos but stays
// axis-aligned.  Commit 2 adds the tangent-aligned rotation.
//
// Author: Coffee + Claude, 2026-05-09.

layout (location = 0) in vec3 position;
layout (location = 1) in vec3 normal;     // unused in commit 1, declared so
                                          // the same VAO layout as Mesh.build_vao
                                          // can be reused without disabling attribs.
layout (location = 2) in vec3 tangent;    // unused
layout (location = 3) in vec3 binormal;   // unused
layout (location = 4) in vec2 uv0;        // unused

// Per-instance mat4 split across 4 vec4 slots.  GLSL spec requires
// mat4 to occupy 4 consecutive attribute locations -- we use
// 9, 10, 11, 12 to leave 5/6 free for the existing `iii`/`ww`
// skin attribs even though pads don't use them.
layout (location =  9) in vec4 a_pad_xform_row0;
layout (location = 10) in vec4 a_pad_xform_row1;
layout (location = 11) in vec4 a_pad_xform_row2;
layout (location = 12) in vec4 a_pad_xform_row3;

uniform mat4 u_chassis_pose;
uniform mat4 u_view;
uniform mat4 u_proj;

out VS_OUT {
    vec3 normal_world;
    vec2 uv0;
    // Pad-local position, passed through unchanged so the
    // fragment shader can tag faces by their dominant axis.
    // Per Coffee 2026-05-10 ("draw x - face in red, + face
    // in green") -- visual debug for chain-closure rotation
    // hunts.
    vec3 pad_local_pos;
} vs_out;

void main() {
    // Reconstruct the per-instance transform.  Rows are uploaded
    // in row-major order (same convention as the rest of TEPY's
    // numpy matrices); the mat4 constructor here takes columns,
    // so we transpose by feeding row vectors as columns.
    mat4 pad_xform = mat4(a_pad_xform_row0,
                          a_pad_xform_row1,
                          a_pad_xform_row2,
                          a_pad_xform_row3);
    pad_xform = transpose(pad_xform);

    vec4 pos_local = pad_xform * vec4(position, 1.0);
    vec4 pos_world = u_chassis_pose * pos_local;
    gl_Position = u_proj * u_view * pos_world;

    // Normals just rotated by chassis_pose's 3x3 (no scale assumed
    // in the pad transform); good enough for a flat-shaded sanity
    // pass.  Real lighting work happens once we add the textured
    // shader.
    mat3 nrm_mat = mat3(u_chassis_pose) * mat3(pad_xform);
    vs_out.normal_world = normalize(nrm_mat * normal);
    vs_out.uv0 = uv0;
    vs_out.pad_local_pos = position;
}
