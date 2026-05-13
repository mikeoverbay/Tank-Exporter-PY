#version 330 core
// Terrain fragment shader (IQ-flavoured rewrite).
//
// Design ideas borrowed from Inigo Quilez's landscape shaders:
//
//   * Domain warping -- sample the height/slope colour parameters
//     at p + warp(p) instead of straight world-xz so the colour
//     bands wobble organically and don't expose the underlying
//     square-grid alignment of the heightmap.
//
//   * Cosine palette (a + b*cos(2pi*(c*t + d))) for a smoothly
//     varying earth-tone gradient across height.  Replaces the
//     three hardcoded RGB constants the previous version used --
//     no more visible "this is grass / this is dirt / this is rock"
//     hard transitions; the whole range is one continuous
//     parametric curve.
//
//   * Distance fog -- exponential falloff to a sky-tone so distant
//     peaks fade and near terrain reads sharp.  Single biggest
//     "this is real terrain not a toy" cue.
//
//   * Sun-direction tint -- warm bias on the lit side, cool bias
//     in shadow.  Cheap atmospheric depth without a full sky model.
//
// Derivative-fBm amplitude attenuation (the technique that makes
// IQ's "Elevated" terrain so smooth in valleys) is a CPU change to
// the heightmap generator and is deferred -- a tool-set pass will
// pick that up next.

in vec3  v_world_pos;
in vec3  v_world_normal;
in float v_height;

uniform vec3  u_light_dir;
uniform float u_height_min;
uniform float u_height_max;
uniform vec3  u_eye;          // camera world-space position (for fog)

// Optional sand-diffuse texture.  When `u_has_sand_tex == 1` we
// sample the bound 2-D texture (with mip-chain + anisotropic
// filtering set up at load time) and use it as the surface
// albedo, tiled across world (xz) at `u_sand_tile_size` metres
// per repeat.  When 0, fall through to the procedural cosine
// palette below -- keeps the shader compatible with terrain
// instances that were constructed without a texture.
uniform sampler2D u_sand_tex;
uniform int       u_has_sand_tex;
uniform float     u_sand_tile_size;

out vec4 FragColor;

