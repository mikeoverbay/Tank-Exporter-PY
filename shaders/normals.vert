#version 330 core
//
// Surface-normal debug-line shader -- VERTEX stage
//
// Lifts the mesh-local position + normal into WORLD SPACE so the
// geometry shader can compute the line endpoints in world coords and
// then transform via view*projection in one go.  Normal is multiplied
// by the upper 3x3 of the model matrix (handles rotation / scale; for
// non-uniform scale use the inverse-transpose, but for tank meshes
// the model matrix is a pure translation+rotation+uniform scale so
// mat3(model) is fine).
//
// Slot 0 = position, slot 1 = normal -- same as the main mesh VAO so
// the same VBO can be re-bound for the debug-pass draw.

layout(location = 0) in vec3 a_pos;
layout(location = 1) in vec3 a_normal;

uniform mat4 model;

out vec3 v_world_pos;
out vec3 v_world_normal;

void main() {
    vec4 wp = model * vec4(a_pos, 1.0);
    v_world_pos    = wp.xyz;
    v_world_normal = mat3(model) * a_normal;

    // gl_Position is overwritten by the geometry stage; the value
    // here is irrelevant but must be set or the GS sees garbage on
    // some drivers.
    gl_Position = wp;
}
