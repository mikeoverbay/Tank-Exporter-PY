#version 330 core

layout(location = 0) in vec3 position;

out vec3 v_dir;          // cube-map sampling direction

uniform mat4 view;
uniform mat4 projection;

void main()
{
    // Strip translation from the view matrix so the skybox is always centred
    // on the camera (infinite distance).
    mat4 view_rot = mat4(mat3(view));

    // Use the raw vertex position as the sampling direction.
    v_dir = position;

    // Write to the far clip plane (z = w after perspective divide → depth 1.0).
    vec4 pos = projection * view_rot * vec4(position, 1.0);
    gl_Position = pos.xyww;
}
