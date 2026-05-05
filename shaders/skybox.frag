#version 330 core

in  vec3 v_dir;
out vec4 FragColor;

uniform samplerCube skybox;

void main()
{
    FragColor = texture(skybox, v_dir);
}
