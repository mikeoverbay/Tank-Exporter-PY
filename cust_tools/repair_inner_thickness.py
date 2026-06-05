"""Generate res_mods full-file vehicle-XML patches that fill in the
missing `<segmentsInnerThickness>` value for the 18 tanks identified
by `sweep_inner_thickness.py`.

Strategy
--------
Per CLAUDE.md frozen-files list and the existing user pattern
(`res_mods/.../scripts/item_defs/vehicles/usa/A14_T30.xml` already in
place as a full plain-text override), WoT only supports FULL-FILE XML
overrides under `res_mods/<version>/scripts/...`.  WoT itself, plus
TEPY's `PkgExtractor`, look up vehicle XMLs at
`scripts/item_defs/vehicles/<nation>/<basename>.xml` -- so a file at
that exact path under the res_mods root wins over the pkg copy.

For each affected tank we:

  1. Extract the original vehicle XML from its pkg (decode BWXML if
     needed).
  2. Compute a sibling-derived repair value (per the task spec).
  3. For every `<chassis>/<variant>` child, find the
     `<segmentsInnerThickness>` element under
     `physicalTracks/.../trackPair/.../trackDebris/.../physicalParams/`
     (the audit's loader-targeted location).  If the element exists
     and is empty, fill its text.  If the element is missing, INSERT
     it as a sibling of `<segmentsOuterThickness>` (or, lacking that,
     at the end of `<physicalParams>`).  Both trackPair sides are
     patched per variant.
  4. Pretty-serialise the modified tree and write to
     - `<res_mods_root>/scripts/item_defs/vehicles/<nation>/<basename>.xml`
     - `<mirror_root>/scripts/item_defs/vehicles/<nation>/<basename>.xml`

Read-only for tankExporterPy/*.py.
"""

import json
import os
import re
import statistics
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tankExporterPy.loaders import PkgExtractor
from tankExporterPy.common import is_bwxml, decode_bwxml


# ---------------------------------------------------------------------------
# Affected tanks: (nation, basename, tier, vclass_hint).
# vclass_hint is used by the median-of-siblings sibling search below.
# ---------------------------------------------------------------------------
AFFECTED = [
    ('france',  'F20_RenaultBS',           2),
    ('france',  'F30_RenaultFT_AC',        2),
    ('france',  'F77_FCM_2C',              3),
    ('germany', 'Env_Artillery',           1),
    ('germany', 'G139_MKA',                2),
    ('poland',  'Pl26_Czolg_P_Wz_46',      9),
    ('uk',      'GB57_Alecto',             4),
    ('uk',      'GB146_Gabler_s_Destroyer', 5),
    ('usa',     'A24_T2_med',              2),
    ('usa',     'A139_M_III_Y',            8),
    ('usa',     'A141_M_IV_Y',             8),
    ('usa',     'A14_T30',                 9),
    ('usa',     'A14_T30_FL',              9),
    ('ussr',    'R08_BT-2',                2),
    ('ussr',    'R84_Tetrarch_LL',         2),
    ('ussr',    'R101_MT25',               6),
    ('ussr',    'R99_T44_122',             7),
    ('ussr',    'R231_Buryan',             9),
]

# Tank-specific repair values + reasoning.  Pre-computed from the audit's
# "Repair guidance" section + per-tank sibling lookups in
# sweep_inner_thickness_all.tsv (US tier-8/9 heavies, low-tier medians).
#
# Each entry: (value_m, source_reasoning).
EXPLICIT_REPAIR = {
    # T30 / T30_FL: US tier-9 TDs, sibling T29/T34/T34_B carry +0.035 - +0.050 m
    # per audit.  Use +0.040 m as the explicit target.
    ('usa', 'A14_T30'):     (0.040,
        'T30-family sibling median (T29, T34, T34_B all carry '
        '+0.035 to +0.050 m); audit recommendation +0.040 m'),
    ('usa', 'A14_T30_FL'):  (0.040,
        'T30 Frontline variant -- same chain geometry as A14_T30, '
        'matched to T29/T34 sibling median +0.040 m'),
}


