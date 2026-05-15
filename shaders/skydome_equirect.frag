#version 330 core
// Skydome fragment shader -- 2-D equirectangular variant.
//
// Per Coffee 2026-05-15 (`maps\skyboxes\01_Karelia_sky\skydome\
// sky_karelia_forward.dds`): WoT ships skydomes as a single
// 4096 x 1024 panorama, NOT a cubemap.  This shader samples
// that texture via the standard equirectangular projection:
//
//     u = atan2(d.x, d.z) / (2 * pi) + 0.5
//     v = acos(d.y) / pi
//
// d is the unit-length world-space direction from the sphere
// origin to the fragment (= v_dir).  u wraps around the dome
// at the equator; v walks from 0 (top of sky) at d.y=+1 to 1
// (bottom) at d.y=-1.
//
// The companion `skydome.vert` already plumbs v_dir as the
// raw vertex position (un-normalised).  We normalise here so a
// sphere at any radius gives consistent UVs.
//
// PNG -> GL upload: TEPY uploads PIL Image bytes top-down (no
// flip).  Combined with OpenGL's bottom-origin texture
// convention, that means v=0 maps to the FIRST row of the PNG
// = the TOP of the image = the bright-sky region.  Matches the
// `acos(d.y)/pi` formula above.
//
// Above-horizon fade: the karelia panorama is a HORIZON BAND
// not a full top-hat -- the texture's top edge is "high sky"
// but doesn't reach the zenith.  The shader doesn't paper over
// that here; the UV simply clamps at v=0 (GL_CLAMP_TO_EDGE on
// the bound texture) so the topmost row of the PNG is sampled
// for the whole upper polar cap.  For karelia's bluish-grey
// cloud band this looks fine; if a future map ships a panorama
// with a hard top edge we can add a smoothstep blend to a
// uniform sky-zenith color.

in  vec3 v_dir;
out vec4 FragColor;

uniform sampler2D u_panorama;
uniform vec3      u_fog_color;
uniform float     u_horizon_t0;   // d.y above this -> pure sky
uniform float     u_horizon_t1;   // d.y below this -> pure fog
// Per-pass alpha for the two-pass rotated-blend trick (see
// SkyDome.render in skybox.py).  First pass writes opaque
// (u_alpha = 1.0), second pass at u_alpha < 1.0 with a tiny
// world rotation so the wrap seam softens via blending.
uniform float     u_alpha;

#define PI 3.14159265359

void main()
{
    vec3 d = normalize(v_dir);
    // Equirectangular UV: u wraps via GL_REPEAT, v clamps so
    // the poles don't sample garbage past the texture edge.
    //
    // Per Coffee 2026-05-15 ("sky is wound backwards"): WoT's
    // panorama is authored for a LEFT-handed (or 180-rotated)
    // azimuth convention vs. GL's right-handed (+Z forward,
    // +X right) world axes.  Negating d.x inside the atan
    // mirrors the horizontal sweep so clouds wrap the same
    // direction the player saw them in-game.
    float u = atan(-d.x, d.z) / (2.0 * PI) + 0.5;
    float v = acos(clamp(d.y, -1.0, 1.0)) / PI;
    // Tiny inward contraction so the dome's 360 sweep samples
    // columns 0.005..0.995 -- skips BC1 edge artefacts.
    u = (u - 0.5) * 0.99 + 0.5;

    // Per Coffee 2026-05-15 ("our dome texture still has a
    // visible line  error in building the dome?"): the mesh is
    // correct; the visible seam is GL auto mip selection going
    // wild at the atan2 discontinuity.  `atan(-d.x, d.z)` jumps
    // by 2*pi when d.z swings from -epsilon to +epsilon at d.x=0
    // (the line behind the camera), so `dFdx(u)` reports ~1.0
    // across a single pixel.  GL interprets that as "the
    // texture covers one screen pixel" and snaps to a tiny
    // mip level -> blurry seam line.
    //
    // Fix: compute the LOD from the world-direction derivative
    // (smooth, no atan discontinuity) and sample via
    // `textureLod` so the GPU never auto-picks a mip from the
    // seam-poisoned UV derivatives.
    //
    //   du/dscreen  ~  |dd/dscreen|  /  (2*pi * |d.xz|)
    //   dv/dscreen  ~  |dd/dscreen|  /  pi
    //
    // Texel-rate estimate = (du * tex_w, dv * tex_h); LOD =
    // log2(max).  Clamp to a sane range so the pole pixels
    // (where |d.xz| -> 0 making du blow up) don't push us off
    // the mip pyramid.
    vec3 dDdx = dFdx(d);
    vec3 dDdy = dFdy(d);
    float screen_d = max(length(dDdx), length(dDdy));
    float xz_len   = max(0.001, length(d.xz));
    float du_rate  = screen_d / (2.0 * PI * xz_len);
    float dv_rate  = screen_d / PI;
    ivec2 tex_sz   = textureSize(u_panorama, 0);
    float duv_px   = max(du_rate * float(tex_sz.x),
                          dv_rate * float(tex_sz.y));
    float mip_lod  = clamp(log2(max(duv_px, 1.0)), 0.0, 10.0);
    vec3 sky = textureLod(u_panorama, vec2(u, v), mip_lod).rgb;

    // Underside fade -- same recipe as the cubemap variant.
    // Above u_horizon_t0 we trust the texture; below
    // u_horizon_t1 we replace with fog so a debug under-terrain
    // view doesn't see a hard ring where the dome meets the
    // ground.  Sphere-direction y, not texture v, so the
    // tuning is independent of the panorama's vertical layout.
    float t = smoothstep(u_horizon_t1, u_horizon_t0, d.y);
    vec3 col = mix(u_fog_color, sky, t);

    FragColor = vec4(col, u_alpha);
}
