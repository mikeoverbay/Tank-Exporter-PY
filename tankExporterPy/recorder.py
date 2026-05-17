"""F3 manual recorder + 1-Turn auto-test snapshot helpers.

This module is a mechanical extraction of the recorder logic that
used to live in `Viewer._build_recorder_frame`,
`Viewer._on_manual_record_clicked` etc.  Each function here takes
the active `Viewer` instance as its first argument and reads /
writes the same `_manual_record_*` / `_turn_test_*` fields the
viewer-bound methods used to operate on -- so callers (the bound
methods on `Viewer`) can delegate verbatim, no behavioural change.

Why split it out
----------------
The viewer module crossed 13k lines in 1.112; the recorder block
(snapshot + per-recorder capture / finalize / save_json) is a
self-contained ~700 lines that doesn't touch GL state.  Moving
it shrinks viewer.py without changing any public interface.

Public API
----------
    build_recorder_frame(viewer, dt, t_accum_s, frame_idx,
                          prev_states=None) -> dict
        Shared per-frame snapshot used by every recorder.

    on_turn_test_clicked(viewer)
    turn_test_capture_frame(viewer, dt)
    turn_test_finalize(viewer, write_json=True, reason='complete')
    turn_test_save_json(viewer, reason='complete') -> path

    on_manual_record_clicked(viewer)
    manual_record_capture_frame(viewer, dt)
    manual_record_finalize(viewer)
    manual_record_save_json(viewer) -> path

The zigzag recorder still lives on `Viewer` for now -- its
finite-state-machine over driving phases tangles deeply with the
drive code, so it earns its own follow-up extraction.

The viewer fields each function reads / writes are documented at
the top of every helper.  None of them are GL-bound.
"""
import math
import os
import json
import shutil
import glob as _glob
import traceback
from datetime import datetime

import numpy as np