# ---------------------------------------------------------------------------
def load_all_sweep():
    """Load sweep_inner_thickness_all.tsv into a list of dicts."""
    tsv = os.path.join(ROOT, 'hand_off', 'sweep_inner_thickness_all.tsv')
    rows = []
    with open(tsv, encoding='utf-8') as fh:
        header = fh.readline().rstrip('\n').split('\t')
        for line in fh:
            cols = line.rstrip('\n').split('\t')
            if len(cols) != len(header):
                continue
            rec = dict(zip(header, cols))
            rows.append(rec)
    return rows


def parsed_value(rec):
    """Return float value if 'yes', else None."""
    if rec.get('has_field') != 'yes':
        return None
    try:
        return float(rec.get('value', ''))
    except (ValueError, TypeError):
        return None


def derive_sibling_value(nation, tier, all_rows, tier_pred=None):
    """Return median of sibling-valued tanks in (nation, tier_pred(tier)).

    `tier_pred` accepts the row's tier and returns True if it should be
    counted as a sibling.  Default: same nation, tier within +-1.
    """
    if tier_pred is None:
        def tier_pred(t):
            try:
                return abs(int(t) - int(tier)) <= 1
            except (ValueError, TypeError):
                return False

    vals = []
    for rec in all_rows:
        if rec.get('nation') != nation:
            continue
        try:
            if not tier_pred(int(rec.get('tier'))):
                continue
        except (ValueError, TypeError):
            continue
        v = parsed_value(rec)
        if v is None:
            continue
        vals.append(v)
    if not vals:
        return None, 0, 'no siblings'
    return (float(statistics.median(vals)), len(vals),
            f'median of {len(vals)} {nation} siblings')


def compute_repair_value(nation, basename, tier, all_rows):
    """Return (value_m, source_str) for the named tank."""
    key = (nation, basename)
    if key in EXPLICIT_REPAIR:
        v, src = EXPLICIT_REPAIR[key]
        return v, src

    # US Yoh-line tier-8 heavies: cross-reference vs US tier 7-9 heavies.
    if key in (('usa', 'A139_M_III_Y'), ('usa', 'A141_M_IV_Y')):
        # Closest siblings are T32 / M103 / T34 / T29 -- pick US tier 7-9 medians.
        def pred(t):
            try:
                return 7 <= int(t) <= 9
            except (ValueError, TypeError):
                return False
        v, n, src = derive_sibling_value('usa', tier, all_rows, tier_pred=pred)
        if v is not None:
            return v, f'US tier 7-9 heavies/TDs: {src}'

    # Present-but-empty mid-to-high-tier cluster: same nation, tier +-1.
    if key in (
        ('ussr',    'R231_Buryan'),
        ('ussr',    'R99_T44_122'),
        ('poland',  'Pl26_Czolg_P_Wz_46'),
        ('germany', 'G139_MKA'),
        ('usa',     'A24_T2_med'),
    ):
        v, n, src = derive_sibling_value(nation, tier, all_rows)
        if v is not None:
            return v, src

    # Tag-absent pre-2014 low-tier cluster: same nation, tier 1-5 median.
    def low_tier_pred(t):
        try:
            return 1 <= int(t) <= 5
        except (ValueError, TypeError):
            return False
    v, n, src = derive_sibling_value(nation, tier, all_rows,
                                     tier_pred=low_tier_pred)
    if v is not None:
        # Per audit: if the median lands within +-5 mm of zero, use 0.0
        # as the empirically-defensible global default.
        if abs(v) <= 0.005:
            return 0.0, f'median of {n} {nation} tier 1-5 siblings = {v:+.5f} m '\
                        f'(<= 5 mm of 0; using global-default 0.0)'
        return v, f'median of {n} {nation} tier 1-5 siblings'

    # Last-resort fallback
    return 0.0, 'no clean sibling data; falling back to global default 0.0'


