#version 330 core
// Off-screen "back-buffer" picker -- fragment stage.
//
// Encodes (mesh_id, primitive_id) into the RGBA8 framebuffer so a
// single glReadPixels at the mouse location is enough to recover
// "which triangle of which mesh did the user hover over".  No
// shading -- the framebuffer is a hidden FBO, never displayed.
//
// Layout
//   R = mesh_id + 1     (0 reserved for "no hit", clear colour)
//   G = primID  & 0xFF
//   B = (primID >>  8) & 0xFF
//   A = (primID >> 16) & 0xFF
//
// 8 bits of mesh ID = up to 255 sub-meshes per tank (we use ~10).
// 24 bits of primitive ID = up to 16 M triangles per mesh
// (largest WoT tank ~250 K).  Both fit with room to spare.

uniform int u_mesh_id;          // 0..254 -- caller adds the offset

out vec4 FragColor;

void main()
{
    // gl_PrimitiveID counts triangles in draw order: 0, 1, 2, ...
    // We use the raw value -- the encode <-> decode pair stays in
    // this file's vocabulary, so no off-by-one drift.
    int prim = gl_PrimitiveID;

    // Pack into bytes.  Divide-by-255.0 because the framebuffer
    // colour pipeline treats the float output as a normalised 0..1
    // range and quantises to 8-bit on write.
    float r = float(u_mesh_id + 1) / 255.0;
    float g = float( prim         & 0xFF) / 255.0;
    float b = float((prim >>  8)  & 0xFF) / 255.0;
    float a = float((prim >> 16)  & 0xFF) / 255.0;

    FragColor = vec4(r, g, b, a);
}
