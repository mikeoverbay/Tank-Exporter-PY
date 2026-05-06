#version 330 core
// Terrain vertex shader.  Pass-through positions + normals + the
// raw world-Y so the fragment shader can do height-banded colour.

layout(location=0) in vec3 a_position;
layout(location=1) in vec3 a_normal;

uniform mat4 u_view;
uniform mat4 u_proj;

out vec3 v_world_pos;
out vec3 v_world_normal;
out float v_height;

void main() {
    v_world_pos    = a_position;
    v_world_normal = normalize(a_normal);
    v_height       = a_position.y;
    gl_Position    = u_proj * u_view * vec4(a_position, 1.0);
}
