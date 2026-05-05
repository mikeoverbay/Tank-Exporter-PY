#version 330 core
//
// Surface-normal debug-line shader -- FRAGMENT stage
//
// Colour now comes from the geometry shader (per-line) instead of
// a uniform, so by-vertex mode can paint each line with its own
// axes colour (|n.x|=R, |n.y|=G, |n.z|=B) while by-face mode keeps
// a uniform cyan.
//
// Both modes set v_color before EmitVertex() in the GS, so within
// any single line strip the colour is constant -- no interpolation
// surprises along the line.

in  vec3 v_color;
out vec4 FragColor;

void main() {
    FragColor = vec4(v_color, 1.0);
}