# ---------------------------------------------------------------------------
def _strip_xmlns_xmlref(xml_str):
    return re.sub(r'<xmlns:[^>]*>[^<]*</xmlns:[^>]*>', '', xml_str)


def load_vehicle_xml_string(pe, nation, basename):
    """Return the vehicle XML as a UTF-8 string with the BW xmlns ref
    stripped so ElementTree can parse it."""
    internal = f'scripts/item_defs/vehicles/{nation}/{basename}.xml'
    local = pe.extract(internal)
    if not local:
        raise FileNotFoundError(f'Could not extract {internal}')
    with open(local, 'rb') as fh:
        raw = fh.read()
    if is_bwxml(raw):
        xml_str = decode_bwxml(raw)
    else:
        xml_str = raw.decode('utf-8', errors='replace')
    return _strip_xmlns_xmlref(xml_str)


def _set_or_insert_thickness(parent, value_text):
    """Inside `parent`, ensure `<segmentsInnerThickness>` exists and has
    text `value_text`.  Empty / missing both qualify for fill.

    Returns True if a change was applied, False if the element was
    already present with a non-empty value (no-op).
    """
    it_el = parent.find('segmentsInnerThickness')
    if it_el is None:
        it_el = ET.SubElement(parent, 'segmentsInnerThickness')
        it_el.text = value_text
        # Try to re-order it next to segmentsOuterThickness for tidiness
        ot_el = parent.find('segmentsOuterThickness')
        if ot_el is not None:
            children = list(parent)
            children.remove(it_el)
            idx = children.index(ot_el)
            for c in children:
                parent.remove(c)
            children.insert(idx + 1, it_el)
            for c in children:
                parent.append(c)
        return True
    if not it_el.text or not it_el.text.strip():
        it_el.text = value_text
        return True
    # Already authored with a real value -- leave alone
    return False


def patch_inner_thickness(root, value_m):
    """Walk every chassis/variant and ensure `<segmentsInnerThickness>`
    is set in BOTH the present-but-empty path
    (`physicalTracks/left|right`, used by mid+ tier modern chassis) AND
    the tag-absent path -- where we insert it under `splineDesc/trackPair`
    so the loader's recursive `best_chassis.iter('segmentsInnerThickness')`
    will pick it up.

    Returns (variants_touched, leaf_blocks_touched).
    """
    chassis_el = root.find('chassis')
    if chassis_el is None:
        return 0, 0
    variants = 0
    leaves = 0
    value_text = f'{value_m:.5f}'
    for variant in list(chassis_el):
        variants_touched = False

        # Case A: present-but-empty cluster (T30, Yoh, Buryan, ...) --
        # the field already exists empty under each side.  Just fill.
        pt = variant.find('physicalTracks')
        if pt is not None:
            for side_tag in ('left', 'right'):
                side = pt.find(side_tag)
                if side is None:
                    continue
                if _set_or_insert_thickness(side, value_text):
                    leaves += 1
                    variants_touched = True

        # Case B: tag-absent cluster (pre-2014 low-tier tanks).  No
        # physicalTracks at all -- only splineDesc.  splineDesc comes
        # in two shapes: a `<trackPair>` wrapper child (most), or
        # direct `<left>`/`<right>` children (Env_Artillery, etc).
        # Insert the field on every left/right we find.
        sd = variant.find('splineDesc')
        if sd is not None:
            containers = list(sd.iter('trackPair'))
            if not containers:
                containers = [sd]
            for cont in containers:
                touched_in_cont = False
                for side_tag in ('left', 'right'):
                    side = cont.find(side_tag)
                    if side is None:
                        continue
                    if _set_or_insert_thickness(side, value_text):
                        leaves += 1
                        touched_in_cont = True
                if not touched_in_cont:
                    if _set_or_insert_thickness(cont, value_text):
                        leaves += 1
                        touched_in_cont = True
                if touched_in_cont:
                    variants_touched = True

        if variants_touched:
            variants += 1
    return variants, leaves


