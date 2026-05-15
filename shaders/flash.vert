#version 330 core
//
// Muzzle-flash plane vertex shader.
//
// Per Coffee 2026-05-14 ("the 2nd emitter is needed for each round's
// current emitter.  it should draw 3 rectangles, rotated at center of
// bottom of each billboard"): each active shot draws 3 world-aligned
// quads at its muzzle position, fanning around the gun-forward axis.
// Each quad is anchored at its bottom-center (= the muzzle); the
// plume extends along u_fwd, with thickness perpendicular along
// u_up.  The 3 quads' u_up vectors are rotated 0 deg / 60 deg /
// 120 deg around u_fwd so the flash reads as a 3D burst from any
// viewing angle.  Unlike the standard particle billboard, these
// quads DO NOT face the camera -- the orientation is fixed in
// world space the moment the shot fires.
//
// Vertex attribute is a unit-quad UV (0..1, 0..1).  The vertex
// shader maps:
//
//   a_uv.x  ->  along u_fwd  (0 = muzzle, 1 = plume tip)
//   a_uv.y  ->  along u_up   (0 = one edge, 0.5 = pivot, 1 = other edge)
//
// World position:
//
//   world = u_muzzle_pos
//         + u_fwd * (a_uv.x * u_length)
//         + u_up  * ((a_uv.y - 0.5) * u_thickness)
//
// Bottom-center pivot = (a_uv.x = 0, a_uv.y = 0.5) -> world = muzzle.

layout(location = 0) in vec2 a_uv;

uniform mat4  u_view;
uniform mat4  u_proj;
uniform vec3  u_muzzle_pos;
uniform vec3  u_fwd;
uniform vec3  u_up;
uniform float u_length;
uniform float u_thickness;

out vec2 v_uv;

void main() {
    vec3 world_pos = u_muzzle_pos
                   + u_fwd * (a_uv.x * u_length)
                   + u_up  * ((a_uv.y - 0.5) * u_thickness);
    gl_Position = u_proj * u_view * vec4(world_pos, 1.0);
    v_uv = a_uv;
}
