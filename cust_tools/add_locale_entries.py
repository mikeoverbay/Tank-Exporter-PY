"""Append new msgids `Mouse` + `Visible` to every locale .po file
and recompile the .mo.

Per Coffee 2026-05-16 ("make sure the language pack is current for
all tiles").  v1.230.4 added `_("Visible")` (panel title) and the
existing Mouse-sens slider added `_("Mouse")` -- but neither
msgid was in the gettext catalogs yet, so every non-English
language showed the English fallback.

Idempotent: re-running just rewrites the entries with the latest
translation table.

Run from project root::

    python cust_tools/add_locale_entries.py
"""
import os
import re
import sys

_HERE         = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
_LOCALE_ROOT  = os.path.join(_PROJECT_ROOT, 'tankExporterPy', 'locale')

# Per-language translations.  Short forms preferred -- both are
# UI labels with limited button / panel-title width budgets.
NEW_ENTRIES = {
    'en':    {'Mouse': 'Mouse',        'Visible': 'Visible'},
    'fr':    {'Mouse': 'Souris',       'Visible': 'Visible'},
    'de':    {'Mouse': 'Maus',         'Visible': 'Sichtbar'},
    'ru':    {'Mouse': 'Мышь',         'Visible': 'Видимый'},
    'es':    {'Mouse': 'Ratón',        'Visible': 'Visible'},
    'es_ar': {'Mouse': 'Ratón',        'Visible': 'Visible'},
    'pt_br': {'Mouse': 'Mouse',        'Visible': 'Visível'},
    'pl':    {'Mouse': 'Mysz',         'Visible': 'Widoczne'},
    'cs':    {'Mouse': 'Myš',          'Visible': 'Viditelné'},
    'it':    {'Mouse': 'Mouse',        'Visible': 'Visibile'},
    'hu':    {'Mouse': 'Egér',         'Visible': 'Látható'},
    'bg':    {'Mouse': 'Мишка',        'Visible': 'Видим'},
    'ro':    {'Mouse': 'Mouse',        'Visible': 'Vizibil'},
    'tr':    {'Mouse': 'Fare',         'Visible': 'Görünür'},
    'uk':    {'Mouse': 'Миша',         'Visible': 'Видимий'},
    'ko':    {'Mouse': '마우스',        'Visible': '표시'},
    'ja':    {'Mouse': 'マウス',        'Visible': '表示'},
    'zh_cn': {'Mouse': '鼠标',         'Visible': '可见'},
    'zh_tw': {'Mouse': '滑鼠',         'Visible': '可見'},
    'vi':    {'Mouse': 'Chuột',        'Visible': 'Hiển thị'},
    'th':    {'Mouse': 'เมาส์',          'Visible': 'มองเห็น'},
}


def _po_has_msgid(text, msgid):
    """True iff the .po text already declares this msgid."""
    pat = r'^\s*msgid\s+"' + re.escape(msgid) + r'"\s*$'
    return re.search(pat, text, flags=re.M) is not None


def _replace_or_append(text, msgid, msgstr):
    """Replace an existing msgid/msgstr pair (= idempotent update)
    or append at end-of-file with a section header comment.
    """
    # Match an existing msgid + msgstr block and rewrite it.
    block_pat = (r'(^msgid\s+"' + re.escape(msgid)
                 + r'"\s*\nmsgstr\s+)"[^"]*"')
    new_text, n = re.subn(
        block_pat, r'\1"' + msgstr + r'"',
        text, count=1, flags=re.M)
    if n > 0:
        return new_text
    # Not present -- append at the bottom.  Keep trailing newline.
    if not text.endswith('\n'):
        text = text + '\n'
    text += (
        '\n'
        '# ---- v1.230.5: added by add_locale_entries.py ----\n'
        f'msgid "{msgid}"\n'
        f'msgstr "{msgstr}"\n'
    )
    return text


def update_one(lang_code):
    po_path = os.path.join(_LOCALE_ROOT, lang_code, 'LC_MESSAGES',
                            'tepy.po')
    if not os.path.isfile(po_path):
        print(f'  [{lang_code}] no tepy.po, skipping')
        return
    with open(po_path, encoding='utf-8') as f:
        text = f.read()
    entries = NEW_ENTRIES.get(lang_code, {})
    if not entries:
        print(f'  [{lang_code}] no translations table, skipping')
        return
    for msgid, msgstr in entries.items():
        text = _replace_or_append(text, msgid, msgstr)
    with open(po_path, 'w', encoding='utf-8') as f:
        f.write(text)
    print(f'  [{lang_code}] updated tepy.po')


def main():
    print(f'add_locale_entries: walking {_LOCALE_ROOT}')
    for lang_code in sorted(os.listdir(_LOCALE_ROOT)):
        if not os.path.isdir(os.path.join(_LOCALE_ROOT, lang_code)):
            continue
        update_one(lang_code)
    # Recompile .mo via the sibling tool.
    print('Recompiling .mo files...')
    sys.path.insert(0, _HERE)
    import build_locale_mo
    if hasattr(build_locale_mo, 'main'):
        build_locale_mo.main()
    else:
        # Fall back -- run script via exec
        with open(os.path.join(_HERE, 'build_locale_mo.py')) as f:
            exec(f.read(), {'__name__': '__main__'})


if __name__ == '__main__':
    main()
