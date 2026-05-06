"""Localization: WoT catalogs (tank names) + TEPY's own UI strings.

This module hosts two related but distinct gettext-based readers:

1. `WoTLocalizer` -- resolves `#catalog:key` references found in
   WoT's `list.xml` entries against `<wot_root>/res/text/lc_messages
   /<catalog>.mo`.  Used to pull tank friendly names ("M40/M43")
   in whatever language the user's WoT install is set to.

2. `Translator` (TEPY's own i18n) + module-level `_()` helper --
   wraps Python's `gettext` against catalogs we ship at
   `tankExporterPy/locale/<lang>/LC_MESSAGES/tepy.mo`.  Source
   `.po` files live next to each `.mo`; compile via
   `cust_tools/build_locale_mo.py`.  English msgids are the
   canonical strings; `_('Grid')` -> 'Grille' / 'Сетка' / etc.
   based on the active language.  Missing translations echo back
   the msgid (English fallback), so a partially-translated
   catalog Just Works.

The `SUPPORTED_LANGUAGES` table at the top covers every language
WoT ships in any region, plus a friendly name for the picker UI.
Runtime language is picked via `set_active_language(code)`;
`_()` then routes through that language's catalog for the rest
of the session.

Public API
----------
    SUPPORTED_LANGUAGES                  list of (code, name) tuples
    WoTLocalizer(wot_root)               WoT in-game catalog reader
    Translator(locale_root)              TEPY i18n catalog reader
    set_active_language(code)            module-level switch
    _(msgid)                             translate via active catalog
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


# =====================================================================
# TEPY's own UI translations (separate from WoTLocalizer above)
# =====================================================================

# Every language WoT ships somewhere in the world.  The set is the
# union across all regions; an individual install only carries
# whichever subset it was built with.  Order: English first, then
# the major ones, then the rest alphabetical-ish.  Friendly names
# in their own script where possible -- the language picker shows
# "Deutsch" not "German".
SUPPORTED_LANGUAGES = [
    ('en',    'English'),
    ('ru',    'Русский'),         # Russian
    ('de',    'Deutsch'),          # German
    ('fr',    'Français'),         # French
    ('es',    'Español'),          # Spanish
    ('es_ar', 'Español (LATAM)'),  # Spanish (Latin America)
    ('pt_br', 'Português (Brasil)'),
    ('pl',    'Polski'),           # Polish
    ('cs',    'Čeština'),          # Czech
    ('it',    'Italiano'),         # Italian
    ('hu',    'Magyar'),           # Hungarian
    ('bg',    'Български'),       # Bulgarian
    ('ro',    'Română'),           # Romanian
    ('tr',    'Türkçe'),           # Turkish
    ('uk',    'Українська'),       # Ukrainian
    ('ko',    '한국어'),           # Korean
    ('ja',    '日本語'),           # Japanese
    ('zh_cn', '简体中文'),         # Chinese (Simplified)
    ('zh_tw', '繁體中文'),         # Chinese (Traditional)
    ('vi',    'Tiếng Việt'),       # Vietnamese
    ('th',    'ไทย'),              # Thai
]

# Map of language code -> friendly name for fast picker rendering.
LANGUAGE_NAMES = dict(SUPPORTED_LANGUAGES)

# TEPY's gettext domain.  Catalogs at
# `<locale_root>/<lang>/LC_MESSAGES/<DOMAIN>.mo`.
_DOMAIN      = 'tepy'
_DEFAULT_LANG = 'en'

# `tankExporterPy/locale/` -- bundled with the package, ships with
# `.mo` catalogs compiled from the sibling `.po` source files.
_DEFAULT_LOCALE_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'locale')


class Translator:
    """gettext-backed translator for TEPY's own UI strings.

    One instance per session.  Switch language via `set_language`;
    every subsequent `gettext` call returns the new catalog's
    translation.  Missing keys (or untranslated msgstrs) echo the
    msgid back, so a partial catalog falls through to the English
    source string instead of returning empty.

    Args:
        locale_root (str|None) : the directory holding
                                 `<lang>/LC_MESSAGES/<domain>.mo`.
                                 Defaults to the bundled
                                 `tankExporterPy/locale/` folder.
        domain      (str)      : gettext domain (default 'tepy').
    """

    def __init__(self, locale_root=None, domain=_DOMAIN):
        self._locale_root = locale_root or _DEFAULT_LOCALE_ROOT
        self._domain      = domain
        self._lang        = _DEFAULT_LANG
        self._t           = gettext.NullTranslations()    # echoes msgid
        self.set_language(_DEFAULT_LANG)

    def set_language(self, code):
        """Switch the active language.

        Unknown / missing catalogs silently fall back to a
        NullTranslations instance, which echoes msgids verbatim --
        i.e. the user sees the English source strings.

        Args:
            code (str): an entry from SUPPORTED_LANGUAGES.

        Returns:
            bool : True if the catalog was found and loaded; False
                   if we fell back to NullTranslations.
        """
        if not code:
            code = _DEFAULT_LANG
        try:
            t = gettext.translation(
                self._domain,
                localedir=self._locale_root,
                languages=[code],
                fallback=False)
        except (FileNotFoundError, OSError):
            self._t    = gettext.NullTranslations()
            self._lang = code
            return False
        self._t    = t
        self._lang = code
        return True

    @property
    def language(self):
        return self._lang

    def gettext(self, msgid):
        """Translate `msgid` via the active catalog.  Falls back to
        the msgid itself when the catalog is missing or the entry
        is untranslated.
        """
        return self._t.gettext(msgid) if msgid else msgid


# Module-level translator + `_()` shortcut.  Code that wants to
# translate a string does:
#
#     from tankExporterPy.localization import _
#     ...
#     btn = self.ui.add_button(_('Grid'), ...)
#
# `_` is the GNU-gettext convention; using it keeps strings
# scannable for translators.
_translator = Translator()


def set_active_language(code):
    """Module-level: switch the active language for `_()` lookups.

    Args:
        code (str): an entry from SUPPORTED_LANGUAGES.

    Returns:
        bool : True if the catalog was found and loaded; False
               if we fell back to NullTranslations (English).
    """
    return _translator.set_language(code)


def get_active_language():
    """Return the currently active language code (e.g. 'en')."""
    return _translator.language


def _(msgid):
    """Translate `msgid` via the active TEPY catalog.

    Convention: `_` is the GNU gettext shorthand.  Code calls it
    inline: `_('Grid')`, `_('Set Paths')`, etc.  Missing entries
    echo the msgid back (English fallback), so it's safe to wrap
    every UI string even when the target language hasn't been
    translated yet.
    """
    return _translator.gettext(msgid)
