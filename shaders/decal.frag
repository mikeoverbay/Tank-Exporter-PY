#version 330 core
// Shell-impact decal fragment shader.
//
// Renders one decal quad sampled from a 2-D shellhole albedo
// texture (WoT's `maps/decals_pbs/PBS_ShellHole_*_AM.dds`,
// extracted to `resources/decals_pbs/*.png` by
// `cust_tools/extract_wot_shellhole_decals.py`).
//
// The texture already carries the alpha mask -- corners fade to
// 0 alpha so adjacent decals composite cleanly without seams.
// We multiply that alpha by an age-driven fade so decals
// disappear after their `scorch_phase` lifetime in the impact
// pool (default 6 s).
//
// Two-stage age fade:
//
//   * 0.00 .. 0.85  -- fully opaque (the crater is fresh).
//   * 0.85 .. 1.00  -- linear fade-out to 0.
//
// The hold-at-full window is generous so the decal sits long
// enough for the player to notice it, then disappears cleanly.
// If you want permanent decals, pin `u_fade_start` past 1.0.

in vec2  v_uv;
in float v_age_frac;

uniform sampler2D u_albedo;
uniform float     u_fade_start;   // start of alpha ramp-down (0..1)
uniform float     u_global_alpha; // overall multiplier (debug / fade)

out vec4 FragColor;

void main()
{
    vec4 src = texture(u_albedo, v_uv);

    // Discard fully-transparent pixels so we don't waste blend
    // bandwidth on the corners.  Threshold below GL_LINEAR's
    // sub-pixel alpha noise floor.
    if (src.a < 0.01) {
        discard;
    }

    // Age-driven fade-out.  Hold full opacity until u_fade_start,
    // then linearly ramp to 0 by age=1.0.  smoothstep gives the
    // ramp a slight ease-in/out so it doesn't pop off.
    float fade_t = clamp(
        (v_age_frac - u_fade_start)
            / max(1.0 - u_fade_start, 1e-3),
        0.0, 1.0);
    float fade = 1.0 - smoothstep(0.0, 1.0, fade_t);

    FragColor = vec4(src.rgb, src.a * fade * u_global_alpha);
}
