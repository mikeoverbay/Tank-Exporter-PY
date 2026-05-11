#version 330 core
//
// pad.frag -- flat-color track-pad fragment shader.
//
// Commit 1 of the instanced pad render: just outputs `u_color`
// modulated by a soft Lambert from a single fixed light direction
// so the pad's surface relief is visible without falling all the
// way to flat-shaded.  Diffuse / normal map sampling lands in a
// later commit.
//
// Author: Coffee + Claude, 2026-05-09.

in VS_OUT {
    vec3 normal_world;
    vec2 uv0;
    vec3 pad_local_pos;
} vs_in;

uniform vec4 u_color;

out vec4 FragColor;

void main() {
    // Per Coffee 2026-05-10 ("only faces with matching X
    // verts"): tag a fragment only when its host triangle has
    // EVERY vertex at the same pad-local X -- i.e., the
    // triangle lies in a constant-X plane.  Detected via the
    // screen-space derivatives of `pad_local_pos.x`; both are
    // zero (to float precision) on a constant-X triangle and
    // non-zero anywhere X varies across the surface.  Misses
    // the chamfered side-edges and the top / bottom / leading
    // / trailing faces -- exactly the behaviour we want for
    // unambiguously tagging the pin-axis side faces.
    float dx = abs(dFdx(vs_in.pad_local_pos.x));
    float dy = abs(dFdy(vs_in.pad_local_pos.x));
    bool on_x_face = (dx + dy) < 1.0e-5;

    vec3 base_rgb = u_color.rgb;
    if (on_x_face) {
        if (vs_in.pad_local_pos.x >= 0.0) {
            base_rgb = vec3(0.10, 1.00, 0.15);   // +X face: green
        } else {
            base_rgb = vec3(1.00, 0.10, 0.10);   // -X face: red
        }
    }

    // Single fixed key light from above-front-right -- close
    // enough to the existing scene's first directional light to
    // read consistently with the rest of the tank.  Half-Lambert
    // (clamp-to-0.4 floor) so back faces aren't black.
    vec3 L = normalize(vec3(0.4, 1.0, 0.5));
    float lambert = max(dot(normalize(vs_in.normal_world), L), 0.0);
    float shade = 0.4 + 0.6 * lambert;
    FragColor = vec4(base_rgb * shade, u_color.a);
}
