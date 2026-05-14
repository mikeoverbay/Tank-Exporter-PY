#version 330 core
//
// pad.frag -- PBR track-pad fragment shader.
//
// Trimmed sibling of mesh.frag: same Cook-Torrance + IBL split-sum
// math, paired with the four PBR textures track_pads.py loads at
// chassis-load time (diffuse / normal / AO / GMM).  No skinning
// state, no wheel-state highlight, no crash-damage layer, no
// armor-color tint -- the pad shoe is a passive prop, not an
// armoured surface.
//
// Texture units match mesh.frag so the global IBL bind in
// viewer.py is reused unchanged:
//     unit 0  diffuse_map      (per-pad-mesh)
//     unit 1  normal_map       (per-pad-mesh, GA-encoded)
//     unit 2  ao_map           (per-pad-mesh, .g channel)
//     unit 3  gmm_map          (per-pad-mesh, R=gloss G=metal)
//     unit 4  irradiance_map   (scene cubemap diffuse)
//     unit 5  brdf_lut         (GGX split-sum LUT)
//     unit 6  prefiltered_map  (raw skybox; see mesh.frag comment)
//
// Author: Coffee + Claude, 2026-05-13.

#define M_PI 3.141592653589793

in VS_OUT {
    vec3 position;
    vec3 normal;
    mat3 TBN;
    vec2 uv0;
    vec3 pad_local_pos;
} fs_in;

out vec4 FragColor;

// ---- Per-mesh textures (bound by TrackPadRenderer.render) ------
uniform sampler2D   diffuse_map;
uniform sampler2D   normal_map;
uniform sampler2D   ao_map;
uniform sampler2D   gmm_map;
uniform int         has_ao_map;
uniform int         has_gmm_map;
uniform int         is_GA_normal;       // 1 = WoT GA-encoded normals

// ---- Global IBL (bound once by viewer._render_track_pad_body) --
uniform samplerCube irradiance_map;
uniform sampler2D   brdf_lut;
uniform samplerCube prefiltered_map;
uniform int         has_irradiance;
uniform int         has_brdf_lut;
uniform int         has_prefiltered;

// ---- Lights (bound once by viewer._render_track_pad_body) ------
#define NUM_LIGHTS 3
uniform vec3        light_pos[NUM_LIGHTS];
uniform vec3        view_pos;

// ---- UI tuning (mirrors mesh.frag's slider knobs) --------------
uniform float       metal_scale;        // Light slider
uniform float       shine_scale;        // Ambient slider
uniform int         use_normal_map;
uniform int         invert_shine;       // AO enable (slider checkbox)

// ---- Debug face-tint passthrough (kept from the original) ------
uniform vec4 u_color;
uniform int  u_show_face_debug;

const float MAX_REFLECTION_LOD = 4.0;
const float c_MinRoughness     = 0.02;

struct PBRInfo {
    float NdotL;
    float NdotV;
    float NdotH;
    float LdotH;
    float VdotH;
    float perceptualRoughness;   // WoT: GLOSSINESS (high = shiny)
    float metalness;
    vec3  reflectance0;
    vec3  reflectance90;
    float alphaRoughness;
    vec3  diffuseColor;
    vec3  specularColor;
};

vec4 SRGBtoLINEAR(vec4 srgbIn)
{
    vec3 bLess  = step(vec3(0.04045), srgbIn.xyz);
    vec3 linOut = mix(srgbIn.xyz / vec3(12.92),
                      pow((srgbIn.xyz + vec3(0.055)) / vec3(1.055), vec3(2.4)),
                      bLess);
    return vec4(linOut, srgbIn.w);
}

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

vec3 getIBLContribution(PBRInfo pbr, vec3 N_dir, vec3 R_dir)
{
    float roughness    = 1.0 - pbr.perceptualRoughness;
    float lod          = roughness * MAX_REFLECTION_LOD;
    vec3 diffuseLight  = texture(irradiance_map,     N_dir       ).rgb;
    vec3 specularLight = textureLod(prefiltered_map, R_dir,  lod ).rgb;
    vec2 brdf_val      = texture(brdf_lut, vec2(pbr.NdotV, roughness)).rg;
    vec3 diffuse  = diffuseLight  * pbr.diffuseColor;
    vec3 specular = specularLight * (pbr.specularColor * brdf_val.x + brdf_val.y);
    return diffuse + specular;
}

vec3 specularReflection(PBRInfo pbr)
{
    return pbr.reflectance0
         + (pbr.reflectance90 - pbr.reflectance0)
         * pow(clamp(1.0 - pbr.VdotH, 0.0, 1.0), 2.0);
}

float geometricOcclusion(PBRInfo pbr)
{
    float NdotL = pbr.NdotL;
    float NdotV = pbr.NdotV;
    const float r = 0.3;
    float attL = 2.0 * NdotL / (NdotL + sqrt(r*r + (1.0 - r*r) * NdotL * NdotL));
    float attV = 2.0 * NdotV / (NdotV + sqrt(r*r + (1.0 - r*r) * NdotV * NdotV));
    return attL * attV;
}

float microfacetDistribution(PBRInfo pbr)
{
    const float roughnessSq = 0.05;
    float f = (pbr.NdotH * roughnessSq - pbr.NdotH) * pbr.NdotH + 1.0;
    return roughnessSq / (M_PI * f * f);
}

vec3 diffuseTerm(PBRInfo pbr)
{
    return pbr.diffuseColor / M_PI;
}

