# Tank Exporter PY -- res_mods `<segmentsInnerThickness>` patches

**Generated 2026-05-24.**

This directory is a duplicate copy of res_mods chassis patches that
fill in missing `<segmentsInnerThickness>` values on 18 tanks
identified by `cust_tools/sweep_inner_thickness.py` (see
`hand_off/TRACK_DATA_AUDIT_2026-05-24.md`, section "Sweep").

## What the patches do

Each file is a FULL-FILE vehicle XML override that mirrors the original
`scripts/item_defs/vehicles/<nation>/<basename>.xml` from the WoT
install's `scripts.pkg`, with `<segmentsInnerThickness>` filled in for
every `<chassis>/<variant>` (in both `physicalTracks/left|right` and
`splineDesc/.../left|right` as appropriate).  Repair values are
sourced per tank from sibling-tank medians; see the per-variant table
in `hand_off/TRACK_DATA_AUDIT_2026-05-24.md` under
"Repair: res_mods chassis patches for missing
`<segmentsInnerThickness>` (2026-05-24)".

Without these patches, TEPY's chassis loader leaves
`segmentsInnerThickness = None` (default 0.0) on the 18 affected tanks
-- which puts the track chain at the bare wheel rim instead of the pad
pitch circle, producing a slow teeth-vs-pads drift.  The fix lets
every chassis load and render its chain correctly.

## Override mode

**Full-file vehicle-XML override**, NOT a partial / merge override.
WoT engine + TEPY's `PkgExtractor` both look up vehicle XMLs at
`scripts/item_defs/vehicles/<nation>/<basename>.xml`, and a file at
that exact path under `res_mods/<version>/` wins over the pkg copy.
The user already had two full-file overrides of this kind in place
(`usa/A14_T30.xml`, `usa/A83_T110E4.xml` at the vehicle level).  No
partial-override / `<xmlref>` inclusion syntax is exposed by either
WoT or TEPY for these files.

## Files

```
scripts/item_defs/vehicles/france/F20_RenaultBS.xml
scripts/item_defs/vehicles/france/F30_RenaultFT_AC.xml
scripts/item_defs/vehicles/france/F77_FCM_2C.xml
scripts/item_defs/vehicles/germany/Env_Artillery.xml
scripts/item_defs/vehicles/germany/G139_MKA.xml
scripts/item_defs/vehicles/poland/Pl26_Czolg_P_Wz_46.xml
scripts/item_defs/vehicles/uk/GB57_Alecto.xml
scripts/item_defs/vehicles/uk/GB146_Gabler_s_Destroyer.xml
scripts/item_defs/vehicles/usa/A24_T2_med.xml
scripts/item_defs/vehicles/usa/A139_M_III_Y.xml
scripts/item_defs/vehicles/usa/A141_M_IV_Y.xml
scripts/item_defs/vehicles/usa/A14_T30.xml
scripts/item_defs/vehicles/usa/A14_T30_FL.xml
scripts/item_defs/vehicles/ussr/R08_BT-2.xml
scripts/item_defs/vehicles/ussr/R84_Tetrarch_LL.xml
scripts/item_defs/vehicles/ussr/R101_MT25.xml
scripts/item_defs/vehicles/ussr/R99_T44_122.xml
scripts/item_defs/vehicles/ussr/R231_Buryan.xml
```

18 files, 26 chassis variants covered.

## How to apply / restore

The patches were already mirrored at generation time to:

```
C:\Games\World_of_Tanks_NA\res_mods\2.2.1.2\
```

So they are live in the user's WoT install already.  This directory
is a BACKUP copy for version control / restoration after a WG update
might wipe `res_mods/2.2.1.2/`.

To restore manually after a wipe, copy the `scripts/` tree from this
directory verbatim into:

```
C:\Games\World_of_Tanks_NA\res_mods\<active_version>\
```

(replace `<active_version>` with whatever version WG has rolled to;
the user's current is `2.2.1.2`).

To regenerate from current install state:

```
cd C:\experiment\experiment\relaxed-bartik-61760d
python cust_tools/repair_inner_thickness.py
```

The script reads `tankExporterPy.json` for paths.  Each run is
deterministic given the same `sweep_inner_thickness_all.tsv` -- the
sibling-derived medians don't change unless WG re-authors a sibling
tank.

## Limitations

* Sibling medians are computed from the full install at the time
  the audit was run.  If WG ships a new line that significantly
  shifts a nation's tier-1-to-5 (or tier-7-to-9) authored-value
  median, the inherited value here may drift from "right for the
  line".  Re-run `sweep_inner_thickness.py` after a major patch to
  refresh the inputs.

* These are vehicle-level patches.  Any further user customisation
  of the same vehicle XML (camo, decals, mod tweaks, etc.) WILL
  collide and must be merged manually.  These patches were written
  on top of the unmodified pkg originals.

* Two of the user's pre-existing files (`usa/A14_T30.xml`,
  `usa/A83_T110E4.xml`) were at the vehicle-level path but the
  patch generator only touched `A14_T30.xml` -- which is now
  OVERWRITTEN with the regenerated copy carrying the fixed
  `<segmentsInnerThickness>` value (+0.04 m).  The pre-existing
  `A83_T110E4.xml` is untouched (T110E4 was NOT on the missing
  list; it already carries +0.0615 m authored).  If the
  pre-existing T30 file had user-authored changes beyond what was
  inferable from the pkg original, those would have been lost --
  the user should diff the pre-existing version against this
  patch (`git diff` if the existing copy was version-controlled,
  or manual diff against a fresh pkg extraction) and merge back
  any custom tweaks.