def serialize_xml(root):
    """Serialise an ElementTree with a leading XML declaration."""
    # ET.tostring with short_empty_elements=False makes empties like
    # <foo></foo> instead of <foo/>, matching the audit's "self-closing"
    # vs "explicit-empty" distinction. We do want short empties for
    # tidiness here; WoT engine accepts either.
    out = ET.tostring(root, encoding='utf-8', xml_declaration=True,
                      short_empty_elements=True)
    return out


# ---------------------------------------------------------------------------
def main():
    cfg_path = os.path.join(ROOT, 'tankExporterPy.json')
    with open(cfg_path, encoding='utf-8') as f:
        cfg = json.load(f)
    pkg_dir = cfg.get('pkg_dir', 'C:/Games/World_of_Tanks_NA/res/packages')
    res_mods_root = cfg.get('res_mods',
                            'C:/Games/World_of_Tanks_NA/res_mods/2.2.1.2')
    wot_root = os.path.normpath(os.path.join(pkg_dir, '..', '..'))
    lookup_xml = cfg.get('lookup_xml') or 'C:/experiment/TheItemList.xml'

    mirror_root = 'C:/experiment/tankExporterPy_resmods_patches'

    print(f'res_mods : {res_mods_root}')
    print(f'mirror   : {mirror_root}')

    pe = PkgExtractor(wot_root, pkg_dir=pkg_dir, lookup_xml=lookup_xml)
    all_rows = load_all_sweep()

    report = []
    for nation, basename, tier in AFFECTED:
        val, src = compute_repair_value(nation, basename, tier, all_rows)
        print(f'\n=== {nation}/{basename} (tier {tier}) ===')
        print(f'    value: {val:+.5f} m  ({src})')

        try:
            xml_str = load_vehicle_xml_string(pe, nation, basename)
        except Exception as exc:
            print(f'    EXTRACT FAILED: {exc}')
            report.append((nation, basename, tier, val, src,
                           f'EXTRACT_FAILED: {exc}', '', '', 0, 0))
            continue
        try:
            root = ET.fromstring(xml_str)
        except Exception as exc:
            print(f'    PARSE FAILED: {exc}')
            report.append((nation, basename, tier, val, src,
                           f'PARSE_FAILED: {exc}', '', '', 0, 0))
            continue

        variants, leaves = patch_inner_thickness(root, val)
        print(f'    patched {variants} chassis variant(s), '
              f'{leaves} physicalParams block(s)')
        out_bytes = serialize_xml(root)

        rel = f'scripts/item_defs/vehicles/{nation}/{basename}.xml'
        target_resmods = os.path.normpath(os.path.join(res_mods_root, rel))
        target_mirror = os.path.normpath(os.path.join(mirror_root, rel))

        os.makedirs(os.path.dirname(target_resmods), exist_ok=True)
        os.makedirs(os.path.dirname(target_mirror), exist_ok=True)
        with open(target_resmods, 'wb') as fp:
            fp.write(out_bytes)
        with open(target_mirror, 'wb') as fp:
            fp.write(out_bytes)
        print(f'    wrote {target_resmods}')
        print(f'    wrote {target_mirror}')
        report.append((nation, basename, tier, val, src, 'OK',
                       target_resmods, target_mirror, variants, leaves))

    # ----- Write a report TSV for the audit append -----
    report_tsv = os.path.join(ROOT, 'hand_off',
                              'repair_inner_thickness_report.tsv')
    with open(report_tsv, 'w', encoding='utf-8') as fp:
        fp.write('nation\tbasename\ttier\tvalue_m\tsource\tstatus\t'
                 'res_mods_path\tmirror_path\tvariants\tleaves\n')
        for row in report:
            fp.write('\t'.join(str(c) for c in row) + '\n')
    print(f'\nReport TSV: {report_tsv}')

    return report


if __name__ == '__main__':
    main()
