#version 330 core
// PBR fragment shader -- WoT tank viewer.
// Lighting formulas adapted directly from tank_fragment.glsl (WoT Tank Exporter).
//
// IBL uses the split-sum path with our pre-baked textures:
//   diffuse  IBL = irradiance_map  × diffuseColor         × S_level
//   specular IBL = prefiltered_map × (specColor×brdf.x + brdf.y) × S_level
//
// !! IMPORTANT — UNIT 6 BINDING ----------------------------------------------
// `prefiltered_map` (sampler unit 6) is bound by viewer.py to the *raw* skybox
// cubemap (Skybox.cubemap_id), NOT the GGX-prefiltered output.  This is
// intentional: the GGX prefilter at roughness=0 produces a degenerate delta
// lobe, which on many GPUs writes mip 0 as all-zero.  The chrome diagnostic
// (mesh_chrome.frag) confirmed this -- with prefiltered_id bound, the tank
// rendered black; switching to cubemap_id immediately produced clean mirror
// reflections.
//
// Consequence: the LOD chain is glGenerateMipmap's box-filter blur, not a
// proper GGX importance-sampled blur.  For tank surfaces this is visually
// acceptable -- roughness varies in a narrow range and mip falloff still
// gives the perceptual "rougher = blurrier reflection" cue.
//
// !! IMPORTANT — NO Y-FLIP ---------------------------------------------------
// The chrome diagnostic also confirmed that sampling the cubemap with the raw
// reflect() result -- no axis flip -- matches the user's reference skybox
// shader convention.  Earlier code applied vec3(R.x, -R.y, R.z) to compensate
// for the prefilter's `direction.y = -direction.y` line; with the raw cube
// bound, that compensation is wrong and produces black/inverted reflections.
//
// GMM texture channels  (WoT convention):
//   R = Gloss   →  glossiness = pow(R / 0.8, 7.0)   HIGH = shiny, LOW = rough/rubber
//   G = Metal   →  metallic   = pow(G / 0.5, 5.0)
//   B = Camo mask (reserved for future camo pass -- not used here)
//
// Specular on rubber is suppressed by two independent factors from tank_fragment.glsl:
//   1.  D *= GMM.r          (NDF weighted by raw gloss channel)
//   2.  specContrib *= S_level * perceptualRoughness * 6.0  (gloss-gated scale)
// Both → near zero when the surface is rough/dark in the gloss map.

#define M_PI 3.141592653589793

in VS_OUT {
    vec3 position;
    vec3 normal;
    mat3 TBN;
    vec2 uv0;
} fs_in;

out vec4 FragColor;

// ---- Texture samplers -------------------------------------------------------
uniform sampler2D   diffuse_map;      // unit 0
uniform sampler2D   normal_map;       // unit 1
uniform sampler2D   ao_map;           // unit 2
uniform sampler2D   gmm_map;          // unit 3  (R=gloss, G=metal, B=camo mask)
uniform samplerCube irradiance_map;   // unit 4  Lambertian irradiance (prefiltered)
uniform sampler2D   brdf_lut;         // unit 5  GGX split-sum LUT
uniform samplerCube prefiltered_map;  // unit 6  RAW skybox cubemap (auto-mipped)
uniform sampler2D   detail_map;       // unit 7  scratch noise (metallicDetailMap)
uniform int         has_detail_map;
uniform vec2        detail_tiling;    // g_detailUVTiling.xy (typical 7,7)

// ---- Lighting ---------------------------------------------------------------
// Three point lights placed 120° apart at radius 10, height 10.  By default
// they are stationary (orbit toggled via the "Orbit" UI button).  All three
// contribute equally to the direct-light accumulation in main().
#define NUM_LIGHTS 3
uniform vec3  light_pos[NUM_LIGHTS];
uniform vec3  view_pos;

// ---- Feature flags ----------------------------------------------------------
uniform int   use_normal_map;
uniform int   is_GA_normal;
uniform int   alpha_test_enable;
uniform float alpha_ref;
uniform int   alpha_in_normal_red;
uniform int   ao_in_diffuse_alpha;
uniform int   has_ao_map;
uniform int   has_gmm_map;
uniform vec3  armor_color;
uniform int   has_armor_color;
uniform int   has_irradiance;
uniform int   has_brdf_lut;
uniform int   has_prefiltered;

