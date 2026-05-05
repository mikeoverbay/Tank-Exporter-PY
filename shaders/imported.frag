#version 330 core
// Simple diffuse + bump shader for imported FBX / GLB / OBJ meshes.
//
// The full PBR shader (mesh.frag) expects WoT-specific data:
//     - GMM channel routing (R=gloss, G=metal, B=camo mask)
//     - AO routed via either AM.alpha or aoMap.green
//     - alpha test in normal-map red channel
//     - nation-color tinting
//     - prefiltered/irradiance/BRDF IBL maps
//     - detail (scratch) noise driving sSpec
// None of that data exists in an arbitrary FBX import, so we use this
// stripped-down shader instead -- diffuse texture + optional normal
// map + Lambertian + small Phong specular from the 3 scene lights.

#define NUM_LIGHTS 3

in VS_OUT {
    vec3 position;
    vec3 normal;
    mat3 TBN;
    vec2 uv0;
} fs_in;

out vec4 FragColor;

uniform sampler2D diffuse_map;     // unit 0
uniform sampler2D normal_map;      // unit 1  (placeholder grey when absent)

uniform vec3  light_pos[NUM_LIGHTS];
uniform vec3  view_pos;

uniform int   use_normal_map;
uniform int   is_GA_normal;        // 1 = WoT GA-encoded NM (decode via .ga);
                                   // 0 = standard RGB normal map

uniform float metal_scale;         // direct-light brightness slider
uniform float shine_scale;         // ambient slider

uniform int   wireframe_mode;

// =============================================================================
// Normal-map decode (handles both standard RGB and WoT GA encoding)
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

void main()
{
    // Base colour from diffuse texture.  No alpha test, no AO, no
    // gloss/metal -- just take the RGB at face value.
    vec3 base = texture(diffuse_map, fs_in.uv0).rgb;

    // Shading normal: geometry by default, perturbed if a real normal
    // map is bound and the toggle is on.
    vec3 N = normalize(fs_in.normal);
    if (use_normal_map > 0) {
        N = normalize(fs_in.TBN * unpackNormal(fs_in.uv0));
    }

    vec3 V    = normalize(view_pos - fs_in.position);
    vec3 lit  = vec3(0.0);

    // Lambertian + tight Phong from each of the 3 scene lights, summed
    // and normalised by NUM_LIGHTS (matches the PBR shader's energy
    // balance so the Light slider feels the same).
    for (int i = 0; i < NUM_LIGHTS; ++i) {
        vec3  L     = normalize(light_pos[i] - fs_in.position);
        float NdotL = max(dot(N, L), 0.0);
        // Diffuse
        lit += base * NdotL;
        // Phong specular -- small white highlight, intensity tied to
        // the light slider so dialing it down kills shine too.
        if (NdotL > 0.0) {
            vec3  R     = reflect(-L, N);
            float spec  = pow(max(dot(R, V), 0.0), 16.0);
            lit += vec3(0.30) * spec;
        }
    }
    lit *= (10.0 * metal_scale / float(NUM_LIGHTS));

    // Flat ambient fill so unlit faces aren't pure black
    vec3 ambient = base * shine_scale * 0.30;

    vec3 result = ambient + lit;

    // sRGB gamma + simple clamp (no tonemap; PBR shader's ACES isn't
    // needed for this simple LDR pipeline)
    result = pow(clamp(result, 0.0, 1.0), vec3(1.0 / 2.2));

    if (wireframe_mode == 1) {
        FragColor = vec4(0.75, 0.75, 0.75, 1.0);
    } else {
        FragColor = vec4(result, 1.0);
    }
}
