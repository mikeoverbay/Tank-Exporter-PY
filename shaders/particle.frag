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
uniform float          u_fade_in_frames;    // 0 = no fade-in (movie-clip
                                            // style for AnimatedBillboard);
                                            // 5 = the legacy spawn-fade
                                            // ParticleSystem uses to soften
                                            // particle pop-in.

out vec4 FragColor;

void main() {
    // Continuous frame position used for FADE ramps.  Per Coffee
    // 2026-05-14 ("age may be screwing the fade outs wrong"): the
    // previous fade computation used `fi` (the floored frame index
    // clamped to num_frames-1), which meant fade_out never reached
    // 0 when fade_end_frame == num_frames -- the smoothstep input
    // hit num_frames-1 and stalled there with alpha ~0.13.
    // Particles then popped at the alpha_zero cull instead of
    // fading smoothly.  Using the continuous v_t * num_frames
    // (range [0, num_frames]) keeps the ramps differentiable to
    // the very end of lifetime.
    float frame_t = v_t * u_num_frames;

    // Floored frame index for texture-array indexing.  Same clamp
    // recipe as before: every frame still gets 1/N of the loop
    // period and we never sample past the last array layer.
    float fi    = floor(clamp(frame_t,
                              0.0, u_num_frames - 1.0));
    int   layer = int(fi);

    vec4 c = texture(u_flipbook, vec3(v_uv, float(layer)));

    // Fade-in over the first u_fade_in_frames frames so spawned
    // particles ease in instead of popping at full opacity.  Set
    // u_fade_in_frames = 0 to disable entirely (AnimatedBillboard
    // uses this for continuous-loop fire / smoke that should never
    // disappear at the loop boundary).
    float fade_in = (u_fade_in_frames > 0.0)
                  ? smoothstep(0.0, u_fade_in_frames, frame_t)
                  : 1.0;
    // Fade-OUT is frame-based so the user can dial in the ramp
    // ("from frame 75 to 91" = u_fade_start_frame=75,
    // u_fade_end_frame=91).  Persisted in config / live-tunable
    // via the Sm Fade slider.  AnimatedBillboard pins both endpoints
    // past the last frame to disable fade-out for continuous loops.
    // Uses the CONTINUOUS frame_t (NOT the floored fi) so the fade
    // reaches 0 cleanly when frame_t crosses u_fade_end_frame.
    float fade_out = 1.0 - smoothstep(u_fade_start_frame,
                                      u_fade_end_frame, frame_t);
    float alpha    = c.a * fade_in * fade_out;

    FragColor = vec4(c.rgb, alpha);
}
