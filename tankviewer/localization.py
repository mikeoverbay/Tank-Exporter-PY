"""WoT localization reader.

WoT stores user-facing strings (tank names, descriptions, module
labels, ...) in standard gettext binary catalogs at:

    <wot_root>/res/text/lc_messages/<catalog>.mo

Each tank's `list.xml` entry carries a `userString` reference of
the form `#<catalog>:<key>` (e.g. `#usa_vehicles:A37_M40M43`).
Resolving that reference yields the friendly, localized name --
"M40/M43" in the example above.

Public API
----------
    WoTLocalizer(wot_root)        -- one instance per session
    WoTLocalizer.lookup(ref)      -- '#cat:key' -> localized str
    WoTLocalizer.is_available     -- True iff lc_messages folder exists

Catalogs are loaded lazily on first use of each `<catalog>:` prefix
and cached for the rest of the session.  Missing catalogs / missing
keys cause `lookup` to fall back to the bare `key` portion of the
ref so the caller never has to special-case None.

The user's WoT install language is whatever the lc_messages folder
contains -- WoT swaps `.mo` files when the user changes their
client language.  We just read whatever's on disk.
"""

import gettext
import os
import re

# `#<catalog>:<key>` reference format.  Catalog names are bare
# (no path / no .mo extension); keys are typically alphanumerics +
# underscores but can carry hyphens / periods on rare entries.
_USERSTRING_RE = re.compile(r'^#([^:]+):(.+)$')


class WoTLocalizer:
    """Resolve `#catalog:key` references against WoT's `.mo` catalogs."""

    def __init__(self, wot_root):
        """Args:
            wot_root (str|None): the WoT install root (parent of
                `res/`).  None or invalid silently disables the
                localizer -- `lookup` then falls through to the
                key portion of every ref.
        """
        self._catalog_dir = None
        self._catalogs    = {}    # catalog_name -> GNUTranslations | None
        self._missing_warned = set()

        if not wot_root:
            return
        cand = os.path.join(wot_root, 'res', 'text', 'lc_messages')
        if os.path.isdir(cand):
            self._catalog_dir = cand

    # ------------------------------------------------------------------
    @property
    def is_available(self):
        """True iff a `lc_messages` folder was found at construction.

        Doesn't guarantee any particular catalog exists -- callers
        can still have a `lookup` miss for a catalog that isn't on
        disk in this install.
        """
        return self._catalog_dir is not None

    # ------------------------------------------------------------------
    def _get_catalog(self, name):
        """Return a `GNUTranslations` for `name`, or None on failure.

        Lazy-loaded + cached.  A missing or unreadable file caches
        None so subsequent lookups skip the disk hit.
        """
        if name in self._catalogs:
            return self._catalogs[name]
        if self._catalog_dir is None:
            self._catalogs[name] = None
            return None
        path = os.path.join(self._catalog_dir, name + '.mo')
        if not os.path.isfile(path):
            if name not in self._missing_warned:
                self._missing_warned.add(name)
                print(f"[localizer] catalog not found: {name}.mo "
                      f"(in {self._catalog_dir})")
            self._catalogs[name] = None
            return None
        try:
            with open(path, 'rb') as fh:
                t = gettext.GNUTranslations(fh)
        except Exception as exc:
            print(f"[localizer] load {name}.mo failed: {exc}")
            self._catalogs[name] = None
            return None
        self._catalogs[name] = t
        return t

    # ------------------------------------------------------------------
    def lookup(self, ref, default=None):
        """Resolve a `#catalog:key` ref to its localized string.

        Args:
            ref     (str|None) : the `userString` from list.xml,
                                 or already-translated text, or None
            default (str|None) : value to return when the ref isn't
                                 a `#catalog:key` form.  None means
                                 "echo the input back" (so callers
                                 can pass plain strings through
                                 without checking the format first).

        Returns:
            str | None  -- the localized string, or `default` when
            `ref` is None / not a ref / catalog missing.

        Behaviour
        ---------
        * `None`                          -> `default` (or None)
        * Plain string (no leading `#`)   -> the string unchanged
        * `#catalog:key` resolved         -> localized translation
        * `#catalog:key` catalog missing  -> the bare key (so the
                                              UI shows something
                                              useful even when the
                                              install is missing
                                              the .mo file)
        * `#catalog:key` key missing      -> the bare key (gettext
                                              echoes the input on a
                                              miss)
        """
        if ref is None:
            return default
        if not isinstance(ref, str) or not ref.startswith('#'):
            return ref if ref else default
        m = _USERSTRING_RE.match(ref)
        if not m:
            return ref
        catalog, key = m.group(1), m.group(2)
        cat = self._get_catalog(catalog)
        if cat is None:
            return key
        # gettext returns the input string when the key isn't in
        # the catalog -- exactly the fallback we want.
        return cat.gettext(key)

    # ------------------------------------------------------------------
    def lookup_basename(self, ref):
        """Convenience: resolve `ref` and strip nothing.

        Returns the localized string OR the raw key when the catalog
        / lookup fails.  Never returns None unless `ref` itself is
        None.  Use this from UI code where you always want a
        non-empty label.
        """
        result = self.lookup(ref)
        if result:
            return result
        # Last-ditch: the key portion of the ref (best human-readable
        # fallback we have).
        if ref and isinstance(ref, str):
            m = _USERSTRING_RE.match(ref)
            if m:
                return m.group(2)
        return ''
