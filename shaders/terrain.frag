#version 330 core
// Terrain fragment shader.  Three-band height blend (low grass /
// mid dirt / high rock) gated by slope -- steep faces always read
// as rock regardless of altitude, the way real cliffs do.  Single
// directional light + a flat ambient lift; no normal map, no
// detail texture (yet).  Cheap enough for a 66K-vertex terrain
// to draw in well under a frame on every recent GPU.
//
// Inputs from the vertex stage:
//   v_world_pos    - position in world space
//   v_world_normal - smooth-shaded normal in world space
//   v_height       - raw world-Y (same as v_world_pos.y, kept
//                    explicit for clarity)
//
// Driven by the Terrain class via these uniforms:
//   u_light_dir   - direction to the light source (world space).
//                   Re-normalised here in case the caller didn't.
//   u_height_min  - lowest world-Y across the heightmap
//   u_height_max  - highest world-Y across the heightmap
//                   The two bounds drive the height-banded colour
//                   blend so the bands always span the full range
//                   regardless of how the terrain is centred.

in vec3  v_world_pos;
in vec3  v_world_normal;
in float v_height;

uniform vec3  u_light_dir;
uniform float u_height_min;
uniform float u_height_max;

out vec4 FragColor;

// Three palette colours for the height blend.  Sat low so the
// terrain doesn't fight the tank's painted hull tones.
const vec3 COLOR_GRASS = vec3(0.36, 0.46, 0.26);   // low / flat
const vec3 COLOR_DIRT  = vec3(0.45, 0.38, 0.28);   // mid slopes
const vec3 COLOR_ROCK  = vec3(0.50, 0.50, 0.52);   // peaks / cliffs

void main() {
    vec3 N = normalize(v_world_normal);
    vec3 L = normalize(u_light_dir);

    // Normalised height in [0..1] across the actual band the
    // heightmap occupies.  Guard against a degenerate flat
    // terrain (max == min) so the divide can't NaN.
    float span = max(u_height_max - u_height_min, 1e-3);
    float t    = clamp((v_height - u_height_min) / span, 0.0, 1.0);

    // Two-segment height blend: grass -> dirt over the lower half,
    // dirt -> rock over the upper half.  Smoothstep edges avoid
    // hard banding at the transition altitudes.
    vec3 col_low  = mix(COLOR_GRASS, COLOR_DIRT, smoothstep(0.10, 0.55, t));
    vec3 col_high = mix(COLOR_DIRT,  COLOR_ROCK, smoothstep(0.55, 0.85, t));
    vec3 base     = mix(col_low, col_high, smoothstep(0.45, 0.65, t));

    // Slope override: dot(N, +Y) gauges flatness.  1.0 = level,
    // 0.0 = vertical.  Below 0.55 we bias toward rock to mimic
    // exposed substrate on cliff faces.
    float flatness = clamp(dot(N, vec3(0.0, 1.0, 0.0)), 0.0, 1.0);
    float rocky    = 1.0 - smoothstep(0.55, 0.80, flatness);
    base = mix(base, COLOR_ROCK, rocky * 0.65);

    // Lambert + soft ambient.  The ambient stays generous (0.35)
    // so shaded cliffs don't go to coal-black; the tank's PBR
    // pipeline already supplies the dramatic shadowing on the
    // hull, the terrain just needs to read as ground.
    float lambert = max(dot(N, L), 0.0);
    vec3  lit     = base * (0.35 + 0.75 * lambert);

    FragColor = vec4(lit, 1.0);
}
