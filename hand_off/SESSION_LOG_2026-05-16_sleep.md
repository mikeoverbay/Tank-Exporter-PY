# Sleep-session investigation log (2026-05-16)

User went to sleep with instruction: "study... try tests.. plot
simple outlines you can see.. keep trying.. move along,.. sample
XZ plot.. what ever.. keep looking for a fix.. dont stop until I
stop you our you find the answer."

## What I investigated

The user's recurring symptoms:
1. "Tracks cant spring if the drive wheel is stopped" -> dropped
   ACCEL/DECEL ramp (v1.219.0).
2. "Ground wheel push pads in Y AND Z, should only snap Y" ->
   Y-only push (v1.220.0).
3. "Segments locked to wheel, slide with wheel Z arc" -> v1.220
   was circle projection, pads rode the wheel rim.  Tried flat
   tangent (v1.221.0), reverted.
4. "Angle is backwards on ground wheels when pressed up.. Z seems
   flipped.. chasing wrong direction" -> direction-locked push
   v1.221.2 (road = always DOWN, roller = always UP).
5. "Travel the chain and watch all pad points.. like it is
   rolling over an edge" -> the v1.220 / v1.221.2 push used the
   FULL Y correction magnitude (target_dY - dY) which exceeded
   the actual penetration depth.  Replaced with `pen`-scaled
   push (v1.221.3) -- proportional response, smooth gradient.
6. "2 at pads at center.. see it?  spline looks better.. pads
   not so much" -> the focus of the sleep session.

## Diagnostic tools built

* `cust_tools/diag_pbd_pileup.py`
    Loads a real tank, runs production homie + PBD pipeline,
    perturbs one road wheel up or down, walks through all PBD
    relaxation iterations capturing pad position snapshots.
    Measures min adjacent pad-to-pad distance per iteration and
    flags pile-up pairs (distance < seg_len/2).  Plots overview +
    zoom around the perturbed wheel.

* `cust_tools/diag_pad_mesh_overlap.py`
    Same load + PBD setup but instead of measuring centre
    distances, builds each pad as an oriented rectangle (length
    = seg_len * 0.95, width 3cm) and SAT-tests for rectangle
    intersection between adjacent pads.  Renders pads with the
    `pos - hub` orientation override (production-style) or with
    plain chord tangent (`--use-override` flag).

## Findings

### Finding 1 (FIXED in v1.221.4): PBD wheel radii mismatch

The homie chain wraps each wheel at `R + segmentsInnerThickness`
(since v1.215.0).  But `viewer._step_chain_pbd` was passing
BARE `rs` to `TrackChainPBD`.  For T110E4 (`inner_t = 0.0615`),
PBD's `seed_from_homie` measures `slack = dist - R = 0.0615`
which is >> `BIND_TOL = 0.003`, so ZERO PADS bound.

The chain became entirely free -- gravity slowly sagged it
under the constraints until pads reached the bare-R circle,
at which point the wheel push fired asymmetrically.

**Fix**: pass `R + inner_t` to PBD too.  T110E4 bound count
went from 0/80 to 11/80.  T30 unaffected (`inner_t = 0`).

### Finding 2 (FIXED in v1.222.0): Two-piece pad doubling

Two-piece pads (T30, T92, T110E4) carry distinct
`segmentOffset` and `segment2Offset` in the chassis XML.
v1.215.0 dropped the runtime per-renderer pivot shift, but
that only correctly handles the SHARED outward offset; it
doesn't compensate for the DIFFERENCE between the two
offsets.

Without the per-variant shift, BOTH `segmentModelLeft` and
`segment2ModelLeft` rendered with their mesh-local origin at
the same chain anchor.  Since both meshes are pad pieces of
similar size, the bboxes overlapped -> "2 pads at center".

**Fix**: apply `+(seg_offset - seg2_offset) * z_axis` shift
ONLY to `segment2*` renderers (segment* and single-piece
tanks like Pudel keep v1.215.0 placement).  This aligns the
two hinge pins in world space so the pair forms one rigid
shoe.

### Finding 3 (not fixed): pad-mesh-rectangle-vs-chord geometry

After v1.221.4 / v1.222.0, my rectangle-overlap diagnostic
still shows 2-5 overlapping pad pairs near sharp chain bends
on a perturbed wheel.  The pad centres ARE seg_len apart but
the pad MESH rectangles have length close to seg_len, so any
non-zero chain curvature causes overlap (chord between centres
is shorter than arc).

This is a fundamental geometry constraint -- a chain of rigid
rectangles can only follow curves with bend angle <=
`2 * asin(W / 2L)` where W is pad width and L is pad length.
Tighter bends physically can't be made by rectangles.

**Not fixed.**  The real solution would be either:
1. Pad the chain spline with extra control points so the curve
   is gentler (more pads per arc -> smaller per-pad bend angle).
2. Shorten the pad rectangle render length (e.g., to 0.7 *
   seg_len, leaving visible hinges).
3. Render pads as JOINT-CHAIN (pad i = mesh from chain[i] to
   chain[i+1], not a rigid rectangle at chain[i]).
None are quick to land safely without risk.

## Open questions for the user

* Does v1.221.4 fix the chain drift on T110E4 / T92?  My
  diagnostic shows 0->11 bound pads but you'd need to run the
  viewer to verify the visual chain stays anchored.

* Does v1.222.0 fix the "2 pads at center"?  I gated the shift
  to `segment2*` renderers ONLY so single-piece pads (Pudel)
  are untouched, but I haven't verified that segment2's
  segment2Offset interpretation matches production reality.
  T30 segment2 shifts forward by 0.055 m; T92 segment2 shifts
  back by 0.027 m.

* The pad-rectangle-vs-curve geometry overlap is real but
  minor (a few cm at most).  Probably visible only at wheels
  with very deep suspension travel; should be acceptable.

## Versions shipped this sleep session

* 1.221.3 -- penetration-scaled push for road / roller
  (replaces full Y-correction magnitude).
* 1.221.4 -- PBD wheel_radii inflated by `inner_thickness`.
* 1.222.0 -- two-piece pad segment2 hinge alignment.
