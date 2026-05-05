#version 330 core
// Camera-facing billboard particle fragment shader.
// Samples a sequenced flipbook texture (GL_TEXTURE_2D_ARRAY of N frames)
// based on the particle's normalised age v_t.

in vec2  v_uv;
in float v_t;

uniform sampler2DArray u_flipbook;
uniform float          u_num_frames;
uniform float          u_fade_start_frame;  // alpha begins ramping down here
uniform float          u_fade_end_frame;    // alpha hits zero here

out vec4 FragColor;

void main() {
    // Current frame index along the flipbook (0 .. num_frames-1).
    // Nearest-neighbour pick (no temporal interpolation).
    float fi = clamp(v_t * (u_num_frames - 1.0), 0.0, u_num_frames - 1.0);
    int   layer = int(floor(fi));

    vec4 c = texture(u_flipbook, vec3(v_uv, float(layer)));

    // Soft fade-in over the first ~5 frames so cards appear instead of
    // popping in.  Fade-OUT is frame-based so the user can dial in the
    // ramp ("from frame 75 to 91" = u_fade_start_frame=75,
    // u_fade_end_frame=91).  Persisted in config / live-tunable via the
    // Sm Fade slider.
    float fade_in  = smoothstep(0.0, 5.0, fi);
    float fade_out = 1.0 - smoothstep(u_fade_start_frame,
                                      u_fade_end_frame, fi);
    float alpha    = c.a * fade_in * fade_out;

    FragColor = vec4(c.rgb, alpha);
}