// ---- UI tuning (maps to tank_fragment.glsl S_level / A_level) ---------------
uniform float metal_scale;          // Sun brightness -- scales all direct light (diffuse+spec)  (slider: Light)
uniform float shine_scale;          // A_level : flat ambient fill                                (slider: Ambient)
uniform int   invert_metal;         // 1 = apply normal map                                        (checkbox: NMap)
uniform int   invert_shine;         // 1 = apply AO                                                (checkbox: AO)
uniform int   wireframe_mode;       // 1 = override final colour with light grey                   (toggle: Wireframe)

// ---- Constants --------------------------------------------------------------
const float MAX_REFLECTION_LOD = 4.0;
const float c_MinRoughness     = 0.02;

// =============================================================================
// PBRInfo  (from tank_fragment.glsl -- unchanged struct layout)
//
// IMPORTANT: perceptualRoughness stores GLOSSINESS (WoT convention).
//   High value = shiny/smooth.  Low value = rough/matte/rubber.
//   Actual material roughness = (1 - perceptualRoughness).
// =============================================================================
struct PBRInfo {
    float NdotL;
    float NdotV;
    float NdotH;
    float LdotH;
    float VdotH;
    float perceptualRoughness;  // WoT: GLOSSINESS  (high = shiny)
    float metalness;
    vec3  reflectance0;         // F0 at normal incidence
    vec3  reflectance90;        // F at grazing
    float alphaRoughness;       // (1 - gloss)^2 -- actual material roughness squared
    vec3  diffuseColor;
    vec3  specularColor;
};

// =============================================================================
// sRGB → linear  (IEC 61966-2-1 -- from tank_fragment.glsl SRGBtoLINEAR)
// =============================================================================
vec4 SRGBtoLINEAR(vec4 srgbIn)
{
    vec3 bLess  = step(vec3(0.04045), srgbIn.xyz);
    vec3 linOut = mix(srgbIn.xyz / vec3(12.92),
                      pow((srgbIn.xyz + vec3(0.055)) / vec3(1.055), vec3(2.4)),
                      bLess);
    return vec4(linOut, srgbIn.w);
}

// =============================================================================
// Normal-map decode  (WoT GA-channel compressed format)
// =============================================================================
vec3 unpackNormal(vec2 uv)
{
    vec4 tn = texture(normal_map, uv);
    vec3 n;
    if (is_GA_normal == 1) {
        n.xy = tn.ga * 2.0 - 1.0;
        n.z  = sqrt(clamp(1.0 - dot(n.xy, n.xy), 0.0, 1.0));
        n.x *= -1.0;
    } else {
        n = tn.rgb * 2.0 - 1.0;
    }
    return normalize(n);
}

// =============================================================================
// IBL contribution  (adapted from tank_fragment.glsl getIBLContribution)
// LOD mapping: roughness = (1 - glossiness)  → higher roughness = higher LOD.
// At lod=0 we hit raw mip 0 (sharp mirror); higher lods walk the auto-generated
// mip chain.
// =============================================================================
vec3 getIBLContribution(PBRInfo pbr, vec3 N_dir, vec3 R_dir)
{
    float roughness    = 1.0 - pbr.perceptualRoughness;       // gloss → roughness
    float lod          = roughness * MAX_REFLECTION_LOD;

    vec3 diffuseLight  = texture(irradiance_map,        N_dir       ).rgb;
    vec3 specularLight = textureLod(prefiltered_map,    R_dir,  lod ).rgb;
    vec2 brdf_val      = texture(brdf_lut, vec2(pbr.NdotV, roughness)).rg;

    vec3 diffuse  = diffuseLight  * pbr.diffuseColor;
    vec3 specular = specularLight * (pbr.specularColor * brdf_val.x + brdf_val.y);

    // !! Intentionally NOT scaled by metal_scale.  The Light slider
    // controls the direct (sun) light intensity only -- the environment
    // contribution is a property of the scene, not the sun.  Original
    // tank_fragment.glsl gates IBL by S_level too, but that conflates
    // env with direct light and feels wrong in interactive use.
    return diffuse + specular;
}

// =============================================================================
// Fresnel  (from tank_fragment.glsl specularReflection)
// Exponent 2.0 instead of Schlick's 5.0 -- softer grazing highlight,
// significantly less specular "halo" on rough/rubber surfaces.
// =============================================================================
vec3 specularReflection(PBRInfo pbr)
{
    return pbr.reflectance0
         + (pbr.reflectance90 - pbr.reflectance0)
         * pow(clamp(1.0 - pbr.VdotH, 0.0, 1.0), 2.0);
}

