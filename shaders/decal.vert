#version 330 core
// Shell-impact decal vertex shader.
//
// Each decal is uploaded as 6 verts (two triangles) with world-
// space positions already laid out by the CPU side -- the Decals
// projector builds the quad in `pos + tangent * (u-0.5) * size +
// bitangent * (v-0.5) * size`, where (tangent, bitangent) is an
// orthonormal basis on the surface normal at the impact point.
//
// We push the entire quad UP the surface normal by a tiny `bias`
// in CPU as part of `pos`, so this stage doesn't need to know
// the normal -- we just transform position through view * proj.
//
// `a_uv` is the per-vertex UV in [0, 1]^2, used by the frag stage
// to sample the shellhole texture.  `a_age_frac` is the decal's
// normalised age (0 at hit, 1 at fade-out), packed into the
// vertex stream so we don't need a per-decal uniform array.

layout(location = 0) in vec3 a_position;
layout(location = 1) in vec2 a_uv;
layout(location = 2) in float a_age_frac;

uniform mat4 u_view;
uniform mat4 u_proj;

out vec2  v_uv;
out float v_age_frac;

void main()
{
    v_uv       = a_uv;
    v_age_frac = a_age_frac;
    gl_Position = u_proj * u_view * vec4(a_position, 1.0);
}
