"""Standalone icon test -- prove that resources/tepy_icon_32.png (or
.ico fallback) attaches correctly to a pygame window's title bar,
taskbar, and alt-tab card on Windows.

Run from the project root (or via test_icon.bat):

    python cust_tools/test_icon.py

What it does
------------
1. Locates `resources/tepy_icon_32.png` first, falls back to
   `resources/tepy.ico`, and reports which one it picked.
2. Calls `pygame.display.set_icon(surf)` BEFORE `set_mode` so SDL
   actually attaches the icon to the next window it creates.  This
   ordering matters on Windows -- calling set_icon after set_mode
   is a silent no-op.
3. Opens a 320x320 window titled `TEPY icon test`, paints the icon
   centred at native size (32 px) plus a 6x scaled preview below it,
   and waits for the user to close the window.
4. Prints surface size, image format, and the icon-load path so the
   user can sanity-check what got loaded.

If the icon shows up in the title bar / taskbar of THIS test window
but NOT in the main TEPY window, the bug is in `tankviewer/viewer.py`,
not in the icon file or pygame's icon handling.
"""

import os
import sys
import pygame

_HERE        = os.path.dirname(os.path.abspath(__file__))
_PROJECT     = os.path.dirname(_HERE)
_RES_DIR     = os.path.join(_PROJECT, 'resources')

_PREFERRED   = os.path.join(_RES_DIR, 'tepy_icon_32.png')
_FALLBACK    = os.path.join(_RES_DIR, 'tepy.ico')


def main():
    pygame.init()

    # ---- 1. Locate icon file -------------------------------------------
    if os.path.isfile(_PREFERRED):
        icon_path = _PREFERRED
        which     = 'tepy_icon_32.png (preferred)'
    elif os.path.isfile(_FALLBACK):
        icon_path = _FALLBACK
        which     = 'tepy.ico (fallback)'
    else:
        print("ERROR: neither resources/tepy_icon_32.png nor "
              "resources/tepy.ico exists.")
        print("Run `python cust_tools/make_icon.py` to generate them.")
        sys.exit(1)
    print(f"icon source : {which}")
    print(f"icon path   : {icon_path}")

    # ---- 2. Load + set BEFORE set_mode ---------------------------------
    icon_surf = pygame.image.load(icon_path)
    print(f"icon size   : {icon_surf.get_size()}")
    print(f"icon format : "
          f"{icon_surf.get_bitsize()}-bit, "
          f"masks={icon_surf.get_masks()}")

    # MUST be before set_mode -- this is the whole point of the test.
    pygame.display.set_icon(icon_surf)

    # ---- 3. Open the test window ---------------------------------------
    screen = pygame.display.set_mode((320, 320))
    pygame.display.set_caption('TEPY icon test')

    # ---- 4. Paint background + icon previews ---------------------------
    bg     = (28, 30, 35)
    accent = (212, 116, 52)   # match COLOR_HIDE_LIGHT in make_icon.py
    screen.fill(bg)

    font   = pygame.font.SysFont('Calibri', 14, bold=True)
    title  = font.render('Tepee icon test -- close window when done',
                         True, (220, 220, 230))
    screen.blit(title, ((320 - title.get_width()) // 2, 8))

    # Native-size copy: top centre.
    icon_native = icon_surf
    nw, nh      = icon_native.get_size()
    screen.blit(icon_native, ((320 - nw) // 2, 36))

    label_native = font.render(f'native: {nw}x{nh}',
                               True, (170, 170, 180))
    screen.blit(label_native,
                ((320 - label_native.get_width()) // 2, 36 + nh + 4))

    # Big preview: 6x scaled, NEAREST so pixels stay crisp.
    big_w, big_h = nw * 6, nh * 6
    big = pygame.transform.scale(icon_native, (big_w, big_h))
    screen.blit(big, ((320 - big_w) // 2, 100))

    label_big = font.render(f'6x scaled: {big_w}x{big_h}',
                            True, (170, 170, 180))
    screen.blit(label_big,
                ((320 - label_big.get_width()) // 2,
                 100 + big_h + 4))

    # Bottom hint: where to look for the icon.
    hint = font.render(
        'Icon should appear: title bar, taskbar, alt-tab',
        True, accent)
    screen.blit(hint, ((320 - hint.get_width()) // 2, 320 - 24))

    pygame.display.flip()

    # ---- 5. Wait for close ---------------------------------------------
    running = True
    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                running = False
        pygame.time.wait(16)

    pygame.quit()


if __name__ == '__main__':
    main()