# ---------------------------------------------------------------------
# Shared per-frame snapshot.
# ---------------------------------------------------------------------
def build_recorder_frame(viewer, dt, t_accum_s, frame_idx,
                          prev_states=None):
    """Build one frame's recorder dict from the viewer's current
    physics state.

    Captures both target and render pose so offline analysis can
    tell solver spikes (target jumps, integrator stays smooth)
    apart from integrator-induced oscillation (target stable,
    render rings).  The integrator-error fields (`e_pitch_deg`,
    `e_roll_deg`, `e_y_m`) and the unclamped per-wheel delta
    (`delta_y`) cover every signal that the second-order spring +
    classifier hysteresis loop reads, so any oscillation in the
    visible chassis pose has to show up here.

    Reads (viewer):  tank_physics, terrain, _frame_timers_complete,
                     _show_track_spline, _track_left, _track_right,
                     _track_test_dy, _auto_circle, _current_forward.
    Writes:          nothing.
    """
    tp = viewer.tank_physics
    STATE_NAMES = {0: 'NONE', 1: 'CONTACT', 2: 'HANGING', 3: 'OVER_COMP'}
    wheels_out = []
    n = len(tp.wheels)
    chassis_render = np.asarray(tp.chassis_matrix(), dtype=np.float64)
    if (viewer.terrain is not None
            and hasattr(viewer.terrain, 'sample_heights')):
        local_h = np.column_stack([
            tp.wheels[:, 0], tp.wheels[:, 1], tp.wheels[:, 2],
            np.ones(len(tp.wheels), dtype=np.float64)])
        world_render_h = (chassis_render @ local_h.T).T
        terrain_render_y = np.asarray(viewer.terrain.sample_heights(
            world_render_h[:, 0], world_render_h[:, 2]),
            dtype=np.float64)
    else:
        terrain_render_y = np.zeros(n, dtype=np.float64)
    HYST     = 0.020
    comp_cap = -float(tp.min_offset)
    ext_cap  = -float(tp.max_offset)
    delta_arr = (np.asarray(tp.last_delta_y, dtype=np.float64)
                 if hasattr(tp, 'last_delta_y')
                 and len(tp.last_delta_y) == n
                 else np.zeros(n, dtype=np.float64))
    n_state_changes = 0
    for i in range(n):
        name = (tp.wheel_bone_names[i]
                if i < len(tp.wheel_bone_names) and tp.wheel_bone_names[i]
                else None)
        local = tp.wheels[i]
        wlw   = (tp.last_wheel_world[i]
                 if i < len(tp.last_wheel_world)
                 else (0.0, 0.0, 0.0))
        ty    = (float(tp.last_terrain_y[i])
                 if i < len(tp.last_terrain_y) else 0.0)
        tgt   = (float(tp.last_target_y[i])
                 if i < len(tp.last_target_y) else 0.0)
        rry   = (float(tp.last_residual_y[i])
                 if i < len(tp.last_residual_y) else 0.0)
        sry   = (float(tp.smoothed_residual_y[i])
                 if hasattr(tp, 'smoothed_residual_y')
                 and i < len(tp.smoothed_residual_y)
                 else 0.0)
        sc    = (int(tp.last_wheel_state[i])
                 if hasattr(tp, 'last_wheel_state')
                 and i < len(tp.last_wheel_state)
                 else 0)
        local_skinned = np.array(
            [float(local[0]),
             float(local[1]) + rry,
             float(local[2]),
             1.0], dtype=np.float64)
        wlw_render = chassis_render @ local_skinned
        d = float(delta_arr[i])
        if sc == 1 or sc == 0:
            dist_to_flip = min(d - (ext_cap - HYST),
                               (comp_cap + HYST) - d)
        elif sc == 2:
            dist_to_flip = (ext_cap + HYST) - d
        elif sc == 3:
            dist_to_flip = d - (comp_cap - HYST)
        else:
            dist_to_flip = 0.0
        sc_prev = (int(prev_states[i])
                   if (prev_states is not None and i < len(prev_states))
                   else sc)
        state_changed = (sc != sc_prev) and prev_states is not None
        if state_changed:
            n_state_changes += 1
        wheels_out.append({
            'idx':             i,
            'side':            'L' if i < tp.n_left else 'R',
            'name':            name,
            'local_xyz':       [float(local[0]), float(local[1]),
                                float(local[2])],
            'world_xyz':       [float(wlw[0]), float(wlw[1]),
                                float(wlw[2])],
            'world_xyz_render': [float(wlw_render[0]),
                                 float(wlw_render[1]),
                                 float(wlw_render[2])],
            'terrain_y':       ty,
            'terrain_y_render': float(terrain_render_y[i]),
            'target_y':        tgt,
            'residual_y':      rry,
            'smoothed_residual_y': sry,
            'delta_y':         float(delta_arr[i]),
            'state_code':      sc,
            'state':           STATE_NAMES.get(sc, '?'),
            'state_prev_code': sc_prev,
            'state_changed':   bool(state_changed),
            'hyst_dist_to_flip_m': float(dist_to_flip),
        })

    return {
        'frame':                 frame_idx,
        't_s':                   float(t_accum_s),
        'dt_s':                  float(dt),
        'yaw_deg':               float(tp.yaw_deg),
        'pitch_deg':             float(tp.pitch_deg),
        'roll_deg':              float(tp.roll_deg),
        'pitch_render':          float(getattr(tp, '_render_pitch_deg', 0.0)),
        'roll_render':           float(getattr(tp, '_render_roll_deg', 0.0)),
        'omega_pitch_dps':       float(getattr(tp, 'omega_pitch_dps', 0.0)),
        'omega_roll_dps':        float(getattr(tp, 'omega_roll_dps', 0.0)),
        'a_lat_mps2':            float(getattr(tp, '_last_a_lat_mps2', 0.0)),
        'speed_mps':             float(getattr(tp, '_last_speed_mps', 0.0)),
        'yaw_rate_dps':          float(getattr(tp, '_last_yaw_rate_dps', 0.0)),
        'lean_target_deg':       float(getattr(tp, '_target_lat_lean_roll_deg', 0.0)),
        'a_long_mps2':           float(getattr(tp, '_last_a_long_mps2', 0.0)),
        'signed_speed_mps':      float(getattr(tp, '_last_signed_speed_mps', 0.0)),
        'pitch_lean_target_deg': float(getattr(tp, '_target_long_lean_pitch_deg', 0.0)),
        'a_lat_smoothed_mps2':   float(getattr(tp, '_smoothed_a_lat_mps2', 0.0)),
        'a_long_smoothed_mps2':  float(getattr(tp, '_smoothed_a_long_mps2', 0.0)),
        'gate_rear_over_render': bool(getattr(
            tp, '_gate_any_rear_over_render', False)),
        'gate_front_over_render': bool(getattr(
            tp, '_gate_any_front_over_render', False)),
        'pos_x':                 float(tp.pos[0]),
        'pos_y':                 float(tp.pos[1]),
        'pos_y_render':          float(getattr(tp, '_render_pos_y', 0.0)),
        'pos_z':                 float(tp.pos[2]),
        'vy_mps':                float(tp.vy),
        'render_vy_mps':         float(getattr(tp, '_render_vy', 0.0)),
        'cur_speed_mps_signed':  float(getattr(viewer, '_current_forward', 0.0)),
        'target_pitch_with_lean_deg': float(getattr(
            tp, '_last_target_pitch_with_lean', 0.0)),
        'target_roll_with_lean_deg':  float(getattr(
            tp, '_last_target_roll_with_lean', 0.0)),
        'e_pitch_deg':           float(getattr(tp, '_last_e_pitch_deg', 0.0)),
        'e_roll_deg':            float(getattr(tp, '_last_e_roll_deg', 0.0)),
        'e_y_m':                 float(getattr(tp, '_last_e_y_m', 0.0)),
        'lift_needed_m':         float(getattr(tp, '_last_lift_needed_m', 0.0)),
        'cur_forward_mps_input': float(getattr(tp, 'cur_forward_mps', 0.0)),
        'auto_circle_active':    bool(getattr(viewer, '_auto_circle', False)),
        'track_spline_active':   bool(getattr(viewer, '_show_track_spline', False)
                                       and getattr(viewer, '_track_left', None) is not None
                                       and getattr(viewer, '_track_right', None) is not None),
        'track_test_dy_m':       float(getattr(viewer, '_track_test_dy', 0.0)),
        'state_changes_count':   int(n_state_changes),
        'frame_timers_ms':       dict(getattr(viewer, '_frame_timers_complete', {})),
        'wheels':                wheels_out,
        'chain_focus':           _build_chain_focus_safe(viewer),
    }


