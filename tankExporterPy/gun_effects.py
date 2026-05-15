"""Parse WoT's gun-effect tables so the runtime knows the per-gun
flash timing, color animation, particle file, and ground-wave
reference.

WoT stores this in two BWXML files inside `scripts/item_defs/
vehicles/`:

  * `<nation>/components/guns.xml` -- per-gun fields including
    `<effects>` (the name of the effect to play, e.g.
    `shot_main_mb`, `shot_small_auto`, `shot_KwK_L46`).
  * `common/gun_effects.xml`        -- definitions for every
    effect name.  Carries the timeline, the pixie `.eff` ref,
    a point-light spec with color keyframes, the shotSound
    references, and a `relatedEffects` block with ground-wave
    info.

Per Coffee 2026-05-14 ("is there any info in the def xml about
mussel flash?") -- we don't try to parse the BigWorld `.eff`
particle files (binary, undocumented); we just consume the
timeline + light + groundwave fields that ARE plain XML once
the BWXML wrapper is stripped.

Public surface:

    GunEffectsTable(pkg_extractor) -- loads gun_effects.xml + every
        per-nation guns.xml on construction.
    table.lookup_for_gun(gun_id) -> dict or None
        Returns the effect spec for the gun id (already-resolved
        from the active tank's `tank_info['gun']['shortName']` or
        equivalent).

Each effect spec is a dict:
    {
        'name':         <effect-name string>,
        'duration_s':   <timeline.end as seconds, float>,
        'pixie_file':   <eff file path or None>,
        'position':     <hardpoint name, usually 'HP_gunFire'>,
        'light': {
            'inner_radius': float,
            'outer_radius': float,
            'duration_s':   float (= timeline[endKey]),
            'keyframes': [
                {'t': 0..1, 'rgb': (r, g, b, a)[0..255], 'multiplier': float},
                ...
            ],
        } or None,
        'ground_wave': {
            'pixie_file': str,
            'surface_kind': str,
        } or None,
    }
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET

from .common import decode_bwxml, is_bwxml


# ---------------------------------------------------------------------------

def _safe_float(text, default=0.0):
    try:
        return float((text or '').strip())
    except (TypeError, ValueError):
        return default


def _parse_color(text):
    """Parse a color element's text -- WoT formats are
    `'R G B'` or `'R G B A'` (space-separated integers 0..255).
    Returns a 4-tuple of floats in [0, 255].
    """
    parts = [p for p in (text or '').replace(',', ' ').split()
             if p.strip()]
    try:
        vals = [float(p) for p in parts[:4]]
    except ValueError:
        return (0.0, 0.0, 0.0, 0.0)
    while len(vals) < 3:
        vals.append(0.0)
    if len(vals) < 4:
        vals.append(255.0)
    return tuple(vals[:4])


def _read_xml(local_path):
    """Read either BWXML or plain-text XML and return the
    ElementTree root, or None on failure."""
    if not local_path or not os.path.isfile(local_path):
        return None
    try:
        with open(local_path, 'rb') as fh:
            raw = fh.read()
    except Exception:
        return None
    try:
        if is_bwxml(raw):
            text = decode_bwxml(raw)
        else:
            text = raw.decode('utf-8', errors='replace')
        return ET.fromstring(text)
    except Exception:
        return None


# ---------------------------------------------------------------------------

class GunEffectsTable:
    """Lazy-loaded gun-effect lookup.

    Builds two indexes:
      * `gun_to_effect` -- gun id (= XML tag) -> effect name
      * `effect_to_spec` -- effect name -> spec dict

    Calling `lookup_for_gun(gun_id)` joins both.

    The class is permissive: any parse failure (file missing,
    BWXML decode crash, malformed entries) falls through to a
    None result.  Callers should treat None as "use built-in
    defaults".

    `nation_paths` is auto-built from the PkgExtractor.  All
    parsing happens up-front (one-shot at viewer startup).
    """

    def __init__(self, pkg_extractor):
        self.gun_to_effect   = {}     # gun_id -> effect_name
        self.effect_to_spec  = {}     # effect_name -> dict
        if pkg_extractor is None:
            return
        # Load shared effects table once.
        eff_path = pkg_extractor.extract(
            'scripts/item_defs/vehicles/common/gun_effects.xml')
        root = _read_xml(eff_path)
        if root is not None:
            self._populate_effects(root)
        # Load each nation's guns.xml -- the BWXML lookup tables
        # live under `scripts/item_defs/vehicles/<nation>/components/
        # guns.xml`.  Walk every known nation.
        try:
            nations = sorted(set(pkg_extractor.list_vehicle_xmls()))
        except Exception:
            nations = []
        for nation in nations:
            gpath = pkg_extractor.extract(
                f'scripts/item_defs/vehicles/{nation}/'
                f'components/guns.xml')
            root = _read_xml(gpath)
            if root is None:
                continue
            self._populate_guns(root)

    def _populate_effects(self, root):
        """Walk gun_effects.xml.  Each top-level child IS an
        effect name (`<shot_main_mb>...</shot_main_mb>`).
        """
        for eff in root:
            name = eff.tag
            spec = self._parse_effect_spec(eff)
            spec['name'] = name
            self.effect_to_spec[name] = spec

    def _parse_effect_spec(self, eff):
        """Parse one `<shot_xxx>` block."""
        spec = {
            'duration_s':  None,
            'pixie_file':  None,
            'position':    None,
            'light':       None,
            'ground_wave': None,
        }
        timeline = eff.find('timeline') or ET.Element('_')
        # `end` is the canonical "effect over" time, in seconds.
        # Light's `endKey` (often `lighting2`) is a fraction of
        # the total duration -- look it up in the same timeline.
        end_t = _safe_float(timeline.findtext('end'), 0.4)
        spec['duration_s'] = end_t
        effects_grp = eff.find('effects')
        if effects_grp is not None:
            pixie = effects_grp.find('pixie')
            if pixie is not None:
                spec['pixie_file'] = (
                    (pixie.findtext('file') or '').strip() or None)
                spec['position'] = (
                    (pixie.findtext('position')
                     or '').strip() or None)
            light = effects_grp.find('light')
            if light is not None:
                # Resolve endKey to seconds via the timeline.
                end_key = (light.findtext('endKey') or '').strip()
                light_end = (
                    _safe_float(timeline.findtext(end_key), end_t)
                    if end_key else end_t)
                kfs = []
                for anim in light.findall('animation'):
                    kfs.append({
                        't':          _safe_float(
                                          anim.findtext('time'), 0.0),
                        'rgba':       _parse_color(
                                          anim.findtext('color')),
                        'multiplier': _safe_float(
                                          anim.findtext('multiplier'),
                                          1.0),
                    })
                spec['light'] = {
                    'duration_s':   light_end,
                    'inner_radius': _safe_float(
                                        light.findtext('innerRadius'),
                                        1.5),
                    'outer_radius': _safe_float(
                                        light.findtext('outerRadius'),
                                        8.0),
                    'keyframes':    kfs,
                }
        related = eff.find('relatedEffects')
        if related is not None:
            gw = related.find('groundWave')
            if gw is not None:
                gw_eff = gw.find('effects')
                if gw_eff is not None:
                    gw_pix = gw_eff.find('pixie')
                    if gw_pix is not None:
                        spec['ground_wave'] = {
                            'pixie_file': (
                                gw_pix.findtext('file') or '').strip(),
                            'surface_kind': (
                                gw_pix.findtext('surfaceMatKind')
                                or 'default').strip(),
                        }
        return spec

    def _populate_guns(self, root):
        """Walk one nation's guns.xml.  Structure:
            <root>
                <ids>...</ids>
                <shared>
                    <_88mm_Kw_K_36_L_56>
                        <effects>shot_main_mb</effects>
                        ...
                    </_88mm_Kw_K_36_L_56>
                    ...
                </shared>
            </root>
        """
        shared = root.find('shared')
        if shared is None:
            return
        for gun in shared:
            gun_id = gun.tag
            eff_name = (gun.findtext('effects') or '').strip()
            if eff_name:
                self.gun_to_effect[gun_id] = eff_name

    # ------------------------------------------------------------------
    def lookup_for_gun(self, gun_id):
        """Return the merged spec for a gun id, or None when the
        gun isn't catalogued.

        `gun_id` is the BWXML tag name from `guns.xml`, e.g.
        `_88mm_Kw_K_36_L_56`.  Most WoT XMLs store this as the
        gun's short id with a leading underscore -- the caller
        may need to add the underscore.
        """
        if not gun_id:
            return None
        # Try direct match, then with/without leading underscore.
        candidates = [gun_id,
                      gun_id.lstrip('_'),
                      '_' + gun_id.lstrip('_')]
        for cand in candidates:
            eff_name = self.gun_to_effect.get(cand)
            if eff_name:
                spec = self.effect_to_spec.get(eff_name)
                if spec:
                    return spec
                return {'name': eff_name, 'duration_s': 0.4,
                        'light': None, 'ground_wave': None,
                        'pixie_file': None, 'position': None}
        return None
