"""TEPY UI theme system.

Four named accent colours + one free-form background colour drive
the entire UI palette:

    c1   primary / "warm" accent.  Used by Export and the Model-
         tool buttons (Meshes / Flip / Compare).  The classic TEPY
         default is burnt orange.
    c2   secondary / "cool" accent.  Used by Import and the UI
         display toggles (Grid / Axes / Skybox / ...).  Default
         is olive.
    c3   text-on-dark accent.  Used for console pane lines (and
         later, any read-only labels we want to pop against the
         dark panels).  Default is wheat.
    c4   tertiary / "warm-but-distinct" accent.  Used by Save Prim
         specifically -- the WoT-native `.primitives_processed`
         writer wanted a colour that read as "warm output, but
         not Export".  Default is burnt yellow.
    bg   form/window background colour.  Free-form -- the user
         picks via a colour picker.  Persisted alongside the
         theme name.

Presets are curated from popular dev / IDE colour schemes
(Solarized, Dracula, Nord, Gruvbox, Tokyo Night, Monokai,
Catppuccin Mocha, One Dark, Material Dark).  Each ships its own
`bg` so picking a preset gives a coherent set; `bg` can be
overridden independently via the colour picker without dropping
the preset's c1/c2/c3/c4.

Public API
----------
    PRESETS                    {name: Theme}
    PRESET_NAMES               sorted name list, default first
    DEFAULT_NAME               'TEPY Default'
    set_active(name)           switch the active theme by preset name
    get_active()               return the live Theme instance
    set_bg(rgba)               override just the bg colour (free-form)
    c1() / c2() / c3() /
    c4() / bg()                shortcuts -- return the active colour
                                as a 4-tuple suitable for glClearColor
                                / shader uniforms / pygame.draw

The module keeps state across imports; viewer + ui code reads via
the accessors so a `set_active(...)` from anywhere flips the
live colours everywhere on the next frame (text textures still
need a rebuild -- deferred to Phase 4).

Naming history: this module was briefly called `motif.py` in
v1.63.0.  Renamed to `theme.py` because "theme" is the conventional
term for "user-selectable colour palette" in IDE land.  Module
name + button label + config key all moved together; the
backward-compat fallback in viewer.py reads the legacy `motif`
config key if it's there.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    """A complete colour set: 4 named accents + 1 background.

    All fields are RGBA tuples in 0..1 (immediate format for
    glClearColor, shader uniforms, and our own ui.py fill calls).
    """
    name:  str
    c1:    tuple   # primary  (warm)            -- Export, Model
    c2:    tuple   # secondary (cool)           -- Import, UI tog.
    c3:    tuple   # text-on-dark               -- console lines
    c4:    tuple   # tertiary (warm-distinct)   -- Save Prim
    bg:    tuple   # background / form fill


# ---------------------------------------------------------------------------
# Curated presets.  Each one supplies all four accents plus a bg.
# Picked for "good c1/c2/c4 contrast against bg at button-fill
# brightness" rather than dogmatic accuracy.

PRESETS = {

    # The original TEPY palette.  Default on every fresh install.
    'TEPY Default': Theme(
        name = 'TEPY Default',
        c1   = (0.65, 0.32, 0.10, 1.0),   # burnt orange (Export)
        c2   = (0.42, 0.45, 0.18, 1.0),   # olive        (Import)
        c3   = (0.96, 0.87, 0.70, 1.0),   # wheat        (console)
        c4   = (0.68, 0.52, 0.10, 1.0),   # burnt yellow (Save Prim)
        bg   = (0.07, 0.07, 0.10, 1.0),   # dark blue-grey
    ),

    # Solarized Dark.  Yellow + cyan as primary accents; orange
    # #cb4b16 fills the c4 slot.
    'Solarized Dark': Theme(
        name = 'Solarized Dark',
        c1   = (0.71, 0.54, 0.00, 1.0),   # yellow #b58900
        c2   = (0.16, 0.63, 0.60, 1.0),   # cyan   #2aa198
        c3   = (0.93, 0.91, 0.84, 1.0),   # base2
        c4   = (0.80, 0.29, 0.09, 1.0),   # orange #cb4b16
        bg   = (0.00, 0.17, 0.21, 1.0),   # base03 #002b36
    ),

    # Dracula.  Orange + green primaries, yellow #f1fa8c c3,
    # purple #bd93f9 c4.
    'Dracula': Theme(
        name = 'Dracula',
        c1   = (1.00, 0.72, 0.42, 1.0),   # orange  #ffb86c
        c2   = (0.31, 0.98, 0.48, 1.0),   # green   #50fa7b
        c3   = (0.95, 0.98, 0.55, 1.0),   # yellow  #f1fa8c
        c4   = (0.74, 0.58, 0.98, 1.0),   # purple  #bd93f9
        bg   = (0.16, 0.16, 0.21, 1.0),
    ),

    # Nord.  Aurora cluster.
    'Nord': Theme(
        name = 'Nord',
        c1   = (0.82, 0.53, 0.44, 1.0),   # nord12 orange
        c2   = (0.64, 0.75, 0.55, 1.0),   # nord14 green
        c3   = (0.92, 0.80, 0.55, 1.0),   # nord13 yellow
        c4   = (0.71, 0.56, 0.68, 1.0),   # nord15 purple
        bg   = (0.18, 0.20, 0.25, 1.0),   # nord0
    ),

    # Gruvbox Dark (medium contrast).
    'Gruvbox Dark': Theme(
        name = 'Gruvbox Dark',
        c1   = (1.00, 0.50, 0.10, 1.0),   # bright orange #fe8019
        c2   = (0.72, 0.73, 0.15, 1.0),   # bright green  #b8bb26
        c3   = (0.98, 0.74, 0.18, 1.0),   # bright yellow #fabd2f
        c4   = (0.56, 0.75, 0.49, 1.0),   # bright aqua   #8ec07c
        bg   = (0.16, 0.16, 0.16, 1.0),
    ),

    # Tokyo Night.
    'Tokyo Night': Theme(
        name = 'Tokyo Night',
        c1   = (1.00, 0.62, 0.39, 1.0),   # orange #ff9e64
        c2   = (0.62, 0.81, 0.42, 1.0),   # green  #9ece6a
        c3   = (0.88, 0.69, 0.41, 1.0),   # yellow #e0af68
        c4   = (0.48, 0.64, 0.97, 1.0),   # blue   #7aa2f7
        bg   = (0.10, 0.11, 0.18, 1.0),
    ),

    # Monokai.  High-saturation accents.
    'Monokai': Theme(
        name = 'Monokai',
        c1   = (0.99, 0.59, 0.12, 1.0),   # orange  #fd971f
        c2   = (0.65, 0.89, 0.18, 1.0),   # green   #a6e22e
        c3   = (0.90, 0.86, 0.45, 1.0),   # yellow  #e6db74
        c4   = (0.97, 0.15, 0.45, 1.0),   # magenta #f92672
        bg   = (0.15, 0.16, 0.13, 1.0),
    ),

    # Catppuccin Mocha.
    'Catppuccin Mocha': Theme(
        name = 'Catppuccin Mocha',
        c1   = (0.98, 0.70, 0.53, 1.0),   # peach    #fab387
        c2   = (0.65, 0.89, 0.63, 1.0),   # green    #a6e3a1
        c3   = (0.98, 0.89, 0.69, 1.0),   # yellow   #f9e2af
        c4   = (0.54, 0.71, 0.98, 1.0),   # sapphire-ish
        bg   = (0.12, 0.13, 0.18, 1.0),
    ),

    # Atom's One Dark.  Cooler family; red + blue as primaries.
    'One Dark': Theme(
        name = 'One Dark',
        c1   = (0.88, 0.42, 0.46, 1.0),   # red    #e06c75
        c2   = (0.34, 0.71, 0.94, 1.0),   # blue   #61afef
        c3   = (0.90, 0.75, 0.48, 1.0),   # yellow #e5c07b
        c4   = (0.78, 0.47, 0.86, 1.0),   # magenta #c678dd
        bg   = (0.16, 0.17, 0.20, 1.0),   # bg     #282c34
    ),

    # Google Material Design Dark.
    'Material Dark': Theme(
        name = 'Material Dark',
        c1   = (1.00, 0.60, 0.00, 1.0),   # deep orange
        c2   = (0.30, 0.69, 0.31, 1.0),   # green
        c3   = (1.00, 0.92, 0.23, 1.0),   # yellow
        c4   = (0.00, 0.59, 0.53, 1.0),   # teal
        bg   = (0.07, 0.07, 0.10, 1.0),
    ),

}

# Stable display order.  Default first; the rest sorted alpha.
PRESET_NAMES = ['TEPY Default'] + sorted(
    n for n in PRESETS if n != 'TEPY Default')

DEFAULT_NAME = 'TEPY Default'


# ---------------------------------------------------------------------------
# Active-theme state.  Module-level singleton; viewer + ui code
# reads via the accessors below.

_active = PRESETS[DEFAULT_NAME]
_active_name = DEFAULT_NAME
# Free-form bg override.  None = use the preset's own bg.  When
# the user picks a custom colour via the picker, this gets set
# and overrides the preset's bg until they reset.
_bg_override = None


def set_active(name):
    """Switch the active theme by preset name.  Unknown names
    fall back to the default; returns True on a successful match.
    """
    global _active, _active_name
    theme = PRESETS.get(name)
    if theme is None:
        _active = PRESETS[DEFAULT_NAME]
        _active_name = DEFAULT_NAME
        return False
    _active = theme
    _active_name = name
    return True


def get_active():
    """The current Theme instance.  Read-only -- mutate via
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
        if len(rgba) == 3:
            rgba = (rgba[0], rgba[1], rgba[2], 1.0)
        _bg_override = tuple(float(x) for x in rgba)


# ---------------------------------------------------------------------------
# Per-colour shortcuts.

def c1():
    return _active.c1


def c2():
    return _active.c2


def c3():
    return _active.c3


def c4():
    return _active.c4


def bg():
    return _bg_override if _bg_override is not None else _active.bg
