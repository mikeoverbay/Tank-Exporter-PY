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
    """Build one frame's recorder dict.

    Per Coffee 2026-05-16 ("save only bare min info we need.
    just tag sections with the wheel ids and be done"): the F3
    recorder is now scoped to the arc-pad-to-wheel rotation
    investigation only.  Everything outside the chain-focus
    payload (chassis pose, integrator state, per-wheel
    suspension classifier, frame timers, render-vs-target
    deltas, ...) has been dropped from the per-frame record --
    the v1.230.5 frame carried 40+ physics fields all unused
    by the chain analysis.

    Schema (v2):
        frame      -- monotonic frame index
        t_s        -- recording-relative time
        dt_s       -- frame delta
        speed_mps  -- tank linear speed (drive context only)
        chain_focus
            'L' / 'R':
              s_offset_m  -- per-side chain arc-length flow
              wheels: list, one per road wheel, ordered as
                      `chassis_info_xml.wheel_roles.road_wheels_<side>`
                {
                  wheel_name      -- bare bone tag, e.g. `W_L1`
                  wheel_angle_rad -- accumulated spin (post-fix)
                  pads: [
                    {idx, angle_rad, on_arc},
                    ...
                  ]
                }

    `prev_states` is retained in the signature so the F3
    caller doesn't change, but is no longer consulted (state
    classifier was the v1 dataset, not v2).

    Reads (viewer):  tank_physics, _frame_timers_complete (unused)
    Writes:          nothing.
    """
    tp = viewer.tank_physics
    return {
        'frame':       int(frame_idx),
        't_s':         float(t_accum_s),
        'dt_s':        float(dt),
        'speed_mps':   float(getattr(tp, '_last_speed_mps', 0.0)),
        'chain_focus': _build_chain_focus_safe(viewer),
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


def _round_floats(obj, places=4):
    """Recursively round every float in `obj` to `places` decimal
    digits.  Per Coffee 2026-05-16 ("chop off the numbers..
    truncate 4 places"): F3 payloads were carrying 17-digit
    float reprs (`-0.9566489999999999`) when 4 is plenty for the
    chain-pad-vs-wheel-rotation analysis -- and the extra
    precision blew up file size in indented JSON.

    Touches floats only.  Booleans, ints, strings, None pass
    through unchanged (`bool` would otherwise sneak through
    `isinstance(x, float)` on some platforms via the int->float
    coercion path, hence the explicit bool guard).
    """
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, float):
        return round(obj, places)
    if isinstance(obj, dict):
        return {k: _round_floats(v, places) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats(v, places) for v in obj]
    return obj


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
    """Bare-minimum per-frame chain-pad-vs-wheel-rotation payload.

    Per Coffee 2026-05-16 ("save only bare min info we need.
    just tag sections with the wheel ids and be done").

    Schema:
        {'L': {'s_offset_m': float,
               'wheels': [{wheel_name, wheel_angle_rad,
                            contact_angle_rad, pads:
                            [{idx, angle_rad, on_arc}, ...]},
                          ...]},
         'R': {... same shape ...}}

    * `wheel_name` is the bare bone tag from
      `chassis_info_xml.wheel_roles.road_wheels_<side>`
      (e.g. `W_L1`); meta carries `wheel_radii` so the analyser
      can look up R when needed.
    * `wheel_angle_rad` is `tp.wheel_angles_rad` with the v1.231
      three-form name-lookup fix -- bare, `+'_BlendBone'`, then
      suffix-stripped -- so the spin actually reaches the JSON.
    * `pads` lists every pad whose centre sits within
      `1.5 * (R + segmentsInnerThickness)` of the hub in YZ:
        - `idx`       chain-pad index (matches across frames so
                       you can track a single pad over time)
        - `angle_rad` `atan2(pad.y - hub.y, pad.z - hub.z)` in
                       chassis-renderer YZ.  Same convention as
                       homie's `_angle_from_hub` -- adjacent
                       frames' delta IS the pad's true angular
                       travel around this wheel.
        - `on_arc`    True only when the renderer's per-pad
                       anchor stash names THIS wheel (within
                       1 mm).  Line pads in the band come out
                       False -- their `angle_rad` is geometric
                       atan2 only.
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
    # Per Coffee 2026-05-16 ("F3 wheel_angle_rad reports 0.0 for
    # every wheel"): the v1.224.0 lookup did a single
    # `names_road.index(wheel_name)` -- but `wheel_name` comes
    # in BARE from `roles['road_wheels_<side>']` (e.g. `W_L1`),
    # while `tp.wheel_bone_names` carries the `_BlendBone`-
    # suffixed form on most tanks (e.g. `W_L1_BlendBone`).  Every
    # lookup raised `ValueError`, ri stayed -1, and `spin`
    # defaulted to 0.0.  The wheel physics is fine (verified
    # 2026-05-16 via direct `advance_wheel_angles` call); this
    # is purely a recorder lookup bug.
    #
    # Fix: try the name as given, then `+'_BlendBone'`, then
    # strip a trailing `_BlendBone` and retry.  Covers every
    # tank naming variant we've encountered so far.
    names_road = list(getattr(tp, 'wheel_bone_names', []))
    wheel_angles = getattr(tp, 'wheel_angles_rad', None)

    def _spin_for(name):
        if not name:
            return 0.0
        candidates = [name, name + '_BlendBone']
        if name.endswith('_BlendBone'):
            candidates.append(name[:-len('_BlendBone')])
        for cand in candidates:
            try:
                ri = names_road.index(cand)
            except ValueError:
                continue
            if (wheel_angles is not None
                    and 0 <= ri < len(wheel_angles)):
                return float(wheel_angles[ri])
            return 0.0
        return 0.0

    # Per Coffee 2026-05-16 ("we need to get that angle to
    # contact in relation to the chassis angle"): viewer
    # stashed `_last_chassis_contact_angle_rad` at the end of
    # `_compute_homie_chain_for_frame` -- the chassis-relative
    # atan2 of world-down in YZ.  At pitch=0 this is -pi/2;
    # nose-up pitch shifts it positive.  Same value for every
    # wheel on the chassis at this point (per-wheel terrain-
    # slope refinement is a follow-up).
    contact_angle_chassis = float(getattr(
        viewer, '_last_chassis_contact_angle_rad', -math.pi * 0.5))
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
        hubs_stash = getattr(viewer, f'_homie_hubs_{side_token}',
                              None)
        onarc_stash = getattr(viewer, f'_homie_onarc_{side_token}',
                                None)
        if (hubs_stash is not None
                and len(hubs_stash) != len(pos)):
            hubs_stash = None
        if (onarc_stash is not None
                and len(onarc_stash) != len(pos)):
            onarc_stash = None
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
            rng = 1.5 * (R + inner_t)
            pads = []
            for i, p in enumerate(pos):
                dy = float(p[1]) - hub_y
                dz = float(p[2]) - hub_z
                if dy * dy + dz * dz > rng * rng:
                    continue
                on_this_arc = False
                if (hubs_stash is not None
                        and onarc_stash is not None
                        and bool(onarc_stash[i])):
                    ph = hubs_stash[i]
                    if (abs(float(ph[1]) - hub_y) < 1e-3
                            and abs(float(ph[2]) - hub_z) < 1e-3):
                        on_this_arc = True
                pads.append({
                    'idx':       int(i),
                    'angle_rad': float(math.atan2(dy, dz)),
                    'on_arc':    bool(on_this_arc),
                })
            per_wheel.append({
                'wheel_name':       wheel_name,
                'wheel_angle_rad':  _spin_for(wheel_name),
                'contact_angle_rad': contact_angle_chassis,
                'pads':             pads,
            })
        if per_wheel:
            arcs = list(getattr(
                viewer, f'_last_chain_arcs_{side_token}', None) or ())
            chords = list(getattr(
                viewer, f'_last_ground_chords_{side_token}',
                None) or ())
            out[side_token] = {
                's_offset_m':    s_offset,
                'wheels':        per_wheel,
                'chain_arcs':    arcs,
                'ground_chords': chords,
            }
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


def _run_plotters(viewer, json_path):
    """Per Coffee 2026-05-17 ("can you have tepy run the apps
    to draw the images when we F3 a frame?"): on every
    successful F3 save, fire the two diagnostic plotters
    against the recording.  Spawned as a non-blocking child
    process so the viewer keeps running; output PNGs land in
    `test_runs/plots/`.
    """
    import subprocess
    import sys
    root = _project_root()
    plotters = (
        os.path.join(root, 'cust_tools',
                      'plot_chain_arc_directions.py'),
        os.path.join(root, 'cust_tools',
                      'plot_chain_ground_chords.py'),
    )
    launched = 0
    for script in plotters:
        if not os.path.isfile(script):
            continue
        try:
            subprocess.Popen(
                [sys.executable, script, json_path, '--frame', '0'],
                cwd=root,
                creationflags=(getattr(subprocess,
                                         'CREATE_NO_WINDOW', 0)
                                if os.name == 'nt' else 0),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            launched += 1
        except Exception as _ex:
            print(f'[manual_record] plotter spawn failed: '
                  f'{os.path.basename(script)}: {_ex}')
    if launched:
        viewer.log(f"manual record: spawned {launched} plotter(s) "
                   f"-> test_runs/plots/",
                   color=(180, 220, 255))


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
        try:
            _run_plotters(viewer, out_path)
        except Exception as _ex_plot:
            viewer.log(
                f"manual record: plotter launch failed: "
                f"{_ex_plot}",
                color=(255, 160, 160))
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
    ci = getattr(viewer, '_pending_chassis_info', None) or {}
    roles_all = ci.get('wheel_roles') or {}
    radii_all = ci.get('wheel_radii') or {}
    # Only retain the road-wheel radii -- the chain-focus payload
    # is scoped to road wheels, so sprockets / idlers / rollers
    # don't need to ride along in meta.
    road_names = set(roles_all.get('road_wheels_L', []))
    road_names.update(roles_all.get('road_wheels_R', []))
    road_radii = {nm: float(radii_all[nm])
                  for nm in road_names if nm in radii_all}
    meta = {
        'schema_version':           2,
        'kind':                     'tank_manual_record',
        'tank':                     tank_name,
        'timestamp':                datetime.now().isoformat(timespec='seconds'),
        'tepy_version':             _tepy_version(),
        'frame_count':              int(len(viewer._manual_record_frames)),
        'duration_s':               float(viewer._manual_record_t_accum_s),
        'segmentsInnerThickness':   float(ci.get('segmentsInnerThickness', 0.0) or 0.0),
        'segmentLength':            float(ci.get('segmentLength', 0.0) or 0.0),
        'segmentsCount':            int(ci.get('segmentsCount', 0) or 0),
        'road_wheels_L':            list(roles_all.get('road_wheels_L', [])),
        'road_wheels_R':            list(roles_all.get('road_wheels_R', [])),
        'wheel_radii':              road_radii,
    }

    payload = {'meta': meta, 'frames': viewer._manual_record_frames}
    payload = _round_floats(payload, 4)
    with open(out_path, 'w', encoding='utf-8') as fh:
        # `default=_json_safe` is the safety net: anything that
        # slipped past the meta+frame builders (= a stray numpy
        # array deep in `chassis_info_xml` or a future field)
        # gets converted on the fly instead of truncating the
        # write.
        json.dump(payload, fh, indent=1, default=_json_safe)
    return out_path
