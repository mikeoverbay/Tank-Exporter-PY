#version 330 core
layout (location = 0) in vec3 position;
layout (location = 1) in vec3 normal;
layout (location = 2) in vec3 tangent;
layout (location = 3) in vec3 binormal;
layout (location = 4) in vec2 uv0;

out VS_OUT {
    vec3 position;
    vec3 normal;
    mat3 TBN;
    vec2 uv0;
} vs_out;

uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;

void main() {
    vs_out.position = vec3(model * vec4(position, 1.0));
    vs_out.normal = normalize(mat3(transpose(inverse(model))) * normal);

    vec3 T = normalize(mat3(model) * tangent);
    vec3 B = normalize(mat3(model) * binormal);
    vec3 N = vs_out.normal;

    float invmax = inversesqrt(max(dot(T, T), dot(B, B)));
    vs_out.TBN = mat3(T * invmax, B * invmax, N * invmax);

    vs_out.uv0 = uv0;
    gl_Position = projection * view * vec4(vs_out.position, 1.0);
}
