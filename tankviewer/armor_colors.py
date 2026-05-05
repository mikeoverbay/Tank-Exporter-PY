"""
Per-tank armor-color lookup.

WoT defines the default body-paint colour for every tank in
`scripts/item_defs/customization/paints/base_paints.xml` -- one entry
per nation, plus a `<vehicleFilter>` that lists special-edition tanks
which DON'T use the nation default (their unique colours are baked into
their textures already, so we want to render those untinted).

This module:

  1. Pulls base_paints.xml out of the WoT install via PkgExtractor on
     first use.
  2. Parses every <paint> block, picks the ones tagged as a nation
     base colour (texture path "<nation>_base_color.png"), and reads
     <color>R G B A</color> as sRGB-normalised floats.
  3. Walks each entry's <exclude><vehicles>...</vehicles></exclude> so
     the lookup can return `None` (= no tint) for excluded tanks.

The loader caches its results in memory for the life of the process.
A fallback hardcoded map kicks in when PkgExtractor is unavailable
(viewer not yet configured with a WoT install path).

Public API:
    ArmorColorLoader(pkg_extractor) -- pkg_extractor may be None
        .get(nation, tank_basename) -> (r, g, b) sRGB-norm | None
        .ensure_loaded()            -> populate the cache

The caller (Viewer.load_vehicle) treats None as "no nation tint" and
hands it to the shader as has_armor_color=0.
"""

import os
import re

from .common import decode_bwxml, is_bwxml


# ---------------------------------------------------------------------------
# Hardcoded fallback -- used when the PkgExtractor isn't configured yet so
# the viewer can still tint tanks before the user points us at a WoT install.
# Values match WoT's base_paints.xml as of 2026-05; refreshed automatically
# when the dynamic loader succeeds.

def _srgb255_to_norm(r, g, b):
    """sRGB integer 0-255 -> normalised 0..1 (no gamma)."""
    return (r / 255.0, g / 255.0, b / 255.0)


_FALLBACK_NATION_COLORS = {
    'ussr':    _srgb255_to_norm( 76,  76,  60),
    'germany': _srgb255_to_norm( 90,  90,  90),
    'usa':     _srgb255_to_norm(105,  98,  77),
    'uk':      _srgb255_to_norm(117, 112,  93),
    'france':  _srgb255_to_norm( 51,  73,  75),
    # Add more as we confirm them; nations not listed return None and
    # the shader falls back to "no tint".
}


# Path inside the WoT pkg layout where the canonical paint catalogue
# lives.  scripts.pkg is the standard host.
_BASE_PAINTS_PATH = 'scripts/item_defs/customization/paints/base_paints.xml'


# Regex for the texture-path -> nation mapping ("usa_base_color.png" -> "usa").
_NATION_FROM_TEX = re.compile(
    r'/repaints/([a-z]+)_base_color\.png\b', re.IGNORECASE)


