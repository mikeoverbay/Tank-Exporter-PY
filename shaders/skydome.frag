#version 330 core
// Skydome fragment shader.
//
// Samples the same env cubemap the existing Skybox does, but
// from a finite-radius sphere centered at the world origin.
// The sampling direction is the per-vertex world-space position
// (normalised here so a sphere at any scale gives the right
// cubemap UV).
//
// Two-stage horizon fade:
//
//   * Above the horizon (y > 0): full cubemap sample.
//   * Below the horizon (y < 0): fade toward the terrain
//     fog colour so the dome's lower hemisphere doesn't show
//     a hard ring where it meets the ground.  Skydome should
//     be FULLY hidden by terrain in normal play (camera above
//     ground), but the fade keeps things clean if the camera
//     ever dips below for orbit / debug views.
//
// Per Coffee 2026-05-14 ("we have skydomes in the game.. make
// a sphere.  rad = map size / 2").

in  vec3 v_dir;
out vec4 FragColor;

uniform samplerCube u_cubemap;
uniform vec3        u_fog_color;   // ground-level haze tint
uniform float       u_horizon_t0;  // y where fog blend starts
uniform float       u_horizon_t1;  // y where fog blend completes
// Per Coffee 2026-05-15 ("double render map ... rotate _ .01
// degree and draw skydome again"): the dome is drawn twice per
// frame -- first opaque at u_alpha=1, then again at u_alpha<1
// with a small azimuth rotation so the seam blurs into a soft
// blend instead of a hard line.
uniform float       u_alpha;

void main()
{
    vec3 d = normalize(v_dir);
    vec3 sky = texture(u_cubemap, d).rgb;

    // Smooth horizon-to-ground blend.  At v_dir.y above u_horizon_t0
    // we see pure sky; below u_horizon_t1 we see pure fog.  Between
    // them smoothstep interpolates.
    float t = smoothstep(u_horizon_t1, u_horizon_t0, d.y);
    vec3 col = mix(u_fog_color, sky, t);

    FragColor = vec4(col, u_alpha);
}