// =============================================================================
// Geometric occlusion  (from tank_fragment.glsl geometricOcclusion)
// Hardcoded r = 0.3 -- conservative, prevents specular blow-out on rough mats.
// =============================================================================
float geometricOcclusion(PBRInfo pbr)
{
    float NdotL = pbr.NdotL;
    float NdotV = pbr.NdotV;
    const float r = 0.3;
    float attL = 2.0 * NdotL / (NdotL + sqrt(r*r + (1.0 - r*r) * NdotL * NdotL));
    float attV = 2.0 * NdotV / (NdotV + sqrt(r*r + (1.0 - r*r) * NdotV * NdotV));
    return attL * attV;
}

// =============================================================================
// NDF  (from tank_fragment.glsl microfacetDistribution)
// Hardcoded roughnessSq = 0.05 -- tight lobe, controlled via gloss scaling.
// =============================================================================
float microfacetDistribution(PBRInfo pbr)
{
    const float roughnessSq = 0.05;
    float f = (pbr.NdotH * roughnessSq - pbr.NdotH) * pbr.NdotH + 1.0;
    return roughnessSq / (M_PI * f * f);
}

// =============================================================================
// Lambertian diffuse  (from tank_fragment.glsl)
// =============================================================================
vec3 diffuseTerm(PBRInfo pbr)
{
    return pbr.diffuseColor / M_PI;
}

// =============================================================================
// ACES filmic tonemap  (Narkowicz 2015 -- not in original, kept for quality)
// =============================================================================
vec3 ACESFilm(vec3 x)
{
    return clamp((x * (2.51 * x + 0.03)) / (x * (2.34 * x + 0.30) + 0.001), 0.0, 1.0);
}