class ArmorColorLoader:
    """Lazy loader / cache for the WoT armor-colour table.

    Construct once per Viewer instance.  The first call to `get()`
    triggers `ensure_loaded()`, which reaches into PkgExtractor and
    parses the catalogue.  Subsequent calls hit the in-memory cache.
    """

    def __init__(self, pkg_extractor=None):
        self._pkg = pkg_extractor
        self._nation_color = {}        # 'usa' -> (r, g, b) sRGB-norm
        self._excluded     = set()     # {'china:Ch01_Type59_Gold', ...}
        self._loaded       = False
        self._load_failed  = False

    # ---- public API ---------------------------------------------------

    def get(self, nation, tank_basename=None):
        """Return the armor colour for a tank, or None when no tint applies.

        Args:
            nation         (str): 'usa' / 'ussr' / 'germany' / ... (lowercase)
            tank_basename  (str | None): tank XML basename without
                extension, e.g. 'A14_T30'.  When provided we also check
                the per-tank exclusion list and return None if the tank
                ships its own baked-in skin.

        Returns:
            (r, g, b) sRGB-normalised float tuple, or None for no-tint.
        """
        if not nation:
            return None
        nation = nation.lower()

        self.ensure_loaded()

        # Per-tank exclusion takes precedence over the nation default
        if tank_basename:
            key = f"{nation}:{tank_basename}"
            if key in self._excluded:
                return None

        return self._nation_color.get(nation)

    def ensure_loaded(self):
        """Populate the cache.  Idempotent.

        Strategy:
            1. Try to extract base_paints.xml via PkgExtractor.
            2. Decode (BWXML if needed) and parse each <paint> block.
            3. Map texture-name -> nation, capture colour, capture
               exclude-vehicle list.
            4. Fall back to the hardcoded table if anything fails.
        """
        if self._loaded or self._load_failed:
            return
        # Without PkgExtractor we can only use the fallback table.
        if self._pkg is None:
            self._nation_color.update(_FALLBACK_NATION_COLORS)
            self._loaded = True
            return

        try:
            local = self._pkg.extract(_BASE_PAINTS_PATH)
        except Exception as exc:
            print(f"[armor_colors] extract failed: {exc} -- using fallback")
            self._nation_color.update(_FALLBACK_NATION_COLORS)
            self._load_failed = True
            return

        if not local or not os.path.isfile(local):
            print(f"[armor_colors] base_paints.xml not found in pkg -- "
                  f"using fallback")
            self._nation_color.update(_FALLBACK_NATION_COLORS)
            self._load_failed = True
            return

        try:
            with open(local, 'rb') as fh:
                blob = fh.read()
            xml_text = (decode_bwxml(blob) if is_bwxml(blob)
                        else blob.decode('utf-8', errors='replace'))
            self._parse(xml_text)
        except Exception as exc:
            print(f"[armor_colors] parse failed: {exc} -- using fallback")
            self._nation_color.update(_FALLBACK_NATION_COLORS)
            self._load_failed = True
            return

        # If the parse yielded nothing useful (rare format change), fall
        # back so we don't render every tank untinted.
        if not self._nation_color:
            print("[armor_colors] no nation colours parsed -- using fallback")
            self._nation_color.update(_FALLBACK_NATION_COLORS)

        self._loaded = True
        print(f"[armor_colors] loaded {len(self._nation_color)} nation "
              f"defaults, {len(self._excluded)} per-tank exclusions")

    # ---- internals ----------------------------------------------------

    def _parse(self, xml_text):
        """Walk every <paint> block and extract nation default + excludes."""
        # Each paint block is independent and self-contained; regex is
        # plenty here without lxml.  We capture the whole paint body and
        # then sub-pattern-match texture / color / exclude inside.
        for blk in re.finditer(r'<paint\b[^>]*>(.*?)</paint>',
                                xml_text, re.S):
            body = blk.group(1)

            # texture -> nation
            tex_m = re.search(r'<texture>([^<]+)</texture>', body)
            col_m = re.search(r'<color>([^<]+)</color>',     body)
            if not (tex_m and col_m):
                continue
            nation_m = _NATION_FROM_TEX.search(tex_m.group(1))
            if not nation_m:
                continue
            nation = nation_m.group(1).lower()

            # color "R G B A" sRGB 0-255
            parts = col_m.group(1).split()
            if len(parts) < 3:
                continue
            try:
                r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
            except ValueError:
                continue
            self._nation_color[nation] = _srgb255_to_norm(r, g, b)

            # vehicleFilter excludes -- the FULL block belongs to this
            # paint, so we scan inside `body` for nested <exclude>
            # entries.  We're only interested in the explicit-vehicle
            # exclusions ("china:Ch01_Type59_Gold ..." etc.).
            for excl in re.finditer(
                    r'<exclude>\s*<vehicles>([^<]+)</vehicles>',
                    body):
                for tok in excl.group(1).split():
                    self._excluded.add(tok.strip())