// =============================================================================
// IQ "hash without sine" -- 2D -> 1D pseudo-random in [0,1].
// Sin-based hashes show visible Moire patterns on big planes; this
// integer-shift recipe is artefact-free and effectively free.
// =============================================================================
float hash2(vec2 p)
{
    p  = fract(p * vec2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}

// =============================================================================
// 2D value noise with smoothstep interpolation.  Cheap and adequate
// for the domain-warp jitter -- we don't need true gradient noise
// here, only "looks not-axis-aligned".
// =============================================================================
float vnoise(vec2 p)
{
    vec2 i = floor(p);
    vec2 f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    float a = hash2(i);
    float b = hash2(i + vec2(1.0, 0.0));
    float c = hash2(i + vec2(0.0, 1.0));
    float d = hash2(i + vec2(1.0, 1.0));
    return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

// =============================================================================
// IQ cosine palette: col(t) = a + b * cos(2pi * (c * t + d)).
// Tunable to land on any palette family by editing (a,b,c,d).  These
// values give a dark mossy/olive low end transitioning through warm
// dirt to a near-sandstone top -- avoids the green/brown/grey
// hardcoded triplet the previous shader used.
// =============================================================================
vec3 palette(float t)
{
    const vec3 a = vec3(0.42, 0.40, 0.36);
    const vec3 b = vec3(0.30, 0.30, 0.32);
    const vec3 c = vec3(0.50, 0.50, 0.50);
    const vec3 d = vec3(0.55, 0.58, 0.60);
    return a + b * cos(6.28318 * (c * t + d));
}

void main()
{
    vec3 N = normalize(v_world_normal);
    vec3 L = normalize(u_light_dir);

    // ---- Domain warp ---------------------------------------------------
    // Two-stage warp: a slow first pass q jitters the input, then a
    // faster second pass r is fed q for further perturbation.  Output
    // `warp` is a small (xz)-space offset added to the parameters
    // below so colour bands don't expose the underlying mesh grid.
    // Frequencies / amplitudes tuned visually -- too much warp and
    // bands stop tracking topology.
    vec2 p = v_world_pos.xz;
    vec2 q = vec2(vnoise(p * 0.30),
                  vnoise(p * 0.30 + vec2(17.0, 53.0)));
    vec2 r = vec2(vnoise(p * 0.80 + q * 4.0),
                  vnoise(p * 0.80 + q * 4.0 + vec2(31.0, 7.0)));
    vec2 warp = (q - 0.5) * 1.2 + (r - 0.5) * 0.5;

    // ---- Base albedo: sand texture (if present) or palette --------------
    // Tile the sand texture across world (xz) at the configured
    // metres-per-repeat.  Anisotropic filtering on the texture (set
    // up at load time) keeps grazing-angle samples crisp; the shader
    // doesn't need to do anything extra for that.  When no sand
    // texture is bound we fall back to the IQ cosine palette so the
    // shader still produces a sensible image.
    vec3 base;
    if (u_has_sand_tex == 1) {
        // Slight warp on the UV so visible texel patterns don't
        // line up exactly with the heightmap grid -- breaks the
        // "I can see the tile" effect at distance.  Warp magnitude
        // is small (a fraction of a tile) so the sand still reads
        // as ground, not as smeared paint.
        vec2 sand_uv = (p + warp * 0.3) / max(u_sand_tile_size, 0.001);
        base = texture(u_sand_tex, sand_uv).rgb;
    } else {
        // Fallback: procedural cosine palette indexed by warped
        // height.  span guards a degenerate flat terrain
        // (max == min) so the divide can't NaN.
        float span     = max(u_height_max - u_height_min, 1e-3);
        float warped_h = v_height + warp.x * 0.6;
        float t        = clamp((warped_h - u_height_min) / span, 0.0, 1.0);
        base           = palette(t);
    }

    // ---- Slope desaturation: cliffs trend toward neutral grey ----------
    // dot(N, +Y) gauges flatness (1.0 = level, 0.0 = vertical).  Below
    // 0.55 the surface mixes increasingly toward a neutral grey to
    // read as exposed substrate, independent of altitude.  Mix
    // strength 0.55 keeps a hint of the palette colour even on
    // verticals so cliffs don't go dead grey.
    float flatness = clamp(dot(N, vec3(0.0, 1.0, 0.0)), 0.0, 1.0);
    float rocky    = 1.0 - smoothstep(0.55, 0.85, flatness);
    vec3  cliff    = mix(base, vec3(0.50, 0.50, 0.52), 0.55);
    base           = mix(base, cliff, rocky);

    // ---- Lambert + sun-direction warm/cool tint ------------------------
    // warm = sunward-side bias (slight orange shift), cool = shadow
    // bias (slight blue shift).  Cheap atmospheric-depth cue.  The
    // (0.30, 0.78) ambient/direct split keeps shaded faces readable
    // without going to pure black.
    float lambert = max(dot(N, L), 0.0);
    vec3  warm    = vec3(1.05, 1.00, 0.92);
    vec3  cool    = vec3(0.85, 0.90, 1.00);
    vec3  tint    = mix(cool, warm, lambert);
    vec3  lit     = base * tint * (0.30 + 0.78 * lambert);

    // ---- Distance fog --------------------------------------------------
    // BYPASSED 1.113.5 per Coffee 2026-05-09 -- fog interferes with
    // the mini-map heightmap visual + corner-matching work.  Restore
    // by un-commenting the mix() below when the spline / track work
    // wraps up.  Original fog_k=0.025 over 40-m world was tuned to
    // fade far edges to ~30-40 % sky tone.
    //
    // vec3  fog_color = vec3(0.66, 0.71, 0.78);
    // float dist      = length(v_world_pos - u_eye);
    // float fog_k     = 0.025;
    // float fog       = 1.0 - exp(-dist * fog_k);
    // vec3  result    = mix(lit, fog_color, fog);
    vec3  result = lit;

    // ---- Height-gradient darken -----------------------------------------
    // Per Coffee 2026-05-10 ("the terrain hurts my eyes.. can we
    // darken it using a gradient from the height map?").  Map the
    // per-fragment v_height into [0, 1] across the terrain's
    // [u_height_min, u_height_max] range, then mix a darken factor
    // between two endpoints.  Low spots dim more (= shadowed
    // valley feel), peaks stay near full brightness.  Cosine-
    // smoothed via smoothstep so the gradient is gentle, not
    // banded.
    //
    // Endpoints tuned for "no eye-strain" -- valleys at 0.55x,
    // peaks at 0.92x.  Adjust by editing the mix() constants
    // below; restore the pre-1.118.57 look by changing the
    // mix(0.55, 0.92, ...) to mix(1.0, 1.0, ...).
    float h_span   = max(u_height_max - u_height_min, 1e-3);
    float h_norm   = clamp(
        (v_height - u_height_min) / h_span, 0.0, 1.0);
    float h_smooth = smoothstep(0.0, 1.0, h_norm);
    float darken   = mix(0.55, 0.92, h_smooth);
    result        *= darken;

    FragColor = vec4(result, 1.0);
}
