#version 330 core
// Full-screen quad vertex shader for the IBL prefilter / BRDF LUT pass.
// Draws two triangles that cover the entire clip-space [-1,1] x [-1,1].
// texCoord goes (0,0) bottom-left to (1,1) top-right.
layout(location = 0) in vec2 position;
out vec2 texCoord;
void main() {
    texCoord    = position * 0.5 + 0.5;
    gl_Position = vec4(position, 0.0, 1.0);
}
