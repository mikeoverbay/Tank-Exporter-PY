# DirectX 11 vs OpenGL Coordinate Systems

## Visual Comparison

### DirectX 11 (Right-Handed)
```
        Y (Up)
        |
        |
        +------ X (Right)
       /
      /
     Z (Away from camera / into screen)
```

**Key Properties:**
- Right-handed coordinate system
- +X points right
- +Y points up  
- +Z points away from camera (into the screen/depth)
- Camera looks down the -Z axis (backwards along Z)
- Normalized Device Coordinates (NDC): X,Y in [-1, 1], Z in [0, 1]

---

### OpenGL (Right-Handed)
```
        Y (Up)
        |
        |
        +------ X (Right)
       /
      /
     Z (Towards camera / out of screen)
```

**Key Properties:**
- Right-handed coordinate system
- +X points right
- +Y points up
- +Z points **towards camera** (out of screen)
- Camera looks down the -Z axis (into the screen)
- Normalized Device Coordinates (NDC): X,Y,Z in [-1, 1]

---

## Key Differences

| Aspect | DirectX 11 | OpenGL |
|--------|-----------|--------|
| **Z-axis direction** | +Z away from camera | +Z towards camera |
| **Eye position** | Looking along -Z (backwards) | Looking along -Z (forward) |
| **Front face winding** | Counter-clockwise (CCW) | Counter-clockwise (CCW) |
| **Back-face culling** | Remove faces where normal points away | Remove faces where normal points away |
| **Clip space Z** | [0, 1] (0=near, 1=far) | [-1, 1] (-1=near, 1=far) |

---

## Coordinate Transformation

To convert from DirectX to OpenGL, the simple Z-flip approach:

```
Position_GL.x = Position_DX.x
Position_GL.y = Position_DX.y
Position_GL.z = -Position_DX.z           // FLIP Z
```

However, this alone causes issues:

### Problem: Z-flip creates reversed faces
When you flip Z coordinates:
1. Triangle winding order appears reversed when viewed from camera
2. Front faces become back faces (culled)
3. Back faces become front faces

### Solution Options:

**Option A: Flip Z + Reverse Winding Order**
```
1. Negate all Z coordinates
2. Reverse triangle winding: (v0, v1, v2) → (v2, v1, v0)
3. Negate Z component of normals
```

**Option B: Flip Z + Reverse Face Culling**
```
1. Negate all Z coordinates
2. Change GL_CW to GL_CCW (or vice versa)
3. Negate Z component of normals
```

**Option C: Use a transformation matrix**
```
glm::mat4 dx_to_gl = glm::scale(glm::mat4(1.0f), glm::vec3(1.0f, 1.0f, -1.0f));
// Apply to positions in vertex shader or during loading
```

---

## Tank Exporter Format Considerations

The World of Tanks `.primitives_processed` format stores:
- **Positions**: (x, y, z) in DirectX space
- **Normals**: (nx, ny, nz) in DirectX space
- **Tangents/Binormals**: Tangent space basis vectors in DirectX space
- **Winding order**: Uses special rules (see VISUAL_PROCESSED_FORMAT.md)

Current handling in tank_viewer.py:
1. ✅ Positions Z-flipped
2. ✅ Normals Z-flipped
3. ✅ Tangents Z-flipped
4. ✅ Binormals Z-flipped
5. ✅ Winding order reversed

---

## Testing the Transformation

To verify the transformation is working correctly:
1. **Lighting**: Normals should face the camera for proper Phong shading
2. **Silhouettes**: Model edges should be sharp and well-defined
3. **Back-face culling**: Interior faces should be hidden
4. **Texturing**: UV coordinates should map correctly (no mirroring)
5. **Shadows**: If implemented, should fall in correct direction

If any of these fail, the transformation needs adjustment.
