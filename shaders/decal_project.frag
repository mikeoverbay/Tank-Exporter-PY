#version 330 core
// Screen-space (volume) decal projector -- fragment stage.
//
// Per Coffee 2026-05-15 (nuTerra DecalProject reference):
// reconstruct the WORLD position of the scene surface at every
// fragment of the decal cube, transform that world position
// into the decal's LOCAL space via the pre-multiplied invMVP,
// clip if it falls outside the unit cube [-0.5, +0.5]^3, and
// otherwise use (local.xy + 0.5) as the texture UV.
//
// Per Coffee 2026-05-15 ("go back to last fix on the terrain
// projector. find that and pull it.. pre pbr added"): reverted
// the PBR (NM + GMM) sampling added in 1.182.0.  Frag is back
// to plain albedo + alpha-blend with a soft cube-edge fade and
// an age-driven fade-out.
//
// How this conforms to underlying geometry:
//
//   * The decal cube is drawn into the framebuffer.  For each
//     of its fragments we get a screen-space pixel coord +
//     the corresponding scene depth (read from u_depth_tex,
//     which is a snapshot of the depth buffer just BEFORE
//     this pass).
//   * From (uv, depth) we reconstruct the WORLD point on
//     whatever surface was previously rendered to that pixel
//     (terrain / tank hull / etc.).  invMVP applied to the
//     normalised-device-coord point gives us that surface
//     point in DECAL LOCAL SPACE (because invMVP folds in the
//     decal_matrix).
//   * If that local point sits outside the unit cube the
//     scene's surface isn't inside the decal volume at this
//     pixel -- discard so we don't paint over distant geometry.
//   * Otherwise sample the decal texture at (local.x,
//     local.z) + 0.5 (TEPY's Y-up ground plane).

flat in mat4 v_inv_mvp;

layout(location = 0) out vec4 FragColor;

uniform sampler2D u_albedo;
uniform sampler2D u_depth_tex;
uniform vec2      u_resolution;       // viewport size (px)
uniform vec2      u_viewport_origin;  // viewport (x, y) in window px
uniform float     u_global_alpha;
uniform float     u_fade_start;     // age frac where fade-out begins

// Per-decal age (uniform; the renderer assigns this from the
// impact pool's scorch_phase).  Kept as a uniform rather than
// a vertex attrib because every fragment of a single decal
// shares the same age.
uniform float u_age_frac;

void main()
{
    // Screen-space UV from the decal cube's rasterised pixel.
    //
    // Per Coffee 2026-05-15 viewport-origin fix: `gl_FragCoord.xy`
    // is in WINDOW pixel coords, but `u_depth_tex` only holds the
    // depth grabbed from the viewport region (origin =
    // `u_viewport_origin`, size = `u_resolution`).  Subtract the
    // origin so the depth texture is sampled correctly when UI
    // panels push the 3D viewport away from (0, 0).
    vec2 uv = (gl_FragCoord.xy - u_viewport_origin) / u_resolution;

    // Scene depth at this pixel.  Captured before the decal
    // pass via glCopyTexImage2D.  Sky / cleared regions read
    // depth = 1.0; the world-position reconstruction sends
    // those points far past the unit cube so the clip
    // discards them.
    float depth = texture(u_depth_tex, uv).r;

    // Rebuild the screen-space NDC point and project it back
    // through invMVP to land in decal-local space.
    vec4 ndc = vec4(uv * 2.0 - 1.0, depth * 2.0 - 1.0, 1.0);
    vec4 local = v_inv_mvp * ndc;
    local.xyz /= local.w;

    // Clip to unit cube.  Anything past the +/-0.5 boundary
    // on any axis is outside the decal volume.
    if (abs(local.x) > 0.5
        || abs(local.y) > 0.5
        || abs(local.z) > 0.5) {
        discard;
    }

    // Per Coffee 2026-05-15 ("on xy plane and projecting in to
    // y-"): TEPY's Y-up world; ground plane = XZ; projection
    // axis = Y.  Sample texture from (local.x, local.z) + 0.5.
    vec2 tex_uv = local.xz + 0.5;
    vec4 c = texture(u_albedo, tex_uv);

    // Soft fade near the +/-Y faces of the volume.  Hides the
    // hard discontinuity where a steep terrain bump exits the
    // cube partway through.  Falls off linearly over the outer
    // 30 % of the cube's half-thickness on each side.
    float edge_fade = 1.0 - smoothstep(0.35, 0.5, abs(local.y));

    // Age fade -- linear ramp from u_fade_start to 1.0.  Pin
    // u_fade_start past 1.0 to disable the fade (e.g. for the
    // aim crosshair which is single-frame).
    float age_fade = 1.0 - clamp(
        (u_age_frac - u_fade_start)
            / max(1.0 - u_fade_start, 1e-3),
        0.0, 1.0);

    float a = c.a * edge_fade * age_fade * u_global_alpha;
    if (a < 0.01) {
        discard;
    }
    FragColor = vec4(c.rgb, a);
}