def _build_chain_focus_safe(viewer):
    try:
        return _build_chain_focus(viewer)
    except Exception as _ex:
        if not getattr(viewer, '_chain_focus_err_logged', False):
            import traceback as _tb
            print(f"[manual_record] chain_focus build failed: "
                  f"{type(_ex).__name__}: {_ex}")
            _tb.print_exc()
            viewer._chain_focus_err_logged = True
        return {}


def _json_safe(obj):
    """Recursively convert `obj` into JSON-native Python types.

    Per Coffee 2026-05-16 ("says nothing saved"): the F3 save
    silently failed because `_pending_chassis_info` carries
    tuples of numpy arrays (`track_sag_L`, `track_sag_R`) that
    `json.dump` doesn't know how to serialise.  The write
    truncated mid-meta and the caught exception only logged
    "save failed" once -- the user just saw "STOPPED (...
    nothing saved)" and moved on.

    Handles:
        * numpy scalars  -> Python scalar
        * numpy ndarrays -> nested list
        * tuples         -> list (json has no tuple type)
        * dicts          -> dict with stringified keys
        * lists          -> list (recursed)
        * bytes          -> latin-1 decoded string
        * everything else passes through; if it's still
          non-native after this pass, `json.dump` will fall
          back to its standard TypeError, which is what we
          want for genuinely unserialisable types.
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    # numpy: scalar -> .item(); array -> .tolist().
    if hasattr(obj, 'tolist'):
        try:
            return obj.tolist()
        except Exception:
            pass
    if hasattr(obj, 'item'):
        try:
            return obj.item()
        except Exception:
            pass
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (bytes, bytearray)):
        try:
            return obj.decode('latin-1')
        except Exception:
            return repr(obj)
    return obj


def _build_chain_focus(viewer):
    """Per Coffee 2026-05-16 ("F3 is bound to record.  lets
    record the seg positions that are affected by one of the
    bottom wheels.  maybe other info?").

    Captures chain-pad context around ONE bottom wheel per side
    so offline analysis can correlate pad motion against
    suspension travel, wheel rotation, and the chain s_offset
    flow.

    Per Coffee 2026-05-16 ("i was on the edge again"): the
    middle-road-wheel focus missed L-side flips when W_L1 was
    at the cap and W_L2 was HANGING.  Now each side returns a
    LIST of focus dicts, one per road wheel, so offline
    analysis can scan every wheel for chain anomalies.

    For each road wheel per side records:

      * `wheel_name`         -- bone name (`W_L2`, etc.).
      * `hub_chassis_yz`     -- wheel hub (Y, Z) in chassis-
        local renderer frame (post Z-flip).
      * `hub_R_m`            -- wheel radius from chassis XML
        `<wheelGroups><groupRadius>` (bare rim).
      * `wheel_angle_rad`    -- accumulated spin angle.
      * `s_offset_m`         -- chain arc-length accumulator.
      * `pads`               -- per-pad list of `(idx, Y, Z,
        d_hub_m)` for every pad whose centre sits within
        `1.5 * (R + inner_thickness)` of the hub in YZ.

    Layout:

        {'L': [{...W_L0...}, {...W_L1...}, ..., {...W_Ln...}],
         'R': [{...W_R0...}, {...W_R1...}, ..., {...W_Rn...}]}
    """
    out = {}
    tp = viewer.tank_physics
    if tp is None:
        return out
    ci = getattr(viewer, '_pending_chassis_info', None) or {}
    roles = ci.get('wheel_roles') or {}
    radii_xml = ci.get('wheel_radii') or {}
    inner_t = float(ci.get('segmentsInnerThickness') or 0.0)
    bones_now = (getattr(viewer, '_last_homie_bones', None)
                 or getattr(viewer, '_track_chassis_bones_bind', None))
    if not isinstance(bones_now, dict) or not bones_now:
        return out
    names_road = list(getattr(tp, 'wheel_bone_names', []))
    wheel_angles = getattr(tp, 'wheel_angles_rad', None)
    for side_token in ('L', 'R'):
        pos_attr = f'_last_homie_pos_{side_token}'
        pos = getattr(viewer, pos_attr, None)
        if pos is None or len(pos) == 0:
            continue
        road = roles.get(f'road_wheels_{side_token}') or []
        if not road:
            continue
        s_offset = float(getattr(
            viewer, f'_track_chain_s_offset_{side_token}', 0.0))
        per_wheel = []
        for wheel_name in road:
            b = bones_now.get(wheel_name)
            if b is None:
                b = bones_now.get(wheel_name + '_BlendBone')
            if b is None:
                continue
            hub_y = float(b[1])
            hub_z = -float(b[2])
            R = float(radii_xml.get(wheel_name) or 0.0)
            if R <= 0.0:
                continue
            spin = 0.0
            try:
                ri = names_road.index(wheel_name)
            except ValueError:
                ri = -1
            if (ri >= 0 and wheel_angles is not None
                    and ri < len(wheel_angles)):
                spin = float(wheel_angles[ri])
            rng = 1.5 * (R + inner_t)
            pads = []
            for i, p in enumerate(pos):
                dy = float(p[1]) - hub_y
                dz = float(p[2]) - hub_z
                d = math.sqrt(dy * dy + dz * dz)
                if d <= rng:
                    pads.append({
                        'idx':   int(i),
                        'y':     float(p[1]),
                        'z':     float(p[2]),
                        'd_hub': float(d),
                    })
            per_wheel.append({
                'wheel_name':      wheel_name,
                'hub_chassis_yz':  [hub_y, hub_z],
                'hub_R_m':         R,
                'inner_t_m':       inner_t,
                'wheel_angle_rad': spin,
                's_offset_m':      s_offset,
                'n_pads_in_band':  len(pads),
                'pads':            pads,
            })
        if per_wheel:
            out[side_token] = per_wheel
    return out


# ---------------------------------------------------------------------
# Shared helpers.
# ---------------------------------------------------------------------
def _project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tank_name(viewer):
    s = getattr(viewer, '_active_set', None)
    if s is not None and getattr(s, 'source_tank_name', None):
        return s.source_tank_name
    return 'unknown_tank'


def _tepy_version():
    return __import__('tankExporterPy', fromlist=['__version__']).__version__


# ---------------------------------------------------------------------
# 1-Turn auto-test.
# ---------------------------------------------------------------------
def on_turn_test_clicked(viewer):
    """Toggle the 1-Turn auto-test recorder.  Cancel-mid-test if
    already active.  Pre-flight requires Susp + Terrain + a tank
    loaded.  See the original Viewer docstring (history)."""
    if viewer._turn_test_active:
        turn_test_finalize(viewer, write_json=False, reason='cancelled')
        return
    if viewer.tank_physics is None or not viewer.meshes:
        viewer.log("turn-test: load a tank first.", color=(255, 160, 160))
        return
    if not viewer.tank_physics_enabled:
        viewer.log("turn-test: enable Susp first.", color=(255, 160, 160))
        return
    if not (viewer.show_terrain and viewer.terrain):
        viewer.log("turn-test: enable Terrain first.", color=(255, 160, 160))
        return

    viewer._turn_test_prev_speed_step  = int(getattr(viewer, '_speed_step', 0))
    viewer._turn_test_prev_auto_circle = bool(getattr(viewer, '_auto_circle', False))

    tp = viewer.tank_physics
    tp.pos[0]    = 0.0
    tp.pos[2]    = 0.0
    tp.yaw_deg   = 0.0
    tp.pitch_deg = 0.0
    tp.roll_deg  = 0.0
    tp.vy        = 0.0
    tp._render_pos_y     = float(tp.pos[1])
    tp._render_pitch_deg = 0.0
    tp._render_roll_deg  = 0.0
    tp._render_vy        = 0.0
    tp.omega_pitch_dps   = 0.0
    tp.omega_roll_dps    = 0.0
    tp.cur_forward_mps   = 0.0
    tp._smoothed_a_lat_mps2  = 0.0
    tp._smoothed_a_long_mps2 = 0.0

    viewer._set_speed_step(1)
    viewer._auto_circle = True

    viewer._turn_test_frames        = []
    viewer._turn_test_t_accum_s     = 0.0
    viewer._turn_test_start_yaw_deg = float(tp.yaw_deg)
    viewer._turn_test_last_yaw_deg  = viewer._turn_test_start_yaw_deg
    viewer._turn_test_total_yaw_deg = 0.0
    viewer._turn_test_tail_t        = None
    viewer._turn_test_prev_states   = None
    viewer._turn_test_active        = True
    if viewer._turn_test_btn is not None:
        viewer._turn_test_btn.active = True

    viewer.log_clear(status='1-Turn Test')
    viewer.log(
        f"turn-test: started.  speed=1, auto-circle ON, "
        f"R={viewer._auto_circle_radius:.2f} m, "
        f"top_kph={viewer._top_speed_kph:.1f}.  "
        f"Will auto-stop after 360 deg.",
        color=(180, 220, 255))


def turn_test_capture_frame(viewer, dt):
    """Per-frame 1-Turn capture.  Accumulates yaw delta, builds a
    snapshot via `build_recorder_frame`, checks the 360-deg
    completion gate."""
    if not viewer._turn_test_active or viewer.tank_physics is None:
        return
    tp = viewer.tank_physics

    cur_yaw = float(tp.yaw_deg)
    d_yaw   = (cur_yaw - viewer._turn_test_last_yaw_deg + 540.0) % 360.0 - 180.0
    viewer._turn_test_total_yaw_deg += d_yaw
    viewer._turn_test_last_yaw_deg   = cur_yaw

    viewer._turn_test_t_accum_s += float(dt)

    frame = build_recorder_frame(
        viewer,
        dt          = dt,
        t_accum_s   = viewer._turn_test_t_accum_s,
        frame_idx   = len(viewer._turn_test_frames),
        prev_states = getattr(viewer, '_turn_test_prev_states', None),
    )
    frame['yaw_total_deg'] = float(viewer._turn_test_total_yaw_deg)
    viewer._turn_test_frames.append(frame)
    if hasattr(tp, 'last_wheel_state'):
        viewer._turn_test_prev_states = list(tp.last_wheel_state)

    BRAKE_TAIL_SECONDS = 2.0
    if abs(viewer._turn_test_total_yaw_deg) >= 360.0:
        tail = getattr(viewer, '_turn_test_tail_t', None)
        if tail is None:
            viewer._turn_test_tail_t = float(viewer._turn_test_t_accum_s)
            viewer._auto_circle = bool(viewer._turn_test_prev_auto_circle)
            try:
                viewer._set_speed_step(0)
            except Exception:
                viewer._speed_step = 0
        elif viewer._turn_test_t_accum_s - tail >= BRAKE_TAIL_SECONDS:
            viewer._turn_test_tail_t = None
            turn_test_finalize(viewer, write_json=True, reason='complete')


def turn_test_finalize(viewer, write_json=True, reason='complete'):
    """Stop the 1-Turn test, restore prior speed / auto-circle
    state, and (when `write_json`) write the JSON file."""
    viewer._auto_circle = bool(viewer._turn_test_prev_auto_circle)
    try:
        viewer._set_speed_step(0)
    except Exception:
        viewer._speed_step = 0
    viewer._current_forward = 0.0

    n_frames = len(viewer._turn_test_frames)
    out_path = None
    if write_json and n_frames > 0:
        try:
            out_path = turn_test_save_json(viewer, reason=reason)
        except Exception as exc:
            viewer.log(f"turn-test: save failed: {exc}",
                       color=(255, 160, 160))
            traceback.print_exc()

    viewer._turn_test_active        = False
    viewer._turn_test_frames        = []
    viewer._turn_test_total_yaw_deg = 0.0
    viewer._turn_test_prev_states   = None
    if viewer._turn_test_btn is not None:
        viewer._turn_test_btn.active = False

    if out_path:
        viewer.log(f"turn-test: {reason} ({n_frames} frames) -> "
                   f"{out_path}", color=(180, 220, 255))
    else:
        viewer.log(f"turn-test: {reason} ({n_frames} frames)",
                   color=(180, 220, 255))


def turn_test_save_json(viewer, reason='complete'):
    """Write the captured 1-Turn run to
    `test_runs/turn_test_<tank>_<ts>.json`."""
    out_dir = os.path.join(_project_root(), 'test_runs')
    os.makedirs(out_dir, exist_ok=True)
    tank_name = _tank_name(viewer)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = os.path.join(out_dir,
                             f'turn_test_{tank_name}_{ts}.json')

    tp = viewer.tank_physics
    meta = {
        'schema_version':         1,
        'kind':                   'tank_turn_test',
        'reason':                 reason,
        'tank':                   tank_name,
        'timestamp':              datetime.now().isoformat(timespec='seconds'),
        'tepy_version':           _tepy_version(),
        'speed_step':             1,
        'top_speed_kph':          float(getattr(viewer, '_top_speed_kph', 50.0)),
        'auto_circle_radius_m':   float(getattr(viewer, '_auto_circle_radius', 5.0)),
        'gravity':                float(tp.gravity),
        'radius_m':               float(tp.radius),
        'min_offset_m':           float(tp.min_offset),
        'max_offset_m':           float(tp.max_offset),
        'track_thickness_m':      float(tp.track_thickness),
        'mass_kg':                float(tp.mass_kg),
        'com_local':              [float(c) for c in tp.com_local],
        'lateral_lean_gain':      float(getattr(tp, 'lateral_lean_gain', 0.0)),
        'longitudinal_lean_gain': float(getattr(tp, 'longitudinal_lean_gain', 0.0)),
        'comp_cap_m':             float(-tp.min_offset),
        'ext_cap_m':              float(-tp.max_offset),
        'envelope_total_m':       float(tp.max_offset - tp.min_offset),
        'n_wheels':               int(len(tp.wheels)),
        'n_left':                 int(tp.n_left),
        'n_right':                int(tp.n_right),
        'wheel_bone_names':       list(tp.wheel_bone_names),
        'wheel_local_positions':  [
            [float(w[0]), float(w[1]), float(w[2])] for w in tp.wheels],
        'state_code_legend':      {
            '0': 'NONE', '1': 'CONTACT', '2': 'HANGING', '3': 'OVER_COMP'},
        'frame_count':            int(len(viewer._turn_test_frames)),
        'duration_s':             float(viewer._turn_test_t_accum_s),
        'final_total_yaw_deg':    float(viewer._turn_test_total_yaw_deg),
        'sign_convention':        {
            'residual_y_positive_means': 'wheel rises into hull (compression)',
            'residual_y_negative_means': 'wheel droops below neutral (extension)',
        },
    }
    ci = getattr(viewer, '_pending_chassis_info', None) or {}
    if ci:
        # Per Coffee 2026-05-16 ("says nothing saved"): some
        # entries in `_pending_chassis_info` are tuples of
        # numpy arrays (e.g. `track_sag_L = (zs_array,
        # ys_array)`).  `json.dump` can't serialise numpy
        # arrays and the write truncated mid-meta -- the
        # recorder silently logged "nothing saved" because the
        # save exception was caught by
        # `manual_record_finalize`'s try/except.  Run the dict
        # through `_json_safe` first.
        meta['chassis_info_xml'] = _json_safe(ci)

    payload = {'meta': meta, 'frames': viewer._turn_test_frames}
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, indent=1)
    return out_path


# ---------------------------------------------------------------------
# F3 manual recorder.
# ---------------------------------------------------------------------
def on_manual_record_clicked(viewer):
    """Toggle the F3 manual recorder.  Pre-flight: tank loaded.
    On START, sweeps stale `manual_*.json` files in `test_runs/`
    and preserves only `_latest` / `_prev` for the current tank.
    """
    if viewer._manual_record_active:
        manual_record_finalize(viewer)
        return
    if viewer.tank_physics is None or not viewer.meshes:
        viewer.log("manual record: load a tank first.",
                   color=(255, 160, 160))
        return

    try:
        out_dir = os.path.join(_project_root(), 'test_runs')
        tank_name = _tank_name(viewer)
        keep = {
            f'manual_{tank_name}_latest.json',
            f'manual_{tank_name}_prev.json',
        }
        removed = 0
        if os.path.isdir(out_dir):
            for path in _glob.glob(os.path.join(out_dir, 'manual_*.json')):
                base = os.path.basename(path)
                if base in keep:
                    continue
                try:
                    os.remove(path)
                    removed += 1
                except Exception:
                    pass
        if removed:
            viewer.log(f"manual record: cleared {removed} stale "
                       f"recording(s).", color=(150, 180, 200))
    except Exception as _ex_sweep:
        print(f"[manual_record] sweep failed: {_ex_sweep}")

    viewer._manual_record_frames      = []
    viewer._manual_record_t_accum_s   = 0.0
    viewer._manual_record_prev_states = None
    viewer._manual_record_dt_min_ms   = float('inf')
    viewer._manual_record_dt_max_ms   = 0.0
    viewer._manual_record_active      = True
    viewer.log_clear(status='Manual Record (F3)')
    viewer.log("manual record: STARTED  -- press F3 again to stop "
               "and save.", color=(180, 220, 255))


def manual_record_capture_frame(viewer, dt):
    """Per-frame F3 snapshot.  Tracks dt extremes for the
    metadata jitter summary."""
    if not viewer._manual_record_active or viewer.tank_physics is None:
        return
    viewer._manual_record_t_accum_s += float(dt)
    dt_ms = float(dt) * 1000.0
    if dt_ms < viewer._manual_record_dt_min_ms:
        viewer._manual_record_dt_min_ms = dt_ms
    if dt_ms > viewer._manual_record_dt_max_ms:
        viewer._manual_record_dt_max_ms = dt_ms
    frame = build_recorder_frame(
        viewer,
        dt          = dt,
        t_accum_s   = viewer._manual_record_t_accum_s,
        frame_idx   = len(viewer._manual_record_frames),
        prev_states = viewer._manual_record_prev_states,
    )
    viewer._manual_record_frames.append(frame)
    tp = viewer.tank_physics
    if hasattr(tp, 'last_wheel_state'):
        viewer._manual_record_prev_states = list(tp.last_wheel_state)


def manual_record_finalize(viewer):
    """Stop the F3 recorder, save JSON, reset state."""
    n_frames = len(viewer._manual_record_frames)
    out_path = None
    if n_frames > 0:
        try:
            out_path = manual_record_save_json(viewer)
        except Exception as exc:
            viewer.log(f"manual record: save failed: {exc}",
                       color=(255, 160, 160))
            traceback.print_exc()

    dt_lo = viewer._manual_record_dt_min_ms
    dt_hi = viewer._manual_record_dt_max_ms
    if dt_lo == float('inf'):
        dt_summary = ''
    else:
        dt_summary = (f"  dt range {dt_lo:.1f}..{dt_hi:.1f} ms "
                      f"(ratio {dt_hi / max(dt_lo, 0.01):.1f}x)")

    viewer._manual_record_active      = False
    viewer._manual_record_frames      = []
    viewer._manual_record_t_accum_s   = 0.0
    viewer._manual_record_prev_states = None
    viewer._manual_record_dt_min_ms   = float('inf')
    viewer._manual_record_dt_max_ms   = 0.0

    if out_path:
        viewer.log(f"manual record: STOPPED ({n_frames} frames) "
                   f"-> {out_path}{dt_summary}",
                   color=(180, 220, 255))
    else:
        viewer.log(f"manual record: STOPPED ({n_frames} frames, "
                   f"nothing saved){dt_summary}",
                   color=(180, 220, 255))


def manual_record_save_json(viewer):
    """Write the F3 recording to a STABLE filename:
    `test_runs/manual_<tank>_latest.json`.  Rolls the previous
    `_latest` to `_prev` first."""
    out_dir = os.path.join(_project_root(), 'test_runs')
    os.makedirs(out_dir, exist_ok=True)
    tank_name = _tank_name(viewer)
    out_path  = os.path.join(out_dir,
                              f'manual_{tank_name}_latest.json')
    prev_path = os.path.join(out_dir,
                              f'manual_{tank_name}_prev.json')
    if os.path.exists(out_path):
        try:
            if os.path.exists(prev_path):
                os.remove(prev_path)
            shutil.move(out_path, prev_path)
        except Exception as _ex_roll:
            print(f"[manual_record] couldn't rotate prev backup: {_ex_roll}")

    tp = viewer.tank_physics
    meta = {
        'schema_version':              1,
        'kind':                        'tank_manual_record',
        'tank':                        tank_name,
        'timestamp':                   datetime.now().isoformat(timespec='seconds'),
        'tepy_version':                _tepy_version(),
        'gravity':                     float(tp.gravity),
        'radius_m':                    float(tp.radius),
        'min_offset_m':                float(tp.min_offset),
        'max_offset_m':                float(tp.max_offset),
        'track_thickness_m':           float(tp.track_thickness),
        'mass_kg':                     float(tp.mass_kg),
        'com_local':                   [float(c) for c in tp.com_local],
        'lateral_lean_gain':           float(getattr(tp, 'lateral_lean_gain', 0.0)),
        'longitudinal_lean_gain':      float(getattr(tp, 'longitudinal_lean_gain', 0.0)),
        'comp_cap_m':                  float(-tp.min_offset),
        'ext_cap_m':                   float(-tp.max_offset),
        'envelope_total_m':            float(tp.max_offset - tp.min_offset),
        'classifier_hyst_m':           0.020,
        'integrator_omegan_pitch_rad_s': 9.0,
        'integrator_omegan_roll_rad_s':  11.0,
        'integrator_omegan_y_rad_s':     14.0,
        'integrator_zeta':             1.0,
        'integrator_mass_ref_kg':      30000.0,
        'n_wheels':                    int(len(tp.wheels)),
        'n_left':                      int(tp.n_left),
        'n_right':                     int(tp.n_right),
        'wheel_bone_names':            list(tp.wheel_bone_names),
        'wheel_local_positions':       [
            [float(w[0]), float(w[1]), float(w[2])] for w in tp.wheels],
        'state_code_legend':           {
            '0': 'NONE', '1': 'CONTACT', '2': 'HANGING', '3': 'OVER_COMP'},
        'frame_count':                 int(len(viewer._manual_record_frames)),
        'duration_s':                  float(viewer._manual_record_t_accum_s),
        'dt_min_ms':                   (float(viewer._manual_record_dt_min_ms)
                                        if viewer._manual_record_dt_min_ms != float('inf')
                                        else 0.0),
        'dt_max_ms':                   float(viewer._manual_record_dt_max_ms),
        'tank_physics_enabled':        bool(getattr(viewer, 'tank_physics_enabled', False)),
        'show_terrain':                bool(getattr(viewer, 'show_terrain', False)),
        'show_track_spline':           bool(getattr(viewer, '_show_track_spline', False)),
        'sign_convention':             {
            'residual_y_positive_means': 'wheel rises into hull (compression)',
            'residual_y_negative_means': 'wheel droops below neutral (extension)',
            'pitch_render_positive_means': 'nose up',
            'roll_render_positive_means':  'left side up (tank leans right)',
            'delta_y_positive_means': 'wheel must compress (rise) to reach terrain',
            'delta_y_negative_means': 'wheel must extend (drop) to reach terrain',
            'hyst_dist_to_flip_m_positive_means': 'safely inside current state band',
            'hyst_dist_to_flip_m_negative_means': 'past hysteresis edge; will flip next frame',
        },
    }
    ci = getattr(viewer, '_pending_chassis_info', None) or {}
    if ci:
        # Per Coffee 2026-05-16 ("says nothing saved"): some
        # entries in `_pending_chassis_info` are tuples of
        # numpy arrays (e.g. `track_sag_L = (zs_array,
        # ys_array)`).  `json.dump` can't serialise numpy
        # arrays and the write truncated mid-meta -- the
        # recorder silently logged "nothing saved" because the
        # save exception was caught by
        # `manual_record_finalize`'s try/except.  Run the dict
        # through `_json_safe` first.
        meta['chassis_info_xml'] = _json_safe(ci)

    payload = {'meta': meta, 'frames': viewer._manual_record_frames}
    with open(out_path, 'w', encoding='utf-8') as fh:
        # `default=_json_safe` is the safety net: anything that
        # slipped past the meta+frame builders (= a stray numpy
        # array deep in `chassis_info_xml` or a future field)
        # gets converted on the fly instead of truncating the
        # write.
        json.dump(payload, fh, indent=1, default=_json_safe)
    return out_path
