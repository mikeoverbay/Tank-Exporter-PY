"""One-off sweep: which tanks omit <segmentsInnerThickness> from their
chassis component XML?

Follow-up to hand_off/TRACK_DATA_AUDIT_2026-05-24.md section 3.4
(T30 found missing the field in a 30-tank sample).  This walks every
tank in the user's WoT install, checks every <chassis>/<variant> in
each vehicle XML, and reports the missing-field set.

Output goes to stdout as JSON-ish lines plus a final TSV blob suitable
for pasting into a markdown table.  READ-ONLY -- no files mutated.
"""

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tankExporterPy.loaders import PkgExtractor
from tankExporterPy.common import is_bwxml, decode_bwxml


def parse_vehicle_xml(pe, nation, xml_name):
    """Return list of (variant_tag, inner_thickness_value, has_splineDesc).

    inner_thickness_value:
        float -> field present and parseable.
        None  -> field present but empty / unparseable.
        '__ABSENT__' -> field tag entirely absent.
    has_splineDesc: True if the variant has any <splineDesc> block at all
        (i.e. is a tracked chassis); False for wheeled vehicles which
        legitimately lack the chain.
    """
    internal = f'scripts/item_defs/vehicles/{nation}/{xml_name}'
    local = pe.extract(internal)
    if not local:
        return None  # tank XML not found
    try:
        with open(local, 'rb') as fh:
            raw = fh.read()
        xml_str = decode_bwxml(raw) if is_bwxml(raw) else raw.decode(
            'utf-8', errors='replace')
        xml_str = re.sub(r'<xmlns:[^>]*>[^<]*</xmlns:[^>]*>', '', xml_str)
        root = ET.fromstring(xml_str)
    except Exception as exc:
        return [('<parse_error>', f'ERR: {exc}', False)]

    chassis_el = root.find('chassis')
    if chassis_el is None:
        return []

    out = []
    for variant in chassis_el:
        tag = variant.tag
        has_spline = (variant.find('.//splineDesc') is not None) or \
                     (variant.find('.//physicalTracks') is not None)
        # Look for the field
        it_elems = list(variant.iter('segmentsInnerThickness'))
        if not it_elems:
            out.append((tag, '__ABSENT__', has_spline))
            continue
        # Take first parseable
        val = None
        for el in it_elems:
            txt = (el.text or '').strip() if el.text else ''
            if txt:
                try:
                    val = float(txt)
                    break
                except ValueError:
                    pass
        out.append((tag, val, has_spline))
    return out


