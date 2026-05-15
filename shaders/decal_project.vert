#version 330 core
// Screen-space (volume) decal projector -- vertex stage.
//
// Per Coffee 2026-05-15 ("look here at my decal projector frag
// and vert  C:\\nuTerra\\nuTerra\\shaders\\Terrain_shaders"):
// ported from nuTerra's DecalProject.{vert,frag}.  Re-targeted
// to GL 3.30 / forward pipeline (no UBO, no #include) so it
// drops cleanly into TEPY's existing shader loader.
//
// The decal is rendered as a UNIT CUBE in local [-0.5, +0.5]^3
// space.  The CPU side builds `u_decal_matrix` to position +
// orient + scale the cube into the world such that:
//
//   * the cube's CENTER sits at the impact point + a small
//     bias up the surface normal;
//   * the cube's local +Z axis aligns with the surface normal
//     (= "perpendicular to the surface");
//   * local X and Y span the surface plane (texture UV axes);
//   * local scale sets the decal's footprint width / height +
//     the cube's vertical extent (the "volume thickness" --
//     how deep into the surface the decal box reaches).
//
// The cube doesn't have to actually overlap any geometry --
// its purpose is just to "claim" the screen pixels where the
// decal might fall.  The frag stage reads the scene depth at
// each claimed pixel and reconstructs the world position to
// decide whether the underlying surface is actually inside
// the decal volume.
//
// `u_inv_view_proj_decal` is the inverse of (proj * view *
// u_decal_matrix), pre-multiplied on the CPU so the frag
// stage's reconstruction is a single mat4 * vec4.

layout(location = 0) in vec3 a_position;   // unit cube verts

uniform mat4 u_view;
uniform mat4 u_proj;
uniform mat4 u_decal_matrix;
uniform mat4 u_inv_view_proj_decal;

// Flat-qualified so the matrix isn't interpolated across the
// triangle (it's the same for every fragment of the same
// decal anyway).
flat out mat4 v_inv_mvp;

void main()
{
    gl_Position = u_proj * u_view * u_decal_matrix
                  * vec4(a_position, 1.0);
    v_inv_mvp   = u_inv_view_proj_decal;
}
