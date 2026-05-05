#version 330 core
layout (location = 0) in vec2 position;
layout (location = 1) in vec2 uv;

out vec2 v_uv;

uniform mat4 projection;

void main() {
    v_uv = uv;
    gl_Position = projection * vec4(position, 0.0, 1.0);
}
