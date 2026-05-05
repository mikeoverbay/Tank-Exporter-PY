"""
Font preview / picker for the splash welcome banner.

Standalone pygame app that walks every system font, renders the
welcome text in each, and shows the result on the same burnt-orange
strip used by the splash so you can pick the one that reads best.

Usage:
    python cust_tools/font_preview.py
    python cust_tools/font_preview.py --text "Welcome to version 1.0.0"
    python cust_tools/font_preview.py --size 32
    python cust_tools/font_preview.py --filter script

Controls:
    Up / Down / mouse wheel : scroll the list
    PageUp / PageDown       : page-step
    Home / End              : jump to top/bottom
    Type any text           : live filter (matches font names containing
                              the typed string, case-insensitive)
    Backspace               : delete one filter character
    Esc                     : clear filter (or quit if filter is empty)
    Enter / left-click row  : print the font name to stdout AND copy it
                              to the clipboard for pasting into splash.py
    F                       : toggle filter to "cursive/script-only"
                              quick-list (Monotype Corsiva, Lucida
                              Handwriting, Segoe Script, ...)
    +/-                     : grow / shrink the preview size
"""

import argparse
import os
import sys

import pygame


# Curated list of cursive / script / handwriting fonts -- when the F
# key is pressed we restrict the list to whichever of these ARE
# installed locally.  Add to taste.
_CURSIVE_NAMES = [
    'monotype corsiva',
    'lucida handwriting',
    'lucida calligraphy',
    'segoe script',
    'brush script mt',
    'edwardian script itc',
    'vivaldi',
    'mistral',
    'french script mt',
    'gigi',
    'kunstler script',
    'palace script mt',
    'rage italic',
    'script mt bold',
    'viner hand itc',
    'pristina',
    'comic sans ms',     # informal but always available
    'ink free',
    'gabriola',          # Vista+ ships this; very ornate
]

# Same burnt-orange strip the splash banner uses (sRGB 0..255).
BG_COLOR        = (199,  84,  13)     # ~ #cc5500
BG_HOVER_COLOR  = (224, 105,  30)     # slightly brighter on hover
TEXT_COLOR      = (255, 255, 255)
PANEL_BG        = ( 24,  26,  32)
ROW_DIVIDER     = ( 56,  60,  68)
HEADER_BG       = ( 38,  42,  50)
LABEL_COLOR     = (210, 215, 225)
ACCENT          = (255, 165,  60)


def get_fonts(filter_substr='', cursive_only=False):
    """Return the sorted list of pygame system-font names matching the
    current filter.  pygame.font.get_fonts() returns names lowercased
    and stripped of spaces -- which is fine; we use them verbatim for
    SysFont() and pretty-print on display by re-spacing the camel-ish
    boundaries.  No fancy reformatting; SysFont accepts the lowercase
    no-space form.
    """
    all_fonts = pygame.font.get_fonts()
    if cursive_only:
        wanted = {n.replace(' ', '').lower() for n in _CURSIVE_NAMES}
        all_fonts = [f for f in all_fonts if f in wanted]
    if filter_substr:
        sub = filter_substr.replace(' ', '').lower()
        all_fonts = [f for f in all_fonts if sub in f]
    return sorted(all_fonts)


def copy_to_clipboard(text):
    """Push `text` to the system clipboard via tkinter (already a project
    dependency).  Returns True on success.
    """
    try:
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(text)
        r.update()
        try:
            r.destroy()
        except Exception:
            pass
        return True
    except Exception as exc:
        print(f"[font-preview] clipboard copy failed: {exc}")
        return False


