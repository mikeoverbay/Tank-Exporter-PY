#version 330 core
//
// Surface-normal debug-line shader -- GEOMETRY stage
//
// Two modes driven by u_mode:
//
//   0 (by-face, default)
//     One line per triangle, from the triangle's CENTROID
//     (arithmetic mean of the 3 vertex positions) in the direction
//     of the TRUE FACE NORMAL computed via cross-product of two
//     edges.  Solid cyan colour.  Cross-product instead of an
//     averaged vertex normal so the line shows the actual
//     triangle's plane orientation -- catches reversed-wound
//     faces (line will poke INTO the mesh) and ignores any
//     smoothing-group / custom-normal skew on the lighting
//     normals.
//
//   1 (by-vertex)
//     Three lines per triangle, one starting at each vertex going
//     in that vertex's own normal.  Each line is coloured by the
//     ABSOLUTE value of the normal vector (|x|->R, |y|->G, |z|->B),
//     so axis-aligned faces show pure red / green / blue and
//     diagonal faces show natural blends.  Doubles up at shared
//     edges since each adjacent triangle emits its own copy --
//     acceptable for debug; speaks loudest visually.
//
// Length-uniform u_normal_length scales every line; 0 disables the
// pass on both the CPU side and via early-return here.

layout(triangles)                        in;
layout(line_strip, max_vertices = 6)     out;

in  vec3 v_world_pos[];
in  vec3 v_world_normal[];

uniform mat4  view;
uniform mat4  projection;
uniform float u_normal_length;
uniform int   u_mode;     // 0 = by-face, 1 = by-vertex

out vec3 v_color;          // passed to the fragment stage

void main() {
    if (u_normal_length <= 0.0) {
        return;
    }
    mat4 vp = projection * view;

    if (u_mode == 0) {
        // ---------- by-face ----------
        // True face normal computed from the triangle's plane
        // (cross-product of two edges).  This is winding-dependent:
        // a CCW triangle yields a normal that points OUT of the
        // visible side, a CW one points the other way -- so any
        // reverse-wound face stands out immediately as a line
        // poking INTO the mesh.  This is what people generally
        // expect from "face normal", distinct from the lighting
        // normal (which can be skewed by smoothing groups + custom
        // per-vertex normals -- that's what the by-vertex mode is for).
        vec3 e1 = v_world_pos[1] - v_world_pos[0];
        vec3 e2 = v_world_pos[2] - v_world_pos[0];
        vec3 n  = cross(e1, e2);
        float L = length(n);
        if (L < 1e-6) return;       // degenerate (zero-area) triangle
        n /= L;

        // Centroid = arithmetic mean of the 3 vertex positions.
        // Line start sits HERE, not at any vertex.
        vec3 c = (v_world_pos[0]
                + v_world_pos[1]
                + v_world_pos[2]) / 3.0;

        v_color = vec3(0.10, 1.00, 0.85);   // cyan
        gl_Position = vp * vec4(c, 1.0);
        EmitVertex();
        gl_Position = vp * vec4(c + n * u_normal_length, 1.0);
        EmitVertex();
        EndPrimitive();
    } else {
        // ---------- by-vertex ----------
        // Three separate 2-vertex line strips, one per triangle
        // vertex.  Each strip's colour comes from |n| of that
        // vertex so the user can see normal orientation at a glance.
        for (int i = 0; i < 3; i++) {
            vec3 n = v_world_normal[i];
            float L = length(n);
            if (L < 1e-6) continue;
            n /= L;

            // |x|=R, |y|=G, |z|=B -- pure-axis normals show pure
            // primary colour, off-axis blend naturally.
            v_color = abs(n);

            gl_Position = vp * vec4(v_world_pos[i], 1.0);
            EmitVertex();
            gl_Position = vp * vec4(v_world_pos[i] + n * u_normal_length, 1.0);
            EmitVertex();
            EndPrimitive();
        }
    }
}
