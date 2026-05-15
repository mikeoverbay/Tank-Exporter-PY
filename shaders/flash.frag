#version 330 core
//
// Muzzle-flash plane fragment shader.
//
// Samples a flipbook frame from the gun_flash GL_TEXTURE_2D_ARRAY
// (8 frames at 256x128 each, extracted from WoT eff_tex atlas).
// `u_frame` is the integer frame index 0..u_num_frames-1; the
// caller advances it based on the shot's flash_phase * num_frames.
//
// `u_alpha` modulates the sprite's alpha so the flash can fade
// out cleanly past the flipbook's last frame, and so the 3-quad
// fan-blend doesn't bake to a saturated solid (each plane
// contributes ~33% to the same world region).

in vec2 v_uv;

uniform sampler2DArray u_flipbook;
uniform int            u_frame;
uniform float          u_alpha;
// Per Coffee 2026-05-14 (WoT keyframe color animation): each
// shot interpolates the 4-keyframe color curve from
// gun_effects.xml by flash_phase and feeds the result here as
// u_tint (RGB multiply) and u_intensity (alpha scalar).
uniform vec3           u_tint;
uniform float          u_intensity;
// Per Coffee 2026-05-14 (WoT shimmer pipeline): the muzzle
// flash combines a COLOR sprite with a SECOND sample from the
// distortion atlas that warps the back-buffer through a normal
// map.  That's what gives WoT's flash its 3D heat-haze look.
// `u_distortion` is the per-shot distortion flipbook (6 frames),
// `u_refraction_tex` is a copy of the current back-buffer
// (taken by the host right before this pass).  `u_viewport_size`
// gives us the screen-space UV for refraction sampling.
uniform sampler2DArray u_distortion;
uniform int            u_dist_frame;
uniform float          u_dist_strength;
uniform sampler2D      u_refraction_tex;
uniform vec2           u_viewport_size;

out vec4 FragColor;

void main() {
    // ---- Flash color sample (color atlas)
    vec4 c = texture(u_flipbook, vec3(v_uv, float(u_frame)));
    vec3 flash_rgb = c.rgb * u_tint;
    float flash_a  = c.a * u_alpha * u_intensity;

    // ---- Distortion sample (normal map in distortion atlas)
    // RG channels encode XY displacement around 0.5 (center =
    // no offset).  Strength scaled by the flash alpha so the
    // refraction fades with the flash itself.
    vec4 d = texture(u_distortion, vec3(v_uv, float(u_dist_frame)));
    vec2 offset = (d.rg - vec2(0.5)) * u_dist_strength * c.a;

    // ---- Refracted back-buffer sample
    vec2 screen_uv = gl_FragCoord.xy / u_viewport_size;
    vec2 sample_uv = clamp(screen_uv + offset,
                            vec2(0.001), vec2(0.999));
    vec3 bg = texture(u_refraction_tex, sample_uv).rgb;

    // Per Coffee 2026-05-14 ("alpha is messing with the
    // terrain. draw order?"): write a proper alpha-weighted
    // composite so transparent quad edges leave the scene
    // untouched.  At c.a == 0 the blend `src*a + dst*(1-a)`
    // collapses to dst (= unchanged terrain).  At c.a == 1 it
    // picks up the full (refracted bg + tinted flash).  No more
    // "the full quad area replaces terrain" artefact.
    if (u_dist_strength > 0.0) {
        vec3 result = bg + flash_rgb * u_intensity;
        float a = c.a * u_alpha;
        FragColor = vec4(result, a);
    } else {
        FragColor = vec4(flash_rgb, flash_a);
    }
}
