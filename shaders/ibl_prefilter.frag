#version 330 core
// IBL prefilter / BRDF-LUT fragment shader.
// Ported from the Khronos glTF-Sample-Renderer ibl_filtering.frag to GL 3.3 core.
// Supports three modes (u_distribution):
//   0 = Lambertian  (diffuse irradiance)
//   1 = GGX         (specular prefiltered env map)
//   2 = Charlie     (sheen LUT)
// When u_isGeneratingLUT == 1, outputs BRDF integration LUT instead.

#define MATH_PI 3.1415926535897932384626433832795

uniform samplerCube u_cubemapTexture;

// Filter parameters
uniform float u_roughness;
uniform int   u_sampleCount;
uniform int   u_width;
uniform float u_lodBias;
uniform int   u_distribution;   // 0=Lambertian, 1=GGX, 2=Charlie
uniform int   u_currentFace;
uniform int   u_isGeneratingLUT;
uniform int   u_floatTexture;   // 0=byte target, 1=float target
uniform float u_intensityScale;

in  vec2 texCoord;
out vec4 fragmentColor;

// Distribution enum
const int cLambertian = 0;
const int cGGX        = 1;
const int cCharlie    = 2;

// ---------------------------------------------------------------------------
// UV / direction helpers
// ---------------------------------------------------------------------------

vec3 uvToXYZ(int face, vec2 uv)
{
    if      (face == 0) return vec3( 1.0,  uv.y, -uv.x);
    else if (face == 1) return vec3(-1.0,  uv.y,  uv.x);
    else if (face == 2) return vec3( uv.x, -1.0,  uv.y);
    else if (face == 3) return vec3( uv.x,  1.0, -uv.y);
    else if (face == 4) return vec3( uv.x,  uv.y, 1.0);
    else                return vec3(-uv.x,  uv.y,-1.0);
}

float saturate(float v) { return clamp(v, 0.0, 1.0); }

// ---------------------------------------------------------------------------
// Hammersley quasi-Monte Carlo
// ---------------------------------------------------------------------------

float radicalInverse_VdC(uint bits)
{
    bits = (bits << 16u) | (bits >> 16u);
    bits = ((bits & 0x55555555u) << 1u) | ((bits & 0xAAAAAAAAu) >> 1u);
    bits = ((bits & 0x33333333u) << 2u) | ((bits & 0xCCCCCCCCu) >> 2u);
    bits = ((bits & 0x0F0F0F0Fu) << 4u) | ((bits & 0xF0F0F0F0u) >> 4u);
    bits = ((bits & 0x00FF00FFu) << 8u) | ((bits & 0xFF00FF00u) >> 8u);
    return float(bits) * 2.3283064365386963e-10;
}

vec2 hammersley2d(int i, int N)
{
    return vec2(float(i) / float(N), radicalInverse_VdC(uint(i)));
}

// ---------------------------------------------------------------------------
// TBN frame
// ---------------------------------------------------------------------------

mat3 generateTBN(vec3 normal)
{
    vec3 bitangent = vec3(0.0, 1.0, 0.0);
    float NdotUp   = dot(normal, vec3(0.0, 1.0, 0.0));
    float epsilon  = 0.0000001;
    if (1.0 - abs(NdotUp) <= epsilon)
    {
        bitangent = (NdotUp > 0.0) ? vec3(0.0, 0.0, 1.0)
                                   : vec3(0.0, 0.0, -1.0);
    }
    vec3 tangent = normalize(cross(bitangent, normal));
    bitangent    = cross(normal, tangent);
    return mat3(tangent, bitangent, normal);
}

// ---------------------------------------------------------------------------
// NDFs and importance-sample structs
// ---------------------------------------------------------------------------

struct MicrofacetDistributionSample {
    float pdf;
    float cosTheta;
    float sinTheta;
    float phi;
};

float D_GGX(float NdotH, float roughness)
{
    float a = NdotH * roughness;
    float k = roughness / (1.0 - NdotH * NdotH + a * a);
    return k * k * (1.0 / MATH_PI);
}

float D_Charlie(float sheenRoughness, float NdotH)
{
    sheenRoughness = max(sheenRoughness, 0.000001);
    float invR  = 1.0 / sheenRoughness;
    float cos2h = NdotH * NdotH;
    float sin2h = 1.0 - cos2h;
    return (2.0 + invR) * pow(sin2h, invR * 0.5) / (2.0 * MATH_PI);
}

MicrofacetDistributionSample GGX_sample(vec2 xi, float roughness)
{
    MicrofacetDistributionSample s;
    float alpha   = roughness * roughness;
    s.cosTheta    = saturate(sqrt((1.0 - xi.y) / (1.0 + (alpha * alpha - 1.0) * xi.y)));
    s.sinTheta    = sqrt(1.0 - s.cosTheta * s.cosTheta);
    s.phi         = 2.0 * MATH_PI * xi.x;
    s.pdf         = D_GGX(s.cosTheta, alpha) / 4.0;
    return s;
}

MicrofacetDistributionSample Charlie_sample(vec2 xi, float roughness)
{
    MicrofacetDistributionSample s;
    float alpha = roughness * roughness;
    s.sinTheta  = pow(xi.y, alpha / (2.0 * alpha + 1.0));
    s.cosTheta  = sqrt(1.0 - s.sinTheta * s.sinTheta);
    s.phi       = 2.0 * MATH_PI * xi.x;
    s.pdf       = D_Charlie(alpha, s.cosTheta) / 4.0;
    return s;
}

