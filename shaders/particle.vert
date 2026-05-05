#version 330 core
// Camera-facing billboard particle vertex shader.
//
// Each particle is rendered as 6 verts (2 triangles, unindexed).  All 6
// verts share the same world-space position (a_pos) and age (a_age);
// only the corner offset and uv differ.  The shader expands each vertex
// out from the center along the camera's right/up axes, scaling by the
// per-particle interpolated size.

layout(location=0) in vec3  a_pos;     // emitter world position
layout(location=1) in vec2  a_offset;  // corner offset (-0.5..0.5)
layout(location=2) in vec2  a_uv;      // corner UV (0..1)
layout(location=3) in float a_age;     // particle age in seconds

uniform mat4  u_view;
uniform mat4  u_proj;
uniform vec3  u_cam_right;     // camera right axis in WORLD space
uniform vec3  u_cam_up;        // camera up    axis in WORLD space
uniform float u_start_size;    // size at t=0
uniform float u_end_size;      // size at t=lifetime
uniform float u_lifetime;      // particle lifetime (seconds)

out vec2  v_uv;
out float v_t;                 // normalised age 0..1

void main() {
    float t    = clamp(a_age / u_lifetime, 0.0, 1.0);
    float size = mix(u_start_size, u_end_size, t);

    vec3 world_pos = a_pos
                   + u_cam_right * (a_offset.x * size)
                   + u_cam_up    * (a_offset.y * size);

    gl_Position = u_proj * u_view * vec4(world_pos, 1.0);
    v_uv = a_uv;
    v_t  = t;
}
