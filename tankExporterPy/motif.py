"""TEPY UI motif (theme) system.

Three named accent colours + one free-form background colour drive
the entire UI palette:

    c1   primary / "warm" accent.  Maps to Export, Save Prim, the
         Model-tool buttons (Meshes / Flip / Compare).  The
         classic TEPY default is burnt orange.
    c2   secondary / "cool" accent.  Maps to Import and the UI
         display toggles (Grid / Axes / Skybox / ...).  The
         classic TEPY default is olive.
    c3   text-on-dark accent.  Used for console pane lines (and
         later, any read-only labels we want to pop against the
         dark panels).  Default is wheat.
    bg   form/window background colour.  Free-form -- the user
         picks a colour outside the preset list via a colour
         picker.  Persisted alongside the named motif.

Presets are a curated list pulled from popular dev / IDE colour
schemes (Solarized, Dracula, Nord, Gruvbox, Tokyo Night, Monokai,
Catppuccin Mocha, Material Dark, ...).  Each one ships its own
`bg` so picking the preset gives a coherent set; the user can then
override `bg` independently via the colour picker.

Public API
----------
    PRESETS                    {name: Motif}
    PRESET_NAMES               sorted name list, default first
    DEFAULT_NAME               'TEPY Default'
    set_active(name)           switch the active motif by preset name
    get_active()               return the live Motif instance
    set_bg(rgba)               override just the bg colour (for
                                the free-form picker)
    c1() / c2() / c3() / bg()  shortcuts -- return the active
                                colour as a 4-tuple suitable for
                                glClearColor / shader uniforms /
                                pygame.draw

The module keeps state across imports; viewer + ui code reads via
the accessors so a `set_active(...)` from anywhere flips colours
everywhere on the next frame (for live geometry colours -- text
textures still need a rebuild, deferred).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Motif:
    """A complete colour set: 3 named accents + 1 background.

    All fields are RGBA tuples in 0..1 (immediate format for
    glClearColor, shader uniforms, and our own ui.py fill calls).
    """
    name:  str
    c1:    tuple   # primary  (warm) accent
    c2:    tuple   # secondary (cool) accent
    c3:    tuple   # text-on-dark accent
    bg:    tuple   # background / form fill


# ---------------------------------------------------------------------------
# Curated presets.  Sourced from the colour-scheme list each
# preset's name names; picked for "good c1/c2 contrast against bg
# at button-fill brightness" rather than dogmatic accuracy.

PRESETS = {

    # The original TEPY palette.  Default on every fresh install
    # so the app looks identical to before this commit unless the
    # user explicitly picks something else.
    'TEPY Default': Motif(
        name = 'TEPY Default',
        c1   = (0.65, 0.32, 0.10, 1.0),   # burnt orange (Export)
        c2   = (0.42, 0.45, 0.18, 1.0),   # olive        (Import)
        c3   = (0.96, 0.87, 0.70, 1.0),   # wheat        (console)
        bg   = (0.07, 0.07, 0.10, 1.0),   # dark blue-grey
    ),

    # Ethan Schoonover's Solarized.  Yellow #b58900 + cyan #2aa198
    # for the two accents; base03 background.
    'Solarized Dark': Motif(
        name = 'Solarized Dark',
        c1   = (0.71, 0.54, 0.00, 1.0),
        c2   = (0.16, 0.63, 0.60, 1.0),
        c3   = (0.93, 0.91, 0.84, 1.0),
        bg   = (0.00, 0.17, 0.21, 1.0),
    ),

    # Dracula community palette.  Orange #ffb86c + green #50fa7b.
    'Dracula': Motif(
        name = 'Dracula',
        c1   = (1.00, 0.72, 0.42, 1.0),
        c2   = (0.31, 0.98, 0.48, 1.0),
        c3   = (0.95, 0.98, 0.55, 1.0),   # yellow #f1fa8c for text
        bg   = (0.16, 0.16, 0.21, 1.0),
    ),

    # arcticicestudio's Nord.  Frost / aurora mix.
    'Nord': Motif(
        name = 'Nord',
        c1   = (0.82, 0.53, 0.44, 1.0),   # nord12 orange
        c2   = (0.64, 0.75, 0.55, 1.0),   # nord14 green
        c3   = (0.92, 0.80, 0.55, 1.0),   # nord13 yellow
        bg   = (0.18, 0.20, 0.25, 1.0),   # nord0 polar night
    ),

    # Pavel Pertsev's Gruvbox Dark (medium contrast).
    'Gruvbox Dark': Motif(
        name = 'Gruvbox Dark',
        c1   = (1.00, 0.50, 0.10, 1.0),   # bright orange #fe8019
        c2   = (0.72, 0.73, 0.15, 1.0),   # bright green  #b8bb26
        c3   = (0.98, 0.74, 0.18, 1.0),   # bright yellow #fabd2f
        bg   = (0.16, 0.16, 0.16, 1.0),   # bg0           #282828
    ),

    # Enkia's Tokyo Night (dark variant).
    'Tokyo Night': Motif(
        name = 'Tokyo Night',
        c1   = (1.00, 0.62, 0.39, 1.0),   # orange #ff9e64
        c2   = (0.62, 0.81, 0.42, 1.0),   # green  #9ece6a
        c3   = (0.88, 0.69, 0.41, 1.0),   # yellow #e0af68
        bg   = (0.10, 0.11, 0.18, 1.0),   # bg     #1a1b26
    ),

    # Wimer Hazenberg's Monokai.  Punchy, high-saturation accents.
    'Monokai': Motif(
        name = 'Monokai',
        c1   = (0.99, 0.59, 0.12, 1.0),   # orange #fd971f
        c2   = (0.65, 0.89, 0.18, 1.0),   # green  #a6e22e
        c3   = (0.90, 0.86, 0.45, 1.0),   # yellow #e6db74
        bg   = (0.15, 0.16, 0.13, 1.0),
    ),

    # Catppuccin Mocha -- newer popular pastel-on-dark palette.
    'Catppuccin Mocha': Motif(
        name = 'Catppuccin Mocha',
        c1   = (0.98, 0.70, 0.53, 1.0),   # peach  #fab387
        c2   = (0.65, 0.89, 0.63, 1.0),   # green  #a6e3a1
        c3   = (0.98, 0.89, 0.69, 1.0),   # yellow #f9e2af
        bg   = (0.12, 0.13, 0.18, 1.0),   # base   #1e1e2e
    ),

    # Atom's One Dark, original.  Cooler than the others; teal +
    # red as the warm/cool pair.
    'One Dark': Motif(
        name = 'One Dark',
        c1   = (0.88, 0.42, 0.46, 1.0),   # red    #e06c75
        c2   = (0.34, 0.71, 0.94, 1.0),   # blue   #61afef
        c3   = (0.90, 0.75, 0.48, 1.0),   # yellow #e5c07b
        bg   = (0.16, 0.17, 0.20, 1.0),   # bg     #282c34
    ),

    # Google Material Design Dark (deep orange + green).
    'Material Dark': Motif(
        name = 'Material Dark',
        c1   = (1.00, 0.60, 0.00, 1.0),   # deep orange
        c2   = (0.30, 0.69, 0.31, 1.0),   # green
        c3   = (1.00, 0.92, 0.23, 1.0),   # yellow
        bg   = (0.07, 0.07, 0.10, 1.0),
    ),

}

# Stable display order.  Default first; the rest sorted alpha.
PRESET_NAMES = ['TEPY Default'] + sorted(
    n for n in PRESETS if n != 'TEPY Default')

DEFAULT_NAME = 'TEPY Default'


# ---------------------------------------------------------------------------
# Active-motif state.  Module-level singleton; viewer + ui code
# reads via the accessors below.

_active = PRESETS[DEFAULT_NAME]
_active_name = DEFAULT_NAME
# Free-form bg override.  None = use the preset's own bg.  When
# the user picks a custom colour via the picker, this gets set
# and overrides the preset's bg until they reset.
_bg_override = None


def set_active(name):
    """Switch the active motif by preset name.  Unknown names
    fall back to the default; returns True on a successful match.
    """
    global _active, _active_name
    motif = PRESETS.get(name)
    if motif is None:
        _active = PRESETS[DEFAULT_NAME]
        _active_name = DEFAULT_NAME
        return False
    _active = motif
    _active_name = name
    return True


def get_active():
    """The current Motif instance.  Read-only -- mutate via
    `set_active` / `set_bg`.
    """
    return _active


def get_active_name():
    return _active_name


def set_bg(rgba):
    """Override just the background colour (free-form picker).

    Pass None to clear the override and revert to the preset's
    own bg.  RGBA tuple in 0..1 either way.
    """
    global _bg_override
    if rgba is None:
        _bg_override = None
    else:
        # Accept (r, g, b) too -- pad alpha.
        if len(rgba) == 3:
            rgba = (rgba[0], rgba[1], rgba[2], 1.0)
        _bg_override = tuple(float(x) for x in rgba)


# ---------------------------------------------------------------------------
# Per-colour shortcuts.  Read-only views suitable for handing
# straight to glClearColor / shader uniforms / pygame fills.

def c1():
    return _active.c1


def c2():
    return _active.c2


def c3():
    return _active.c3


def bg():
    return _bg_override if _bg_override is not None else _active.bg
