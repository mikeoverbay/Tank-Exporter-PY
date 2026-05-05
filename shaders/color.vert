#version 330 core
layout (location = 0) in vec3 position;
layout (location = 1) in vec3 color;

out VS_OUT {
    vec3 color;
} vs_out;

uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;

void main() {
    vs_out.color = color;
    gl_Position = projection * view * model * vec4(position, 1.0);
}