def main():
    cfg = {}
    cfg_path = os.path.join(ROOT, 'tankExporterPy.json')
    if os.path.isfile(cfg_path):
        with open(cfg_path, encoding='utf-8') as f:
            cfg = json.load(f)
    pkg_dir = cfg.get('pkg_dir') or 'C:/Games/World_of_Tanks_NA/res/packages'
    wot_root = os.path.normpath(os.path.join(pkg_dir, '..', '..'))
    lookup_xml = cfg.get('lookup_xml') or 'C:/experiment/TheItemList.xml'

    pe = PkgExtractor(wot_root, pkg_dir=pkg_dir, lookup_xml=lookup_xml)

    all_xmls = pe.list_vehicle_xmls(with_tier=True)
    # all_xmls: {nation: [{'xml','tier','vclass','user_string'}, ...]}

    rows = []  # (nation, tier, basename, variant, value, has_spline)
    n_tanks_total = 0
    n_variants_total = 0
    n_variants_missing = 0
    tanks_missing = set()

    for nation in sorted(all_xmls.keys()):
        for ent in all_xmls[nation]:
            xml_name = ent['xml']
            basename = xml_name[:-4] if xml_name.endswith('.xml') else xml_name
            tier = ent.get('tier')
            res = parse_vehicle_xml(pe, nation, xml_name)
            if res is None:
                continue
            n_tanks_total += 1
            tank_has_miss = False
            for variant, val, has_spline in res:
                n_variants_total += 1
                # "Missing" = field absent OR empty text (loader treats both
                # identically -- both fall to default 0.0)
                missing = (val == '__ABSENT__') or (val is None)
                # Only count missing for TRACKED chassis (skip wheeled EBRs --
                # they legitimately have no chain).
                if missing and has_spline:
                    n_variants_missing += 1
                    tank_has_miss = True
                rows.append((nation, tier, basename, variant, val,
                             has_spline, missing))
            if tank_has_miss:
                tanks_missing.add((nation, basename))

    print(f'\n=== SWEEP SUMMARY ===')
    print(f'tanks parsed       : {n_tanks_total}')
    print(f'chassis variants   : {n_variants_total}')
    print(f'variants missing IT: {n_variants_missing}')
    print(f'tanks with >=1 miss: {len(tanks_missing)}')

    # Per-nation totals
    per_nation = defaultdict(lambda: [0, 0, 0, 0])  # tanks, variants, missing, tanks_missing
    nation_tanks_missing = defaultdict(set)
    nation_tanks_seen = defaultdict(set)
    nation_variants = Counter()
    nation_variants_missing = Counter()
    for nation, tier, basename, variant, val, has_spline, missing in rows:
        nation_tanks_seen[nation].add(basename)
        nation_variants[nation] += 1
        if missing and has_spline:
            nation_variants_missing[nation] += 1
            nation_tanks_missing[nation].add(basename)

    print('\n=== PER NATION ===')
    print(f'{"nation":<10} {"tanks":>6} {"variants":>9} {"miss_var":>9} {"miss_tank":>10}')
    for nation in sorted(nation_tanks_seen.keys()):
        print(f'{nation:<10} {len(nation_tanks_seen[nation]):>6} '
              f'{nation_variants[nation]:>9} '
              f'{nation_variants_missing[nation]:>9} '
              f'{len(nation_tanks_missing[nation]):>10}')

    # Write missing list to TSV
    out_tsv = os.path.join(ROOT, 'hand_off',
                           'sweep_inner_thickness_missing.tsv')
    with open(out_tsv, 'w', encoding='utf-8') as fp:
        fp.write('nation\ttier\tbasename\tvariant\thas_spline\tvalue\n')
        for nation, tier, basename, variant, val, has_spline, missing in rows:
            if missing and has_spline:
                vstr = '' if val == '__ABSENT__' else (
                    '<empty>' if val is None else f'{val}')
                fp.write(f'{nation}\t{tier}\t{basename}\t{variant}\t'
                         f'{has_spline}\t{vstr}\n')
    print(f'\nWrote {out_tsv}')

    # Also write a complete dump (all rows) for analysis
    out_all = os.path.join(ROOT, 'hand_off', 'sweep_inner_thickness_all.tsv')
    with open(out_all, 'w', encoding='utf-8') as fp:
        fp.write('nation\ttier\tbasename\tvariant\thas_spline\thas_field\tvalue\n')
        for nation, tier, basename, variant, val, has_spline, missing in rows:
            has_field = 'no' if val == '__ABSENT__' else (
                'empty' if val is None else 'yes')
            vstr = '' if val == '__ABSENT__' else (
                '' if val is None else f'{val:.5f}')
            fp.write(f'{nation}\t{tier}\t{basename}\t{variant}\t'
                     f'{has_spline}\t{has_field}\t{vstr}\n')
    print(f'Wrote {out_all}')

    # Distribution of values where present
    present_values = [val for _, _, _, _, val, hs, _ in rows
                       if hs and isinstance(val, float)]
    print(f'\n=== VALUE DISTRIBUTION (tracked variants with field) ===')
    print(f'n with parseable value: {len(present_values)}')
    if present_values:
        import statistics
        print(f'min/max  : {min(present_values):+.5f} / '
              f'{max(present_values):+.5f}')
        print(f'mean     : {statistics.mean(present_values):+.5f}')
        print(f'median   : {statistics.median(present_values):+.5f}')
        # Sign distribution
        neg = sum(1 for v in present_values if v < 0)
        zero = sum(1 for v in present_values if v == 0)
        pos = sum(1 for v in present_values if v > 0)
        print(f'signs    : neg={neg} zero={zero} pos={pos}')
        # Histogram in 5 mm bins from -50 to +100 mm
        bins = Counter()
        for v in present_values:
            b = int(round(v * 1000 / 5)) * 5
            bins[b] += 1
        print('histogram (5 mm bins):')
        for b in sorted(bins):
            print(f'  {b:+4d} mm: {"#" * bins[b]} ({bins[b]})')


if __name__ == '__main__':
    main()
