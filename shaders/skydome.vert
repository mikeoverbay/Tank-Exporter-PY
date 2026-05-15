#version 330 core
// Skydome -- finite-radius sphere at the world origin.
//
// Unlike `skybox.vert` (which strips translation + writes z=w
// so the cube sits at infinity), this is a REAL mesh in world
// space.  The vertex stage just runs the standard MVP transform
// and forwards the (unit-length) sampling direction.
//
// `position` enters as the un-scaled unit-sphere vertex; the
// CPU side multiplies it by the actual radius before upload, so
// we don't need a uniform here.  `v_dir` is normalised in the
// frag stage rather than here -- saves a normalize() per vertex
// and keeps interpolation in the bigger frequency band.
//
// Per Coffee 2026-05-14 ("we have skydomes in the game.. make a
// sphere. rad = map size / 2").

layout(location = 0) in vec3 position;

out vec3 v_dir;

uniform mat4 view;
uniform mat4 projection;

void main()
{
    v_dir = position;
    gl_Position = projection * view * vec4(position, 1.0);
}
