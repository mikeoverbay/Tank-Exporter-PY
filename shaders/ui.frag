#version 330 core
in vec2 v_uv;
out vec4 FragColor;

uniform vec4 u_color;
uniform sampler2D u_tex;
// 0 = solid color (button bg)
// 1 = text mask -- sample alpha only, RGB comes from u_color
// 2 = image    -- sample full RGBA, modulated by u_color
uniform int u_use_tex;

void main() {
    if (u_use_tex == 1) {
        vec4 t = texture(u_tex, v_uv);
        FragColor = vec4(u_color.rgb, t.a * u_color.a);
    } else if (u_use_tex == 2) {
        FragColor = texture(u_tex, v_uv) * u_color;
    } else {
        FragColor = u_color;
    }
}