// =============================================================================
// Main
// =============================================================================
void main()
{
    // ---- Sample textures -------------------------------------------------------
    vec4 diff_samp = texture(diffuse_map, fs_in.uv0);
    vec4 norm_samp = texture(normal_map,  fs_in.uv0);

    // Alpha test (threshold in sRGB space, before linearise -- matches WoT)
    // Done BEFORE we touch diff_samp.rgb so the alpha threshold is unchanged.
    float alpha = (alpha_in_normal_red == 1) ? norm_samp.r : diff_samp.a;
    if (alpha_test_enable == 1 && alpha < alpha_ref) discard;

    // ---- AM darken: multiply the diffuse sample by itself ---------------------
    // x*x in sRGB space pushes midtones down hard while leaving bright spots
    // mostly intact:  0.2 -> 0.04  (5x darker),  0.5 -> 0.25  (2x darker),
    // 0.9 -> 0.81  (barely changed).  Effect is similar to a gamma-2.2
    // linearisation pre-pass, but kept separate from the real SRGBtoLINEAR
    // conversion below so it acts as an extra contrast/saturation boost on
    // top of the existing sRGB->linear math.  Alpha is left untouched.
    diff_samp.rgb *= diff_samp.rgb;

    // ---- AO  (WoT: applied to raw sRGB color BEFORE linearisation) ------------
    // ao_in_diffuse_alpha: skinned/track meshes store AO in diffuse alpha
    // has_ao_map: standalone AO texture, sample .g channel (WoT aoMap.g)
    float ao = 1.0;
    if (invert_shine == 1) {
        if (ao_in_diffuse_alpha == 1) {
            ao = diff_samp.a;
        } else if (has_ao_map == 1) {
            ao = texture(ao_map, fs_in.uv0).g;
        }
    }
    vec4 color = diff_samp;
    color.rgb *= ao;              // WoT: color.rgb *= AO.g  (sRGB domain)

    // ---- Nation armor color tinting -------------------------------------------
    // Luminance-preserving multiplicative tint, hardcoded at full strength.
    // armor_color is divided by its own Rec.601 luminance so the resulting
    // "tint vector" has perceived brightness ~1; multiplying the diffuse by
    // it shifts HUE without dimming the surface.  Texture detail is fully
    // preserved.  Tint slider used to live here -- now baked at 1.0.
    if (has_armor_color == 1) {
        float armor_luma = dot(armor_color, vec3(0.299, 0.587, 0.114));
        vec3  armor_tint = armor_color / max(armor_luma, 0.05);  // brightness-1 hue
        color.rgb       *= armor_tint;
    }

    // ---- sRGB → linear  (WoT: SRGBtoLINEAR after AO + armor tint) ------------
    color = SRGBtoLINEAR(color);

    // ---- GMM material parameters  (WoT tank_fragment.glsl layout) ------------
    // GMM.r = gloss:  HIGH = shiny.  perceptualRoughness stores GLOSSINESS.
    // GMM.g = metal:  metallic boosted 1.5x then clamped (WoT convention)
    float perceptualRoughness = 0.2;    // gloss default (moderate shine)
    float metallic            = 0.0;
    float gloss_raw           = 0.2;    // raw GMM.r, used to scale NDF
    if (has_gmm_map == 1) {
        vec3 gmm        = texture(gmm_map, fs_in.uv0).rgb;
        gloss_raw           = gmm.r;
        perceptualRoughness = clamp(pow(gmm.r / 0.8, 7.0), c_MinRoughness, 1.0);
        metallic            = clamp(pow(gmm.g / 0.5, 5.0) * 1.5, 0.0, 1.0);
    }
    // alphaRoughness = (1 - gloss)^2  (actual material roughness squared)
    float roughness      = 1.0 - perceptualRoughness;
    float alphaRoughness = roughness * roughness;

    // ---- PBR material setup  (from tank_fragment.glsl) -----------------------
    vec3 f0            = vec3(0.04);
    vec3 diffuseColor  = color.rgb * (vec3(1.0) - f0) * (1.0 - metallic);
    vec3 specularColor = mix(f0, color.rgb, metallic);

    float reflectance   = max(max(specularColor.r, specularColor.g), specularColor.b);
    float reflectance90 = clamp(reflectance * 25.0, 0.0, 1.0);
    vec3  specEnvR0     = specularColor;
    vec3  specEnvR90    = vec3(reflectance90);

    // ---- Shading normals ------------------------------------------------------
    // N_geom : the un-perturbed geometry normal.  Used by the IBL reflection
    //          vector so cubemap reflections sweep with the BIG SHAPE of the
    //          part instead of jittering across normal-map detail (matches
    //          tank_fragment.glsl, where R is computed from v_Normal, not n).
    // N      : the (possibly normal-map-perturbed) shading normal used by all
    //          per-fragment lighting math (Lambert NdotL, Fresnel VdotH, etc.)
    //          and by the sSpec Phong reflection vector below.
    vec3 N_geom = normalize(fs_in.normal);
    vec3 N      = N_geom;
    if (use_normal_map > 0 && invert_metal == 1) {
        N = normalize(fs_in.TBN * unpackNormal(fs_in.uv0));
    }

    // ---- View vector (light-independent) -------------------------------------
    vec3  v     = normalize(view_pos - fs_in.position);   // toward camera
    float NdotV = abs(dot(N, v)) + 0.001;

    // tank_fragment.glsl uses directional lights (no 1/d² fall-off).
    // We do the same so the three orbiting / stationary "fill" lights
    // give consistent brightness across the whole mesh regardless of
    // distance, matching WoT's three-directional setup.

    // ---- Per-light direction + NdotL  (used by both ambient and direct) -----
    // NdotL_max approximates "the brightest single light hitting this fragment"
    // and feeds the ambient wraparound term so the lit side stays brighter
    // than the back even with multiple fill lights.
    vec3  l_arr[NUM_LIGHTS];
    float NdotL_arr[NUM_LIGHTS];
    float NdotL_max = 0.001;
    for (int i = 0; i < NUM_LIGHTS; ++i) {
        l_arr[i]     = normalize(light_pos[i] - fs_in.position);
        NdotL_arr[i] = clamp(dot(N, l_arr[i]), 0.001, 1.0);
        NdotL_max    = max(NdotL_max, NdotL_arr[i]);
    }

    // PBRInfo for IBL.  Only NdotV / perceptualRoughness / colours are
    // consulted by getIBLContribution; the other slots receive NdotL_max
    // as a placeholder.
    PBRInfo pbrInputs = PBRInfo(
        NdotL_max, NdotV, NdotL_max, NdotL_max, NdotL_max,
        perceptualRoughness, metallic,
        specEnvR0, specEnvR90, alphaRoughness,
        diffuseColor, specularColor
    );

    // ==========================================================================
    // 1. Ambient fill   (tank_fragment.glsl: diffuse * A_level * 0.25 * (1+NdotL))
    //    Uses the brightest light's NdotL so the lit side still gets the
    //    wraparound boost even with multiple fill lights.
    // ==========================================================================
    vec3 base_diffuse = diffuseTerm(pbrInputs);
    vec3 Lo_ambient   = base_diffuse * shine_scale * 0.25 * (1.0 + NdotL_max);
    vec3 result       = Lo_ambient;

    // ==========================================================================
    // 2. IBL contribution  (tank_fragment.glsl: NdotV * IBL * gloss)
    //    Rough surfaces (low gloss) receive less environment light --
    //    further suppresses rubber reflections.
    //
    //    !! No Y-flip on either sample direction.  The chrome diagnostic
    //    confirmed sampling the raw cubemap (now bound to unit 6) with the
    //    direct reflect() result -- same convention as the user's reference
    //    skybox shader -- gives correct orientation.  Diffuse irradiance is
    //    sampled the same way for consistency; if it ever looks rotated we
    //    revisit this line, not both.
    //
    //    Diffuse uses the perturbed N (it's a wide cosine average -- bumps
    //    don't cause shimmer the way they do for specular).  Specular uses
    //    the GEOMETRY normal so glass / chrome / headlight lenses get clean
    //    reflections instead of mushy bumpy ones (matches tank_fragment.glsl's
    //    `reflect(v, -v_Normal)`).
    // ==========================================================================
    if (has_irradiance == 1 && has_prefiltered == 1 && has_brdf_lut == 1)
    {
        vec3 R = reflect(-v, N_geom);
        result += getIBLContribution(pbrInputs, N, R)
                  * NdotV * perceptualRoughness;
    }

    // ==========================================================================
    // 3. Direct light contribution -- looped over NUM_LIGHTS sources
    //    (tank_fragment.glsl formulas, but accumulated across 3 lights).
    //
    //    sSpec is a Phong "scratch" highlight modulated by detail and metallic.
    //    The detail texture (vehicles/russian/Tank_detail/Details_map.dds in
    //    practice) tiles ~7x across the surface and provides the per-pixel
    //    variation that gives chrome / lens surfaces their punchy sparkle.
    //    Uses the BUMPED reflection so micro-detail flickers across the
    //    highlight (intentional, matches WoT).
    //
    //    metal_scale (Light slider) acts as a single sun-brightness knob.
    //    The accumulated 3-light sum is divided by NUM_LIGHTS so total
    //    brightness stays in the same neighbourhood as the previous
    //    single-light setup at slider=1.
    // ==========================================================================
    vec3 R_bump = reflect(-v, N);
    float detail_rg;
    if (has_detail_map == 1) {
        vec4 detail = texture(detail_map, fs_in.uv0 * detail_tiling);
        detail_rg = detail.r * detail.g;
    } else {
        detail_rg = 0.4;     // collapses scrach to metallic
    }
    float scrach = pow(detail_rg / 0.4, 2.0) * metallic;

    vec3 Lo_direct = vec3(0.0);
    for (int i = 0; i < NUM_LIGHTS; ++i) {
        vec3  l     = l_arr[i];
        vec3  h     = normalize(v + l);
        float NdotL = NdotL_arr[i];
        float NdotH = clamp(dot(N, h), 0.0, 1.0);
        float LdotH = clamp(dot(l, h), 0.0, 1.0);
        float VdotH = clamp(dot(v, h), 0.0, 1.0);

        PBRInfo pbr_i = PBRInfo(
            NdotL, NdotV, NdotH, LdotH, VdotH,
            perceptualRoughness, metallic,
            specEnvR0, specEnvR90, alphaRoughness,
            diffuseColor, specularColor
        );

        vec3  F_i = specularReflection(pbr_i);
        float G_i = geometricOcclusion(pbr_i);
        // D weighted by raw gloss; rubber / matte surfaces stay matte
        float D_i = microfacetDistribution(pbr_i) * gloss_raw;

        vec3 diff_i = (1.0 - F_i) * diffuseTerm(pbr_i);
        vec3 spec_i = F_i * G_i * D_i / (4.0 * NdotL * NdotV);
        vec3 sSpec_i = vec3(1.0) * pow(max(dot(R_bump, l), 0.0), 10.0) * scrach;

        Lo_direct += NdotL
                   * (sSpec_i + diff_i + spec_i * perceptualRoughness * 6.0);
    }
    result += Lo_direct * (10.0 * metal_scale / float(NUM_LIGHTS));

    // ACES filmic tonemap then sRGB gamma
    result = ACESFilm(result);
    result = pow(clamp(result, 0.0, 1.0), vec3(1.0 / 2.2));

    // Wireframe override: when on, every passing fragment renders as
    // a uniform light grey so the line work is readable regardless of
    // the underlying material shading / lighting state.
    if (wireframe_mode == 1) {
        FragColor = vec4(0.75, 0.75, 0.75, 1.0);
    } else {
        FragColor = vec4(result, 1.0);
    }
}