def main():
    p = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--text', default='Welcome to version 1.0.0',
                   help='Sample text rendered in every font (default: '
                        'the splash welcome banner string).')
    p.add_argument('--size', type=int, default=32,
                   help='Preview font size in points (default: 32, '
                        'matches the splash banner).')
    p.add_argument('--filter', default='',
                   help='Initial substring filter on font names.')
    p.add_argument('--cursive-only', action='store_true',
                   help='Only show cursive / script / handwriting faces '
                        'from the curated list.')
    args = p.parse_args()

    pygame.init()
    pygame.font.init()

    width, height = 1100, 720
    screen = pygame.display.set_mode((width, height), pygame.RESIZABLE)
    pygame.display.set_caption('Tank Exporter PY -- Font Preview')

    # Header / list / footer fonts (separate from the preview font we
    # build per-row in the main loop).
    ui_font     = pygame.font.SysFont('Calibri', 14, bold=True)
    header_font = pygame.font.SysFont('Calibri', 18, bold=True)
    name_font   = pygame.font.SysFont('Consolas', 14)

    sample_text  = args.text
    preview_size = max(8, int(args.size))
    filter_str   = args.filter or ''
    cursive_only = bool(args.cursive_only)
    fonts        = get_fonts(filter_str, cursive_only)
    scroll_top   = 0     # index of the topmost visible row
    hover_idx    = -1    # absolute row index under cursor (-1 = none)

    ROW_H        = 60     # per-row height
    HEADER_H     = 56
    FOOTER_H     = 28

    clock = pygame.time.Clock()
    running = True
    while running:
        # ---- recompute layout each frame so resize is responsive ------
        width, height = screen.get_size()
        list_top = HEADER_H
        list_h   = max(1, height - HEADER_H - FOOTER_H)
        rows_visible = max(1, list_h // ROW_H)

        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.VIDEORESIZE:
                screen = pygame.display.set_mode(ev.size, pygame.RESIZABLE)
            elif ev.type == pygame.MOUSEWHEEL:
                scroll_top = max(0, scroll_top - ev.y * 3)
            elif ev.type == pygame.MOUSEMOTION:
                mx, my = ev.pos
                if list_top <= my < list_top + list_h:
                    rel = (my - list_top) // ROW_H
                    hover_idx = scroll_top + int(rel)
                    if hover_idx >= len(fonts):
                        hover_idx = -1
                else:
                    hover_idx = -1
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                if 0 <= hover_idx < len(fonts):
                    name = fonts[hover_idx]
                    print(f"selected: {name}")
                    copy_to_clipboard(name)
            elif ev.type == pygame.KEYDOWN:
                k = ev.key
                if k == pygame.K_ESCAPE:
                    if filter_str:
                        filter_str = ''
                        fonts = get_fonts(filter_str, cursive_only)
                        scroll_top = 0
                    else:
                        running = False
                elif k == pygame.K_RETURN and 0 <= hover_idx < len(fonts):
                    name = fonts[hover_idx]
                    print(f"selected: {name}")
                    copy_to_clipboard(name)
                elif k == pygame.K_DOWN:
                    scroll_top = min(max(0, len(fonts) - rows_visible),
                                     scroll_top + 1)
                elif k == pygame.K_UP:
                    scroll_top = max(0, scroll_top - 1)
                elif k == pygame.K_PAGEDOWN:
                    scroll_top = min(max(0, len(fonts) - rows_visible),
                                     scroll_top + rows_visible)
                elif k == pygame.K_PAGEUP:
                    scroll_top = max(0, scroll_top - rows_visible)
                elif k == pygame.K_HOME:
                    scroll_top = 0
                elif k == pygame.K_END:
                    scroll_top = max(0, len(fonts) - rows_visible)
                elif k == pygame.K_BACKSPACE:
                    if filter_str:
                        filter_str = filter_str[:-1]
                        fonts = get_fonts(filter_str, cursive_only)
                        scroll_top = 0
                elif k == pygame.K_f and (
                        ev.mod & (pygame.KMOD_CTRL | pygame.KMOD_ALT) == 0):
                    # Plain F (not Ctrl/Alt-F) toggles cursive-only.
                    # When the user is also typing into the filter box
                    # we DON'T eat 'f' below -- this branch wins first.
                    cursive_only = not cursive_only
                    fonts = get_fonts(filter_str, cursive_only)
                    scroll_top = 0
                elif k == pygame.K_PLUS or k == pygame.K_EQUALS:
                    preview_size = min(72, preview_size + 2)
                elif k == pygame.K_MINUS:
                    preview_size = max(8, preview_size - 2)
                elif ev.unicode and ev.unicode.isprintable():
                    filter_str += ev.unicode
                    fonts = get_fonts(filter_str, cursive_only)
                    scroll_top = 0

        # ---- render -------------------------------------------------------
        screen.fill(PANEL_BG)

        # Header
        pygame.draw.rect(screen, HEADER_BG, (0, 0, width, HEADER_H))
        title = header_font.render(
            'Font Preview -- click a row to print + copy the font name',
            True, (235, 240, 250))
        screen.blit(title, (12, 6))

        # Sub-header: counts + filter + cursive toggle + size hint
        sub = (f"showing {len(fonts)} font(s)"
               + (f"   filter='{filter_str}'" if filter_str else "")
               + (f"   [F=cursive-only ON]" if cursive_only else
                  f"   [F=cursive-only off]")
               + f"   size={preview_size}pt   (+/- to change)")
        screen.blit(ui_font.render(sub, True, LABEL_COLOR), (12, 30))

        # Footer hint
        footer_y = height - FOOTER_H
        pygame.draw.rect(screen, HEADER_BG, (0, footer_y, width, FOOTER_H))
        screen.blit(ui_font.render(
            'arrows / wheel = scroll   |   click / Enter = select   |   '
            'type to filter   |   F = cursive-only   |   +/- = size   |   Esc = clear / quit',
            True, LABEL_COLOR), (12, footer_y + 6))

        # Per-row rendering -- visible window only.
        end = min(len(fonts), scroll_top + rows_visible + 1)
        for ri in range(scroll_top, end):
            name = fonts[ri]
            y = list_top + (ri - scroll_top) * ROW_H

            # Row background: hover highlights, otherwise alternating
            # tones for readability.
            row_bg = (PANEL_BG if ri % 2 == 0 else
                      (PANEL_BG[0] + 6, PANEL_BG[1] + 6, PANEL_BG[2] + 6))
            if ri == hover_idx:
                row_bg = (60, 50, 40)
            pygame.draw.rect(screen, row_bg, (0, y, width, ROW_H))
            pygame.draw.line(screen, ROW_DIVIDER,
                             (0, y + ROW_H - 1), (width, y + ROW_H - 1))

            # Left column: font name in plain monospace so it's always
            # readable regardless of the previewed face.
            name_label = name_font.render(name, True, ACCENT)
            screen.blit(name_label,
                        (12, y + (ROW_H - name_label.get_height()) // 2))

            # Right column: burnt-orange strip with the welcome text
            # rendered in the candidate font.  This is the same
            # context the splash uses, so what reads well here will
            # read well there too.
            preview_x = 240
            preview_w = max(0, width - preview_x - 16)
            preview_y = y + 8
            preview_h = ROW_H - 16
            pygame.draw.rect(screen,
                             BG_HOVER_COLOR if ri == hover_idx else BG_COLOR,
                             (preview_x, preview_y, preview_w, preview_h),
                             border_radius=4)
            try:
                font = pygame.font.SysFont(name, preview_size)
                surf = font.render(sample_text, True, TEXT_COLOR)
                # Clip so an extra-wide font doesn't paint into the next row
                clip = pygame.Rect(preview_x + 12, preview_y,
                                   preview_w - 24, preview_h)
                old_clip = screen.get_clip()
                screen.set_clip(clip)
                screen.blit(surf, (preview_x + 12,
                                   preview_y + (preview_h - surf.get_height()) // 2))
                screen.set_clip(old_clip)
            except Exception as exc:
                err = ui_font.render(f"<failed: {exc}>", True, (255, 80, 80))
                screen.blit(err, (preview_x + 12,
                                  preview_y + (preview_h - err.get_height()) // 2))

        pygame.display.flip()
        clock.tick(60)

    pygame.quit()


if __name__ == '__main__':
    main()
