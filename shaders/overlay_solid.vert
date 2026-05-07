#version 330 core
// Overlay solid-colour shader -- vertex stage.
// Shared by the picker's filled-triangle / edge-line / vertex-point
// passes.  Position-only attribute layout (no per-vertex colour);
// the fragment shader applies a uniform colour.
//
// `u_model` matches the main mesh shader's model matrix for the
// picked mesh -- without it the highlight would render at the
// triangle's MODEL-space position (origin) while the tank reads
// at its world-space position.  Caller uploads the picked mesh's
// own model_matrix every overlay draw.

layout(location = 0) in vec3 a_position;

uniform mat4 u_view;
uniform mat4 u_proj;
uniform mat4 u_model;

void main()
{
    gl_Position = u_proj * u_view * u_model * vec4(a_position, 1.0);
}
