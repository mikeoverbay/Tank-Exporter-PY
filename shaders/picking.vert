#version 330 core
// Off-screen "back-buffer" picker -- vertex stage.
//
// The picker fragment shader does the (mesh_id, primitive_id) ->
// RGBA encoding; this stage just reproduces the SAME vertex
// transform the main mesh shader applies (proj * view * model)
// so the picker FBO lines up pixel-perfect with the visible
// scene.  Without `u_model` here, every mesh would project from
// origin / model space, while the visible scene has each mesh
// shifted to its world position by its own model matrix --
// meaning the cursor lands on a screen pixel where the picker
// has no triangle but the visible scene does, or vice versa.
// Symptom: hover the tank, get hits on empty space underneath.

layout(location = 0) in vec3 a_position;

uniform mat4 u_view;
uniform mat4 u_proj;
uniform mat4 u_model;

void main()
{
    gl_Position = u_proj * u_view * u_model * vec4(a_position, 1.0);
}
