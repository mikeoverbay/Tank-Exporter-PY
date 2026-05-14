#version 330 core
//
// pad.vert -- PBR-capable instanced track-pad vertex shader.
//
// One draw call renders N copies of a pad mesh, each placed at a
// different chassis-local transform supplied via the per-instance
// vertex attribute `a_pad_xform` (a mat4 split across attribute
// locations 9..12).  See `glVertexAttribDivisor` setup in
// track_pads.TrackPadRenderer for the divisor=1 binding.
//
// Pipeline:
//   model_world = u_chassis_pose * a_pad_xform
//   pos_world   = model_world * vec4(position, 1)
//
// Outputs mirror mesh.vert's VS_OUT so the PBR fragment shader can
// reuse the same lighting math (world-space position + normal +
// TBN basis + uv).  No skinning -- pads are rigid instances; the
// per-instance mat4 already places each pad on its spline anchor.
//
// Author: Coffee + Claude, 2026-05-09 (textured pass: 2026-05-13).

layout (location = 0) in vec3 position;
layout (location = 1) in vec3 normal;
layout (location = 2) in vec3 tangent;
layout (location = 3) in vec3 binormal;
layout (location = 4) in vec2 uv0;

// Per-instance mat4 split across 4 vec4 slots (locations 9..12).
layout (location =  9) in vec4 a_pad_xform_row0;
layout (location = 10) in vec4 a_pad_xform_row1;
layout (location = 11) in vec4 a_pad_xform_row2;
layout (location = 12) in vec4 a_pad_xform_row3;

uniform mat4 u_chassis_pose;
uniform mat4 u_view;
uniform mat4 u_proj;

out VS_OUT {
    vec3 position;       // world space
    vec3 normal;         // world space (for IBL diffuse + lighting)
    mat3 TBN;            // world-space tangent basis (normal mapping)
    vec2 uv0;
    vec3 pad_local_pos;  // unchanged from earlier debug path
} vs_out;

void main() {
    // Rows uploaded row-major; mat4 ctor wants columns -> transpose.
    mat4 pad_xform = transpose(mat4(a_pad_xform_row0,
                                    a_pad_xform_row1,
                                    a_pad_xform_row2,
                                    a_pad_xform_row3));

    mat4 model_world = u_chassis_pose * pad_xform;

    vec4 pos_world = model_world * vec4(position, 1.0);
    vs_out.position = pos_world.xyz;
    gl_Position = u_proj * u_view * pos_world;

    // Normal / tangent transform via the 3x3 of the world model.
    // Pad transforms are rigid (rotation + translation, no scale)
    // so the simple mat3(model) is exact and avoids the cost of
    // inverse-transpose every vertex.
    mat3 nrm_mat = mat3(model_world);
    vec3 N = normalize(nrm_mat * normal);
    vec3 T = normalize(nrm_mat * tangent);
    vec3 B = normalize(nrm_mat * binormal);

    float invmax = inversesqrt(max(dot(T, T), dot(B, B)));
    vs_out.TBN    = mat3(T * invmax, B * invmax, N * invmax);
    vs_out.normal = N;
    vs_out.uv0    = uv0;
    vs_out.pad_local_pos = position;
}
