"""Mouse-driven camera controls extracted from viewer.py.

Per Coffee 2026-05-10 ("break out mouse functions in to its own
file"): three pure helpers that each take a `viewer` reference and
the cursor delta, then mutate the relevant camera state.  No
keybind logic, no event-loop awareness; the caller decides which
helper to invoke based on modifier-key state.

Helpers
-------
* `apply_orbit(viewer, dx, dy)`
        Mode-aware orbit (free cam / chase / commander POV) -- the
        same yaw / pitch math the LEFT-drag branch in viewer.py
        used inline.

* `apply_pan(viewer, dx, dy)`
        XZ ground-plane pan.  Bound to Shift + LEFT-drag.

* `apply_y_lift(viewer, dy)`
        Y-axis lift of the look-at point.  Bound to Ctrl + LEFT-drag.

Direction convention (per Coffee 2026-05-09):
    mouse moves up    -> image moves up
    mouse moves right -> image moves right
i.e. the camera centre moves in the OPPOSITE direction of the
cursor delta so the world tracks the cursor.

Speeds (Coffee 2026-05-10 "if we are at 1.0 make it 0.6" --
slowed every mouse-driven camera response by 0.6x):
    orbit pitch / yaw : dx * 0.30  /  dy * 0.30      (was 0.50)
    XZ pan            : 0.01 * camera.distance * 0.12  (was 0.20)
    Y lift            : 0.01 * camera.distance * 0.06  (was 0.10)
Pitch is clamped to keep the camera from flipping over the
look-at on every mode.
"""
import numpy as np


def apply_orbit(viewer, dx, dy):
    """LEFT-drag orbit, mode-aware.

    Mode 0 (free cam / orbit): rotate the world-frame orbit.
    Mode 1 (chase): rotate the chassis-local chase eye around the
            turret HP look-at.
    Mode 2 (driver / commander POV): rotate the head in
            chassis-local space (yaw + pitch).
    """
    if viewer.camera_mode == 2:
        viewer._head_yaw_deg   += dx * 0.3
        viewer._head_pitch_deg += dy * 0.3
        viewer._head_pitch_deg  = float(np.clip(
            viewer._head_pitch_deg, -85.0, +85.0))
    elif viewer.camera_mode == 1:
        viewer._chase_yaw_deg   += dx * 0.3
        viewer._chase_pitch_deg += dy * 0.3
        viewer._chase_pitch_deg  = float(np.clip(
            viewer._chase_pitch_deg, -85.0, +85.0))
    else:
        viewer.camera.yaw   += dx * 0.3
        viewer.camera.pitch += dy * 0.3
        viewer.camera.pitch  = float(np.clip(
            viewer.camera.pitch, -89.0, 89.0))


def apply_pan(viewer, dx, dy):
    """XZ ground-plane pan.  Camera right + forward vectors are
    flattened to the ground plane so the pan tracks the camera's
    current orientation.

    Sign convention:
      dx > 0 (mouse right) -> centre moves -right (= world LEFT)
                              -> visible scene shifts RIGHT
      dy > 0 (mouse down)  -> centre moves +fwd (away from cam)
                              -> visible scene shifts DOWN

    Both match the stated "image follows cursor" rule.
    """
    speed = 0.01 * viewer.camera.distance * 0.12
    view  = viewer.camera.get_view_matrix()
    # camera right (view[0]) and -backward (view[2] negated) flat
    # to the XZ plane.
    right_xz = np.array(
        [view[0, 0], 0.0, view[0, 2]], dtype=np.float32)
    fwd_xz   = np.array(
        [-view[2, 0], 0.0, -view[2, 2]], dtype=np.float32)
    r_len = float(np.linalg.norm(right_xz))
    f_len = float(np.linalg.norm(fwd_xz))
    if r_len > 1e-6:
        right_xz /= r_len
    if f_len > 1e-6:
        fwd_xz   /= f_len
    viewer.camera.center -= right_xz * (dx * speed)
    viewer.camera.center += fwd_xz   * (dy * speed)


def apply_zoom_drag(viewer, dy):
    """Right-button drag zoom.  Per Coffee 2026-05-10 ("Right
    down should also zoom like mouse wheel"): vertical mouse
    motion while right-button is held zooms the camera, with
    the same sign convention as the wheel:
        dy > 0 (mouse moves DOWN) -> zoom OUT (distance grows)
        dy < 0 (mouse moves UP)   -> zoom IN  (distance shrinks)

    Speed factor 0.005 per pixel of dy gives a comfortable
    "drag 100 px = ~50 % distance change" feel.  Mode-aware:
        Mode 1 (chase) zooms `_chase_distance` (chassis-local).
        Mode 2 (commander POV) is fixed-position; zoom is a
            no-op (matches wheel behaviour).
        Otherwise free-cam zooms `camera.distance`.

    Same per-frame distance multiplication as the wheel handler
    so the chain feels identical whether you scroll or drag.
    """
    factor = 1.0 + dy * 0.005
    if viewer.camera_mode == 1:
        viewer._chase_distance = max(
            0.5, viewer._chase_distance * factor)
    elif viewer.camera_mode != 2:
        viewer.camera.distance *= factor


def apply_y_lift(viewer, dy):
    """Y-lift the look-at centre.

    Sign convention:
      dy > 0 (mouse down) -> centre.y INCREASES (camera looks higher)
                             -> visible scene shifts DOWN
      dy < 0 (mouse up)   -> centre.y DECREASES
                             -> visible scene shifts UP

    Half the pan-XZ speed factor (0.06 vs 0.12) so Y lift feels
    deliberate and lands precisely instead of overshooting on
    quick vertical drags.
    """
    speed_y = 0.01 * viewer.camera.distance * 0.06
    viewer.camera.center[1] += dy * speed_y