vec3 ACESFilm(vec3 x)
{
    return clamp((x * (2.51 * x + 0.03)) / (x * (2.34 * x + 0.30) + 0.001), 0.0, 1.0);
}

void main()
{
    // ---- Diffuse sample ---------------------------------------------------
    vec4 diff_samp = texture(diffuse_map, fs_in.uv0);

    // AM self-multiply (matches mesh.frag) -- pushes midtones down,
    // gives the pad rubber its grimy contrast.
    diff_samp.rgb *= diff_samp.rgb;

    // ---- AO  (skinned/track meshes carry AO in diffuse alpha; pad meshes
    // generally carry a dedicated ao_map.  Honour both -- match mesh.frag
    // routing.) ------------------------------------------------------------
    float ao = 1.0;
    if (invert_shine == 1) {
        if (has_ao_map == 1) {
            ao = texture(ao_map, fs_in.uv0).g;
        } else {
            ao = diff_samp.a;
        }
    }
    vec4 color = diff_samp;
    color.rgb *= ao;

    color = SRGBtoLINEAR(color);

    // ---- GMM material parameters -----------------------------------------
    float perceptualRoughness = 0.2;
    float metallic            = 0.0;
    float gloss_raw           = 0.2;
    if (has_gmm_map == 1) {
        vec3 gmm = texture(gmm_map, fs_in.uv0).rgb;
        gloss_raw           = gmm.r;
        perceptualRoughness = clamp(pow(gmm.r / 0.8, 7.0), c_MinRoughness, 1.0);
        metallic            = clamp(pow(gmm.g / 0.5, 5.0) * 1.5, 0.0, 1.0);
    }
    float roughness      = 1.0 - perceptualRoughness;
    float alphaRoughness = roughness * roughness;

    // ---- PBR material setup ----------------------------------------------
    vec3 f0            = vec3(0.04);
    vec3 diffuseColor  = color.rgb * (vec3(1.0) - f0) * (1.0 - metallic);
    vec3 specularColor = mix(f0, color.rgb, metallic);
    float reflectance   = max(max(specularColor.r, specularColor.g), specularColor.b);
    float reflectance90 = clamp(reflectance * 25.0, 0.0, 1.0);
    vec3  specEnvR0     = specularColor;
    vec3  specEnvR90    = vec3(reflectance90);

    // ---- Shading normals -------------------------------------------------
    vec3 N_geom = normalize(fs_in.normal);
    vec3 N      = N_geom;
    if (use_normal_map == 1) {
        N = normalize(fs_in.TBN * unpackNormal(fs_in.uv0));
    }

    vec3  v     = normalize(view_pos - fs_in.position);
    float NdotV = abs(dot(N, v)) + 0.001;

    vec3  l_arr[NUM_LIGHTS];
    float NdotL_arr[NUM_LIGHTS];
    float NdotL_max = 0.001;
    for (int i = 0; i < NUM_LIGHTS; ++i) {
        l_arr[i]     = normalize(light_pos[i] - fs_in.position);
        NdotL_arr[i] = clamp(dot(N, l_arr[i]), 0.001, 1.0);
        NdotL_max    = max(NdotL_max, NdotL_arr[i]);
    }

    PBRInfo pbrInputs = PBRInfo(
        NdotL_max, NdotV, NdotL_max, NdotL_max, NdotL_max,
        perceptualRoughness, metallic,
        specEnvR0, specEnvR90, alphaRoughness,
        diffuseColor, specularColor
    );

    // ---- 1. Ambient fill -------------------------------------------------
    vec3 base_diffuse = diffuseTerm(pbrInputs);
    vec3 Lo_ambient   = base_diffuse * shine_scale * 0.25 * (1.0 + NdotL_max);
    vec3 result       = Lo_ambient;

    // ---- 2. IBL ----------------------------------------------------------
    if (has_irradiance == 1 && has_prefiltered == 1 && has_brdf_lut == 1) {
        vec3 R = reflect(-v, N_geom);
        result += getIBLContribution(pbrInputs, N, R)
                  * NdotV * perceptualRoughness;
    }

    // ---- 3. Direct lights (Cook-Torrance) --------------------------------
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
        float D_i = microfacetDistribution(pbr_i) * gloss_raw;

        vec3 diff_i = (1.0 - F_i) * diffuseTerm(pbr_i);
        vec3 spec_i = F_i * G_i * D_i / (4.0 * NdotL * NdotV);

        Lo_direct += NdotL
                   * (diff_i + spec_i * perceptualRoughness * 6.0);
    }
    result += Lo_direct * (10.0 * metal_scale / float(NUM_LIGHTS));

    result = ACESFilm(result);
    result = pow(clamp(result, 0.0, 1.0), vec3(1.0 / 2.2));

    // ---- Debug face tint (preserved from earlier shader) -----------------
    // Per Coffee 2026-05-13 ("move face coloring of the tracks ... to the
    // debug enabled state"): only re-tint when debug is on.  Detects a
    // constant-X face via the derivatives of pad_local_pos.x.
    if (u_show_face_debug == 1) {
        float dx = abs(dFdx(fs_in.pad_local_pos.x));
        float dy = abs(dFdy(fs_in.pad_local_pos.x));
        bool on_x_face = (dx + dy) < 1.0e-5;
        if (on_x_face) {
            if (fs_in.pad_local_pos.x >= 0.0) {
                result = vec3(0.10, 1.00, 0.15);
            } else {
                result = vec3(1.00, 0.10, 0.10);
            }
        }
    }

    FragColor = vec4(result, 1.0);
}