MicrofacetDistributionSample Lambertian_sample(vec2 xi, float roughness)
{
    MicrofacetDistributionSample s;
    s.cosTheta = sqrt(1.0 - xi.y);
    s.sinTheta = sqrt(xi.y);
    s.phi      = 2.0 * MATH_PI * xi.x;
    s.pdf      = s.cosTheta / MATH_PI;
    return s;
}

// ---------------------------------------------------------------------------
// Importance sample dispatch
// ---------------------------------------------------------------------------

vec4 getImportanceSample(int sampleIndex, vec3 N, float roughness)
{
    vec2 xi = hammersley2d(sampleIndex, u_sampleCount);

    MicrofacetDistributionSample s;
    if      (u_distribution == cLambertian) s = Lambertian_sample(xi, roughness);
    else if (u_distribution == cGGX)        s = GGX_sample(xi, roughness);
    else                                    s = Charlie_sample(xi, roughness);

    vec3 localDir = normalize(vec3(
        s.sinTheta * cos(s.phi),
        s.sinTheta * sin(s.phi),
        s.cosTheta
    ));
    vec3 dir = generateTBN(N) * localDir;
    return vec4(dir, s.pdf);
}

// ---------------------------------------------------------------------------
// Mip-filtered sample LOD  (GPU Gems 3, ch.20 / Krivanek & Colbert)
// ---------------------------------------------------------------------------

float computeLod(float pdf)
{
    return 0.5 * log2(6.0 * float(u_width) * float(u_width)
                      / (float(u_sampleCount) * pdf));
}

// ---------------------------------------------------------------------------
// Prefiltered colour integration
// ---------------------------------------------------------------------------

vec3 filterColor(vec3 N)
{
    vec3  color  = vec3(0.0);
    float weight = 0.0;

    for (int i = 0; i < u_sampleCount; ++i)
    {
        vec4  imp = getImportanceSample(i, N, u_roughness);
        vec3  H   = imp.xyz;
        float pdf = imp.w;
        float lod = computeLod(pdf) + u_lodBias;

        if (u_distribution == cLambertian)
        {
            color += textureLod(u_cubemapTexture, H, lod).rgb * u_intensityScale;
        }
        else // GGX or Charlie
        {
            vec3  V     = N;
            vec3  L     = normalize(reflect(-V, H));
            float NdotL = dot(N, L);
            if (NdotL > 0.0)
            {
                float useLod = (u_roughness == 0.0) ? u_lodBias : lod;
                color  += textureLod(u_cubemapTexture, L, useLod).rgb
                          * u_intensityScale * NdotL;
                weight += NdotL;
            }
        }
    }

    if (weight != 0.0) color /= weight;
    else               color /= float(u_sampleCount);

    return color;
}

// ---------------------------------------------------------------------------
// BRDF LUT generation (GGX + Charlie split-sum)
// ---------------------------------------------------------------------------

float V_SmithGGXCorrelated(float NoV, float NoL, float roughness)
{
    float a2  = pow(roughness, 4.0);
    float GGXV = NoL * sqrt(NoV * NoV * (1.0 - a2) + a2);
    float GGXL = NoV * sqrt(NoL * NoL * (1.0 - a2) + a2);
    return 0.5 / (GGXV + GGXL);
}

float V_Ashikhmin(float NdotL, float NdotV)
{
    return clamp(1.0 / (4.0 * (NdotL + NdotV - NdotL * NdotV)), 0.0, 1.0);
}

vec3 LUT(float NdotV, float roughness)
{
    vec3 V = vec3(sqrt(1.0 - NdotV * NdotV), 0.0, NdotV);
    vec3 N = vec3(0.0, 0.0, 1.0);

    float A = 0.0, B = 0.0, C = 0.0;

    for (int i = 0; i < u_sampleCount; ++i)
    {
        vec4  imp   = getImportanceSample(i, N, roughness);
        vec3  H     = imp.xyz;
        vec3  L     = normalize(reflect(-V, H));
        float NdotL = saturate(L.z);
        float NdotH = saturate(H.z);
        float VdotH = saturate(dot(V, H));

        if (NdotL > 0.0)
        {
            if (u_distribution == cGGX)
            {
                float V_pdf = V_SmithGGXCorrelated(NdotV, NdotL, roughness)
                              * VdotH * NdotL / NdotH;
                float Fc = pow(1.0 - VdotH, 5.0);
                A += (1.0 - Fc) * V_pdf;
                B += Fc * V_pdf;
            }
            if (u_distribution == cCharlie)
            {
                float sheenDist = D_Charlie(roughness, NdotH);
                float sheenVis  = V_Ashikhmin(NdotL, NdotV);
                C += sheenVis * sheenDist * NdotL * VdotH;
            }
        }
    }

    return vec3(4.0 * A, 4.0 * B, 4.0 * 2.0 * MATH_PI * C) / float(u_sampleCount);
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

void main()
{
    if (u_isGeneratingLUT == 1)
    {
        fragmentColor = vec4(LUT(texCoord.x, texCoord.y), 1.0);
        return;
    }

    // Map texCoord → face direction
    vec2 uv        = texCoord * 2.0 - 1.0;
    vec3 scan      = uvToXYZ(u_currentFace, uv);
    vec3 direction = normalize(scan);
    direction.y    = -direction.y;   // match cubemap face convention

    vec3 color = filterColor(direction);

    if (u_floatTexture == 0)
    {
        color = clamp(color / u_intensityScale, 0.0, 1.0);
    }

    fragmentColor = vec4(color, 1.0);
}
