#version 330 core
// Overlay solid-colour shader -- fragment stage.
// Outputs a single uniform RGBA every fragment.  Used by the
// triangle picker for its filled-triangle highlight, edge lines,
// and vertex point markers -- each of those passes just rebinds
// `u_color` and re-draws the same VAO.

uniform vec4 u_color;

out vec4 FragColor;

void main()
{
    FragColor = u_color;
}
