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
    // Current frame index along the flipbook (0 .. num_frames-1).
    //
    // Use `floor(v_t * num_frames)` (NOT `v_t * (num_frames - 1)`) so
    // every frame gets exactly 1/N of the loop period.  The previous
    // formula effectively skipped the LAST frame -- frame N-1's
    // displayable t-range was [1.0, 1.0) which never has any duration.
    // For a movie-clip-style billboard that's a visible hitch every
    // wrap of the loop.  The clamp keeps us inside texture-array
    // bounds when v_t reaches exactly 1.0.
    float fi    = floor(clamp(v_t * u_num_frames,
                              0.0, u_num_frames - 1.0));
    int   layer = int(fi);

    vec4 c = texture(u_flipbook, vec3(v_uv, float(layer)));

    // Fade-in over the first u_fade_in_frames frames so spawned
    // particles ease in instead of popping at full opacity.  Set
    // u_fade_in_frames = 0 to disable entirely (AnimatedBillboard
    // uses this for continuous-loop fire / smoke that should never
    // disappear at the loop boundary).
    float fade_in = (u_fade_in_frames > 0.0)
                  ? smoothstep(0.0, u_fade_in_frames, fi)
                  : 1.0;
    // Fade-OUT is frame-based so the user can dial in the ramp
    // ("from frame 75 to 91" = u_fade_start_frame=75,
    // u_fade_end_frame=91).  Persisted in config / live-tunable
    // via the Sm Fade slider.  AnimatedBillboard pins both endpoints
    // past the last frame to disable fade-out for continuous loops.
    float fade_out = 1.0 - smoothstep(u_fade_start_frame,
                                      u_fade_end_frame, fi);
    float alpha    = c.a * fade_in * fade_out;

    FragColor = vec4(c.rgb, alpha);
}
