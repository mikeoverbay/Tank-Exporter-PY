"""
Main viewer application -- window, event loop, mesh loading, rendering.

Classes:
    Viewer : creates the Pygame/OpenGL window, loads the primitives_processed
             file, manages Camera/Grid/Axes/Sphere/UIManager, and runs the
             render loop.
             Constructor: Viewer(filepath)
             Methods:
                 run()                           -- start the event loop
                 load_mesh(filepath)             -- parse + upload one file
                 handle_input()                  -- process SDL events
                 render()                        -- draw one frame
                 _build_ui()                     -- create menu buttons
                 _on_resize(w, h)                -- handle VIDEORESIZE
                 _apply_button_action(btn)        -- sync button -> viewer flag
                 _sync_button_state(attr, value)  -- sync viewer flag -> button
"""

import ctypes
import math
import os
import re
import shutil
import time
from collections import deque

import numpy as np
import pygame
from pygame.locals import (DOUBLEBUF, KEYDOWN, K_F11, MOUSEBUTTONDOWN, MOUSEBUTTONUP,
                            MOUSEMOTION, MOUSEWHEEL, OPENGL, QUIT, RESIZABLE,
                            VIDEORESIZE, K_ESCAPE, K_n, K_r, K_w, K_h, K_c, K_o,
                            K_a, K_d, K_s, K_x, K_z, K_F2,
                            K_0, K_1, K_2, K_3, K_4, K_5, K_6, K_7, K_8, K_9)
from OpenGL.GL import *

from .loaders  import MeshParser, VisualLoader, TextureLoader, VehicleXMLLoader, PkgExtractor
from .         import config as _config
from .         import theme   as _theme
from .mesh     import Mesh
from .scene    import Camera, Grid, Axes, Sphere, LineBatch
from .shaders  import (ShaderProgram, SimpleColorShader,
                       ParticleShader, ImportedShader, NormalsShader,
                       TerrainShader)
from .skybox   import Skybox
from .terrain  import Terrain
from .particles import FlipbookTexture, ParticleSystem, AnimatedBillboard
from .ui       import UIManager, UITreeView, UITreeNode, UITabBar
from .localization import _


# ---------------------------------------------------------------------------
# Nation armor colours
# ---------------------------------------------------------------------------
# The per-nation default tint colour used to live here as a hardcoded
# table guessed from the legacy VB Tank Exporter source.  It now comes
# from `tankExporterPy.armor_colors.ArmorColorLoader`, which parses the
# authoritative WoT data file `scripts/item_defs/customization/paints/
# base_paints.xml` and also honours its per-tank exclusion list (Type
# 59 Gold, Skorpion BF, etc. ship with their unique colour baked into
# the texture and should render untinted).
#
# The hardcoded fallback values for the same 11 nations now live inside
# ArmorColorLoader._FALLBACK_NATION_COLORS in armor_colors.py and only
# come into play when the user hasn't pointed us at a WoT install yet.
# ---------------------------------------------------------------------------


def _split_into_submeshes(parsed_group, materials):
    """Yield (synth_group_dict, material_dict) tuples, one per sub-mesh.

    A WoT primitives_processed top-level group can carry multiple sub-mesh
    ranges (its 'prim_groups' list -- each entry is {start_index,
    prim_count, start_vertex, vertex_count}) sharing one vertex buffer.
    The matching visual file's renderSet has a parallel list of
    materials, one per sub-mesh.

    For each sub-mesh:
      * Slice the vertex arrays to [start_vertex, start_vertex+vertex_count)
      * Slice the index buffer to [start_index, start_index+prim_count*3)
        and rebase indices by subtracting start_vertex (since the original
        indices are global to the shared buffer).
      * Pair with materials[i] (or {} if the visual file is shorter).

    Tanks with simple single-mesh components (no equipment / decoration
    overlays) come out of MeshParser with prim_groups=[] -- those still
    yield a single tuple covering the whole group.
    """
    sub_ranges = parsed_group.get('prim_groups') or []
    verts_full = parsed_group['vertices']
    idx_full   = parsed_group['indices']

    if not sub_ranges:
        # No sub-mesh metadata: treat the whole group as one sub-mesh.
        sub_ranges = [{
            'start_index':  0,
            'prim_count':   len(idx_full) // 3,
            'start_vertex': 0,
            'vertex_count': len(verts_full['positions']),
        }]

    for i, sub in enumerate(sub_ranges):
        sv = sub['start_vertex']
        nv = sub['vertex_count']
        si = sub['start_index']
        ni = sub['prim_count'] * 3

        # None-safe slice: optional fields (tangents/binormals/bone_indices/
        # bone_weights) may be None for formats that don't carry them.
        sub_verts = {k: (v[sv:sv + nv] if v is not None else None)
                     for k, v in verts_full.items()}
        sub_idx   = idx_full[si:si + ni].copy()
        if sv:
            sub_idx -= sv     # rebase to local-vertex indices

        synth = {
            'name':        parsed_group['name'],
            'format':      parsed_group['format'],
            'vertices':    sub_verts,
            'indices':     sub_idx,
            'prim_groups': [],
        }
        material = materials[i] if i < len(materials) else {}
        yield synth, material


def _nation_from_xml_path(xml_path):
    """Extract nation folder name from a vehicle XML path.

    e.g.  .../vehicles/usa/A14_T30.xml  →  'usa'
          .../vehicles/germany/Pz_IV.xml →  'germany'
    Returns None if the pattern is not found.
    """
    parts = xml_path.replace('\\', '/').split('/')
    try:
        idx = next(i for i, p in enumerate(parts) if p == 'vehicles')
        return parts[idx + 1].lower()
    except (StopIteration, IndexError):
        return None


class MeshSet:
    """A loaded scene -- either a WoT vehicle (source_type='wot') or
    an imported FBX/GLB/OBJ scene (source_type='fbx').

    The viewer keeps two of these (fbx_set + pkg_set) so the user can
    hold both representations of the same tank simultaneously, flip
    between them for visual comparison, or align mesh order before
    re-export to .primitives_processed.

    Attributes:
        meshes           : list[Mesh]
        source_type      : None | 'wot' | 'fbx'
        source_tank_name : str | None  -- WoT XML basename (e.g. 'It21_Lion')
                                          when known.  Set on every WoT load
                                          and on FBX import when the FBX
                                          carried the magic _WOT_TANK_<name>
                                          empty (or the filename matches).
        exhaust_points   : list of {component, name, pos, fwd}.  Filled by
                           VisualLoader.find_exhaust_nodes for WoT loads or
                           by viewer._populate_exhaust_for_tank() for
                           FBX imports that resolved a source_tank_name.
        exhaust_pixie    : 'gas_medium' / 'diesel_large' / etc., from the
                           def XML's <exhaust><pixie>...</pixie></exhaust>.
                           Drives smoke style; None for FBX without a
                           resolved source tank.
        armor_color      : (r, g, b) sRGB-norm tuple driven by nation.
                           None for FBX (no nation context).
        scene_bbox       : (np.array(3), np.array(3)) for camera fit.
                           None until the load completes.
    """

    __slots__ = (
        'meshes',
        'source_type',
        'source_tank_name',
        'exhaust_points',
        'exhaust_pixie',
        'armor_color',
        'scene_bbox',
        'tank_info',
        # res_mods Extract destination, computed once per load.  Both
        # are absolute disk paths; None = "no extract location yet"
        # (fresh viewer / cleared scene / FBX import without canonical
        # paths).  See Viewer._stash_extract_paths.
        'extract_tank_root',
        'extract_variant_dir',
        'extract_damaged',
    )

    def __init__(self):
        self.meshes           = []
        self.source_type      = None
        self.source_tank_name = None
        self.exhaust_points   = []
        self.exhaust_pixie    = None
        self.armor_color      = None
        self.scene_bbox       = None
        # Cached info-panel payload from VehicleXMLLoader.parse_info
        # (populated by load_vehicle).  Held on the set so flipping
        # back to a previously-loaded tank can rebuild the left info
        # tree without a full XML re-parse.  None for FBX imports and
        # standalone .primitives_processed loads.
        self.tank_info        = None
        # res_mods Extract destination, computed by load_vehicle once
        # nation + xml basename + damaged flag are all known.
        # tank_root  : <res_mods>/vehicles/<full_nation>/<xml_base>/
        # variant_dir: <tank_root>/<crash|normal>/
        # damaged    : True/False, mirrors how variant_dir was named
        # so external callers can read the variant without parsing
        # the path back out.  None when no canonical pkg path could
        # be derived (FBX import without source_tank_name, etc.).
        self.extract_tank_root   = None
        self.extract_variant_dir = None
        self.extract_damaged     = False

    def cleanup(self):
        """Free GPU resources for every mesh and reset to empty state.
        Safe to call when nothing is loaded."""
        for mesh in self.meshes:
            try:
                mesh.cleanup()
            except Exception as exc:
                print(f"[meshset] mesh cleanup error: {exc}")
        self.meshes           = []
        self.source_type      = None
        self.source_tank_name = None
        self.exhaust_points   = []
        self.exhaust_pixie    = None
        self.armor_color      = None
        self.scene_bbox       = None
        self.tank_info        = None
        self.extract_tank_root   = None
        self.extract_variant_dir = None
        self.extract_damaged     = False

    def is_loaded(self):
        return bool(self.meshes)


class Viewer:
    """Main OpenGL viewer application for WoT .primitives_processed files.

    Args:
        filepath (str): absolute path to a .primitives_processed file
    """

    # Width of the right-hand tank-browser tree panel (pixels).
    # 286 = wide enough for an 11-tab tier strip ('1' .. '11') with
    # ~26 px per tab and a couple of px of edge margin.
    TREE_PANEL_W = 286

    # Heights of the control regions inside the side panels (pixels).
    # Left panel: top block holds display toggles, action buttons,
    # lighting sliders + checkboxes.  Below it sits the info-tree.
    # Right panel: bottom block holds smoke sliders + HP-marker toggle.
    # Above it sits the tab-bar + tank-list tree.
    # Floor / fallback height of the left controls block.  At runtime
    # `_layout_widgets` overwrites this on the instance with the actual
    # height the button-group + slider + checkbox stack ends up
    # claiming, so the info-tree below sits cleanly under whatever the
    # layout produced.  This class-level value is just the lower bound
    # used before the first layout pass runs.
    LEFT_CONTROLS_H  = 274      # default floor; instance value computed
    # Right panel: floor / fallback height.  Auto-computed at runtime
    # in _layout_widgets (same pattern as LEFT_CONTROLS_H) so the
    # tank-list tree above tracks however many sliders the panel
    # ends up with -- adding more sliders later doesn't need a
    # matching constant bump.
    RIGHT_CONTROLS_H = 280      # default floor; instance value computed

    # Tier-filter tab labels (strings).  '1' .. '11' for WoT tiers I-XI.
    TREE_TIER_TABS = [str(t) for t in range(1, 12)]

    # Engine-class names used by WoT.  Each tank's def XML carries an
    # `<exhaust><pixie>` value that picks one of these to drive smoke
    # particle scale + behaviour in-game.  We mirror the same set as
    # the keys in our per-class smoke/fire settings dict so tweaks the
    # user makes while a tier-1 (gas_small) is on screen save into the
    # 'gas_small' slot, and don't bleed into 'diesel_large' when a
    # heavy is loaded next.  The active editing slot is auto-wired
    # from the loaded tank's pixie -- there's no manual override.
    EXHAUST_PIXIE_CLASSES = (
        'gas_small',     'gas_medium',     'gas_large',
        'diesel_small',  'diesel_medium',  'diesel_large',  'diesel_strv',
    )

    # Built-in defaults applied the first time the user runs a build
    # with per-class settings (or when the persisted dict is missing a
    # class / field).  Loosely scaled by name -- *_small spawns visibly
    # less than *_large; the user dials from there.  Persisted to
    # config under `smoke_groups` / `fire_groups` (see cleanup()).
    # Falls through to 'gas_medium' for any unknown / null pixie.
    _SMOKE_GROUP_DEFAULTS = {
        'gas_small':     {'start_size': 0.06, 'end_size': 0.16, 'speed': 1.5,
                          'fade_start_frame': 30.0, 'fade_end_frame': 60.0},
        'gas_medium':    {'start_size': 0.10, 'end_size': 0.25, 'speed': 2.0,
                          'fade_start_frame': 30.0, 'fade_end_frame': 60.0},
        'gas_large':     {'start_size': 0.18, 'end_size': 0.42, 'speed': 2.5,
                          'fade_start_frame': 30.0, 'fade_end_frame': 60.0},
        'diesel_small':  {'start_size': 0.07, 'end_size': 0.18, 'speed': 1.6,
                          'fade_start_frame': 30.0, 'fade_end_frame': 60.0},
        'diesel_medium': {'start_size': 0.11, 'end_size': 0.27, 'speed': 2.0,
                          'fade_start_frame': 30.0, 'fade_end_frame': 60.0},
        'diesel_large':  {'start_size': 0.20, 'end_size': 0.45, 'speed': 2.5,
                          'fade_start_frame': 30.0, 'fade_end_frame': 60.0},
        'diesel_strv':   {'start_size': 0.09, 'end_size': 0.22, 'speed': 1.8,
                          'fade_start_frame': 30.0, 'fade_end_frame': 60.0},
    }
    _FIRE_GROUP_DEFAULTS = {
        'gas_small':     {'size': 1.0},
        'gas_medium':    {'size': 1.6},
        'gas_large':     {'size': 2.4},
        'diesel_small':  {'size': 1.1},
        'diesel_medium': {'size': 1.7},
        'diesel_large':  {'size': 2.6},
        'diesel_strv':   {'size': 1.4},
    }
    # Fallback class used when the loaded tank's pixie is missing
    # or unknown -- common for turret-only stub entries.
    _DEFAULT_PIXIE_CLASS = 'gas_medium'

    # Width of the left-hand info panel (collapsible tank stats).
    INFO_PANEL_W = 280

    # Folder of tank-thumbnail PNGs (filename = <tank_xml_basename>.png).
    # Located at the project root, sibling to the tankExporterPy package.
    THUMB_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'thumb_nails')

    # Authoritative "currently in-game" tank list (Tank Exporter export
    # backed by the WoT API).  Used to filter the tree -- anything not
    # listed here is treated as a dev / removed / non-playable entry.
    TANKS_TXT = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'tanks.txt')

    # Project-root resources directory.  Used to cache shared textures
    # (notably the detail / scratch noise map) so we skip the expensive
    # PKG extraction on every session.
    RESOURCES_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'resources')

    # ------------------------------------------------------------------
    # Active-set property forwarders
    # ------------------------------------------------------------------
    # The viewer keeps TWO MeshSet containers:
    #     self._fbx_set  -- populated by load_imported_payload (FBX/GLB/OBJ)
    #     self._pkg_set  -- populated by load_mesh / load_vehicle (WoT pkg)
    # Exactly one is "active" at a time (self._active_set_name).  The
    # render loop, the mesh-visibility window, the camera fit, and the
    # smoke / hp_lines all read from the active set.  Loading either
    # source replaces ONLY that set's contents; the inactive set is
    # preserved so the user can flip back-and-forth without re-loading.
    #
    # All the per-load attributes that USED to live directly on Viewer
    # (self.meshes, self.source_type, self.source_tank_name,
    # self._exhaust_points, self._exhaust_pixie, self._armor_color,
    # self._scene_bbox) are now @property forwarders that read/write
    # the active MeshSet.  This keeps every existing call site
    # (`self.meshes.append(m)`, `for m in self.meshes:`,
    # `self.meshes = []`, ...) working unchanged.

    @property
    def _active_set(self):
        return (self._fbx_set if self._active_set_name == 'fbx'
                else self._pkg_set)

    @property
    def meshes(self):
        return self._active_set.meshes
    @meshes.setter
    def meshes(self, value):
        self._active_set.meshes = value

    @property
    def source_type(self):
        return self._active_set.source_type
    @source_type.setter
    def source_type(self, value):
        self._active_set.source_type = value

    @property
    def source_tank_name(self):
        return self._active_set.source_tank_name
    @source_tank_name.setter
    def source_tank_name(self, value):
        self._active_set.source_tank_name = value

    @property
    def _exhaust_points(self):
        return self._active_set.exhaust_points
    @_exhaust_points.setter
    def _exhaust_points(self, value):
        self._active_set.exhaust_points = value

    @property
    def _exhaust_pixie(self):
        return self._active_set.exhaust_pixie
    @_exhaust_pixie.setter
    def _exhaust_pixie(self, value):
        self._active_set.exhaust_pixie = value

    @property
    def _armor_color(self):
        return self._active_set.armor_color
    @_armor_color.setter
    def _armor_color(self, value):
        self._active_set.armor_color = value

    @property
    def _scene_bbox(self):
        return self._active_set.scene_bbox
    @_scene_bbox.setter
    def _scene_bbox(self, value):
        self._active_set.scene_bbox = value

    def __init__(self, filepath=None, cfg=None):
        # Two-set storage (must be created BEFORE any property setter
        # below fires -- self.meshes = [] / self.source_type = None /
        # etc. all route through the active set's attribute).
        self._fbx_set         = MeshSet()
        self._pkg_set         = MeshSet()
        # Default active set is the WoT-pkg side -- matches old behavior
        # for users who never touch the Import button.  load_vehicle /
        # load_mesh keep this as 'pkg'; load_imported_payload switches
        # to 'fbx' for the duration of the import.
        self._active_set_name = 'pkg'
        # Damaged-variant flag.  Set by load_vehicle (when the user
        # checks "Load Damaged" in the load dialog) and by FBX import
        # if the input filename ends with `_damaged`.  Read by
        # _on_export_clicked to tag the default save name and by
        # Save Prim to route writes to crash/ instead of normal/.
        self._loaded_damaged = False
        # Crash-tile channel offset (0/1/2) added to the per-fragment
        # world-space hash inside mesh.frag.  Increments every
        # successful load_vehicle so reloading the same damaged tank
        # rotates which of the three R/G/B grunge variants land where
        # -- one panel that came up "channel R" last time becomes G
        # this time and B the time after.  Three loads in a row
        # cycle through every layout the tile can produce.  Only
        # affects the crash shader path; ignored for normal loads.
        self._crash_channel_offset = 0
        # Merge supplied config with defaults so callers can omit it
        self._cfg = _config.load()
        if cfg:
            self._cfg.update(cfg)

        pygame.init()
        pygame.font.init()

        # Startup window size: 1024 x 576.  Aspect-matched to
        # the current splash banner (1672 x 941, ratio ~1.778).
        # `1024 * 941 / 1672` rounds to 576, so the banner fits
        # the window pixel-for-pixel without stretch or padding.
        # After splash teardown, `_go_maximized` takes over.
        self.width  = 1024
        self.height = 576

        # ---- Windows taskbar identity ----------------------------------
        # By default Windows lumps every script run by `python.exe`
        # under one taskbar entry that uses the Python icon.  Setting
        # an explicit AppUserModelID via shell32 tells Windows to treat
        # TEPY as its own app, which (a) gives us a separate taskbar
        # group and (b) lets the icon we install via set_icon below
        # actually appear in the taskbar instead of the Python default.
        # No-op on non-Windows; harmless when shell32 isn't reachable.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                'mikeoverbay.TEPY.viewer.1')
        except Exception:
            pass

        # ---- Window icon -- MUST be set BEFORE set_mode ----------------
        # On Windows, pygame.display.set_icon attaches the icon to the
        # NEXT window pygame creates.  Calling it after set_mode is a
        # silent no-op (the window already exists with the default
        # robot icon).  So we install the icon first, then create the
        # window, and only then write the caption text.
        #
        # SDL_image can be flaky about multi-size .ico files (it tends
        # to pick an arbitrary frame, often too small to look right at
        # the title-bar size).  We prefer dedicated PNG sidecars
        # written by `cust_tools/make_icon.py` and try them in
        # priority order: 24 px first (matches Windows' title-bar
        # request size on most builds, no awkward downscale), then
        # 32, then 48, then the .ico as last resort.
        from . import __version__ as _APP_VERSION
        self._app_version = _APP_VERSION
        try:
            _res_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'resources')
            _icon_path = None
            for _candidate in ('tepy_icon_24.png',
                               'tepy_icon_32.png',
                               'tepy_icon_48.png',
                               'tepy.ico'):
                _full = os.path.join(_res_dir, _candidate)
                if os.path.isfile(_full):
                    _icon_path = _full
                    break
            if _icon_path:
                # Note: do NOT .convert_alpha() here -- that requires
                # a display surface to exist, and we're intentionally
                # running BEFORE set_mode.  pygame.image.load on a
                # PNG already returns a 32-bit RGBA surface, which is
                # what set_icon wants anyway.
                _icon_surf = pygame.image.load(_icon_path)
                pygame.display.set_icon(_icon_surf)
                print(f"[viewer] icon: {os.path.basename(_icon_path)} "
                      f"({_icon_surf.get_size()})")
            else:
                print(f"[viewer] icon: no tepy_icon_*.png or tepy.ico in "
                      f"{_res_dir}")
        except Exception as exc:
            print(f"[viewer] icon skipped: {exc}")

        # vsync=1 -- ask SDL to enable vertical sync (one swap per
        # refresh).  Without this, pygame.display.flip() returns the
        # instant the swap is SCHEDULED, the driver can drop or
        # coalesce frames, and the wall-clock interval between flips
        # is jittery enough to make the FPS readout misleading.
        # Falls back gracefully on drivers that refuse vsync (some
        # remote-desktop / virtualised GPUs) -- pygame just ignores
        # the request.  Pygame 2.0+ required for the kwarg; we're on
        # >= 2.5 already (see requirements/requirements.txt).
        pygame.display.set_mode(
            (self.width, self.height),
            DOUBLEBUF | OPENGL | RESIZABLE,
            vsync=1,
        )
        pygame.display.set_caption(f"TEPY  v{_APP_VERSION}")

        # Fullscreen state machine.  Splash renders in the windowed
        # client area we just opened (so it centres correctly inside
        # a known viewport); once init is done, run() promotes to
        # fullscreen via _go_fullscreen.  F11 toggles back to a
        # windowed state at the dimensions we remembered here.
        self._is_fullscreen = False
        self._windowed_w    = self.width
        self._windowed_h    = self.height

        glEnable(GL_DEPTH_TEST)
        # Activate persisted theme before we touch any colour.  All
        # subsequent button accents / clear-colour / console-text
        # tints read from theme.c1() / c2() / c3() / c4() / bg(),
        # so a `set_active(...)` here flips the entire palette
        # before any widget gets built.
        # Theme name lives under `theme` in config; `motif` is the
        # legacy v1.63.0 key kept as a fallback so an existing
        # tankExporterPy.json from that version still resolves.
        theme_name = (self._cfg.get('theme')
                      or self._cfg.get('motif')   # legacy key
                      or _theme.DEFAULT_NAME)
        _theme.set_active(theme_name)
        # Optional persisted bg override (free-form colour picker).
        bg_override = (self._cfg.get('theme_bg')
                       or self._cfg.get('motif_bg'))   # legacy
        if bg_override:
            _theme.set_bg(tuple(bg_override))
        glClearColor(*_theme.bg())

        # ---- Splash screen ------------------------------------------------
        # Painted as soon as the GL context exists so the user sees
        # something immediately while the rest of init runs (shaders
        # compile, IBL bake, tier-tree build).  Self-contained shader
        # so it doesn't depend on UIManager (which is created later).
        # Set to None on failure -- splash is a UX nicety, not load-bearing.
        self.splash = None
        try:
            from .splash import Splash
            from . import __version__ as _APP_VERSION
            # Prefer the TEPY-branded banner (resources/tepy_banner.png --
            # the sepia stagecoach scene with our burnt-orange title
            # baked into the bottom-centre).  Fall back to the bare
            # splash.png if the banner hasn't been generated on this
            # checkout yet -- run cust_tools/make_banner.py to produce
            # tepy_banner.png from splash.png + the current title text.
            _res_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'resources')
            _banner = os.path.join(_res_dir, 'tepy_banner.png')
            _bare   = os.path.join(_res_dir, 'splash.png')
            _splash_path = _banner if os.path.isfile(_banner) else _bare
            if os.path.isfile(_splash_path):
                _welcome = f"Welcome to version {_APP_VERSION}"
                self.splash = Splash(_splash_path, self.width, self.height,
                                     welcome_text=_welcome)
                self.splash.render()
                pygame.display.flip()
                # Hide the OS window chrome while the splash is up.
                # Restored just before the maximised main window
                # comes online (in `run()`).
                self._set_window_borderless(True)
        except Exception as exc:
            print(f"[viewer] splash skipped: {exc}")
            self.splash = None

        # Splash status updates at key init points so users don't think
        # the app is hung during the multi-second startup phase.
        self._splash_status('Compiling shaders...')

        # Shaders
        self.shader          = ShaderProgram()      # WoT PBR
        self.color_shader    = SimpleColorShader()  # debug lines / spheres
        self.imported_shader = ImportedShader()     # FBX-import simple shader
        # Surface-normal debug-line shader (vert + geom + frag).
        # Driven by the right-panel "Normals" slider; length = 0
        # disables the pass entirely.  Built once at startup; cheap
        # to keep around even when off.
        self.normals_shader  = NormalsShader()

        # Camera and scene helpers
        # Camera reports the visible 3D viewport size, which excludes both
        # the left info panel and the right tree panel.  Initialised with
        # the EXPANDED inset; _on_resize() is called after the UIManager
        # is built (later in __init__) and re-applies the actual inset
        # taking the persisted info_panel_collapsed flag into account.
        self.camera      = Camera()
        self.camera.width  = max(1, self.width
                                  - self.INFO_PANEL_W - self.TREE_PANEL_W)
        self.camera.height = self.height

        self.grid         = Grid(cell_size=0.25, grid_cells=50)
        self.axes         = Axes(scale=5.0)
        self.light_sphere = Sphere(radius=0.2, sectors=32, stacks=16)
        # Small orange marker sphere drawn at every discovered hardpoint
        # (HP_Fire / HP_engineExhaust / etc.).  Smaller radius than the
        # light marker so it doesn't dominate the screen on small tanks,
        # bright orange so it pops against painted hull.
        self.hp_sphere    = Sphere(radius=0.08, sectors=16, stacks=8,
                                   color=(1.0, 0.45, 0.05))
        # Line batch for exhaust direction vectors -- each hardpoint gets a
        # 0.5-unit ray showing the smoke emit direction.  Re-uploaded on
        # every load_vehicle call.
        self.hp_lines     = LineBatch(line_width=2.5)
        # Line batch for fire-billboard outlines (debug-only).  When
        # the "Show Fire" checkbox is on, render() rebuilds this from
        # `self.fire_billboards.emitters` each frame -- four segments
        # per quad forming a yellow rectangle that traces the
        # bottom-anchored billboard the textured pass draws.  Lets
        # the user verify HP_Fire placement, billboard size, and the
        # camera-facing math at a glance.
        self.fire_outlines = LineBatch(line_width=1.5)

        # Line batch for the look-at crosshair.  Three perpendicular
        # 10-unit-total lines (±5 on each axis from camera.center)
        # in pale pink, only drawn while the user is actively
        # changing the look-at point: Shift held (lift on Y) or
        # middle-mouse-button held (pan on XZ).  When neither is
        # held, the crosshair is hidden so it doesn't clutter the
        # normal viewing scene.
        self.lookat_lines = LineBatch(line_width=1.5)
        # Tank-physics debug overlay: per-wheel ground contacts +
        # target wheel-centre markers + yellow suspension lines.
        # Painted only when both Debug checkbox + tank physics
        # active so a normal viewing session isn't cluttered.
        self.physics_lines = LineBatch(line_width=1.5)
        # Hull bounding-box overlay: 12 line edges around the
        # AABB of every component=='hull' mesh.  Computed at load
        # time + transformed by chassis_pose each frame so the
        # box follows the tank.
        self.hull_box_lines = LineBatch(line_width=1.5)
        # Flag set by `handle_input` each tick; checked by
        # `render` to decide whether to draw the crosshair.  Lives
        # on `self` (not a render-time recheck) so we can capture
        # the held state at exactly the same instant as the camera
        # mutation it pairs with.
        self._show_lookat_lines = False

        # Previous-foreground HWND saved on WINDOWENTER so we can
        # restore that window's focus when the cursor leaves.
        # `None` between leave/enter cycles -- we only save when
        # we actually steal focus, not on every event.  Windows-
        # only state; meaningless on Linux / macOS where SDL2
        # already does the right thing on cursor enter.
        self._prev_foreground_hwnd = None

        # ---- Particle system (engine smoke, billboard flipbook) ------------
        # Flipbook is N PNG frames (256x256 RGBA) at resources/smoke/.
        # Loaded once at startup and held for the life of the viewer.
        # ParticleSystem owns the per-particle CPU state + dynamic VBO;
        # set_emitters() is called every load_vehicle to point it at the
        # current tank's HP_Track_Exhaus_* nodes.
        # ---- Runtime extract: WoT fire / smoke flipbooks ----------
        # The PNGs under resources/fire/ and resources/smoke/ are
        # slices of Wargaming's eff_tex.dds particle atlas, so we
        # CAN'T ship them with the repo.  Instead, when those
        # folders are empty (fresh clone or freshly gitignored),
        # we extract the atlas from the user's local particles.pkg
        # and slice the two grids the viewer actually uses.  Output
        # is identical to running cust_tools/extract_wot_fire_atlas.py
        # by hand; this just removes the manual step.  Skips work
        # entirely when the folders already have PNGs.
        self._splash_status('Checking fire / smoke flipbooks...')
        try:
            from cust_tools.extract_wot_fire_atlas import (
                ensure_runtime_flipbooks)
            cfg_pkg_dir = (self._cfg.get('pkg_dir', '') or '').strip()
            ensure_runtime_flipbooks(
                resources_dir=os.path.join(
                    os.path.dirname(os.path.dirname(__file__)),
                    'resources'),
                extra_pkg_paths=(cfg_pkg_dir,) if cfg_pkg_dir else (),
            )
        except Exception as exc:
            print(f"[viewer] runtime flipbook extract skipped: {exc}")

        self._splash_status('Loading smoke flipbook (91 frames)...')
        self.particle_shader      = None
        self.smoke_flipbook       = None
        self.smoke_particles      = None
        self.fire_smoke_particles = None
        try:
            self.particle_shader = ParticleShader()
            smoke_dir = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                'resources', 'smoke')
            self.smoke_flipbook  = FlipbookTexture(smoke_dir)
            self.smoke_particles = ParticleSystem(self.smoke_flipbook,
                                                  max_particles=1024)
            # Restore persisted fade range immediately -- this way the
            # values continue to apply even if the slider is later
            # removed (the slider only writes back into these attributes).
            # Then clamp into the loaded flipbook's actual frame range
            # so swapping in a shorter set (e.g. WoT's 64-frame smoke
            # vs the old 91-frame procedural smoke) doesn't leave the
            # fade-end pointing past the last frame -- particles would
            # disappear at full opacity instead of fading out cleanly.
            self.smoke_particles.fade_start_frame = float(
                self._cfg.get('smoke_fade_start_frame', 75.0))
            self.smoke_particles.fade_end_frame   = float(
                self._cfg.get('smoke_fade_end_frame',   91.0))
            self._clamp_fade_to_flipbook(
                self.smoke_particles, self.smoke_flipbook, label='smoke')

            # Second smoke ParticleSystem dedicated to FIRE-point
            # smoke (the plume rising off each HP_Fire flame on a
            # damaged tank).  Shares the same flipbook + the same
            # tunable values as the engine-exhaust smoke -- only
            # the per-system cap is different:
            #   1024 particles (engine exhaust, multi-emitter cone)
            #    400 particles (fire-point smoke, smaller load)
            # Splitting them lets us cap fire smoke without also
            # clipping the busier engine-exhaust plume.  The
            # flipbook itself is shared, not duplicated -- only
            # the particle pool + VBO are independent.
            self.fire_smoke_particles = ParticleSystem(
                self.smoke_flipbook, max_particles=400)
            self.fire_smoke_particles.fade_start_frame = (
                self.smoke_particles.fade_start_frame)
            self.fire_smoke_particles.fade_end_frame = (
                self.smoke_particles.fade_end_frame)
        except Exception as exc:
            print(f"[viewer] Smoke particles disabled: {exc}")
            self.particle_shader = None
            self.smoke_flipbook  = None
            self.smoke_particles = None
            self.fire_smoke_particles = None

        # Fire: animated BILLBOARD (one looping quad per HP_Fire
        # emitter), NOT a particle system.  A burning hulk's flames
        # are anchored at fixed hardpoints and just animate in place;
        # a particle stream gave us flames floating upward and
        # away, which read as "engine exhaust" not "tank on fire".
        # See particles.py / AnimatedBillboard for the rendering
        # model: same ParticleShader as smoke, but each emitter is
        # a single permanent looping quad fed `(time + offset) %
        # lifetime` for its flipbook frame.
        self._splash_status('Loading fire flipbook...')
        self.fire_flipbook   = None
        self.fire_billboards = None
        if self.particle_shader is not None:
            try:
                fire_dir = os.path.join(
                    os.path.dirname(os.path.dirname(__file__)),
                    'resources', 'fire')
                self.fire_flipbook   = FlipbookTexture(fire_dir)
                self.fire_billboards = AnimatedBillboard(self.fire_flipbook)
                # Burning-tank-tuned defaults; Fire Size / Fire FPS
                # sliders override these live.
                self.fire_billboards.size        = 1.6   # m
                self.fire_billboards.fps         = 30.0  # frames/sec
                self.fire_billboards.sync_jitter = 0.7   # s offset between emitters
                # Restore persisted slider values.  We renamed the
                # config keys when we switched from the particle path
                # (fire_start_size / fire_speed) to the billboard
                # path (fire_size / fire_fps); fall back to the old
                # keys so a previously-running install carries its
                # tuning forward.
                self.fire_billboards.size = float(
                    self._cfg.get('fire_size',
                        self._cfg.get('fire_start_size',
                                       self.fire_billboards.size)))
                self.fire_billboards.fps  = float(
                    self._cfg.get('fire_fps',  self.fire_billboards.fps))
            except Exception as exc:
                print(f"[viewer] Fire billboards disabled: {exc}")
                self.fire_flipbook   = None
                self.fire_billboards = None

        # Fire spawn points -- populated by load_vehicle when the
        # damaged variant is loaded; empty otherwise.  Same dict shape
        # as `_exhaust_points` ({component, name, pos, fwd}) so the
        # particle system can reuse `set_emitters` unchanged, but the
        # `fwd` vector is always (0, 1, 0) -- fire goes UP regardless
        # of the artist's HP_Fire_* node orientation.
        self._fire_points = []

        # Skybox / environment cubemap
        self._splash_status('Building skybox + IBL maps (irradiance, BRDF, prefilter)...')
        _env_dir  = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                 'resources', 'environment_maps')
        _x_file   = os.path.join(_env_dir, 'cube_model.x')
        self.skybox      = None
        self.show_skybox = True
        try:
            self.skybox = Skybox(_x_file, _env_dir)
        except Exception as exc:
            print(f"[viewer] Skybox not loaded: {exc}")

        # Procedural ground.  Generated once at startup; kept hidden
        # by default so a clean tank screenshot still works.  Toggle
        # is the `Terrain` button in the left-panel UI section.
        #
        # Source priority:
        #   1. config['terrain_heightmap'] -- on-disk grayscale image
        #      that we resample + smooth into a height grid.  This
        #      is the preferred path -- gives the user full control
        #      over the surface shape via any image editor.
        #   2. Falls back to the procedural Perlin-fBm generator if
        #      no image is configured (or the configured path is
        #      missing / undecodable).
        self._splash_status('Generating procedural terrain...')
        self.terrain        = None
        self.terrain_shader = None
        try:
            self.terrain_shader = TerrainShader()
            # Image path can come from the config or the bundled
            # default location (resources/heightmap.png).  When
            # neither exists, fall through to the Perlin path.
            cfg_img = (self._cfg.get('terrain_heightmap') or '').strip()
            default_img = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'resources', 'heightmap.png')
            terrain_img = None
            if cfg_img and os.path.isfile(cfg_img):
                terrain_img = cfg_img
            elif os.path.isfile(default_img):
                terrain_img = default_img

            # Default: 1025-vertex grid (was 257) -- bumped 4x per
            # side so each ~67 cm sand ripple gets ~4 verts across,
            # which is what the detail-displacement companion needs
            # to actually read as ripples instead of an averaged-out
            # smear.  ~1 M verts / ~2 M tris -- comfortable for any
            # remotely modern desktop GPU on a static mesh.
            #
            # 160 m world (4x the original 40 m so the tank reads as
            # inside a real landscape rather than perched on a small
            # island), 3 m macro height range.  base_y stays 0 --
            # both heightmap generators (image + procedural) anchor
            # the lowest sample at y=0 so the tank's tracks sit on
            # flat ground at the world origin without an offset.
            kwargs = dict(seed=0, size=1025, world_size=160.0,
                          height_scale=3.0, base_y=0.0)

            # Sand diffuse texture.  Search order:
            #   1. config['terrain_sand_texture']  (explicit override)
            #   2. resources/sand_painted.png  (procedural -- generated
            #      by `cust_tools/paint_sand_desert.py`)
            #   3. resources/sand.png  (user-supplied photo / external)
            # Tiled at 50 m per repeat so the 160 m terrain shows
            # ~3 tile-cycles across -- dense enough that distant
            # detail doesn't smear, sparse enough that the seam
            # direction doesn't dominate.
            res_root = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'resources')
            cfg_sand     = (self._cfg.get('terrain_sand_texture') or '').strip()
            painted_sand = os.path.join(res_root, 'sand_painted.png')
            photo_sand   = os.path.join(res_root, 'sand.png')

            sand_path = None
            if cfg_sand and os.path.isfile(cfg_sand):
                sand_path = cfg_sand
            elif os.path.isfile(painted_sand):
                sand_path = painted_sand
            elif os.path.isfile(photo_sand):
                sand_path = photo_sand
            if sand_path:
                kwargs['sand_path']      = sand_path
                kwargs['sand_tile_size'] = float(
                    self._cfg.get('terrain_sand_tile_size', 50.0))
                print(f"[viewer] Terrain sand: {sand_path}")

            # Detail-displacement heightmap: companion to the sand
            # colour texture.  When `paint_sand_desert.py` runs it
            # writes both a colour PNG and a `<name>_height.png`
            # grayscale next to it; we auto-pair them so the
            # geometry ripples line up with the colour texture
            # ripples without the user having to configure anything.
            cfg_det = (self._cfg.get('terrain_detail_heightmap') or '').strip()
            painted_det = os.path.join(res_root, 'sand_painted_height.png')
            detail_path = None
            if cfg_det and os.path.isfile(cfg_det):
                detail_path = cfg_det
            elif sand_path and sand_path.lower().endswith('_painted.png'):
                # Pair-by-suffix: explicit user override wins; otherwise
                # use the painted-height companion when sand_painted.png
                # is the active colour.
                if os.path.isfile(painted_det):
                    detail_path = painted_det
            elif os.path.isfile(painted_det):
                detail_path = painted_det
            if detail_path:
                kwargs['detail_image_path']   = detail_path
                kwargs['detail_tile_size']    = float(
                    self._cfg.get('terrain_detail_tile_size',
                                   kwargs.get('sand_tile_size', 50.0)))
                kwargs['detail_height_scale'] = float(
                    self._cfg.get('terrain_detail_height_scale', 0.05))
                print(f"[viewer] Terrain detail: {detail_path} "
                      f"(±{kwargs['detail_height_scale']:.2f} m)")
            if terrain_img:
                self._splash_status(
                    f'Loading terrain from {os.path.basename(terrain_img)}...')
                kwargs['image_path']         = terrain_img
                kwargs['image_smooth_sigma'] = float(
                    self._cfg.get('terrain_smooth_sigma', 1.0))
                kwargs['image_edge_fade']    = float(
                    self._cfg.get('terrain_edge_fade', 0.10))
                kwargs['image_curve_gamma']  = float(
                    self._cfg.get('terrain_curve_gamma', 1.0))
                print(f"[viewer] Terrain source: {terrain_img}")
            self.terrain = Terrain(**kwargs)
        except Exception as exc:
            print(f"[viewer] Terrain disabled: {exc}")
            self.terrain        = None
            self.terrain_shader = None

        # Triangle picker (Tools -> Pick Tri).  Lazily constructs its
        # FBO + shaders on the first frame the picker is enabled, so
        # there's no startup cost when the feature isn't in use.
        from .picker import TrianglePicker
        self.picker = TrianglePicker()

        # Tank-on-terrain physics.  When the Terrain toggle is on
        # AND a tank is loaded, this samples per-wheel terrain Y
        # each frame and produces a chassis pose (translate + tilt)
        # that places the tank visually on the ground.  Hardcoded
        # to T110E4 wheel geometry for now; tank physics enabled
        # unconditionally when terrain is up so the tank doesn't
        # float over the sand.  Toggleable in code via
        # `self.tank_physics_enabled = False` if you want the old
        # "tank centred at world origin" behaviour back.
        from .tank_physics import TankPhysics
        self.tank_physics         = TankPhysics.for_t110e4()
        # User-controlled toggle.  Lives on the right-panel "Susp"
        # checkbox so Professor Coffee can flip the test on / off
        # without restarting the app.  Persisted under
        # `suspension_test` in `tankExporterPy.json`.  Default
        # OFF so the tank loads at world origin like before; flip
        # the checkbox to engage per-wheel terrain conformance.
        self._suspension_test     = bool(
            self._cfg.get('suspension_test', False))
        self.tank_physics_enabled = self._suspension_test

        # ---- Drive speed control (keys 1..9 + 0) ---------------------
        # Stepped speed selector for the arrow-key tank drive.  Steps:
        #   0 = stopped (default)
        #   1 = current tank's max forward speed (kph)
        #   9 = 0.1 kph (creep)
        #   2..8 = linear interpolation between 1 and 9.
        # `_top_speed_kph` is parsed per-tank from the gameplay XML's
        # <speedLimits><forward> when load_vehicle wires it up; falls
        # back to 50 kph when the parse misses.  Conversion to scene
        # units assumes 1 unit = 1 yard (TEPY's terrain scale -- the
        # heightmap's quad is 256 yards on a side).
        self._top_speed_kph    = 50.0    # default; overridden per load
        self._speed_step       = 8       # 0..9, 0 = stopped, 9 = 0.5 kph creep
                                          # Default 8 = a slow drive feel
                                          # (~5 kph at the 50 kph default
                                          # cap) so the tank moves at a
                                          # readable pace the moment
                                          # the user hits W on a fresh
                                          # session.  0 was the old
                                          # default and made the tank
                                          # feel inert at startup.
        # Contact-wheel red highlight toggle.  Mirrors `tank_physics
        # _enabled` by default so the moment Susp goes on, the four
        # corner wheels light up red -- making it visually obvious
        # which wheels are anchoring the plane fit.  H key toggles
        # it independently in case the highlight is distracting
        # while inspecting wheel geometry.
        self._highlight_contacts = True

        # ---- Camera mode (cycle with `C`) ------------------------------
        # 0 = orbit (default trackball, mouse-driven, free-fly)
        # 1 = driver-side chase camera (anchored behind + above + driver
        #     side of the chassis, rotates with chassis_pose so it
        #     follows pitch / roll / yaw) -- DEFAULT at startup + load
        # 2 = commander view (anchored at the centre of the hull
        #     above the chassis, looking forward along the tank's
        #     visible-front axis; turret + gun meshes are hidden in
        #     this mode so the commander has a clear view-out)
        #
        # `camera_mode` is NOT persisted and is touched by EXACTLY
        # TWO sites (per 2026-05-08 user rule):
        #   * `load_vehicle` (the tank-tree pick path)  -> reset to 1
        #   * the `C` key handler in handle_input       -> cycle (mod 3)
        # The assignment below is the ctor's once-only initial value;
        # the runtime never touches camera_mode anywhere else.
        self.camera_mode    = 1
        self._N_CAMERA_MODES = 3

        # Commander head rotation (mode 2 only).  Driven by LEFT-drag
        # while in commander seat -- rotates the look-direction within
        # CHASSIS-LOCAL space, so "looking left" stays left-of-tank as
        # the tank turns (the chassis yaw is applied on top via the
        # chassis_matrix multiplication in _anchored_view_matrix).
        # Reset to 0 / 0 (looking straight forward, level) on every
        # entry into commander mode -- see the C key handler.
        self._head_yaw_deg   = 0.0    # +ve = look right of forward
        self._head_pitch_deg = 0.0    # +ve = look up

        # Chase orbit state (mode 1 only).  Same idea as the commander
        # head rotation: stored in CHASSIS-LOCAL space so the chase
        # camera tracks the chassis yaw automatically -- if the user
        # drags to look at the tank's right side, that orientation
        # PERSISTS through tank turns (eye stays right-of-tank no
        # matter how the tank yaws in world space).
        #
        #   _chase_yaw_deg = 0  -> eye directly BEHIND the tank
        #                          (behind = +Z chassis-local, since
        #                          tank front = -Z chassis-local)
        #   _chase_pitch_deg    -> elevation above the chassis Y plane
        #   _chase_distance     -> zoom
        self._chase_yaw_deg   = 0.0
        self._chase_pitch_deg = 25.0
        self._chase_distance  = 6.0

        # ---- Auto circle-drive (`O` toggles) ---------------------------
        # When True, the tank ignores the arrow keys and instead
        # drives in a circle of radius `_auto_circle_radius` at the
        # CURRENT speed step.  Yaw rate is derived so the tank
        # always faces along the tangent: omega = v / R.  Useful
        # for hands-free demo / suspension testing -- you can watch
        # the chassis tilt cleanly while the tank pulls a steady
        # arc around the heightmap centre.
        self._auto_circle        = False
        self._auto_circle_radius = 25.0   # metres

        # Current forward velocity (m/s, signed in the render-Z
        # convention -- negative = visible forward).  Ramped each
        # frame toward the target speed using `_DRIVE_ACCEL` /
        # `_DRIVE_DECEL`, so the tank spools up and brakes
        # smoothly instead of snapping to step speed.  Persists
        # across pause / cruise toggles so re-engaging picks up
        # where the inertia left off.
        self._current_forward    = 0.0
        # Scene units are METRES (chassis primitives + gameplay XML
        # both use metres).  Earlier yard-conversion was off by ~9%.
        # 1 kph = 1000 / 3600 m/s = 0.2778 m/s = 0.2778 units/s.
        self._KPH_TO_UNITS_PER_S = 1000.0 / 3600.0
        # Backwards-compat alias used by older comments / log lines
        # that still reference yds/s.  Same value as the metres
        # conversion (1 unit = 1 metre).
        self._KPH_TO_YDS_PER_S   = self._KPH_TO_UNITS_PER_S

        # Toggleable display flags
        self.show_grid    = False
        self.show_axes    = False
        self.show_skybox  = False
        self.show_light   = True
        self.show_terrain = bool(self._cfg.get('show_terrain', False))
        self.wireframe    = False
        # Cheap-Phong fallback toggle.  When True, the WoT mesh path
        # uses the same simple diffuse + bump shader the FBX import
        # path uses (no PBR / IBL / GMM / damage layer / armour
        # tint).  Useful for diagnostic A/B comparison or when the
        # PBR pipeline misbehaves on a particular tank's materials.
        # Wired to the 'Shaded' button in the Model group; runtime
        # state only -- not persisted because it's a "look at this
        # for a moment" toggle, not a session preference.
        self.shaded_mode  = False
        self.use_normal_map = True
        # Re-entrancy guard.  True from the moment the user picks a
        # tank to the moment that load returns (success OR error);
        # tree clicks made while True are dropped so the user can't
        # queue up a second load on top of an in-flight one.
        # Cleared in `_load_tank_with_options`'s try/finally.
        self._tank_loading = False
        # Source type of the currently loaded scene:
        #   None  -- nothing loaded
        #   'wot' -- loaded via load_mesh / load_vehicle (full PBR pipeline,
        #            GMM / AO routing / alpha-test / nation tint apply)
        #   'fbx' -- loaded via load_imported_payload (simple diffuse +
        #            bump shader, no PBR specials, no nation tint)
        # Read by render() to pick the right shader and gate features that
        # only make sense for native WoT data.
        self.source_type = None
        # When source_type == 'fbx', we may have learned the original WoT
        # tank XML name (from the FBX filename or an embedded custom
        # property).  Set so the exhaust-emitter lookup can pull the
        # matching def-XML / visual-file hardpoints from the user's WoT
        # install.  None = unknown / no lookup possible.
        self.source_tank_name = None
        # Three-light setup: stationary by default, "Orbit" button toggles.
        # Lights are placed at 120° apart on a horizontal ring of radius
        # LIGHT_RADIUS at height LIGHT_HEIGHT.  When orbit is on, the
        # ring rotates; when off, the lights sit at angle offsets 0/120/240°.
        self.orbit_lights = False
        self.LIGHT_HEIGHT = 10.0
        self.LIGHT_RADIUS = 10.0
        self.NUM_LIGHTS   = 3

        # 2-D menu bar
        self.ui = UIManager()
        # Slider / checkbox widget references (set by _build_ui)
        self._metal_slider       = None
        self._shine_slider       = None
        self._smoke_start_slider    = None
        self._smoke_end_slider      = None
        self._smoke_speed_slider    = None
        self._smoke_fade_slider     = None
        self._smoke_fade_end_slider = None
        self._fire_size_slider      = None
        self._normals_slider        = None
        self._normals_mode_cb       = None
        self._invert_metal_cb    = None
        self._invert_shine_cb    = None
        # Master "Debug" checkbox.  When checked, EVERY on-screen
        # debug overlay lights up: HP markers, fire-card outlines,
        # and anything else we add later.  The convention going
        # forward: if you're rendering geometry that's only useful
        # for diagnosing what's loaded (NOT something a normal user
        # ever wants to see), gate it on `self._debug`.  Persisted
        # in config under `debug`.  Default off.
        self._debug_cb           = None
        # Suspension-test checkbox.  Mirrors / drives
        # `self._suspension_test` and (via the per-frame mirror in
        # render()) `self.tank_physics_enabled`.
        self._suspension_cb      = None

        # Master debug-overlay flag.  Gated by the right-panel
        # `Debug` checkbox; mirrored into / out of `self._debug_cb`
        # each frame so the checkbox state and rendering decisions
        # stay in sync.  When True, every on-screen debug overlay
        # lights up (HP markers, fire-card outlines, ...).
        # Backward compat: pre-v1.50 used two separate keys
        # (`show_hardpoints`, `show_fire_cards`).  If either was
        # true in the old config, we start in debug mode so the
        # user doesn't lose their preference.  cleanup() drops the
        # legacy keys on save.
        self._debug = bool(
            self._cfg.get('debug', False)
            or self._cfg.get('show_hardpoints', False)
            or self._cfg.get('show_fire_cards', False))

        # Per-engine-class smoke / fire settings.  Loaded from config
        # (each class is a dict of float fields) and merged with the
        # built-in defaults so a missing field falls through to the
        # default rather than crashing.  `self._active_engine_class` tracks
        # which class's values are currently mirrored into the
        # sliders -- it's auto-wired to the loaded tank's
        # `<exhaust><pixie>` value by load_vehicle / load_mesh, so
        # tweaks the user makes while a gas_small tank is on screen
        # save into 'gas_small' rather than bleeding into 'diesel_large'
        # when a heavy is loaded next.
        self._smoke_groups = self._merge_group_dict(
            self._cfg.get('smoke_groups', {}) or {},
            self._SMOKE_GROUP_DEFAULTS)
        self._fire_groups  = self._merge_group_dict(
            self._cfg.get('fire_groups', {}) or {},
            self._FIRE_GROUP_DEFAULTS)
        # One-time migration: a config written by v1.45 or older
        # carries flat single-slot keys ('smoke_start_size',
        # 'smoke_end_size', 'smoke_speed', 'smoke_fade_start_frame',
        # 'smoke_fade_end_frame', 'fire_size').  Fold them into the
        # 'gas_medium' slot of the new dicts so the user's tuning
        # isn't thrown away when v1.46+ first runs against an old
        # JSON.  cleanup() drops the legacy keys from self._cfg so
        # the next save writes the new structure cleanly.
        self._migrate_legacy_smoke_fire_config()
        # Default editing target = the fallback class (gas_medium)
        # until a tank loads and tells us its real engine class.
        self._active_engine_class = self._DEFAULT_PIXIE_CLASS

        # ---- TEPY UI language ----------------------------------------
        # Read the user's chosen language from config (default 'en')
        # and wire `_()` to that catalog BEFORE _build_ui runs --
        # button / slider / checkbox labels resolve through `_()`
        # at construction time, so the catalog has to be active
        # first.  Changing the language mid-session does NOT
        # retro-translate already-built widgets; the picker dialog
        # tells the user it takes effect on next launch.
        from .localization import set_active_language as _set_lang
        cfg_lang = (self._cfg.get('language', '') or 'en').strip() or 'en'
        _set_lang(cfg_lang)

        self._build_ui()

        # Info-panel collapse state -- restore from config and register the
        # toggle callback so a click on the spine re-runs layout + persists.
        self.ui.info_collapsed    = bool(self._cfg.get('info_panel_collapsed', False))
        self.ui.info_panel_full_w = self.INFO_PANEL_W
        self.ui.on_info_toggle    = self._on_info_panel_toggled

        # PKG extractor: created eagerly so the tank-browser tree can be
        # populated before any mesh is loaded.  Reused by load_mesh /
        # load_vehicle later.
        self._splash_status('Indexing WoT packages (pre-warming archives)...')
        self._pkg_extractor = None
        self._init_pkg_extractor_early()

        # Armor-colour lookup -- reads WoT's base_paints.xml via the
        # PkgExtractor on first use to get the authoritative per-nation
        # default tint colours plus the special-edition exclusion list.
        # Falls back to a hardcoded table when the extractor isn't
        # configured (no WoT install pointed to yet).
        from .armor_colors import ArmorColorLoader
        self._armor_loader = ArmorColorLoader(self._pkg_extractor)

        # Localization: read WoT's `.mo` catalogs (e.g.
        # `usa_vehicles.mo`) so we can resolve every tank's
        # `userString` reference (`#usa_vehicles:A37_M40M43`) to its
        # friendly localized name ("M40/M43").  WoT swaps the `.mo`
        # files when the user changes client language, so whatever's
        # currently on disk is what the user expects to see.  Used
        # by the tree builder when `tanks.txt` is missing AND by
        # the tank-list rebuild to enrich `tanks_index.txt`.
        from .localization import WoTLocalizer
        cfg_pkg_dir = (self._cfg.get('pkg_dir', '') or '').strip()
        wot_root_for_locale = (
            os.path.dirname(os.path.dirname(cfg_pkg_dir))
            if cfg_pkg_dir else None)
        self._localizer = WoTLocalizer(wot_root_for_locale)

        # Shared texture cache (abspath -> GLuint).  Used for textures that
        # are identical across many sub-meshes -- chiefly the detail/scratch
        # noise map, which every tank reuses from
        # vehicles/russian/Tank_detail/Details_map.dds.  Without this we'd
        # DDS-decode and GPU-upload the same bytes 7-30+ times per vehicle.
        # Owned by the viewer; freed in cleanup() (NOT by Mesh.cleanup, see
        # mesh.py -- detail_tex_id is intentionally absent from its delete list
        # since the texture is shared).
        self._shared_tex_cache = {}

        # Tank-thumbnail resolution cache (xml_basename -> png_path|None).
        # Populated lazily by _thumb_path_for_xml.
        self._thumb_basenames  = None    # set of available PNG basenames
        self._thumb_path_cache = {}

        # Tank-Exporter display-name lookup (xml_basename -> display name).
        # Populated by _build_tree_panel from tanks.txt; read by the
        # thumbnail setters so the label above the thumb can be set
        # whether the load came from the tree or the command line.
        self._tanks_display_map = {}

        # Per-tier cached trees (built once at startup; tab clicks just
        # swap pointers).  Initialised to an empty list so any
        # _on_tier_tab_changed event that races startup is a no-op.
        self._tier_tree_cache = []

        # ---- Bring the UI shell up FIRST -------------------------------
        # The tier-tree build below takes 1-2 seconds; without the layout
        # done first, the user stares at a blank window throughout.
        # Order matters here:
        #   1. info panel (empty UITreeView -- needed so _on_resize has
        #      a target on the left)
        #   2. tab bar   (the amber-progress highlight in step 4 lives
        #      here, so it must exist before render starts firing)
        #   3. _on_resize -- positions every widget, sets the panel
        #      background rects, finalises the camera viewport
        # After this point render() draws a fully laid-out empty UI.
        self._build_info_panel(None)
        self._build_tab_bar()
        self._on_resize(self.width, self.height)

        # ---- Now do the slow work with the UI visible ------------------
        # Each tier-tree build pumps events + renders so the user sees
        # the tab being highlighted amber as it's built.  _build_all_tier_trees
        # detects the existing tab_bar and skips the lazy-build.
        self._splash_status('Building tank-browser tree (per tier, per nation)...')
        self._build_all_tier_trees()

        # Pre-warm caches that USED to lazy-load on the first
        # load_vehicle call.  Without this, the very first tank load
        # paid the full extract+BWXML-decode cost of:
        #   - WoT base_paints.xml (ArmorColorLoader.ensure_loaded)
        #   - 5 shared component XMLs per nation (engines.xml,
        #     radios.xml, fuelTanks.xml, guns.xml, shells.xml) inside
        #     VehicleXMLLoader._shared_xml_cache
        # Subsequent loads were instant because the caches were warm.
        # Moving the work here means the user pays it ONCE during the
        # already-expected splash wait instead of stalling mid-load.
        self._splash_status('Pre-warming armor-paint + component XML caches...')
        try:
            self._prewarm_first_load_caches()
        except Exception as exc:
            print(f"[viewer] cache pre-warm failed: {exc}")

        self._splash_status('Almost ready...')

        # Auto-prompt for paths on first run / if the configured path is
        # missing or invalid.  The dialog opens; the user can save and
        # the tree refreshes; or cancel and run with no tree.
        if not self._paths_are_configured():
            self._show_paths_dialog()

        # Nation armor color (set by load_vehicle, cleared by load_mesh)
        # Stored as linear float (r, g, b) tuple or None.
        self._armor_color = None

        # Meshes and input state
        self.meshes      = []
        # Exhaust spawn points discovered on the loaded vehicle (populated
        # in load_vehicle by VisualLoader.find_exhaust_nodes, filtered
        # against the def XML's authoritative <nodes> list).
        # Each entry: {component, name, pos (np.float32[3]), fwd (np.float32[3])}.
        # Used by the upcoming particle system for flipbook smoke spawn.
        self._exhaust_points = []
        # Pixie preset name from <exhaust><pixie>...</pixie></exhaust> in
        # the def XML.  Examples: 'gas_medium', 'diesel_medium',
        # 'diesel_large'.  Null when the def XML has no exhaust block.
        # Will eventually drive smoke colour / density / lifetime.
        self._exhaust_pixie  = None
        self.mouse_last  = [0, 0]
        self.frame_count = 0
        self.running     = True
        self._scene_bbox         = None   # set in load_mesh; used by 'R' camera reset
        self._min_zoom_distance  = 0.5   # updated after mesh load to 5 % of mesh radius

        # ---- Frame timing ---------------------------------------------
        # We measure FPS ourselves rather than relying on pygame's
        # `Clock.tick` machinery -- pygame's number is the rate-cap
        # we asked for (60 with vsync), not what we actually achieved.
        # Our reading is wall-clock time between successive
        # `pygame.display.flip()` returns, averaged over a ring of
        # the last ~120 frames (~2 s at 60 Hz, smooths out one-frame
        # hiccups without lagging the display).
        #
        # ON RENDER-DONE SIGNALS in OpenGL / pygame:
        #   * `pygame.display.flip()` -- with vsync on (the SDL
        #     default for DOUBLEBUF), flip blocks until the next
        #     vertical refresh.  By the time it returns the previous
        #     frame's swap has been scheduled.  This is what we use
        #     for "frame done" timing here -- closest to what the
        #     USER perceives as a presented frame.
        #   * `glFinish()` -- explicit, blocks the CPU until every
        #     queued GPU command has completed.  Stronger sync than
        #     flip's "swap scheduled".  Stalls the pipeline; only call
        #     it when you specifically need a hard sync point (e.g.
        #     to time GPU work for a profiler).
        #   * `glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0)` +
        #     `glClientWaitSync` -- non-blocking; insert a fence after
        #     your draw calls, query/wait it on a later frame to know
        #     when the GPU finished THAT frame.  Modern engines use
        #     this for accurate GPU-time profiling without a stall.
        # We're going with option 1 -- it answers the question every
        # user actually cares about ("is the app running smoothly?").
        # Block-average over 5 consecutive frames.  Each frame's wall
        # time goes into `_fps_accum_ms`; on the 5th frame we divide
        # to get the mean, refresh the caption, and reset both
        # counter and accumulator to zero for the next block.  In
        # between blocks (counter < 5) the caption stays put -- the
        # display only updates 12x/sec at 60 fps, which is faster
        # than a human reads a number anyway.
        self._fps_block_count = 0      # 0..4 -- frames accumulated this block
        self._fps_accum_ms    = 0.0    # running sum of frame times
        self._fps_display     = 0.0    # last published average for the caption
        self._fps_avg_ms      = 0.0    # last published mean ms / frame
        self._last_frame_end  = time.perf_counter()

        # ---- GPU-time profiling (GL_TIME_ELAPSED queries) -----------------
        # Ping-pong pool of two GL timer queries.  Each frame we
        # BEGIN/END one of them; on the next frame we READ the
        # OTHER one's result.  By that point the GPU is guaranteed
        # to have finished frame N-1's work, so the read never
        # stalls.  The reported number is true GPU work time --
        # independent of any vsync stall, so wall-clock vs GPU ms
        # makes "am I CPU-bound or vsync-bound?" trivially readable.
        #
        # Queries are lazy-init: GL context exists by the time
        # `render()` runs but not in `__init__`, so we create them
        # on the first render call instead.  Failed init (driver
        # without GL 3.3 timer queries) silently disables the path
        # -- `_gl_query_ids` stays None and every accessor short-
        # circuits.
        self._gl_query_ids       = None        # [qid_a, qid_b] | None
        self._gl_query_idx       = 0           # index of THIS frame's query
        self._gl_query_in_flight = [False, False]
        self._gpu_accum_ms       = 0.0         # parallel block accumulator
        self._gpu_avg_ms         = 0.0         # last published GPU ms

        # Previous frame's measured wall-clock time, fed back as the
        # particle-integration `dt` for the next frame.  Initialised
        # to one 60 Hz frame so the very first iteration doesn't
        # advance simulation by zero (particles would never spawn).
        self._prev_frame_ms      = 1000.0 / 60.0
        # Computed in run() each frame -- read by render() (notably the
        # particle system) for time-step-correct integration.  Initial
        # value of 0 means the first frame does no simulation step.
        self._frame_dt      = 0.0

        # filepath is optional -- when omitted, the viewer starts with an
        # empty scene and the user picks a tank from the tree panel.
        if filepath:
            if filepath.lower().endswith('.xml'):
                self.load_vehicle(filepath)
            else:
                self.load_mesh(filepath)

        # Startup summary line for the in-app console.  Concise -- the
        # full verbose breakdown still streams to stdout above this.
        try:
            n_pkg_entries = (len(self._pkg_extractor._file_to_pkg)
                             if (self._pkg_extractor is not None
                                 and getattr(self._pkg_extractor,
                                             '_file_to_pkg', None))
                             else 0)
        except Exception:
            n_pkg_entries = 0
        self.log_status('Startup')
        self.log(f"Tank Exporter PY v{self._app_version} -- ready.")
        if n_pkg_entries:
            self.log(f"PkgExtractor: {n_pkg_entries:,} entries indexed")
        else:
            self.log_error("PkgExtractor not configured -- open Set Paths")

        # Tkinter availability probe.  Every modal dialog in TEPY
        # (Set Paths file pickers, Import / Export, Language picker,
        # FBX upgrade popup) routes through `tkinter.filedialog` /
        # `tkinter.messagebox`.  If the user's Python install is
        # missing the `tcl/tk and IDLE` optional component, those
        # dialogs all fail with an ImportError that previously
        # surfaced only as a stdout print.  Surface it in the
        # in-app console too so a tester running off `go.bat` sees
        # the diagnosis without alt-tabbing to cmd.
        try:
            import tkinter as _tk_probe   # noqa: F401
        except ImportError as exc:
            self.log_error(
                "tkinter missing from this Python install -- "
                "Set Paths / Import / Export / Language pickers "
                "WILL NOT OPEN.")
            self.log_error(
                f"   underlying error: {exc}")
            self.log_error(
                "   Fix: re-run the python.org installer, choose "
                "Modify, and check the 'tcl/tk and IDLE' option.")
            self.log_error(
                "   Or in Windows Settings > Apps, find Python > "
                "Modify > check 'tcl/tk and IDLE' > Repair.")

        # Auto-rebuild TheItemList.xml when it's missing.  The file is
        # .gitignored (70+ MB, machine-specific to the user's WoT
        # install) so a fresh checkout always lands here on first run.
        # Done LAST in __init__ so the UI is fully up by the time we
        # start streaming "scanning N pkg archives" into the console
        # -- otherwise the user just sees a hang on the splash.
        try:
            self._ensure_itemlist()
        except Exception as exc:
            self.log_error(f"ItemList auto-rebuild failed: {exc}")

        # End-of-preload: restore the OS window chrome (title bar
        # + resize frame + min/max/close).  Was being done in run()
        # AFTER splash cleanup, but on Windows that left the chrome
        # restoration RACING the SW_MAXIMIZE call -- the WM
        # SetWindowPos / WM_NCCALCSIZE messages hadn't drained
        # before we asked the OS to maximise, so the maximise
        # took effect on a popup-style window and the chrome
        # never came back visibly.
        #
        # Doing the chrome restore at the end of __init__ gives
        # the message loop several ms to process the style change
        # while the splash is still on screen.  Splash teardown
        # in run() then sees an already-chromed window and the
        # subsequent SW_MAXIMIZE keeps the chrome.
        try:
            self._set_window_borderless(False)
            # Pump pending WM messages so the WM_NCCALCSIZE +
            # WM_NCPAINT triggered by the style change can
            # process before we transition out of __init__.
            pygame.event.pump()
        except Exception as exc:
            print(f"[viewer] end-of-preload chrome restore failed: {exc}")

    # ------------------------------------------------------------------
    # Per-group smoke / fire settings helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_group_dict(persisted, defaults):
        """Merge a persisted-config group dict with built-in defaults.

        Each `defaults` entry is `{group_name: {field: float, ...}}`.
        Missing groups OR missing fields in the persisted dict fall
        through to the defaults so a partial config never leaves a
        slider with NaN.  Field values are coerced to float.

        Args:
            persisted (dict): user's saved group settings (may be empty)
            defaults  (dict): built-in defaults (the fallback table)

        Returns:
            dict: merged result -- always has every group + every field.
        """
        out = {}
        for g, dflt in defaults.items():
            slot = persisted.get(g, {}) or {}
            out[g] = {f: float(slot.get(f, dflt[f])) for f in dflt}
        return out

    # Legacy single-slot keys written by v1.45 and older.  Listed
    # here so both _migrate_legacy_smoke_fire_config (read-side)
    # and cleanup() (drop-side) can't drift out of sync.  Each tuple
    # is (legacy_key, new_field).
    _LEGACY_SMOKE_KEYS = (
        ('smoke_start_size',       'start_size'),
        ('smoke_end_size',         'end_size'),
        ('smoke_speed',            'speed'),
        ('smoke_fade_start_frame', 'fade_start_frame'),
        ('smoke_fade_end_frame',   'fade_end_frame'),
    )
    _LEGACY_FIRE_KEYS = (
        ('fire_size',       'size'),
        ('fire_start_size', 'size'),  # even older alias from before
                                       # the billboard refactor
    )
    # Pure deletion list -- removed at cleanup() unconditionally
    # whether or not their value got migrated above.
    _LEGACY_DROPPED_KEYS = ('fire_fps',)

    def _migrate_legacy_smoke_fire_config(self):
        """Fold pre-v1.46 flat smoke / fire keys into the new
        per-engine-class dicts so the user's existing tuning
        survives the first run against the new build.

        Migration rules:
            * Each legacy `smoke_*` value lands in
              `self._smoke_groups['gas_medium']` (the fallback
              class) under the new field name.
            * `fire_size` (or older alias `fire_start_size`) lands
              in `self._fire_groups['gas_medium']['size']`.
            * Values are coerced to float; non-numeric / missing
              keys are silently skipped.
            * Legacy keys are NOT removed from `self._cfg` here --
              cleanup() drops them at save time so a crash before
              save still preserves the originals.

        No-op when the new dicts already came back from
        `self._cfg.get('smoke_groups', ...)` -- we treat the
        presence of the new keys as "user has already saved at
        least once with the new layout, don't re-migrate."
        """
        if 'smoke_groups' in self._cfg or 'fire_groups' in self._cfg:
            return
        target_smoke = self._smoke_groups.get(self._DEFAULT_PIXIE_CLASS)
        if target_smoke is not None:
            for legacy, new_field in self._LEGACY_SMOKE_KEYS:
                if legacy in self._cfg:
                    try:
                        target_smoke[new_field] = float(self._cfg[legacy])
                    except (TypeError, ValueError):
                        pass
        target_fire = self._fire_groups.get(self._DEFAULT_PIXIE_CLASS)
        if target_fire is not None:
            for legacy, new_field in self._LEGACY_FIRE_KEYS:
                if legacy in self._cfg:
                    try:
                        target_fire[new_field] = float(self._cfg[legacy])
                        break  # first match wins; don't overwrite
                               # with the older alias
                    except (TypeError, ValueError):
                        pass

    def _persist_all_sliders(self, write_json=True):
        """Single source of truth for slider persistence.

        Snapshots EVERY slider's current value onto `self._cfg`.
        Per-engine-class sliders (smoke / fire) get routed into
        `self._smoke_groups[self._active_engine_class]` /
        `self._fire_groups[self._active_engine_class]` -- the routing key
        is `self._active_engine_class` (the engine-class auto-wired from
        the loaded tank's `<exhaust><pixie>` value, e.g. `gas_small`,
        `diesel_large`).  Global sliders (Light / Ambient / Normals)
        land in flat config keys regardless of engine class.

        Called from three places, all going through this same routine
        so the routing logic can't drift:

            1. Mouse-up after a slider drag (`handle_input`) --
               pass `write_json=True` so tweaks persist instantly.
            2. Per-frame mirror in `render()` -- `write_json=False`
               (no disk I/O on every frame; just keeps the dicts
               in sync so a `_set_active_engine_class` swap doesn't lose
               in-progress edits).
            3. `cleanup()` on window close -- `write_json=False`
               since cleanup writes its own JSON after also
               capturing checkbox state + dropping legacy keys.

        Args:
            write_json (bool): when True, also writes self._cfg to
                disk via _config.save.  Failures are logged, not
                raised -- a missed mid-session save still leaves
                cleanup()'s on-exit save as a fallback.
        """
        # ---- Per-engine-class sliders -- route by self._active_engine_class
        # (the engine level) into the matching slot.
        engine = self._active_engine_class
        g = self._smoke_groups.get(engine)
        if g is not None:
            if self._smoke_start_slider:
                g['start_size']       = float(self._smoke_start_slider.value)
            if self._smoke_end_slider:
                g['end_size']         = float(self._smoke_end_slider.value)
            if self._smoke_speed_slider:
                g['speed']            = float(self._smoke_speed_slider.value)
            if self._smoke_fade_slider:
                g['fade_start_frame'] = float(self._smoke_fade_slider.value)
            if self._smoke_fade_end_slider:
                g['fade_end_frame']   = float(self._smoke_fade_end_slider.value)
        gf = self._fire_groups.get(engine)
        if gf is not None and self._fire_size_slider:
            gf['size'] = float(self._fire_size_slider.value)
        # Re-link the dicts onto self._cfg.  Idempotent when nothing
        # changed; needed when first establishing the link or when
        # a fresh dict was rebuilt by `_merge_group_dict`.
        self._cfg['smoke_groups'] = self._smoke_groups
        self._cfg['fire_groups']  = self._fire_groups

        # ---- Global (non-engine-keyed) sliders -- flat config keys.
        if self._metal_slider:
            self._cfg['light_value']    = float(self._metal_slider.value)
        if self._shine_slider:
            self._cfg['ambient_value']  = float(self._shine_slider.value)
        if self._normals_slider:
            self._cfg['normals_length'] = float(self._normals_slider.value)

        if write_json:
            try:
                _config.save(self._cfg)
            except Exception as exc:
                print(f"[viewer] slider persist write failed: {exc}")

    def _load_active_engine_class(self):
        """Push the active group's stored values onto the sliders."""
        g = self._smoke_groups.get(self._active_engine_class)
        if g is not None:
            if self._smoke_start_slider:
                self._smoke_start_slider.value = g['start_size']
            if self._smoke_end_slider:
                self._smoke_end_slider.value = g['end_size']
            if self._smoke_speed_slider:
                self._smoke_speed_slider.value = g['speed']
            if self._smoke_fade_slider:
                self._smoke_fade_slider.value = g['fade_start_frame']
            if self._smoke_fade_end_slider:
                self._smoke_fade_end_slider.value = g['fade_end_frame']
        gf = self._fire_groups.get(self._active_engine_class)
        if gf is not None and self._fire_size_slider:
            self._fire_size_slider.value = gf['size']

    def _set_active_engine_class(self, group):
        """Switch which engine-class the smoke / fire sliders edit.

        Auto-wired by load_vehicle / load_mesh from the loaded tank's
        `<exhaust><pixie>` value.  Saves the current slider values
        into the previous class's slot (so any in-progress tweaks are
        preserved) and loads the new class's stored values onto the
        sliders.  No-op if `group` is None / unknown / already active.

        Args:
            group (str | None): an engine-class key from
                EXHAUST_PIXIE_CLASSES.  None or an unknown value falls
                back to `_DEFAULT_PIXIE_CLASS` (gas_medium).
        """
        if not group or group not in self._smoke_groups:
            group = self._DEFAULT_PIXIE_CLASS
        if group == self._active_engine_class:
            return
        # Capture the OUTGOING engine class's slider values into its
        # group slot before we re-key.  No JSON write here -- the
        # mouse-up handler already persisted on release; this is just
        # the in-memory snapshot.
        self._persist_all_sliders(write_json=False)
        self._active_engine_class = group
        self._load_active_engine_class()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        """Create the top-bar toggle buttons, PBR sliders, and invert checkboxes."""
        # --- Row 1: toggle buttons (fixed at y=4 regardless of bar height) ---
        x = self.ui.BUTTON_PADDING
        y = 4
        h = 22
        # NOTE on i18n: every visible label is wrapped in `_()`.  The
        # button's `.label` attribute receives the translated string,
        # so `btn_by_label` lookups in `_layout_widgets` need to use
        # the same `_('...')` form.  When the language doesn't change
        # mid-session, `_('Grid')` is deterministic so the lookups
        # work identically to the pre-i18n code.
        # UI display toggles -- viewport-rendering flags (grid /
        # axes lines / sky / lights / wireframe / etc.).  Olive
        # accent in the IDLE state; the existing global burnt-
        # orange "this toggle is ON" colour still wins when active.
        # Categorised separately from the model-tool action
        # buttons (Meshes / Flip / Compare) which get a burnt-
        # orange accent -- see the `Model` group in
        # `_layout_widgets`.
        # UI display toggles -- secondary accent (theme.c2).
        # Default theme's c2 is the original olive; other themes
        # supply their own complementary colour.
        #
        # Toggle entries are (label, attr, theme_slot).  The slot
        # tag determines which theme accent the IDLE state pulls
        # from -- 'c2' for the viewport-display group (Grid / Axes
        # / etc.) and 'c1' for the Model-group toggles (Wireframe /
        # Shaded) so they read as the same category as Meshes /
        # Flip / Compare on the laid-out panel.  Layout placement
        # is owned by `_layout_widgets`; here we just register the
        # buttons + their accent slot so the renderer can tint them
        # correctly even before the first layout pass.
        _UI_OLIVE = _theme.c2()
        _MODEL_C1 = _theme.c1()
        for label, attr, slot in [
            (_('Grid'),      'show_grid',     'c2'),
            (_('Axes'),      'show_axes',     'c2'),
            (_('Light'),     'show_light',    'c2'),
            (_('Orbit'),     'orbit_lights',  'c2'),
            (_('Skybox'),    'show_skybox',   'c2'),
            (_('Terrain'),   'show_terrain',  'c2'),
            # Model-group toggles -- live in the Model group in
            # _layout_widgets but registered here so the standard
            # toggle pipeline (set/sync attr, persist, etc.) covers
            # them like every other on/off state.
            (_('Wireframe'), 'wireframe',     'c1'),
            (_('Shaded'),    'shaded_mode',   'c1'),
        ]:
            initial = getattr(self, attr, False)
            btn      = self.ui.add_button(label, x, y, 70, h, active=initial)
            btn.attr = attr
            btn.accent_color = _MODEL_C1 if slot == 'c1' else _UI_OLIVE
            btn.theme_slot   = slot
            x       += btn.w + self.ui.BUTTON_SPACING

        # --- Action buttons (one-shot, no toggle attr) -------------------
        x       += self.ui.BUTTON_SPACING   # extra gap from toggle group

        # Set Paths -- folder icon left of the label so the user
        # immediately reads the button as "folder/file picker".
        # `📁` (U+1F4C1) lives in Segoe UI Symbol; the icon
        # texture is built via the helper that uses that font
        # specifically because Calibri doesn't carry the glyph.
        set_paths_btn = self.ui.add_button(
            _('Set Paths'), x, y, 84, h, active=False,
            action=self._show_paths_dialog)
        self.ui.set_button_icon(set_paths_btn, '📁')
        x       += 84 + self.ui.BUTTON_SPACING

        # Model tools.  Operate on the loaded mesh set rather than
        # the viewport.  Burnt-orange accent so they read as a
        # different category from the olive UI toggles.  Same
        # palette family, distinct role.
        # Model tools -- primary accent (theme.c1).
        # Default theme's c1 is the original burnt orange; swapping
        # theme rotates this through whatever the new preset
        # supplies (Dracula orange, Solarized yellow, ...).
        _MODEL_BURNT_ORANGE = _theme.c1()

        # 'Meshes' opens / closes the mesh-visibility window.  Always
        # available; population happens at load time.
        meshes_btn = self.ui.add_button(_('Meshes'), x, y, 70, h, active=False,
                                          action=self._toggle_mesh_window)
        meshes_btn.accent_color = _MODEL_BURNT_ORANGE
        meshes_btn.theme_slot   = 'c1'
        x       += 70 + self.ui.BUTTON_SPACING

        # 'Export' opens a Save dialog and spawns Blender (--background)
        # to write FBX / GLB / GLTF / OBJ.  Disabled visually when no
        # tank is loaded -- the action callback also short-circuits.
        # Export button.  Accent: burnt orange (matches the
        # selected-tree-row highlight; "this exits the app's data
        # boundary" cue).
        export_btn = self.ui.add_button(
            _('Export'), x, y, 70, h, active=False,
            action=self._on_export_clicked)
        export_btn.accent_color = _theme.c1()
        export_btn.theme_slot   = 'c1'
        x       += 70 + self.ui.BUTTON_SPACING

        # 'Import' opens a file picker and spawns Blender to read an
        # FBX/GLB/OBJ back into the viewer's scene.  Round-trip companion
        # to Export -- decodes WoT* color attributes and reconstructs the
        # original per-vertex stream.
        # Accent: olive green ("this brings data INTO the app" cue,
        # complementary to Export's burnt orange).
        import_btn = self.ui.add_button(
            _('Import'), x, y, 70, h, active=False,
            action=self._on_import_clicked)
        import_btn.accent_color = _theme.c2()
        import_btn.theme_slot   = 'c2'
        x       += 70 + self.ui.BUTTON_SPACING

        # 'Flip' toggles the active mesh set between FBX (imported) and
        # PKG (WoT-loaded).  No-op when the other set is empty.  Lets
        # the user A/B-compare an import against the in-game data
        # before exporting back out to .primitives_processed.
        flip_btn = self.ui.add_button(_('Flip'), x, y, 70, h, active=False,
                                        action=self._flip_active_set)
        flip_btn.accent_color = _MODEL_BURNT_ORANGE
        flip_btn.theme_slot   = 'c1'
        x       += 70 + self.ui.BUTTON_SPACING

        # 'Compare' opens the side-by-side per-part stats window
        # (face/vert/index counts, UV2 presence, vertex format) so the
        # user can verify the imported FBX matches the PKG data slot
        # for slot before re-export.
        compare_btn = self.ui.add_button(_('Compare'), x, y, 70, h,
                                           active=False,
                                           action=self._on_compare_clicked)
        compare_btn.accent_color = _MODEL_BURNT_ORANGE
        compare_btn.theme_slot   = 'c1'
        x       += 70 + self.ui.BUTTON_SPACING

        # 'Save Prim' opens a component picker (Hull / Chassis /
        # Turret / Gun) and writes the chosen parts back out as
        # WoT-native .primitives_processed files.  Companion to
        # Export (which writes FBX/GLB/OBJ) -- this one targets the
        # game's own format.
        # Accent: burnt yellow.  Slots between Export's burnt orange
        # and Import's olive in the warm-palette trio so the three
        # IO actions read as related but distinct at a glance.
        save_prim_btn = self.ui.add_button(
            _('Save Prim'), x, y, 70, h, active=False,
            action=self._on_save_prim_clicked)
        # Save Prim takes its own slot, theme.c4 -- a "warm but
        # distinct from Export" accent.  Default theme's c4 is
        # burnt yellow (the original Save Prim colour); other
        # themes supply their own (Solarized orange, Dracula
        # purple, Nord aurora purple, ...).
        save_prim_btn.accent_color = _theme.c4()
        save_prim_btn.theme_slot   = 'c4'
        x       += 70 + self.ui.BUTTON_SPACING

        # 'Language' opens a Tk dropdown picker.  Sister to Set Paths
        # in the IO group.  Selection persists in tankExporterPy.json
        # under `language` and applies on next launch.
        self.ui.add_button(_('Language'), x, y, 70, h, active=False,
                           action=self._on_language_clicked)
        x       += 70 + self.ui.BUTTON_SPACING

        # 'Theme' opens a Tk picker listing every preset palette.
        # Phase 1: dropdown with restart-to-apply (paired with the
        # button-accent live updates we already do at startup).
        # Phase 2 will replace this with an in-app scrolling
        # color-pair preview window with live preview on hover.
        self.ui.add_button(_('Theme'), x, y, 70, h, active=False,
                           action=self._on_theme_clicked)
        x       += 70 + self.ui.BUTTON_SPACING

        # 'ItemList' rebuilds TheItemList.xml from scratch by walking
        # every kept pkg under <wot>/res/packages.  Use after a game
        # patch (new tanks / new file kinds) to refresh the lookup
        # table so PkgExtractor hits the O(1) dict path on first try
        # instead of falling back to a multi-pkg scan.  See
        # cust_tools/rebuild_itemlist.py for the underlying tool.
        # Position is the placeholder before _layout_widgets does the
        # real grid placement; the (x, y) here doesn't matter beyond
        # registration.
        self.ui.add_button(_('ItemList'), x, y, 70, h, active=False,
                           action=self._on_rebuild_itemlist_clicked)
        x       += 70 + self.ui.BUTTON_SPACING

        # 'Pick Tri' toggles the off-screen back-buffer triangle picker.
        # Lives in the Tools group so it doesn't bloat the main UI bar.
        # When ON, every frame runs an extra mesh-pass into a hidden
        # FBO encoding (mesh_id, primitive_id) per pixel; the pixel
        # under the cursor is read back and the picked triangle's
        # vertex data dumped to the console.  The same hover triggers
        # an overlay highlight on the live scene.  See picker.py.
        pick_btn = self.ui.add_button(
            _('Pick Tri'), x, y, 70, h, active=False,
            action=self._on_pick_tri_clicked)
        # Tag with the same theme slot as our other diagnostic buttons.
        pick_btn.accent_color = _theme.c1()
        pick_btn.theme_slot   = 'c1'
        # Stash the button so the toggle handler can mutate `active`
        # for the wheat locked-on border to render.
        self._pick_tri_btn = pick_btn
        x       += 70 + self.ui.BUTTON_SPACING

        # ---- Extract group (top of the left panel) ----------------
        # Three buttons that operate on the user's res_mods tree:
        #   * Extract           -- copy chosen tank parts (+ optional
        #                          textures) from pkg into res_mods
        #   * Open Extract Loc  -- open Explorer at res_mods tank root
        #   * Remove            -- delete res_mods tank tree (with
        #                          typed-confirmation safeguard)
        # Layout placement (and the section header) is owned by
        # `_layout_widgets`; here we just register the buttons.
        # Per-button accent tagging (c1 / c2 / c4) so the three
        # res_mods buttons read as a related-but-distinct trio at
        # a glance: Extract = primary action (c1), Open Extract
        # Loc = secondary nav (c2), Remove = warm-but-distinct
        # warning (c4 -- burnt yellow on TEPY Default).  c3 is
        # skipped here because it's the text-on-dark slot (wheat
        # cream on default) and looks washed-out as a button fill.
        # `theme_slot` carries the slot name through to
        # `_apply_theme_live` so the trio re-tints correctly when
        # the user switches preset.
        extract_btn = self.ui.add_button(
            _('Extract'), x, y, 70, h, active=False,
            action=self._on_extract_clicked)
        extract_btn.accent_color = _theme.c1()
        extract_btn.theme_slot   = 'c1'
        x       += 70 + self.ui.BUTTON_SPACING

        open_loc_btn = self.ui.add_button(
            _('Open Extract Loc'), x, y, 70, h, active=False,
            action=self._on_open_extract_loc_clicked)
        open_loc_btn.accent_color = _theme.c2()
        open_loc_btn.theme_slot   = 'c2'
        x       += 70 + self.ui.BUTTON_SPACING

        remove_btn = self.ui.add_button(
            _('Remove from res_mods'), x, y, 70, h, active=False,
            action=self._on_remove_from_resmods_clicked)
        remove_btn.accent_color = _theme.c4()
        remove_btn.theme_slot   = 'c4'
        x       += 70 + self.ui.BUTTON_SPACING

        # --- Sliders + Invert checkboxes --------------------------------
        # Pan slider was removed (baked to 0.20 inside handle_input).
        tx = self.ui.SLIDER_TRACK_X
        tw = self.ui.SLIDER_TRACK_W
        cx = self.ui.SLIDER_CB_X

        # Track centre-y for the slider rows
        cy1, cy2, cy3, cy4, cy5, cy6 = self.ui._SLIDER_CY

        # Initial values come from the persisted config -- the user's last
        # session is restored.  Falls back to _DEFAULTS if absent.
        # NOTE: track_w supplied here is REPLACED by `_layout_widgets`
        # (uses its own L_TRACK_W) every layout pass.  Pass `tw` for
        # consistency with the other sliders, but the on-screen width
        # is whatever L_TRACK_W is set to in `_layout_widgets`.
        light_init   = float(self._cfg.get('light_value',   0.10))
        ambient_init = float(self._cfg.get('ambient_value', 0.50))
        self._metal_slider = self.ui.add_slider(_('Light'),   tx, cy1, tw,
                                                value=light_init,   value_max=0.25,
                                                group_id='lighting')
        self._shine_slider = self.ui.add_slider(_('Ambient'), tx, cy2, tw,
                                                value=ambient_init, value_max=1.0,
                                                group_id='lighting')

        # Smoke particle tunables (drive the smoke ParticleSystem each
        # frame).  Initial slider values come from the per-group
        # settings table -- one set of values for small / medium /
        # large engines.  The radio-checkbox row below selects which
        # group the sliders edit; default editing target is 'medium'
        # (matches `self._active_engine_class` in __init__).  Per-tank live
        # render uses the loaded tank's own group based on its
        # <exhaust><pixie> value -- see _set_active_engine_class / load_vehicle.
        active_smoke = self._smoke_groups[self._active_engine_class]
        active_fire  = self._fire_groups[self._active_engine_class]

        # Fade-range slider value_max scales with the actual loaded
        # flipbook frame count -- otherwise swapping in a shorter set
        # leaves the slider track running out into "no flipbook frame"
        # territory.
        smoke_n_frames = (float(self.smoke_flipbook.frame_count)
                          if self.smoke_flipbook else 91.0)
        self._smoke_start_slider = self.ui.add_slider(
            _('Sm Start'), tx, cy3, tw, value=active_smoke['start_size'],
            value_max=0.5, group_id='smoke')
        self._smoke_end_slider   = self.ui.add_slider(
            _('Sm End'),   tx, cy4, tw, value=active_smoke['end_size'],
            value_max=1.0, group_id='smoke')
        self._smoke_speed_slider = self.ui.add_slider(
            _('Sm Speed'), tx, cy5, tw, value=active_smoke['speed'],
            value_max=8.0, group_id='smoke')
        # Fade range: alpha begins ramping at FadeS, hits zero at FadeE.
        self._smoke_fade_slider     = self.ui.add_slider(
            _('Sm FadeS'), tx, cy6,        tw, value=active_smoke['fade_start_frame'],
            value_max=smoke_n_frames, group_id='smoke')
        self._smoke_fade_end_slider = self.ui.add_slider(
            _('Sm FadeE'), tx, cy6 + 25,   tw, value=active_smoke['fade_end_frame'],
            value_max=smoke_n_frames, group_id='smoke')

        # Fire BILLBOARD slider -- size only.  FPS is hard-coded
        # (30 fps / 91 frames = 3.0 s loop) since we never wanted it
        # exposed; removed in favour of per-engine-class sizes.  Final
        # geometry set in _layout_widgets.
        self._fire_size_slider = self.ui.add_slider(
            _('Fire Size'), tx, cy6 + 75, tw, value=active_fire['size'],
            value_max=4.0, group_id='smoke')

        # Surface-normal debug lines.  Slider drives world-space line
        # length; 0 = off.  Default loaded from the persisted config
        # so the user's preferred setting survives across sessions.
        # Range max 0.5 is plenty for tank-scale meshes -- tracks +
        # smaller equipment look right around 0.05-0.15, hull faces
        # around 0.20-0.40.  Final on-screen position is set in
        # _layout_widgets alongside the smoke sliders.
        normals_init = float(self._cfg.get('normals_length', 0.0))
        self._normals_slider = self.ui.add_slider(
            _('Normals'), tx, cy6 + 50,   tw, value=normals_init,
            value_max=0.5, group_id='smoke')
        # Per-vertex toggle for the normals shader.  Unchecked (default)
        # = by-face mode (one cyan line per triangle from the centroid).
        # Checked = by-vertex mode (3 axes-coloured lines per triangle,
        # one per vertex).  Persisted in config.  cb_size literal here
        # since the cb_size local is defined a few lines further down --
        # 14 px matches every other checkbox in this section.
        self._normals_per_vertex = bool(
            self._cfg.get('normals_per_vertex', False))
        self._normals_mode_cb = self.ui.add_checkbox(
            _('PerVtx'), cx, cy6 + 50 - 7, 14,
            checked=self._normals_per_vertex, group_id='smoke')

        cb_size = 14
        self._invert_metal_cb = self.ui.add_checkbox(
            _('NMap'), cx, cy1 - cb_size // 2, cb_size, checked=True,
            group_id='lighting')
        self._invert_shine_cb = self.ui.add_checkbox(
            _('AO'),   cx, cy2 - cb_size // 2, cb_size, checked=True,
            group_id='lighting')
        # Master Debug checkbox.  Replaces the previous Show HP +
        # Show Fire pair.  When checked, every on-screen debug
        # overlay lights up at once -- see `self._debug` for the
        # convention going forward.  Position finalised in
        # `_layout_widgets`.
        self._debug_cb = self.ui.add_checkbox(
            _('Debug'), cx, cy6 - cb_size // 2, cb_size,
            checked=self._debug, group_id='smoke')
        # Suspension-test toggle.  Drives the per-wheel terrain-
        # conformance physics.  Position finalised in
        # `_layout_widgets`; group_id='smoke' tags it as a right-
        # panel widget so the spine-collapse override leaves it
        # alone (left-vs-right discrimination is by group_id, see
        # PITFALLS / CLAUDE.md).
        self._suspension_cb = self.ui.add_checkbox(
            _('Susp'), cx, cy6 - cb_size // 2, cb_size,
            checked=self._suspension_test, group_id='smoke')

    # ------------------------------------------------------------------
    # Window / GL state
    # ------------------------------------------------------------------

    def _on_resize(self, w, h):
        """Update viewport, camera aspect ratio, and widget positions."""
        self.width  = max(1, w)
        self.height = max(1, h)

        # Console panel (bottom-anchored, between the side panels) eats
        # vertical space the camera would otherwise use.  Re-read its
        # current height every resize so collapse / drag-resize updates
        # immediately reshape the 3D viewport instead of waiting for
        # the next window-resize event.
        console_h = (self.ui.console.height_for_layout()
                     if self.ui.console else 0)

        # Camera viewport spans the height between top-of-window and
        # top-of-console.  Width respects the info-panel collapse spine.
        left_inset = self.ui.info_left_inset(self.INFO_PANEL_W)
        self.camera.width  = max(1, self.width - left_inset - self.TREE_PANEL_W)
        self.camera.height = max(1, self.height - console_h)

        # Position the console: spans the 3D viewport's horizontal
        # band (between the two side panels), anchored to the bottom.
        if self.ui.console:
            self.ui.console.set_geometry(
                left_inset,
                self.height - console_h,
                self.camera.width)

        # ---- Right panel: tab bar + tree on top, smoke controls at bottom
        # The tree shrinks to leave room for the smoke control block.
        tab_h = 0
        if self.ui.tab_bar:
            self.ui.tab_bar.x = self.width - self.TREE_PANEL_W
            self.ui.tab_bar.y = 0
            self.ui.tab_bar.w = self.TREE_PANEL_W
            tab_h = self.ui.tab_bar.HEIGHT
        # Reposition EVERY cached tier tree, not just the live one --
        # otherwise tabs that haven't been viewed since the last resize
        # keep their stale x/y/w/h and visibly jump when clicked.
        # self.ui.tree is one of the cached trees too; the loop below
        # covers it as a side effect.
        # Position every widget inside its panel FIRST -- _layout_widgets
        # computes the actual heights both the LEFT (button block +
        # sliders + checkboxes) AND the RIGHT (sliders + checkboxes)
        # control regions claim, and writes them back into
        # self.LEFT_CONTROLS_H / self.RIGHT_CONTROLS_H.  Every consumer
        # below (info-tree positioning, tank-list tree height, control
        # rects for hit-testing) reads those instance values, so we
        # have to compute them BEFORE the trees and rects are sized.
        self._layout_widgets()

        # Tank-list tree (right panel) -- height fits between the tab
        # bar at the top and the auto-computed slider block at the
        # bottom.
        tree_x = self.width - self.TREE_PANEL_W
        tree_w = self.TREE_PANEL_W
        tree_y = tab_h
        tree_h = max(1, self.height - tab_h - self.RIGHT_CONTROLS_H)
        for cached in getattr(self, '_tier_tree_cache', ()):
            if cached is None:
                continue
            cached.x = tree_x
            cached.y = tree_y
            cached.w = tree_w
            cached.h = tree_h
        # Defensive: handle the (rare) case where self.ui.tree isn't in
        # the cache yet (e.g. cold path during _build_all_tier_trees).
        if self.ui.tree is not None:
            self.ui.tree.x = tree_x
            self.ui.tree.y = tree_y
            self.ui.tree.w = tree_w
            self.ui.tree.h = tree_h

        # ---- Left panel: control block on top, info tree below ----------
        if self.ui.info_panel:
            self.ui.info_panel.x = 0
            self.ui.info_panel.y = self.LEFT_CONTROLS_H
            self.ui.info_panel.w = self.INFO_PANEL_W
            self.ui.info_panel.h = max(1, self.height - self.LEFT_CONTROLS_H)

        # ---- Control-region rects (used by UIManager for backgrounds + hit)
        # When the info-panel is collapsed via the spine, the LEFT
        # controls disappear too -- the whole side becomes just the
        # spine.  Right side is independent of left collapse state.
        if self.ui.info_collapsed:
            self.ui.left_controls_rect = (0, 0, 0, 0)
        else:
            self.ui.left_controls_rect = (
                0, 0, self.INFO_PANEL_W, self.LEFT_CONTROLS_H)
        self.ui.right_controls_rect = (
            self.width - self.TREE_PANEL_W,
            self.height - self.RIGHT_CONTROLS_H,
            self.TREE_PANEL_W,
            self.RIGHT_CONTROLS_H,
        )

        # Hide left-panel widgets when the info-panel is collapsed.
        #
        # Discriminator is `group_id` -- right-panel widgets carry
        # 'smoke' / 'fire' / 'normals' tags assigned at `add_slider`
        # / `add_checkbox` time; everything else is treated as
        # left-panel.  This is ROBUST regardless of where the widget
        # is currently positioned, which matters because the
        # right-panel layout body is SKIPPED when the Debug section
        # is collapsed -- those widgets keep their default x=0
        # (sliders) or x=SLIDER_CB_X=278 (checkboxes) until the
        # Debug section is re-expanded.  An x-based check would
        # then misclassify them as left-panel and force them to
        # visible=True at (0, 0) -- the "phantom widgets at the top
        # of the left pane" bug.  See PITFALLS.md P1 for the
        # original-style coord-based override that this replaces.
        RIGHT_GROUP_IDS = ('smoke', 'fire', 'normals')
        left_visible = not self.ui.info_collapsed
        for btn in self.ui.buttons:
            if btn.x < self.INFO_PANEL_W:
                btn.visible = left_visible
        for sl in self.ui.sliders:
            if getattr(sl, 'group_id', '') in RIGHT_GROUP_IDS:
                continue
            sl.visible = left_visible
        for cb in self.ui.checkboxes:
            if getattr(cb, 'group_id', '') in RIGHT_GROUP_IDS:
                continue
            cb.visible = left_visible

    # ------------------------------------------------------------------
    # PKG extractor + tree-panel helpers
    # ------------------------------------------------------------------

    def _resolve_detail_map_path(self, rel_path, res_mods_root):
        """Return a local-disk path for the detail map at *rel_path*,
        using a persistent cache under self.RESOURCES_DIR so subsequent
        sessions skip the (slow) PKG extraction.

        Strategy
        --------
        1. If `resources/<basename>` already exists -> return it.
        2. Otherwise call VisualLoader.resolve_hd_path to find / extract
           it via res_mods, res/, or PkgExtractor.
        3. On a successful extraction, copy the file into
           `resources/<basename>` so step 1 hits next time.

        Args:
            rel_path      (str): relative path declared in the visual
                                 file's metallicDetailMap property (e.g.
                                 'vehicles/russian/Tank_detail/Details_map.dds').
            res_mods_root (str): res_mods root for resolve_hd_path.

        Returns:
            absolute path on disk, or None if every resolution attempt
            failed.
        """
        if not rel_path:
            return None

        basename   = os.path.basename(rel_path)
        local_path = os.path.join(self.RESOURCES_DIR, basename)

        # Step 1: hit the local cache
        if os.path.isfile(local_path):
            return local_path

        # Step 2: fall back to the existing resolver (res_mods -> res/ -> pkg)
        resolved, _used_hd = VisualLoader.resolve_hd_path(
            rel_path, res_mods_root, self._pkg_extractor)
        if not resolved or not os.path.isfile(resolved):
            return None

        # Step 3: copy into the project's resources/ for next session
        try:
            os.makedirs(self.RESOURCES_DIR, exist_ok=True)
            shutil.copy2(resolved, local_path)
            print(f"[viewer] cached detail map -> {os.path.relpath(local_path)}")
            return local_path
        except Exception as exc:
            print(f"[viewer] could not cache detail map ({exc}); "
                  f"using temp-extracted copy")
            return resolved

    # ------------------------------------------------------------------
    def _clamp_fade_to_flipbook(self, particle_system, flipbook, label=''):
        """Clamp `particle_system.fade_start_frame / fade_end_frame`
        into the loaded `flipbook`'s actual frame count.

        Reason: the persisted config defaults (fade_end=91) assume
        the original 91-frame procedural / explosion flipbook.  After
        swapping in a shorter set (e.g. WoT's 64-frame smoke or
        32-frame flame columns), the unclamped fade_end points past
        the last frame -- particles disappear at full opacity at
        the final frame instead of fading smoothly.

        Policy: keep whatever the user has set, but cap at the actual
        frame count.  If the resulting fade_end <= fade_start, push
        fade_start back to ~75% of fade_end so there's at least a
        sensible band.  Logs a one-liner to stdout per particle
        system so the user can see the clamp happen.
        """
        if flipbook is None or particle_system is None:
            return
        n = float(flipbook.frame_count)
        old_start = float(particle_system.fade_start_frame)
        old_end   = float(particle_system.fade_end_frame)

        end   = min(old_end, n)
        start = min(old_start, max(0.0, n - 1.0))
        # Make sure start is meaningfully below end.  When end gets
        # clamped down hard, the previously-saved start might land
        # at-or-past end -- pick something ~75% of end so the fade
        # band still has shape.
        if start >= end - 0.5:
            start = max(0.0, end * 0.75)

        particle_system.fade_start_frame = start
        particle_system.fade_end_frame   = end

        changed = (abs(start - old_start) > 0.5
                   or abs(end - old_end) > 0.5)
        if changed:
            print(f"[viewer] {label} fade clamped to flipbook frames "
                  f"(N={int(n)}): "
                  f"start {old_start:.0f}->{start:.0f}, "
                  f"end {old_end:.0f}->{end:.0f}")

    # ------------------------------------------------------------------
    def _resolve_tank_nation(self, tank_name):
        """Look up which nation owns a tank XML basename.

        Args:
            tank_name (str): tank XML basename, e.g. 'It21_Lion' (case
                             insensitive; trailing extension is stripped).

        Returns:
            (nation, tank_basename) on success, or (None, tank_basename)
            when neither an exact nor a fuzzy match resolves.
            PkgExtractor must already be initialised; returns
            (None, '') when it isn't.

        Resolution strategy
        -------------------
        1. **Exact match (case-insensitive)** -- walks every nation's
           list.xml entries; first hit wins.
        2. **Fuzzy match** -- if no exact hit, runs
           `difflib.get_close_matches` against every basename across
           every nation with a 0.6 similarity cutoff.  The closest
           match is returned with the corrected basename; the caller
           sees the *fuzzy-resolved* basename, not the input.  This
           catches case mismatches an exact-lower walk wouldn't but
           more importantly catches typos / legacy WoT naming
           differences (e.g. an old FBX named `T-34-85` vs the
           in-game `T-34-85_2`).  Logs a one-liner so the user can
           see what was substituted.
        """
        if not tank_name or self._pkg_extractor is None:
            return (None, '')
        tank_basename = os.path.splitext(os.path.basename(tank_name))[0]
        try:
            nations = self._pkg_extractor.list_vehicle_xmls(with_tier=True)
        except Exception as exc:
            print(f"[viewer] resolve-tank-nation: list_vehicle_xmls failed: {exc}")
            return (None, tank_basename)

        # ---- 1. Exact (case-insensitive) match ----
        target = f"{tank_basename.lower()}.xml"
        for nat, entries in nations.items():
            for e in entries:
                if e.get('xml', '').lower() == target:
                    return (nat, tank_basename)

        # ---- 2. Fuzzy fallback ----
        # Build a flat (basename -> nation) map across every list.xml,
        # then ask difflib for the closest hit.  Cutoff 0.6 is the
        # difflib default -- low enough to catch single-letter typos
        # in long names, high enough that 'Type 59' doesn't match
        # 'Type 62'.  We compare BASENAMES (no '.xml' suffix) since
        # FBX filenames don't carry it.
        try:
            import difflib
        except Exception:
            return (None, tank_basename)

        all_basenames = []
        basename_to_nation = {}
        for nat, entries in nations.items():
            for e in entries:
                xml_name = e.get('xml', '')
                if not xml_name.lower().endswith('.xml'):
                    continue
                base = xml_name[:-4]   # strip '.xml'
                all_basenames.append(base)
                # First nation to claim a basename wins (basenames are
                # unique across nations in practice -- list.xml files
                # don't share entries -- but be defensive).
                basename_to_nation.setdefault(base, nat)

        candidates = difflib.get_close_matches(
            tank_basename, all_basenames, n=1, cutoff=0.6)
        if candidates:
            best = candidates[0]
            nat  = basename_to_nation.get(best)
            print(f"[viewer] resolve-tank-nation: '{tank_basename}' "
                  f"not in any list.xml -- fuzzy-matched to "
                  f"'{best}' (nation={nat!r})")
            return (nat, best)

        return (None, tank_basename)

    def _populate_exhaust_for_tank(self, tank_name):
        """Look up engine-exhaust hardpoints for a tank XML basename
        (e.g. 'It21_Lion') and fill self._exhaust_points + the smoke
        particle system's emitters.

        Used by load_imported_payload so smoke spawns at the correct
        rear-of-hull positions even when the geometry came from an FBX
        re-import (no .visual_processed available locally to walk).

        Strategy:
            1. Find the tank's def XML across all nations the
               PkgExtractor knows about.
            2. Read the <exhaust><nodes> list from that def XML.
            3. Extract the matching Hull.visual_processed.
            4. Walk it for HP_Track_Exhaus_* etc., apply the same Z-flip
               + offset logic as load_vehicle.

        Silent on failure -- a non-WoT FBX or an unrecognised tank name
        just leaves _exhaust_points empty and smoke disabled.
        """
        nation, tank_basename = self._resolve_tank_nation(tank_name)
        if nation is None:
            if tank_basename:
                print(f"[viewer] exhaust-for-tank: '{tank_basename}' not in any "
                      f"nation's list.xml -- no smoke emitters")
            return

        # 1+2: def XML -> exhaust spec (pixie + node names)
        def_zip_path = f'scripts/item_defs/vehicles/{nation}/{tank_basename}.xml'
        def_local = self._pkg_extractor.extract(def_zip_path)
        if not def_local:
            print(f"[viewer] exhaust-for-tank: can't extract {def_zip_path}")
            return
        spec = VehicleXMLLoader.find_engine_exhaust(def_local)
        if not spec:
            print(f"[viewer] exhaust-for-tank: no <exhaust> block in "
                  f"{tank_basename}")
            return
        self._exhaust_pixie  = spec.get('pixie')
        spec_nodes_lower = set(n.lower() for n in (spec.get('nodes') or []))
        print(f"[viewer] exhaust-for-tank: {tank_basename}  "
              f"pixie={self._exhaust_pixie!r}  nodes={spec.get('nodes')}")
        # Auto-wire the per-engine-class smoke / fire sliders to the
        # newly-loaded tank's class.  Tweaks the user makes from here
        # on save into THIS tank's class (e.g. 'gas_small') instead of
        # whatever was active before (likely 'gas_medium' from the
        # default).  See _set_active_engine_class for the swap mechanics.
        self._set_active_engine_class(self._exhaust_pixie)

        # 3: Hull visual file -> walk for hardpoints
        hull_vis_path = (f'vehicles/{nation}/{tank_basename}/'
                         f'normal/lod0/Hull.visual_processed')
        hull_local = self._pkg_extractor.extract(hull_vis_path)
        if not hull_local:
            print(f"[viewer] exhaust-for-tank: can't extract {hull_vis_path}")
            return

        # 4: Find nodes + apply BW->GL Z-flip; offset is identity here
        # because imported meshes carry their world placement in the
        # mesh.model_matrix already (no separate component-offset).
        hits = VisualLoader.find_exhaust_nodes(hull_local)
        if spec_nodes_lower:
            hits = [(n, p, f) for (n, p, f) in hits if n in spec_nodes_lower]

        for name, pos_bw, fwd_bw in hits:
            pos_gl = np.array([pos_bw[0], pos_bw[1], -pos_bw[2]],
                              dtype=np.float32)
            fwd_gl = np.array([fwd_bw[0], fwd_bw[1], -fwd_bw[2]],
                              dtype=np.float32)
            self._exhaust_points.append({
                'component': 'hull',
                'name':      name,
                'pos':       pos_gl,
                'fwd':       fwd_gl,
            })
            print(f"  {name:24s}  pos=({pos_gl[0]:+.3f}, "
                  f"{pos_gl[1]:+.3f}, {pos_gl[2]:+.3f})")

        # Build hp_lines for visualisation, point smoke at the new emitters
        EXHAUST_VECTOR_LEN = 0.5
        cyan = (0.20, 1.00, 1.00)
        segments = []
        for hp in self._exhaust_points:
            start = tuple(float(v) for v in hp['pos'])
            end   = tuple(float(v) for v in (hp['pos'] + hp['fwd'] * EXHAUST_VECTOR_LEN))
            segments.append((start, end, cyan))
        self.hp_lines.update(segments)

        if self.smoke_particles is not None:
            self.smoke_particles.reset()
            self.smoke_particles.set_emitters(self._exhaust_points)

    # ------------------------------------------------------------------
    def _init_pkg_extractor_early(self):
        """Create self._pkg_extractor up-front so the tree can be populated
        before any mesh is loaded.

        Reads the config exclusively -- no hardcoded fallback.  When
        'pkg_dir' isn't set or doesn't exist, leaves the extractor as
        None; the user is prompted via the Set-Paths dialog at startup.
        """
        cfg_pkg_dir    = self._cfg.get('pkg_dir',    '').strip()
        cfg_lookup_xml = self._cfg.get('lookup_xml', '').strip() or None
        if not cfg_pkg_dir or not os.path.isdir(cfg_pkg_dir):
            self._pkg_extractor = None
            return
        wot_root = os.path.dirname(os.path.dirname(cfg_pkg_dir))
        try:
            # Hand the splash-status updater as the progress callback
            # so the user sees pkg-by-pkg progress during pre-warm
            # instead of a silent 3-6 second pause.  When the splash
            # has already been torn down (e.g. Set-Paths re-init mid-
            # session), _splash_status is a no-op so this is safe.
            self._pkg_extractor = PkgExtractor(
                wot_root,
                pkg_dir=cfg_pkg_dir,
                lookup_xml=cfg_lookup_xml,
                progress_callback=self._splash_status,
            )
        except Exception as exc:
            print(f"[viewer] PKG extractor init failed: {exc}")
            self._pkg_extractor = None

    def _paths_are_configured(self):
        """True iff config has a usable 'pkg_dir' (the only required path).
        res_mods and lookup_xml are optional -- the extractor + bundled
        lookup table can both work without them."""
        p = (self._cfg.get('pkg_dir') or '').strip()
        return bool(p) and os.path.isdir(p)

    def _prewarm_first_load_caches(self):
        """Trigger every "lazy on first tank load" cache during splash.

        Three categories of one-shot lazy state used to pay their
        full cost on the FIRST `load_vehicle` call, making it 6-7x
        slower than every subsequent load:

            1. `ArmorColorLoader.ensure_loaded()` -- extracts and parses
               WoT's `base_paints.xml`.  Hundreds of ms.

            2. `VehicleXMLLoader._shared_xml_cache` -- class-level dict
               keyed by `<nation>/components/<file>.xml`.  On the first
               tank load of any nation, `parse_info()` pulls 5 of these
               from `scripts.pkg`: engines, radios, fuelTanks, guns,
               shells.  Each pays a BWXML decode cost.

            3. **Pillow's DDS codec + the GL driver's BC-format /
               mipmap-gen path.**  This is the BIG one.  The first
               handful of `TextureLoader.load_texture` calls warm
               Pillow's plugin registry, the driver's tex-upload
               memory pools, and the JIT for `glGenerateMipmap` on
               compressed sources.  After that, every subsequent
               upload is much faster -- accounting for a measured
               ~6 s gap between first-tank-load (7.5 s) and
               second-tank-load (~1 s) of the same data.

        Pre-warming all three during splash means the user pays the
        cost ONCE during the already-expected startup wait, instead
        of stalling mid-load on the first tank.  No-ops cleanly when
        the PkgExtractor isn't configured (fresh install before
        Set Paths) -- just leaves the caches cold.

        Safe to call multiple times; every step is idempotent.
        """
        if self._pkg_extractor is None:
            return

        # 1. Armor-paint loader (cheap; one file)
        try:
            self._armor_loader.ensure_loaded()
        except Exception as exc:
            print(f"[viewer] armor-color pre-warm failed: {exc}")

        # 2. Per-nation shared component XMLs.  Drive nation list from
        # the same source the tier-tree builder uses so we can't drift
        # out of sync.  Five files per nation, all in scripts.pkg.
        from .loaders import VehicleXMLLoader, TextureLoader
        try:
            nations = list(self._pkg_extractor.list_vehicle_xmls(
                with_tier=False).keys())
        except Exception as exc:
            print(f"[viewer] could not enumerate nations for pre-warm: {exc}")
            nations = []

        SHARED_FILES = ('engines.xml', 'radios.xml', 'fuelTanks.xml',
                        'guns.xml', 'shells.xml')
        n_warmed = 0
        for nation in nations:
            for fname in SHARED_FILES:
                try:
                    VehicleXMLLoader._read_shared_xml(
                        f'scripts/item_defs/vehicles/{nation}/components/'
                        f'{fname}',
                        self._pkg_extractor)
                    n_warmed += 1
                except Exception as exc:
                    # One bad nation/file shouldn't kill the rest --
                    # the lazy path will just hit it normally on first
                    # load if it's recoverable.
                    print(f"[viewer] pre-warm {nation}/{fname} failed: "
                          f"{exc}")
        print(f"[viewer] pre-warmed {n_warmed} shared component XMLs "
              f"across {len(nations)} nations")

        # 3. Pillow + GL driver warm-up.  We push ONE real DDS through
        # the full TextureLoader pipeline (PIL open + transpose +
        # glTexImage2D + glGenerateMipmap) so all the lazy
        # initialisation happens here instead of stalling the first
        # tank's 40+ texture uploads.
        #
        # Use Details_map.dds -- the shared scratch noise map every
        # tank references via metallicDetailMap.  It's cached on disk
        # at resources/Details_map.dds (copied in by
        # _resolve_detail_map_path on the very first session ever),
        # so this is just a local file open.  We stash the result
        # into _shared_tex_cache under its abs path, which means the
        # first tank to request 'detail' skips the load entirely --
        # double win.
        detail_path = os.path.join(self.RESOURCES_DIR, 'Details_map.dds')
        if os.path.isfile(detail_path):
            try:
                key = os.path.abspath(detail_path)
                if key not in self._shared_tex_cache:
                    tex_id = TextureLoader.load_texture(detail_path)
                    self._shared_tex_cache[key] = tex_id
                    print(f"[viewer] pre-warmed Pillow + GL via "
                          f"Details_map.dds (tex_id={tex_id})")
            except Exception as exc:
                print(f"[viewer] Pillow/GL pre-warm failed: {exc}")
        else:
            # First-ever run on this install: detail map hasn't been
            # cached to resources/ yet.  We could pull it from the pkg
            # but it's an edge case (one slow first-tank-load every
            # fresh install) so leave it lazy.  After the first tank
            # loads, the file will be in resources/ for next session.
            print(f"[viewer] Details_map.dds not cached yet -- "
                  f"Pillow/GL pre-warm skipped (one-time)")

    # ------------------------------------------------------------------
    # Mesh-visibility window
    # ------------------------------------------------------------------

    def _toggle_mesh_window(self):
        """Bar-button action: show / hide the persistent mesh-visibility
        window.  Position the window to the right of the info panel the
        first time it opens; subsequent opens preserve its current spot."""
        mw = self.ui.mesh_window
        if not mw.active:
            # Anchor to the right of the info panel (or the spine, when the
            # panel is collapsed), just below the bar.
            mw.x = self.ui.info_left_inset(self.INFO_PANEL_W) + 16
            mw.y = self.ui.BAR_HEIGHT + 16
        mw.toggle()

    def _on_info_panel_toggled(self):
        """Callback registered with UIManager.on_info_toggle.  Fires after
        the user clicks the spine and the UIManager has already flipped
        self.ui.info_collapsed.  Re-runs layout so panels + camera
        viewport reflow, then persists the new state to config."""
        self._on_resize(self.width, self.height)
        self._cfg['info_panel_collapsed'] = bool(self.ui.info_collapsed)
        try:
            _config.save(self._cfg)
        except Exception as exc:
            print(f"[viewer] config save (info_panel_collapsed) failed: {exc}")

    def _populate_mesh_window(self):
        """Refresh the mesh-window's row list to match self.meshes.  All
        rows reset to visible -- mirrors the Mesh.visible default after
        every load.  Called from the end of load_vehicle / load_mesh.

        Display-name fallback chain:
            1. mesh.identifier  -- WoT material identifier (native loads
                                   only; e.g. 'tank_hull_01')
            2. mesh.name        -- primitive-group / imported-object name
                                   (set by both native loads and FBX imports)
            3. f'mesh_{i}'      -- last-resort placeholder
        """
        mw = self.ui.mesh_window
        mw.on_toggle = self._on_mesh_visibility_toggled
        rows = []
        for i, mesh in enumerate(self.meshes):
            label = (mesh.identifier
                     or getattr(mesh, 'name', '')
                     or f'mesh_{i}')
            rows.append((i, f'#{i:>2}  {label}'))
        mw.populate(rows, self.ui._make_tex)

    def _on_mesh_visibility_toggled(self, mesh_index, new_visible):
        """Mesh-window callback: mirror the checkbox state onto the Mesh."""
        if 0 <= mesh_index < len(self.meshes):
            self.meshes[mesh_index].visible = bool(new_visible)

    def _show_paths_dialog(self):
        """Open the modal Set-Paths dialog with current config as initial."""
        initial = {
            'pkg_dir':    self._cfg.get('pkg_dir',    ''),
            'res_mods':   self._cfg.get('res_mods',   ''),
            'lookup_xml': self._cfg.get('lookup_xml', ''),
        }
        self.ui.paths_dialog.show(initial, on_confirm=self._on_paths_saved)

    # ------------------------------------------------------------------
    def _on_language_clicked(self):
        """Action-button callback: show a Tk dropdown for language choice.

        WoT-style global language list (see
        `tankExporterPy.localization.SUPPORTED_LANGUAGES`) -- each
        choice maps to a `<lang>/LC_MESSAGES/tepy.mo` catalog
        bundled with the package.  Selection persists in
        tankExporterPy.json under the `language` key.

        Restart-to-apply: every label texture is built once at
        `_build_ui` time, so a mid-session language change wouldn't
        retro-translate already-built buttons.  The picker dialog
        explains this and offers to acknowledge.

        No-ops cleanly when tkinter isn't available (headless
        environments, etc.) -- logs and returns.
        """
        try:
            import tkinter as tk
            from tkinter import ttk
        except ImportError:
            self.log_error("Language picker: tkinter not available.")
            return
        from .localization import (
            SUPPORTED_LANGUAGES, LANGUAGE_NAMES,
            get_active_language)

        cur = get_active_language() or 'en'
        cur_label = LANGUAGE_NAMES.get(cur, cur)

        root = tk.Tk()
        root.title("TEPY -- Language")
        try:
            root.attributes('-topmost', True)
        except Exception:
            pass
        # Center on screen
        root.geometry("+400+250")
        root.resizable(False, False)

        frame = ttk.Frame(root, padding=12)
        frame.pack(fill='both', expand=True)

        ttk.Label(frame,
                  text="Choose UI language:",
                  font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        ttk.Label(frame,
                  text=f"Current: {cur_label}  ({cur})",
                  foreground='gray').pack(anchor='w', pady=(0, 8))

        # Dropdown -- show "Native name (code)" so even unfamiliar
        # codes are recognisable from the script.
        choices = [f"{name} ({code})"
                   for code, name in SUPPORTED_LANGUAGES]
        choice_var = tk.StringVar(value=f"{cur_label} ({cur})")
        combo = ttk.Combobox(frame, values=choices, state='readonly',
                             textvariable=choice_var, width=30)
        combo.pack(fill='x', pady=(0, 8))

        ttk.Label(frame,
                  text="Takes effect on next launch.",
                  foreground='gray').pack(anchor='w')

        result = {'code': None}

        def _ok():
            picked = choice_var.get()
            # Extract code from "Name (code)" format.
            if '(' in picked and picked.endswith(')'):
                code = picked.rsplit('(', 1)[1][:-1].strip()
                result['code'] = code
            root.destroy()

        def _cancel():
            root.destroy()

        button_row = ttk.Frame(frame)
        button_row.pack(fill='x', pady=(8, 0))
        ttk.Button(button_row, text='Save',   command=_ok).pack(side='right')
        ttk.Button(button_row, text='Cancel', command=_cancel).pack(side='right',
                                                                    padx=(0, 6))

        root.protocol("WM_DELETE_WINDOW", _cancel)
        root.mainloop()

        chosen = result['code']
        if chosen is None or chosen == cur:
            return     # cancel, or no change

        # Persist + log.  Do not call set_active_language here --
        # that would split the UI between English (already-built
        # widgets) and the new language (any future widget builds).
        # Cleaner to persist and prompt for restart.
        self._cfg['language'] = chosen
        try:
            _config.save(self._cfg)
        except Exception as exc:
            self.log_error(f"Language: config save failed: {exc}")
            return
        self.log(f"Language set to {chosen} -- restart TEPY to apply.")
        self._show_info_popup(
            "Language saved",
            f"UI language set to {LANGUAGE_NAMES.get(chosen, chosen)} ({chosen}).\n\n"
            f"Restart TEPY for the change to take effect.")

    # ------------------------------------------------------------------
    def _toggle_debug_collapsed(self):
        """Flip the right-panel Debug section's collapse state.

        Dedicated handler -- distinct from `_toggle_section_collapsed`
        which the left panel uses.  Targeted updates only:

            1. Flip section_collapsed['Debug'] in config.
            2. Re-run `_layout_widgets()` so the right-panel block
               sees the new state and computes the right
               RIGHT_CONTROLS_H + visibility for the right widgets.
               Left-panel layout is idempotent on this call --
               buttons / sliders / checkboxes stay where they were.
            3. Update the two right-side things that depend on
               RIGHT_CONTROLS_H: tank-list tree height (per cached
               tier tree) and right_controls_rect (panel-BG render
               + hit-test bounds).
            4. Persist.

        Critically does NOT call `_on_resize` -- avoids the spine-
        collapse override loop at the end of _on_resize, which
        would unconditionally clobber left-panel widget visibility.
        """
        d = self._cfg.get('section_collapsed') or {}
        if not isinstance(d, dict):
            d = {}
        d['Debug'] = not bool(d.get('Debug', False))
        self._cfg['section_collapsed'] = d

        # Re-run panel-internal layout (positions + visibility +
        # the new RIGHT_CONTROLS_H value).
        self._layout_widgets()

        # Right-side consumers of RIGHT_CONTROLS_H -- update them
        # by hand.  Tab-bar y is anchored to 0 (top), unaffected.
        # Camera viewport doesn't depend on RIGHT_CONTROLS_H either,
        # so we don't touch self.camera.{width,height}.
        tab_h = self.ui.tab_bar.HEIGHT if self.ui.tab_bar else 0
        tree_h = max(1, self.height - tab_h - self.RIGHT_CONTROLS_H)
        for cached in getattr(self, '_tier_tree_cache', ()):
            if cached is None:
                continue
            cached.h = tree_h
        if self.ui.tree is not None:
            self.ui.tree.h = tree_h

        self.ui.right_controls_rect = (
            self.width - self.TREE_PANEL_W,
            self.height - self.RIGHT_CONTROLS_H,
            self.TREE_PANEL_W,
            self.RIGHT_CONTROLS_H,
        )

        try:
            _config.save(self._cfg)
        except Exception as exc:
            print(f"[debug-collapse] save failed: {exc}")

    def _toggle_section_collapsed(self, section_key):
        """Flip the collapse state of one left-panel section.

        Section keys are the stable English names from
        `_layout_widgets.section_keys` ('res_mods' / 'UI' / 'Model'
        / 'IO' / 'Tools') -- chosen so a runtime language switch
        doesn't desync the persisted state from the displayed
        labels.  Re-runs `_layout_widgets` so the new visibility +
        chevron glyph (▼ / ▶) takes effect on the next frame, and
        persists the dict to disk so the layout survives a restart.
        """
        # Defensive: cfg dict mutation should never silently drop
        # other keys we don't know about.
        d = self._cfg.get('section_collapsed') or {}
        if not isinstance(d, dict):
            d = {}
        d[section_key] = not bool(d.get(section_key, False))
        self._cfg['section_collapsed'] = d
        self._layout_widgets()
        try:
            _config.save(self._cfg)
        except Exception as exc:
            # Don't fight the user over a config write -- a missing
            # save just means the new collapse state won't persist
            # past the session.
            print(f"[section-collapse] save failed: {exc}")

    # ------------------------------------------------------------------
    def _apply_theme_live(self, name):
        """Switch active theme and re-tint every live UI accent
        without rebuilding widgets / restarting TEPY.

        Cheap because the renderer reads `btn.accent_color` and
        `glClearColor` afresh each frame -- so updating those plus
        flipping `theme.set_active(...)` is enough.  Buttons are
        tagged with `theme_slot` ('c1' / 'c2' / 'c3' / 'c4') at
        `_build_ui` time so we know which slot to pull from for
        each button on a re-theme.

        Returns True on success, False if `name` isn't a known
        preset.

        What does NOT live-update:
          * Console lines already in the scrollback (their bitmap
            had c3 baked in at write time).  New lines pick up the
            new c3 automatically.
          * Pre-rendered button labels are alpha-only blits drawn
            over the accent fill, so they look correct against the
            new accent without rebuild.
        """
        if not _theme.set_active(name):
            return False
        glClearColor(*_theme.bg())
        slot_lookup = {
            'c1': _theme.c1, 'c2': _theme.c2,
            'c3': _theme.c3, 'c4': _theme.c4,
        }
        for btn in self.ui.buttons:
            slot = getattr(btn, 'theme_slot', None)
            fn   = slot_lookup.get(slot)
            if fn is not None:
                btn.accent_color = fn()
        return True

    # ------------------------------------------------------------------
    def _on_theme_clicked(self):
        """Action-button callback: show the swatch-preview theme picker.

        Tk Toplevel with a scrollable list of preset rows.  Each row
        renders the preset's c1 / c2 / c3 / c4 / bg as five small
        coloured squares followed by the preset name.  Clicking a row
        selects it (visual highlight in the list); pressing Set commits
        the choice -- saves to `tankExporterPy.json` under `theme` and
        applies live via `_apply_theme_live` so the running session
        re-tints without a restart.  Cancel closes the dialog without
        changes.

        The bg-override colour picker is a separate future widget --
        this dialog only switches between the curated presets.
        """
        try:
            import tkinter as tk
            from tkinter import ttk
        except ImportError:
            self.log_error("Theme picker: tkinter not available.")
            return

        cur_name = _theme.get_active_name()

        root = tk.Tk()
        root.title("TEPY -- Theme")
        try:
            root.attributes('-topmost', True)
        except Exception:
            pass
        root.geometry("+400+200")
        root.resizable(False, False)

        outer = ttk.Frame(root, padding=10)
        outer.pack(fill='both', expand=True)

        ttk.Label(outer, text="Click a theme to select.  Set to apply.",
                  font=('Segoe UI', 10, 'bold')).pack(anchor='w',
                                                       pady=(0, 6))

        # ---- Scrollable list of preset rows ----------------------
        # Tk Listbox can't render coloured swatches per row, so we
        # build a Canvas (scrollable) and pack one Frame per preset
        # inside it.  The window-on-canvas trick gives us scroll
        # bars without abandoning normal layout for the rows.
        list_h = min(360, 36 * len(_theme.PRESET_NAMES) + 6)
        list_w = 360

        canvas = tk.Canvas(outer, width=list_w, height=list_h,
                           highlightthickness=1,
                           highlightbackground='#777',
                           bg='#202020')
        vbar   = ttk.Scrollbar(outer, orient='vertical',
                               command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)

        canvas.pack(side='left', fill='both', expand=True)
        vbar.pack(side='right', fill='y')

        rows_frame = tk.Frame(canvas, bg='#202020')
        rows_window = canvas.create_window((0, 0),
                                            window=rows_frame,
                                            anchor='nw')

        def _on_rows_resize(_evt):
            canvas.configure(scrollregion=canvas.bbox('all'))
            # Stretch the inner frame to canvas width so row clicks
            # land anywhere across the full list, not just on the
            # text run.
            canvas.itemconfigure(rows_window, width=canvas.winfo_width())

        rows_frame.bind('<Configure>', _on_rows_resize)

        # Mouse-wheel scroll inside the list (Windows convention:
        # delta is +/- 120 per notch, divide by 120 for unit scroll
        # and negate so wheel-up scrolls list-up).
        def _on_mousewheel(evt):
            canvas.yview_scroll(int(-evt.delta / 120), 'units')
        canvas.bind('<MouseWheel>', _on_mousewheel)
        rows_frame.bind('<MouseWheel>', _on_mousewheel)

        # ---- Row builder -----------------------------------------
        # Each preset gets one Frame.  Inside: a Canvas with five
        # filled rectangles (c1 / c2 / c3 / c4 / bg) followed by a
        # Label with the preset name.  The whole row binds Button-1
        # to set the current selection.
        SWATCH = 22       # square edge in px
        SW_GAP = 3        # gap between swatches
        ROW_PAD_Y = 4

        def _rgba_to_hex(rgba):
            r, g, b = rgba[:3]
            return '#{:02x}{:02x}{:02x}'.format(
                int(r * 255), int(g * 255), int(b * 255))

        selected = {'name': cur_name}
        row_widgets = {}   # name -> (frame, label) for highlight updates

        UNSEL_BG  = '#202020'   # row bg when not selected
        SEL_BG    = '#3a3a4a'   # row bg when selected
        UNSEL_FG  = '#ddd'      # row text when not selected
        SEL_FG    = '#ffffff'   # row text when selected

        def _highlight(name):
            """Repaint row backgrounds so only `name` shows the
            selection tint."""
            for n, (frm, lbl, swc) in row_widgets.items():
                bg = SEL_BG if n == name else UNSEL_BG
                fg = SEL_FG if n == name else UNSEL_FG
                frm.configure(bg=bg)
                lbl.configure(bg=bg, fg=fg)
                swc.configure(bg=bg)

        def _select(name):
            selected['name'] = name
            _highlight(name)

        def _build_row(name):
            preset = _theme.PRESETS[name]
            row = tk.Frame(rows_frame, bg=UNSEL_BG, padx=6,
                           pady=ROW_PAD_Y, cursor='hand2')
            row.pack(fill='x', expand=True)

            # Five swatches drawn in one canvas so we can lay them
            # out tightly without per-swatch frame overhead.
            sw_w = SWATCH * 5 + SW_GAP * 4
            sw   = tk.Canvas(row, width=sw_w, height=SWATCH,
                             bg=UNSEL_BG, highlightthickness=0)
            sw.pack(side='left')
            colours = [preset.c1, preset.c2, preset.c3,
                       preset.c4, preset.bg]
            for i, rgba in enumerate(colours):
                x0 = i * (SWATCH + SW_GAP)
                sw.create_rectangle(x0, 0, x0 + SWATCH, SWATCH,
                                    fill=_rgba_to_hex(rgba),
                                    outline='#444')

            label_text = name + ('   (current)' if name == cur_name else '')
            lbl = tk.Label(row, text=label_text, bg=UNSEL_BG,
                           fg=UNSEL_FG, font=('Segoe UI', 10),
                           padx=10, anchor='w')
            lbl.pack(side='left', fill='x', expand=True)

            row_widgets[name] = (row, lbl, sw)

            for w in (row, lbl, sw):
                w.bind('<Button-1>', lambda _e, n=name: _select(n))
                w.bind('<MouseWheel>', _on_mousewheel)

        for name in _theme.PRESET_NAMES:
            _build_row(name)
        _highlight(cur_name)

        # ---- Bottom button row ------------------------------------
        result = {'apply': False}

        def _set():
            result['apply'] = True
            root.destroy()

        def _cancel():
            result['apply'] = False
            root.destroy()

        # The bottom button row needs to live OUTSIDE `outer` (which
        # holds the canvas + vscroll) so it doesn't get pushed off
        # the bottom by the scrollable list.
        btn_row = ttk.Frame(root, padding=(10, 4, 10, 10))
        btn_row.pack(fill='x', side='bottom')
        ttk.Button(btn_row, text='Set',    command=_set
                   ).pack(side='right')
        ttk.Button(btn_row, text='Cancel', command=_cancel
                   ).pack(side='right', padx=(0, 6))

        root.protocol("WM_DELETE_WINDOW", _cancel)
        root.mainloop()

        if not result['apply']:
            return
        chosen = selected['name']
        if chosen == cur_name:
            return

        # Apply live first so the user gets visual confirmation
        # the moment the dialog closes; persist after.  If save
        # fails we still keep the live theme so the rest of this
        # session shows the chosen palette -- they can pick again
        # next launch.
        if not self._apply_theme_live(chosen):
            self.log_error(f"Theme: unknown preset {chosen!r}")
            return
        self._cfg['theme'] = chosen
        # Drop the legacy `motif` key from the on-disk config so
        # we don't keep two competing values around once the user
        # has actively picked a theme through the new picker.
        self._cfg.pop('motif', None)
        try:
            _config.save(self._cfg)
        except Exception as exc:
            self.log_error(f"Theme: config save failed: {exc}")
            return
        self.log(f"Theme set to {chosen!r} (live).")

    # ------------------------------------------------------------------
    def _show_info_popup(self, title, message):
        """Show a transient Tkinter info dialog ('OK' button only).

        Used by import / export flows for status notifications that
        the user should see but doesn't need to act on (e.g. "FBX
        was auto-upgraded from 6.1 to 7.3 before loading").
        Failures are swallowed -- a missing tkinter or display
        shouldn't break the import flow.
        """
        try:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk(); r.withdraw()
            try:
                r.attributes('-topmost', True)
            except Exception:
                pass
            messagebox.showinfo(title, message, parent=r)
            r.destroy()
        except Exception as exc:
            print(f"[viewer] info popup failed ({title!r}): {exc}")

    def _show_error_popup(self, title, message):
        """Show a transient Tkinter error dialog ('OK' button only).

        Sibling of `_show_info_popup`; uses messagebox.showerror so
        the icon + colour match the OS error style.  Same swallow-
        on-failure behaviour.
        """
        try:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk(); r.withdraw()
            try:
                r.attributes('-topmost', True)
            except Exception:
                pass
            messagebox.showerror(title, message, parent=r)
            r.destroy()
        except Exception as exc:
            print(f"[viewer] error popup failed ({title!r}): {exc}")

    # ------------------------------------------------------------------
    def _show_format_picker(self, direction):
        """Modal tkinter form listing every Blender IO format as a
        radio button.  Supported formats are enabled; the rest are
        greyed out and labelled '(not yet)' so the user can see
        what's coming.  Single-select -- exactly one format at a time
        for both Import and Export.

        Args:
            direction (str): 'export' or 'import' -- drives which
                             format flags are checked for "supported"
                             AND which window title is shown.

        Returns:
            list[str] -- a one-element list with the chosen extension
            (bare, no dot), or an empty list when the user cancelled
            or hit Continue with no radio selected.  Returned as a
            list rather than a string so the caller can iterate
            uniformly with the previous (multi-select) API contract.
        """
        try:
            import tkinter as tk
            from tkinter import ttk
        except ImportError:
            print(f"[{direction}] tkinter not available -- "
                  f"can't show format picker.  Defaulting to FBX.")
            return ['fbx']

        from . import io_formats

        title = ('Export tank -- pick format' if direction == 'export'
                 else 'Import tank -- pick format')

        win = tk.Tk()
        win.title(title)
        try:
            win.attributes('-topmost', True)
        except Exception:
            pass
        win.resizable(False, False)
        win.geometry('360x420')

        # Header label
        hdr = ('Pick the format to write.'
               if direction == 'export' else
               'Pick the format to import.')
        tk.Label(win, text=hdr, justify='left',
                 padx=12, pady=8).pack(fill='x')

        ttk.Separator(win, orient='horizontal').pack(fill='x', padx=8)

        # Single shared StringVar drives the radio group -- exactly one
        # format can be selected at a time.  Default-tick FBX since
        # that's the most-asked-for round-trip format; an empty value
        # would still be valid but forces an extra click for the common
        # case.  User can switch to any other supported format with
        # one click before hitting Continue.
        key = 'export' if direction == 'export' else 'import_'
        choice_var = tk.StringVar(value='fbx')
        rows_frame = tk.Frame(win, padx=12, pady=8)
        rows_frame.pack(fill='both', expand=True)
        for f in io_formats.FORMATS:
            supported = bool(f[key])
            label = f"{f['name']}  (.{f['ext']})"
            if not supported:
                label += '   -- not yet'
            rb = tk.Radiobutton(
                rows_frame, text=label,
                variable=choice_var, value=f['ext'],
                anchor='w', justify='left',
                state=('normal' if supported else 'disabled'))
            rb.pack(fill='x', anchor='w')

        # Result holder -- _confirm() and _cancel() write into this
        chosen = []

        def _confirm():
            chosen.clear()
            ext = choice_var.get()
            if ext:
                chosen.append(ext)
            win.destroy()

        def _cancel():
            chosen.clear()
            win.destroy()

        # Buttons row
        btn_frame = tk.Frame(win, padx=12, pady=8)
        btn_frame.pack(fill='x', side='bottom')
        tk.Button(btn_frame, text='Cancel', width=10,
                  command=_cancel).pack(side='right', padx=4)
        tk.Button(btn_frame, text='Continue', width=10,
                  command=_confirm).pack(side='right', padx=4)

        # Window close (X) treated as Cancel
        win.protocol('WM_DELETE_WINDOW', _cancel)
        win.mainloop()

        # If Import got more than one tick, narrow to the first since the
        # downstream file dialog can only open one path at a time.
        return chosen

    def _on_export_clicked(self):
        """Action-button callback for the 'Export' button.

        Workflow:
            1. Bail out with a console note if no tank is currently loaded.
            2. Verify Blender is reachable.
            3. Show the format-picker form (radio buttons for every
               Blender IO format; supported ones enabled).
            4. Open a Save dialog filtered to the chosen extension and
               spawn Blender to write it.

        Blocks the UI thread for ~3 s per format while Blender runs.
        """
        # 1. Guard: nothing to export without a loaded tank
        if not self.meshes:
            self.log_clear(status='Export')
            self.log_error("Export: no tank loaded -- pick one from the tree first.")
            return

        # Fresh console for this action.
        self.log_clear(status='Export')

        # Lazy imports keep startup fast / avoid tk dependency until used
        try:
            import tkinter as tk
            from tkinter import filedialog
        except ImportError:
            self.log_error("Export: tkinter not available -- can't show file dialog.")
            return
        from .exporters import export_vehicle, find_blender_executable
        from . import io_formats

        # 2. Verify Blender is reachable BEFORE pestering the user with a dialog
        blender_exe = (self._cfg.get('blender_exe') or '').strip() or None
        resolved_blender = find_blender_executable(blender_exe)
        if not resolved_blender:
            print("[export] Blender not found.  Install Blender or set "
                  "config['blender_exe'].")
            return
        print(f"[export] Blender: {resolved_blender}")

        # 3. Show format-picker form -- user picks ONE format (radio).
        chosen_exts = self._show_format_picker('export')
        if not chosen_exts:
            print("[export] cancelled.")
            return
        print(f"[export] format: {chosen_exts[0]}")

        # 4. Default filename stem -- prefer the WoT XML basename
        # (e.g. 'A14_T30') because it carries the nation prefix letter
        # (A=USA, R=USSR, G=Germany, ...), the id number, the
        # underscore, AND the short display name in one canonical
        # token -- exactly what users want for cross-referencing
        # exports back to the in-game tank.  Falls back to the
        # tanks.txt display name (e.g. 'T30') if the XML basename
        # isn't known (e.g. user loaded a standalone .primitives_processed),
        # and finally to a generic 'Tank' label.
        xml_basename = (getattr(self, 'source_tank_name', '') or '').strip()
        display_name = (
            (self.ui.tree.loaded_thumb_name or '').strip()
            if (self.ui and self.ui.tree) else ''
        )
        default_stem = xml_basename or display_name or 'Tank'
        # Strip filesystem-illegal chars (keep alphanumerics, space,
        # underscore, dash -- the basename uses only those anyway).
        default_stem = ''.join(
            c for c in default_stem if c.isalnum() or c in ' _-')
        # Tag damaged-variant exports so a later round-trip
        # (FBX -> Blender -> FBX -> Save Prim) doesn't lose the
        # variant info and silently overwrite the normal-variant
        # files in res_mods.  The importer reads the tag back off
        # the filename to restore `self._loaded_damaged`.  Idempotent
        # -- if the user is exporting an already-damaged-tagged set
        # (re-export of an imported `_damaged` FBX), we don't double
        # the suffix.
        if (getattr(self, '_loaded_damaged', False)
                and not default_stem.lower().endswith('_damaged')):
            default_stem = f"{default_stem}_damaged"

        # 5. Prompt for the filename + spawn Blender.  The loop runs
        # exactly once today (radio = single format) but kept as a
        # loop so re-introducing multi-format export later is a one-line
        # picker change rather than a structural rewrite.
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes('-topmost', True)
        except Exception:
            pass

        try:
            for ext in chosen_exts:
                entry = io_formats.lookup(ext)
                if not entry or not entry.get('export'):
                    self.log_error(f"Export {ext!r} not yet implemented -- skipped")
                    continue
                self.log_status(f"Export: {ext.upper()}")
                out_path = filedialog.asksaveasfilename(
                    parent=root,
                    title=f"Export tank -- {entry['name']}",
                    defaultextension=f".{ext}",
                    initialfile=f"{default_stem}.{ext}",
                    filetypes=[(entry['name'], f"*.{ext}"),
                               ('All files', '*.*')],
                )
                if not out_path:
                    self.log(f"Export {ext}: cancelled.")
                    continue

                # Output goes EXACTLY where the user picked it.  The
                # texture sidecar folder lives next to the file, named
                # '<stem>_textures/' (configured by collect_payload --
                # see tankExporterPy/exporters/common.py).  This is the
                # convention the rest of our pipeline + Blender expect:
                #     <user_dir>/A14_T30.fbx
                #     <user_dir>/A14_T30_textures/<tex>.dds
                self.log(f"Export {ext}: {os.path.basename(out_path)}")
                ok, msg = export_vehicle(
                    self, out_path, blender_exe=blender_exe,
                    on_log=lambda s, e=ext: print(f"[export:{e}] {s}"))
                if ok:
                    self.log(f"Export {ext}: DONE -- {msg}")
                else:
                    self.log_error(f"Export {ext}: FAILED -- {msg}")
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Component picker -- used by Save-Prim to choose which tank
    # parts (Hull / Chassis / Turret / Gun) to write back to disk as
    # .primitives_processed files.
    # ------------------------------------------------------------------

    # Display order + canonical key for each writable component.
    _COMPONENT_ORDER = (
        ('hull',    'Hull'),
        ('chassis', 'Chassis'),
        ('turret',  'Turret'),
        ('gun',     'Gun'),
    )

    def _classify_meshes_by_component(self):
        """Return {component_key: [Mesh, ...]} for every mesh in the
        currently active set.  Component key matches mesh.component
        (set by load_vehicle) -- one of 'hull' / 'chassis' / 'turret'
        / 'gun' / '' (untagged).

        Untagged meshes (FBX imports, standalone load_mesh, etc.) are
        bucketed under the empty-string key so the caller can decide
        whether to surface them.  In practice the Save-Prim dialog
        ignores that bucket -- it only writes properly-tagged WoT
        components.
        """
        out = {key: [] for key, _label in self._COMPONENT_ORDER}
        out[''] = []
        for m in self.meshes:
            comp = getattr(m, 'component', '') or ''
            if comp not in out:
                out[''].append(m)
            else:
                out[comp].append(m)
        return out

    def _show_component_picker(self):
        """Modal tkinter form for the Save Prim flow.  Two-section layout:

            [ Components ]
              [x] Hull       (N meshes)
              [x] Chassis    (N meshes)
              [x] Turret     (N meshes)
              [x] Gun        (N meshes)
            -----------------------------
            [ Texture handling ]
              (o) Extract Game textures
              ( ) Use new textures...
            -----------------------------
                 [ Cancel ]   [ Continue ]

        Defaults:
            * Every component with >= 1 mesh starts ticked.
            * Texture mode defaults to 'extract' (copy game textures
              into res_mods at canonical paths).

        When the user picks 'custom' AND clicks Continue, a folder
        picker opens immediately so the chosen DDS folder is captured
        before the form returns.

        Returns:
            dict with keys:
                'components' : list[str] -- chosen component keys
                'texture_mode': 'extract' | 'custom'
                'custom_dir' : str | None -- only set when mode is
                               'custom'; absolute path the user picked
            On cancel, returns {'components': []}.
        """
        try:
            import tkinter as tk
            from tkinter import ttk, filedialog
        except ImportError:
            print("[save-prim] tkinter not available -- "
                  "can't show component picker.")
            return {'components': []}

        groups = self._classify_meshes_by_component()

        win = tk.Tk()
        win.title('Save Prim -- pick components + texture mode')
        try:
            win.attributes('-topmost', True)
        except Exception:
            pass
        win.resizable(False, False)
        win.geometry('360x340')

        tk.Label(win,
                 text='Tick the components to write back as\n'
                      '.primitives_processed files.',
                 justify='left', padx=12, pady=8).pack(fill='x')
        ttk.Separator(win, orient='horizontal').pack(fill='x', padx=8)

        # ---- component checkboxes -------------------------------------
        rows_frame = tk.Frame(win, padx=12, pady=8)
        rows_frame.pack(fill='x')

        vars_by_key = {}
        for key, label in self._COMPONENT_ORDER:
            n = len(groups.get(key, []))
            present = n > 0
            v = tk.BooleanVar(value=present)
            vars_by_key[key] = v
            text = (f"{label}    ({n} mesh{'es' if n != 1 else ''})"
                    if present else
                    f"{label}    (none loaded)")
            cb = tk.Checkbutton(
                rows_frame, text=text, variable=v,
                anchor='w', justify='left',
                state=('normal' if present else 'disabled'))
            cb.pack(fill='x', anchor='w')

        # ---- separator + texture-mode radios --------------------------
        ttk.Separator(win, orient='horizontal').pack(fill='x', padx=8)
        tex_label = tk.Label(win, text='Textures:',
                             justify='left', padx=12, pady=4)
        tex_label.pack(fill='x', anchor='w')

        tex_frame = tk.Frame(win, padx=12, pady=4)
        tex_frame.pack(fill='x')
        tex_mode = tk.StringVar(value='extract')
        tk.Radiobutton(
            tex_frame,
            text='Extract Game textures  (copy from pkg, canonical paths)',
            variable=tex_mode, value='extract',
            anchor='w', justify='left').pack(fill='x', anchor='w')
        tk.Radiobutton(
            tex_frame,
            text='Use new textures...    (pick a folder of replacement DDS)',
            variable=tex_mode, value='custom',
            anchor='w', justify='left').pack(fill='x', anchor='w')

        result = {'components': []}

        def _confirm():
            chosen = []
            for key, _label in self._COMPONENT_ORDER:
                if vars_by_key[key].get():
                    chosen.append(key)
            mode = tex_mode.get()

            # When the user picked 'custom', prompt for the DDS folder
            # right now.  Cancelling the folder picker = cancel the
            # whole save (rather than silently falling back to extract,
            # which would surprise the user).
            custom_dir = None
            if mode == 'custom':
                # withdraw the form briefly so the folder dialog isn't
                # visually layered awkwardly on top
                try:
                    win.withdraw()
                except Exception:
                    pass
                custom_dir = filedialog.askdirectory(
                    parent=win,
                    title='Save Prim -- pick folder of replacement DDS files',
                    mustexist=True)
                if not custom_dir:
                    print("[save-prim] custom-textures folder picker "
                          "cancelled -- aborting save")
                    win.destroy()
                    return

            result['components']   = chosen
            result['texture_mode'] = mode
            result['custom_dir']   = custom_dir
            win.destroy()

        def _cancel():
            result['components'] = []
            win.destroy()

        btn_frame = tk.Frame(win, padx=12, pady=8)
        btn_frame.pack(fill='x', side='bottom')
        tk.Button(btn_frame, text='Cancel', width=10,
                  command=_cancel).pack(side='right', padx=4)
        tk.Button(btn_frame, text='Continue', width=10,
                  command=_confirm).pack(side='right', padx=4)
        win.protocol('WM_DELETE_WINDOW', _cancel)
        win.mainloop()
        return result

    def _on_save_prim_clicked(self):
        """Action-button callback for the 'Save Prim' button.

        Writes the chosen tank components back as
        .primitives_processed files into the configured res_mods
        folder, mirroring the pkg's internal directory layout.  WoT
        scans `res_mods/<version>/` ahead of `res/packages/` at load
        time, so a file dropped at the same canonical path becomes an
        automatic override.

        Workflow:
            1. Bail out if nothing's loaded.
            2. Bail out if res_mods isn't configured (Set Paths first).
            3. Show the component picker (Hull / Chassis / Turret / Gun)
               + texture-mode radios (Extract Game / Use new).
            4. For each ticked component:
               a) Compose dest path under res_mods + write the
                  .primitives_processed (currently a stub).
               b) Walk the matching .visual_processed and copy every
                  referenced texture into res_mods at its canonical
                  path.  Custom-texture mode prefers files in the
                  user's folder by basename, falling back to the pkg
                  original.

        File names match the originals -- no visual rewrite needed
        since each texture lands at the same canonical path the
        visual already references.
        """
        # Fresh console for this action.
        self.log_clear(status='Save Prim')

        if not self.meshes:
            self.log_error("Save Prim: no tank loaded -- pick one first.")
            return

        # res_mods must be configured -- the whole point of this
        # button is writing into the WoT install's override path.
        res_mods = (self._cfg.get('res_mods') or '').strip()
        if not res_mods or not os.path.isdir(res_mods):
            self.log_error(
                f"Save Prim: res_mods not configured.  Open Set Paths "
                f"and point at res_mods/<current version>/ first.")
            return
        self.log(f"res_mods: {os.path.basename(res_mods)}")

        # Heads-up when the user is writing the unmodified PKG meshes
        # straight back -- per project policy, real Save Prim usage
        # comes after an FBX import (the FBX has the user's edits).
        # Saving the active PKG set IS still a valid operation -- it
        # produces the same bytes the game already has and is useful
        # for round-trip testing the encoder.
        active = getattr(self, '_active_set_name', 'pkg')
        fbx_loaded = bool(getattr(self._fbx_set, 'meshes', None))
        if active == 'pkg' and not fbx_loaded:
            self.log("Note: round-trip test -- re-encoding the PKG "
                     "meshes (same data the game already has).")
        elif active == 'pkg' and fbx_loaded:
            self.log("Note: active set is PKG.  Flip to FBX before "
                     "Save Prim if you want the edited geometry written.")

        from .writers import write_primitives, PrimitivesWriteOptions

        # Pick which components to write + texture handling
        groups = self._classify_meshes_by_component()
        picked = self._show_component_picker()
        chosen      = picked.get('components', [])
        texture_mode = picked.get('texture_mode', 'extract')
        custom_dir   = picked.get('custom_dir')
        if not chosen:
            self.log("Save Prim: cancelled.")
            return
        self.log_status(f"Save Prim: {', '.join(chosen)}")
        self.log(f"components: {', '.join(chosen)}  "
                 f"textures={texture_mode}")

        opts = PrimitivesWriteOptions()
        for key in chosen:
            label = dict(self._COMPONENT_ORDER)[key]
            meshes_for_comp = groups.get(key, [])
            if not meshes_for_comp:
                self.log(f"{label}: no meshes -- skipped")
                continue

            # All meshes in a component share one source file.  Pull
            # the canonical pkg-relative path off any of them.
            pkg_path = ''
            for m in meshes_for_comp:
                p = getattr(m, 'primitives_zip', '')
                if p:
                    pkg_path = p
                    break
            if not pkg_path:
                print(f"[save-prim] {label}: no source pkg path on any "
                      f"mesh (was the tank loaded via load_vehicle?) -- "
                      f"skipped")
                continue

            # 1. Compose .primitives_processed output and call writer.
            # When the loaded set is the damaged variant -- either
            # because the user ticked "Load Damaged" in the load
            # dialog OR because the imported FBX filename ended in
            # `_damaged` -- redirect the canonical `/normal/` path
            # segment to `/crash/`.  WoT puts the destroyed model
            # variant under the same tree with that one folder
            # swap (e.g. `vehicles/german/G102_Pz_III/normal/lod0/...`
            # vs `.../G102_Pz_III/crash/lod0/...`).  Without this
            # the write would land on the normal-variant file in
            # res_mods and silently overwrite the wrong primitives.
            zip_path = pkg_path
            if getattr(self, '_loaded_damaged', False):
                # Use a literal slash match (canonical pkg paths
                # always use forward slashes) and only swap when
                # the path actually contains `/normal/`, so we
                # never accidentally rewrite a path that's already
                # damaged or that uses some other folder we
                # haven't seen.
                if '/normal/' in zip_path:
                    zip_path = zip_path.replace('/normal/', '/crash/')
                else:
                    self.log_error(
                        f"{label}: damaged flag is set but path "
                        f"{pkg_path!r} has no '/normal/' segment "
                        f"-- writing to canonical path as-is.  "
                        f"Verify the result lands in the right "
                        f"folder before reusing this file.")

            rel = zip_path.replace('/', os.sep).lstrip(os.sep)
            out_path = os.path.join(res_mods, rel)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)

            print(f"[save-prim] {label}: writing -> {out_path}")
            ok, msg = write_primitives(meshes_for_comp, out_path,
                                        options=opts)
            # Surface a clear per-component success/fail line in the
            # in-app console so the user can see at a glance which
            # parts wrote and which didn't.  Stdout gets the same.
            if ok:
                self.log(f"{label}: written successfully -- {msg}")
                print(f"[save-prim] {label}: DONE -- {msg}")
            else:
                self.log_error(f"{label}: FAILED -- {msg}")
                print(f"[save-prim] {label}: FAILED -- {msg}")

            # 2. Save matching textures from the visual into res_mods
            #    at their canonical paths.  Independent of whether the
            #    primitives writer succeeded -- worth doing the texture
            #    copy now so the user has a partial mod skeleton even
            #    while the encoder is being built.
            #
            #    Use `zip_path` (post-redirect) so a damaged-variant
            #    save reads the matching damaged visual_processed
            #    (which references the cracked / damaged textures)
            #    rather than the normal one.
            visual_zip = zip_path.replace('.primitives_processed',
                                           '.visual_processed')
            self._save_component_textures(
                label, visual_zip, res_mods,
                texture_mode, custom_dir)

    def _save_component_textures(self, label, visual_zip,
                                  res_mods, texture_mode, custom_dir):
        """Copy every texture referenced by `visual_zip` into res_mods.

        Args:
            label         (str): component label for log ('Hull' etc.)
            visual_zip    (str): canonical pkg path of the .visual_processed
            res_mods      (str): destination root (the
                                 res_mods/<version>/ folder)
            texture_mode  (str): 'extract' (always pkg) or 'custom'
                                 (prefer custom_dir, fall back to pkg)
            custom_dir    (str | None): folder of user-supplied DDS
                                 files (basename match against texture)
        """
        if self._pkg_extractor is None:
            print(f"[save-prim] {label}: PkgExtractor not configured "
                  f"-- can't extract textures")
            return

        # Pull the visual to a local file
        local_visual = self._pkg_extractor.extract(visual_zip)
        if not local_visual or not os.path.isfile(local_visual):
            print(f"[save-prim] {label}: visual not found in pkg "
                  f"({visual_zip}) -- no texture copy")
            return

        # Decode the visual to extract every texture reference
        try:
            with open(local_visual, 'rb') as fh:
                blob = fh.read()
            from .common import decode_bwxml, is_bwxml
            xml = (decode_bwxml(blob) if is_bwxml(blob)
                   else blob.decode('utf-8', errors='replace'))
        except Exception as exc:
            print(f"[save-prim] {label}: visual decode failed: {exc}")
            return

        # Every <Texture>...</Texture> element holds a canonical
        # pkg-relative path.  Dedupe -- many materials share the same
        # AM/NM/AO/GMM texture across multiple primitive groups.
        import re
        textures = sorted(set(
            m.group(1).strip()
            for m in re.finditer(r'<Texture>([^<]+)</Texture>', xml)))
        if not textures:
            print(f"[save-prim] {label}: no <Texture> entries in visual "
                  f"-- nothing to copy")
            return
        print(f"[save-prim] {label}: {len(textures)} texture(s) referenced "
              f"by visual")

        # Per-texture copy.  Custom mode tries the user's folder first
        # (basename match), falls back to extract from pkg.
        import shutil
        copied = 0
        skipped = 0
        for tex_zip in textures:
            dest_rel = tex_zip.replace('/', os.sep).lstrip(os.sep)
            dest = os.path.join(res_mods, dest_rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)

            src = None
            origin = ''
            if texture_mode == 'custom' and custom_dir:
                cand = os.path.join(custom_dir, os.path.basename(tex_zip))
                if os.path.isfile(cand):
                    src = cand
                    origin = 'custom'

            if src is None:
                # Fall back to pkg extraction.  resolve_hd_path picks
                # _hd over SD when both are present, mirroring the
                # game's load-time preference -- ship the same flavour
                # the user is currently rendering.
                from .loaders import VisualLoader
                resolved, _used_hd = VisualLoader.resolve_hd_path(
                    tex_zip, res_mods, self._pkg_extractor)
                if resolved and os.path.isfile(resolved):
                    src = resolved
                    origin = 'pkg'

            if src is None:
                print(f"  [skip] {tex_zip}  (not found in pkg or custom)")
                skipped += 1
                continue
            try:
                shutil.copy2(src, dest)
                copied += 1
            except Exception as exc:
                print(f"  [fail] {tex_zip}  ({exc})")
                skipped += 1
                continue
            print(f"  [{origin}] {tex_zip}")
        print(f"[save-prim] {label}: textures copied={copied} skipped={skipped}")

    # ==================================================================
    # Extract / Open / Remove -- res_mods I/O
    # ==================================================================

    def _dump_bone_angles_to_console(self, tp):
        """Per-frame console readout of every wheel bone's state.

        Clears the terminal each frame and reprints the chassis pose
        + per-wheel state lines.  Keeps the most recent data on the
        screen so the user can watch wheel deflection live as the
        tank drives over terrain.

        ANSI cursor-home + clear-screen via `\\x1b[H\\x1b[2J`.  Falls
        back to a `cls`/`clear` system call on terminals that don't
        honour ANSI (older Windows cmd, although Win10+ should
        support it).
        """
        import sys
        try:
            # ANSI: cursor home + clear screen.  One write keeps the
            # output atomic enough that a screen-tear between frames
            # is rare.  Doesn't fully clear the OS-level scrollback
            # but readers see only the latest state.
            sys.stdout.write('\x1b[H\x1b[2J')

            sys.stdout.write(
                f'TEPY tank-physics live state\n'
                f'================================\n'
                f'pos     : ({tp.pos[0]:+.3f}, {tp.pos[1]:+.3f}, {tp.pos[2]:+.3f}) m\n'
                f'yaw     : {tp.yaw_deg:+8.3f} deg\n'
                f'pitch   : {tp.pitch_deg:+8.3f} deg\n'
                f'roll    : {tp.roll_deg:+8.3f} deg\n'
                f'vy      : {tp.vy:+8.3f} m/s\n'
                f'\n'
                f'  {"#":>2s} {"bone":<22s}  {"state":<8s}  '
                f'{"resid_y":>9s}  {"susp_arm_deg":>12s}  '
                f'{"target_y":>9s}  {"terrain":>9s}\n'
            )
            STATE_NAMES = ['NONE   ', 'CONTACT', 'HANGING', 'OVER   ']
            n = len(tp.wheels)
            # Approx suspension-arm length for the angle estimate.
            # Real WoT torsion-bar arm is about half the wheel
            # radius; using radius gives a comfortable upper bound.
            ARM = max(getattr(tp, 'radius', 0.33), 0.30)
            import math
            for i in range(n):
                name = (tp.wheel_bone_names[i]
                        if i < len(tp.wheel_bone_names)
                        and tp.wheel_bone_names[i]
                        else f'(slot{i})')
                state_idx = (int(tp.last_wheel_state[i])
                             if hasattr(tp, 'last_wheel_state')
                             and len(tp.last_wheel_state) > i
                             else 0)
                state = (STATE_NAMES[state_idx]
                         if 0 <= state_idx < len(STATE_NAMES)
                         else '?      ')
                ry = (float(tp.last_residual_y[i])
                      if hasattr(tp, 'last_residual_y')
                      and len(tp.last_residual_y) > i
                      else 0.0)
                # arc-length / arm = approximate arm rotation
                # (radians).  Good enough for a debug readout; real
                # torsion-bar arm length isn't surfaced in the def
                # XML for any of the tanks we've parsed.
                susp_deg = math.degrees(ry / ARM) if ARM else 0.0
                ty = (float(tp.last_terrain_y[i])
                      if hasattr(tp, 'last_terrain_y')
                      and len(tp.last_terrain_y) > i
                      else 0.0)
                wc = (float(tp.last_target_y[i])
                      if hasattr(tp, 'last_target_y')
                      and len(tp.last_target_y) > i
                      else 0.0)
                sys.stdout.write(
                    f'  {i:>2d} {name:<22s}  {state}  '
                    f'{ry:+9.4f}  {susp_deg:+12.2f}  '
                    f'{wc:+9.4f}  {ty:+9.4f}\n')
            sys.stdout.flush()
        except Exception as exc:
            # Don't take down the render loop if the console pipe
            # ever errors out (e.g. piped to /dev/null).  Printed
            # once.
            if not getattr(self, '_dump_console_warned', False):
                print(f'[viewer] bone-angle console dump failed: {exc}')
                self._dump_console_warned = True

    def _render_physics_timer_overlay(self, width, height):
        """Draw a grass-green "physics: X.XX ms" readout at the
        upper-left of the main window.

        Reads `self._physics_ms` (smoothed in `render()` after the
        physics tick).  Caches the rendered text texture keyed on
        the formatted string -- only rebuilds when the displayed
        value rounds to a different `0.01 ms` step, so the per-
        frame cost is one quad draw plus a dict lookup.

        Uses the existing UI shader path -- bind, set u_color to
        grass-green, draw the alpha-mask text texture as a quad in
        UI / pixel coordinates.
        """
        if not pygame.font.get_init():
            return
        ui = self.ui
        if ui is None or not hasattr(ui, 'shader'):
            return
        text = f"physics:{self._physics_ms:6.2f} ms"
        cache = getattr(self, '_physics_overlay_cache', None)
        if cache is None or cache[0] != text:
            # Build / rebuild the text texture.  Lazy-init the
            # cached pygame.font on first call -- size 18 reads
            # comfortably at typical render scales.
            font = getattr(self, '_physics_overlay_font', None)
            if font is None:
                try:
                    font = pygame.font.SysFont('Consolas', 18, bold=True)
                except Exception:
                    font = pygame.font.Font(None, 18)
                self._physics_overlay_font = font
            surf = font.render(text, True, (255, 255, 255))
            data = pygame.image.tostring(surf, 'RGBA', False)
            tw, th = surf.get_width(), surf.get_height()
            # Free the previous texture if we had one cached.
            if cache is not None:
                try:
                    glDeleteTextures([cache[1]])
                except Exception:
                    pass
            tid = glGenTextures(1)
            glBindTexture(GL_TEXTURE_2D, tid)
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, tw, th, 0,
                          GL_RGBA, GL_UNSIGNED_BYTE, data)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
            glBindTexture(GL_TEXTURE_2D, 0)
            self._physics_overlay_cache = (text, tid, tw, th)
            cache = self._physics_overlay_cache

        _, tid, tw, th = cache
        # Draw via the UI shader's text-mask path.  Same recipe as
        # `ui._draw_tex` but with an explicit grass-green tint
        # (mode-1 fragment multiplies texture alpha by u_color).
        glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
        glDisable(GL_DEPTH_TEST)
        glDisable(GL_CULL_FACE)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        ui.shader.use()
        ui.shader.set_mat4('projection', ui._ortho(width, height))
        glBindVertexArray(ui.quad_vao)
        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, tid)
        ui.shader.set_int('u_tex', 0)
        ui.shader.set_int('u_use_tex', 1)
        # Grass green (RGB 90 / 200 / 80, alpha 1.0).  Reads cleanly
        # against the dark scene background and against bright
        # sky-coloured sand areas without going neon.
        ui.shader.set_vec4('u_color', 0.35, 0.78, 0.31, 1.0)
        # Position 10 px into the SCENE area (right of the
        # info / tree panel) -- earlier (10, 8) put the overlay
        # at the global window upper-left, which is inside the
        # left info panel and got hidden by panel chrome on
        # higher tank loads.  `INFO_PANEL_W` is the canonical
        # left-panel width.
        overlay_x = self.INFO_PANEL_W + 10
        overlay_y = 8
        ui._draw_quad(overlay_x, overlay_y, tw, th)
        ui.shader.set_int('u_use_tex', 0)
        glBindVertexArray(0)

    def _build_hull_box_local(self):
        """Build the chassis-local AABB of every component=='hull'
        mesh.  Cached as `self._hull_box_local` (np.ndarray of shape
        (2, 3) -- min and max corners).  Re-built when load_vehicle
        clears it via `self._hull_box_local = None`.
        """
        import numpy as np
        hull_meshes = [m for m in self.meshes
                       if getattr(m, 'component', '') == 'hull']
        if not hull_meshes:
            self._hull_box_local = None
            return
        # Each mesh has a `bind_model_matrix` (the load-time pose
        # before chassis_pose was layered on).  Bring vertices into
        # chassis-local space by applying that matrix; the chassis
        # box then rides with the chassis when chassis_pose
        # rotates / translates the whole rig.
        all_pts = []
        for m in hull_meshes:
            bm = (m.bind_model_matrix
                  if hasattr(m, 'bind_model_matrix')
                  else m.model_matrix)
            pts = np.asarray(m.positions, dtype=np.float64)
            ones = np.ones((len(pts), 1))
            ph = np.hstack([pts, ones])
            world = (bm @ ph.T).T[:, :3]
            all_pts.append(world)
        pts_all = np.concatenate(all_pts, axis=0)
        bmin = pts_all.min(axis=0)
        bmax = pts_all.max(axis=0)
        self._hull_box_local = np.array([bmin, bmax])

    def _render_hull_box(self, view, proj):
        """Draw the 12 wireframe edges of the hull AABB,
        transformed by `chassis_pose`."""
        import numpy as np
        bmin, bmax = self._hull_box_local[0], self._hull_box_local[1]
        # 8 corners of the box.
        corners = np.array([
            [bmin[0], bmin[1], bmin[2]],
            [bmax[0], bmin[1], bmin[2]],
            [bmax[0], bmax[1], bmin[2]],
            [bmin[0], bmax[1], bmin[2]],
            [bmin[0], bmin[1], bmax[2]],
            [bmax[0], bmin[1], bmax[2]],
            [bmax[0], bmax[1], bmax[2]],
            [bmin[0], bmax[1], bmax[2]],
        ], dtype=np.float64)
        # Apply chassis_pose so the box follows the tank.
        if self.tank_physics is not None:
            chassis = np.asarray(self.tank_physics.chassis_matrix(),
                                  dtype=np.float64)
            ones = np.ones((8, 1))
            ph = np.hstack([corners, ones])
            corners = (chassis @ ph.T).T[:, :3]
        # 12 edges of an AABB.
        edges = [
            (0, 1), (1, 2), (2, 3), (3, 0),  # bottom face
            (4, 5), (5, 6), (6, 7), (7, 4),  # top face
            (0, 4), (1, 5), (2, 6), (3, 7),  # vertical edges
        ]
        ORANGE = (1.0, 0.55, 0.10)
        segs = [(tuple(corners[a]), tuple(corners[b]), ORANGE)
                for a, b in edges]
        self.hull_box_lines.update(segs)
        self.hull_box_lines.render(self.color_shader, view, proj)

    def _anchored_view_matrix(self):
        """Return the view matrix for the currently-active camera mode.

        Modes:
          * 0 (orbit)     -> the existing trackball camera.
          * 1 (chase)     -> camera anchored to the chassis pose, sitting
                             above + behind + on the driver-side (left)
                             of the tank, looking at a point 5 m ahead
                             of the chassis centre.  Rolls / pitches /
                             yaws with the tank.
          * 2 (commander) -> camera at hull-centre above the chassis,
                             looking straight forward along the tank's
                             visible-front axis (= chassis-local -Z in
                             our convention).
        """
        if self.camera_mode == 0 or self.tank_physics is None:
            return self.camera.get_view_matrix()

        # Chassis pose places our chassis-local frame in world space.
        chassis = np.asarray(self.tank_physics.chassis_matrix(),
                              dtype=np.float64)

        # Pick chassis-local eye + look-at + up per mode.  Visible-front
        # of the tank lives at chassis-local -Z (the user's repeated
        # convention).  +X is right, -X left.
        if self.camera_mode == 1:
            # Chassis-locked chase camera.  Both the eye AND the
            # look-at are computed in chassis-local space, then
            # transformed by chassis_matrix to world.  Because the
            # eye offset is relative to the chassis frame (NOT the
            # world frame), the camera tracks the tank's yaw
            # automatically -- "looking at the tank's right side"
            # stays right-of-tank as the tank turns.  Same
            # "child-out-the-window" rotation behavior we use for
            # the commander seat.  User request 2026-05-08.
            #
            # Driver-position chassis-local: +X side (right; US
            # tanks like T110E4 put the driver on the right hull),
            # near front (Z = -2.0), at hatch height (+0.6 Y).
            driver_local = np.array([+0.7, +0.6, -2.0],
                                     dtype=np.float64)
            # Orbit-style offset around the driver in chassis-local
            # frame.  Same convention as scene.Camera:
            #   yaw = 0 -> eye on +Z side of center (= BEHIND the
            #              tank, since tank front = -Z chassis-local)
            #   pitch>0 -> eye lifted above the XZ plane
            #   distance -> radial zoom
            cy_ = math.cos(math.radians(self._chase_yaw_deg))
            sy_ = math.sin(math.radians(self._chase_yaw_deg))
            cp_ = math.cos(math.radians(self._chase_pitch_deg))
            sp_ = math.sin(math.radians(self._chase_pitch_deg))
            d   = float(self._chase_distance)
            eye_local_xyz = driver_local + np.array(
                [d * cp_ * sy_, d * sp_, d * cp_ * cy_],
                dtype=np.float64)
            eye_local    = np.array(
                [eye_local_xyz[0], eye_local_xyz[1],
                 eye_local_xyz[2], 1.0], dtype=np.float64)
            target_local = np.array(
                [driver_local[0], driver_local[1],
                 driver_local[2], 1.0], dtype=np.float64)
            # Mirror the chase eye world-position into self.camera
            # so any consumer that reads self.camera.center / etc.
            # (debug overlays, etc.) gets a sensible value.
            target_world_pos = (chassis @ target_local)[:3]
            self.camera.center = target_world_pos.astype(np.float32)
        else:
            # Commander POV: centre of hull, head height above the
            # chassis floor.  The head can rotate via LEFT-drag (see
            # `handle_input` commander branch); `_head_yaw_deg` and
            # `_head_pitch_deg` carry the head orientation IN
            # CHASSIS-LOCAL space -- so "looking left" stays
            # left-of-tank when the tank yaws, exactly the
            # "child looking out a side window" behavior.
            #
            # Forward direction at zero head rotation is chassis-
            # local -Z (visible-front-of-tank convention).  Apply
            # head yaw around chassis-local +Y, then head pitch
            # around chassis-local +X, to that forward vector.
            head_yaw   = math.radians(self._head_yaw_deg)
            head_pitch = math.radians(self._head_pitch_deg)
            cy_, sy_ = math.cos(head_yaw),   math.sin(head_yaw)
            cp_, sp_ = math.cos(head_pitch), math.sin(head_pitch)
            # Forward = -Z, then yaw (around Y) -> tilts in XZ:
            #   (sin(yaw), 0, -cos(yaw))
            # Then pitch (around X) -> tilts in YZ:
            #   X stays, Y = sin(pitch) * (-cos(yaw_part... hmm))
            # Simplest: build forward = R_yaw * R_pitch * (0, 0, -1).
            # R_pitch * (0, 0, -1) = (0, sin(pitch) * 1, -cos(pitch))
            #   wait, R_pitch around X: (x, y*cp - z*sp, y*sp + z*cp)
            #   so (0, 0, -1) -> (0, -(-1)*sp, -1*cp) = (0, sp, -cp)
            # R_yaw around Y on (0, sp, -cp):
            #   (x*cy - (-1)*sy * ..., wait around Y: (x*cy + z*sy, y, -x*sy + z*cy))
            #   (0*cy + (-cp)*sy, sp, -0*sy + (-cp)*cy)
            #   = (-cp*sy, sp, -cp*cy)
            fwd = np.array([-cp_ * sy_, sp_, -cp_ * cy_], dtype=np.float64)
            eye_local    = np.array([+0.0, +1.95, +0.0, 1.0],
                                    dtype=np.float64)
            target_local = np.array([
                eye_local[0] + 10.0 * fwd[0],
                eye_local[1] + 10.0 * fwd[1],
                eye_local[2] + 10.0 * fwd[2],
                1.0], dtype=np.float64)

        eye    = (chassis @ eye_local)[:3]
        target = (chassis @ target_local)[:3]
        # Up vector: chassis-local +Y rotated by chassis pose (so it
        # rolls with the tank).  Use the rotation block only -- no
        # translation on a direction vector.
        up_local  = np.array([0.0, 1.0, 0.0], dtype=np.float64)
        up        = (chassis[:3, :3] @ up_local)
        up       /= max(np.linalg.norm(up), 1e-9)

        # Build the view matrix via gluLookAt (matches the orbit
        # camera's recipe so projection / depth all stay consistent).
        from OpenGL.GL  import glMatrixMode, glLoadIdentity, glGetFloatv
        from OpenGL.GL  import GL_MODELVIEW, GL_MODELVIEW_MATRIX
        from OpenGL.GLU import gluLookAt
        glMatrixMode(GL_MODELVIEW)
        glLoadIdentity()
        gluLookAt(float(eye[0]), float(eye[1]), float(eye[2]),
                  float(target[0]), float(target[1]), float(target[2]),
                  float(up[0]), float(up[1]), float(up[2]))
        return np.array(glGetFloatv(GL_MODELVIEW_MATRIX),
                         dtype=np.float32).T

    def _kph_for_step(self, step):
        """Return the kph corresponding to a speed step (0..9).

        Convention: 1 = current tank's `_top_speed_kph` (HARD CAP --
        never exceeds the per-tank gameplay-XML maximum forward
        speed), 9 = 0.5 kph (creep), 2..8 linearly interpolate
        between, 0 = stopped.

        Hard cap: `_top_speed_kph` is set per-tank from
        `<speedLimits><forward>` in the gameplay XML.  Step 1
        returns exactly that value -- we don't allow any step to
        exceed it.  Default 50 kph fallback applies only when the
        XML didn't carry the block (rare; modded / premium tanks).

        Bumped from 0.1 to 0.5 kph at the bottom step at Professor
        Coffee's request -- 0.1 felt unresponsive when nudging the
        tank one wheel into a divot.
        """
        s = max(0, min(9, int(step)))
        if s == 0:
            return 0.0
        # Per-tank XML max.  The `min()` guarantees step 1 never
        # produces more than the XML cap even if `_top_speed_kph`
        # got stomped to a higher value somewhere upstream.
        top = float(self._top_speed_kph or 50.0)
        # k=1 -> top, k=9 -> 0.5, linear in between.
        t   = (s - 1) / 8.0
        kph = top + t * (0.5 - top)
        # Belt-and-braces clamp: never exceed the XML max.
        if kph > top:
            kph = top
        return kph

    def _speed_units_per_sec(self):
        """Convert the current speed step to a movement rate in scene
        units (= metres) per second.  Honours the `_kph_for_step`
        hard cap so the velocity never exceeds the per-tank XML max."""
        return self._kph_for_step(self._speed_step) * self._KPH_TO_UNITS_PER_S

    # Backwards-compat alias -- some sites still call `_speed_yards_per_sec`.
    def _speed_yards_per_sec(self):
        return self._speed_units_per_sec()

    def _set_speed_step(self, step):
        """Apply a new speed step + log to the in-app console.

        Called by the KEYDOWN handler.  Also resets the
        `_drive_keys_seen` flag so the user gets a fresh "tank pos"
        line the next time they press an arrow key, which makes
        the speed change easy to verify visually.
        """
        s = max(0, min(9, int(step)))
        self._speed_step = s
        kph = self._kph_for_step(s)
        ups = kph * self._KPH_TO_UNITS_PER_S
        if s == 0:
            self.log(f"speed: 0 -- stopped",
                     color=(180, 180, 180))
        else:
            self.log(f"speed: step {s}/9  =  {kph:.2f} kph  "
                     f"({ups:.3f} m/s)  "
                     f"[cap {self._top_speed_kph:.1f} kph]",
                     color=(180, 220, 255))
        # Re-arm the one-shot drive diagnostic so changing speed
        # mid-drive re-prints the position line on the next keypress.
        self._drive_keys_seen = False

    def _update_emitters_for_chassis_pose(self, chassis_pose):
        """Transform every smoke / fire emitter through `chassis_pose`
        and re-publish to the particle systems.

        Why this exists
        ---------------
        Engine-exhaust + fire-spawn hardpoints (`HP_engineExhaust_*`,
        `HP_Fire_*`, etc.) get parsed out of the per-component
        `.visual_processed` files at load time as bind-pose world
        positions -- mesh-local coords plus the component offset,
        with a BW->GL Z negation already applied.  They were
        published once via `set_emitters` at load time.

        The tank-physics pipeline now wraps every per-mesh transform
        in a `chassis_pose` (translation + yaw + plane-fit pitch /
        roll), so the rendered geometry no longer sits at the bind
        pose -- but the hardpoints, captured pre-physics, do.  Result
        without this routine: smoke plumes keep emitting from the
        spot the tank started at, while the tank drives away.

        What it does
        ------------
        First call lazily snapshots `bind_pos` / `bind_fwd` onto each
        hardpoint dict -- these never change for a given tank load.
        Every call (one per frame) builds a fresh emitter list with
        positions = `chassis_pose @ bind_pos` and forwards =
        `chassis_pose[:3,:3] @ bind_fwd` (rotation only, no world
        translation on a direction vector), then hands the list to
        `set_emitters` on each active particle system.

        Cost: O(n_hardpoints) per frame.  Typical tank: 4-8 exhaust +
        2-4 fire hardpoints.  Negligible.
        """
        # Compose bind pos/fwd once per tank load -- avoids rebuilding
        # the snapshots when set_emitters is called repeatedly during
        # a single load session.
        for hp in (getattr(self, '_exhaust_points', None) or []):
            if 'bind_pos' not in hp:
                hp['bind_pos'] = np.asarray(hp['pos'], dtype=np.float32).copy()
                hp['bind_fwd'] = np.asarray(hp['fwd'], dtype=np.float32).copy()
        for hp in (getattr(self, '_fire_points', None) or []):
            if 'bind_pos' not in hp:
                hp['bind_pos'] = np.asarray(hp['pos'], dtype=np.float32).copy()
                hp['bind_fwd'] = np.asarray(hp['fwd'], dtype=np.float32).copy()

        # Transform helper.  Position gets the full 4x4; direction
        # gets just the rotation 3x3.  Both arrive as float32 so
        # the particle systems' cast in set_emitters is a no-op.
        R = np.asarray(chassis_pose[:3, :3], dtype=np.float32)
        def _xform(hp_list):
            out = []
            for hp in hp_list:
                bp = hp['bind_pos']
                bf = hp['bind_fwd']
                # 4x4 @ vec4 -- one matmul, three components out.
                p4 = np.array([bp[0], bp[1], bp[2], 1.0], dtype=np.float32)
                wp = (chassis_pose @ p4)[:3]
                wf = R @ bf
                # Update the live records too so any consumer that
                # reads hp['pos'] / hp['fwd'] (HP-marker overlay,
                # exhaust direction lines) gets the current world
                # values.
                hp['pos'] = wp.astype(np.float32)
                hp['fwd'] = wf.astype(np.float32)
                out.append({'component': hp.get('component', ''),
                            'name':      hp.get('name',      ''),
                            'pos':       wp,
                            'fwd':       wf})
            return out

        ex = _xform(getattr(self, '_exhaust_points', None) or [])
        fr = _xform(getattr(self, '_fire_points',    None) or [])

        # Re-publish to whichever particle systems are alive on this
        # session.  Use `update_emitter_positions` (per-frame in-
        # place position refresh) instead of `set_emitters` (full
        # reset) -- the latter zeroes the spawn accumulator and
        # re-rolls the billboard RNG phases every frame, which
        # caused the "one smoke spawns, the other doesn't" bug
        # at typical spawn rates (the fractional accumulator
        # below 1.0 was being thrown away every frame, so floor()
        # was always 0).  `update_emitter_positions` falls back
        # to `set_emitters` automatically when the emitter count
        # changes (tank reload).
        if getattr(self, 'smoke_particles', None) is not None:
            self.smoke_particles.update_emitter_positions(ex)
        if getattr(self, 'fire_smoke_particles', None) is not None:
            self.fire_smoke_particles.update_emitter_positions(fr)
        if getattr(self, 'fire_billboards', None) is not None:
            self.fire_billboards.update_emitter_positions(fr)

        # Refresh the cyan exhaust-direction debug lines too --
        # they're built from hp['pos'] + hp['fwd'] * 0.5, so the
        # update needs to happen after the transform above.
        if hasattr(self, 'hp_lines'):
            EXHAUST_VECTOR_LEN = 0.5
            cyan = (0.20, 1.00, 1.00)
            segments = []
            for hp in (getattr(self, '_exhaust_points', None) or []):
                start = tuple(float(v) for v in hp['pos'])
                end   = tuple(float(v) for v in
                              (hp['pos'] + hp['fwd'] * EXHAUST_VECTOR_LEN))
                segments.append((start, end, cyan))
            self.hp_lines.update(segments)

    def _stash_extract_paths(self):
        """Compute the res_mods Extract paths for the loaded tank and
        store them on the active MeshSet.

        Path derivation is "res_mods + the canonical pkg path the
        tank loaded from" -- read straight off the first mesh's
        `primitives_zip`.  No synthesis from xml basename / nation
        parsing: whatever pkg path the loader actually consumed is
        what Open Extract Loc opens.  That guarantees the open
        target matches where Extract just wrote, including the
        damaged-vs-normal variant (if the loader pulled from
        /crash/lod0/, we land in the crash dir).

        Canonical pkg layout: `vehicles/<nation>/<tank>/<variant>/lod<n>/<file>`
        Example primitives_zip:
            vehicles/american/A14_T30/normal/lod0/Hull.primitives_processed

        Stashed paths (all absolute disk):
            extract_tank_root   = <res_mods>/vehicles/<nation>/<tank>/
                                  Whole tank, both variants under it.
                                  Used by Remove from res_mods.
            extract_variant_dir = <res_mods>/vehicles/<nation>/<tank>/<variant>/
                                  THE ROOT WHERE TEXTURES ARE EXPORTED TO --
                                  one level above lod0.  Used by Open
                                  Extract Loc.
            extract_damaged     = (variant == 'crash')

        Stores None on each path attr when prerequisites aren't met:
            * res_mods not configured
            * no canonical pkg path on any mesh (FBX-only imports,
              hand-loaded standalone .primitives_processed)
            * pkg path doesn't follow the vehicles/<nation>/<tank>/
              <variant>/lod<n>/<file> shape
        """
        s = self._active_set
        s.extract_tank_root   = None
        s.extract_variant_dir = None
        s.extract_damaged     = bool(getattr(self, '_loaded_damaged', False))

        res_mods = (self._cfg.get('res_mods') or '').strip()
        if not res_mods or not os.path.isdir(res_mods):
            print("[stash-extract] res_mods not configured -- "
                  "no extract path stashed")
            return

        # Pull the literal pkg path off the first mesh that has one.
        # Components share a single primitives_zip (one .primitives_processed
        # source per Hull / Chassis / Turret / Gun) so any mesh in any
        # component will give us the variant + nation + tank dirs.
        pkg_path = ''
        for m in self.meshes or []:
            p = (getattr(m, 'primitives_zip', '') or '').strip()
            if p:
                pkg_path = p
                break
        if not pkg_path:
            print("[stash-extract] no primitives_zip on any mesh -- "
                  "can't stash extract path")
            return

        pkg_norm = pkg_path.replace(os.sep, '/').lstrip('/')
        parts    = pkg_norm.split('/')
        # Need at minimum: vehicles / nation / tank / variant / lod / file
        if (len(parts) < 6 or parts[0].lower() != 'vehicles'
                or not parts[1] or not parts[2] or not parts[3]):
            print(f"[stash-extract] unexpected pkg path layout "
                  f"{pkg_path!r} -- can't stash")
            return

        # parts[:3] = ['vehicles', '<nation>', '<tank>']
        # parts[:4] = ['vehicles', '<nation>', '<tank>', '<variant>']
        tank_root_rel   = os.sep.join(parts[:3])
        variant_dir_rel = os.sep.join(parts[:4])
        s.extract_tank_root   = os.path.normpath(
            os.path.join(res_mods, tank_root_rel))
        s.extract_variant_dir = os.path.normpath(
            os.path.join(res_mods, variant_dir_rel))
        # Trust the variant in the actual loaded path over the
        # caller's `damaged` flag -- if the loader fell back to
        # /normal/ when /crash/ was missing, our open-loc target
        # should reflect what's actually there.
        s.extract_damaged = (parts[3].lower() == 'crash')

        # Rotate the crash-shader channel offset on every successful
        # damaged load.  See Viewer.__init__ for the rationale -- in
        # short: three loads in a row cycle through every layout the
        # 3-channel tile can produce, so the user can A/B-compare
        # variants by hitting Load Damaged repeatedly.  We bump it
        # only on damaged loads so a normal->damaged->normal sequence
        # doesn't waste the rotation.
        if s.extract_damaged:
            self._crash_channel_offset = (
                self._crash_channel_offset + 1) % 3
            print(f"[crash] channel offset = "
                  f"{self._crash_channel_offset}")

    # ------------------------------------------------------------------
    def _resmods_tank_root(self):
        """Return the active set's stashed extract_tank_root, or None.

        Set by `_stash_extract_paths` at the end of `load_vehicle`.
        Falls back to None when no tank has been loaded with a
        canonical pkg path in this session.  Used by Remove from
        res_mods (operates on the whole-tank folder, both variants).
        """
        return getattr(self._active_set, 'extract_tank_root', None)

    def _resmods_variant_dir(self):
        """Return the active set's variant-specific extract subfolder
        (`<tank_root>/normal/` or `.../crash/`), or None when none
        has been stashed.

        Used by Open Extract Loc so the user lands on the exact
        folder that matches the variant they're currently looking
        at.  Different from `_resmods_tank_root` -- callers that
        want the WHOLE-TANK directory (e.g. Remove) should use that
        method instead.
        """
        return getattr(self._active_set, 'extract_variant_dir', None)

    # ------------------------------------------------------------------
    def _show_extract_picker(self):
        """Modal Tk dialog for the Extract flow.

        Layout:
            [ Components ]
              [x] Hull       (N meshes)
              [x] Chassis    (N meshes)
              [x] Turret     (N meshes)
              [x] Gun        (N meshes)
            ----
            [x] Extract textures  (.dds / .png references)
            ----
                 [ Cancel ]   [ Extract ]

        Returns:
            dict {'components': [keys...], 'textures': bool} on
            confirm.  Empty list on cancel.
        """
        try:
            import tkinter as tk
            from tkinter import ttk
        except ImportError:
            self.log_error("Extract: tkinter not available.")
            return {'components': []}

        groups = self._classify_meshes_by_component()

        win = tk.Tk()
        win.title('Extract -- pick components + textures')
        try:
            win.attributes('-topmost', True)
        except Exception:
            pass
        win.resizable(False, False)
        win.geometry('360x300')

        tk.Label(win,
                 text='Tick the components to copy from pkg into\n'
                      'res_mods.  Existing textures will be left\n'
                      'in place -- never overwritten.',
                 justify='left', padx=12, pady=8).pack(fill='x')
        ttk.Separator(win, orient='horizontal').pack(fill='x', padx=8)

        # ---- component checkboxes (default ON for any present) -------
        rows_frame = tk.Frame(win, padx=12, pady=8)
        rows_frame.pack(fill='x')
        vars_by_key = {}
        for key, label in self._COMPONENT_ORDER:
            n = len(groups.get(key, []))
            present = n > 0
            v = tk.BooleanVar(value=present)
            vars_by_key[key] = v
            text = (f"{label}    ({n} mesh{'es' if n != 1 else ''})"
                    if present else
                    f"{label}    (none loaded)")
            cb = tk.Checkbutton(
                rows_frame, text=text, variable=v,
                anchor='w', justify='left',
                state=('normal' if present else 'disabled'))
            cb.pack(fill='x', anchor='w')

        # ---- texture toggle -------------------------------------------
        ttk.Separator(win, orient='horizontal').pack(fill='x', padx=8)
        tex_frame = tk.Frame(win, padx=12, pady=8)
        tex_frame.pack(fill='x')
        tex_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            tex_frame,
            text='Extract textures (only when missing in res_mods)',
            variable=tex_var,
            anchor='w', justify='left').pack(fill='x', anchor='w')

        # ---- bottom buttons -------------------------------------------
        result = {'components': [], 'textures': False}

        def _do_extract():
            chosen = [k for k, v in vars_by_key.items() if v.get()]
            result['components'] = chosen
            result['textures']   = bool(tex_var.get())
            win.destroy()

        def _cancel():
            win.destroy()

        btn_row = tk.Frame(win, padx=12, pady=10)
        btn_row.pack(side='bottom', fill='x')
        ttk.Button(btn_row, text='Extract', command=_do_extract
                   ).pack(side='right')
        ttk.Button(btn_row, text='Cancel', command=_cancel
                   ).pack(side='right', padx=(0, 6))

        win.protocol('WM_DELETE_WINDOW', _cancel)
        win.mainloop()
        return result

    # ------------------------------------------------------------------
    def _on_extract_clicked(self):
        """Copy chosen tank components (+ optional textures) from pkg
        into the configured res_mods folder.

        Workflow mirrors the Save Prim path-composition logic but
        sources bytes straight from PkgExtractor instead of the
        in-memory primitives writer.  Per-file rules:

            * .primitives_processed : overwrite (Extract is the user
              explicitly requesting fresh source)
            * .visual_processed     : overwrite (descriptor coupled
              to primitives -- they have to match)
            * Textures (.dds etc.)  : NEVER overwrite -- if a file
              already lives at the dest, the extract is skipped and
              logged as 'kept existing'.  Protects user edits.

        Damaged-variant handling: when the active set was loaded
        as crashed, the canonical `/normal/` segment of every pkg
        path is rewritten to `/crash/` before composing the dest --
        same swap Save Prim does, same reason.
        """
        self.log_clear(status='Extract')

        if not self.meshes:
            self.log_error("Extract: no tank loaded -- pick one first.")
            return

        res_mods = (self._cfg.get('res_mods') or '').strip()
        if not res_mods or not os.path.isdir(res_mods):
            self.log_error(
                "Extract: res_mods not configured.  Open Set Paths "
                "and point at res_mods/<current version>/ first.")
            return
        if self._pkg_extractor is None:
            self.log_error("Extract: PkgExtractor not available.")
            return

        picked = self._show_extract_picker()
        chosen = picked.get('components', [])
        do_textures = bool(picked.get('textures', False))
        if not chosen:
            self.log("Extract: cancelled.")
            return

        self.log(f"res_mods: {os.path.basename(res_mods)}")
        self.log(f"components: {', '.join(chosen)}  "
                 f"textures={'yes' if do_textures else 'no'}")

        # Damaged-variant path swap: canonical /normal/ -> /crash/.
        # Same logic Save Prim uses (see _on_save_prim_clicked).
        loaded_damaged = bool(getattr(self, '_loaded_damaged', False))

        groups = self._classify_meshes_by_component()
        total_parts_copied   = 0
        total_parts_skipped  = 0
        total_tex_copied     = 0
        total_tex_kept       = 0
        total_tex_missing    = 0

        for key in chosen:
            label = dict(self._COMPONENT_ORDER)[key]
            meshes_for_comp = groups.get(key, [])
            if not meshes_for_comp:
                self.log(f"{label}: no meshes -- skipped")
                continue

            # Collect every distinct pkg path on this component's
            # meshes (one mesh = one .primitives_processed source,
            # but multi-mesh hulls land on the same file).
            seen_zip = set()
            for m in meshes_for_comp:
                p = (getattr(m, 'primitives_zip', '') or '').strip()
                if not p or p in seen_zip:
                    continue
                seen_zip.add(p)
                if loaded_damaged and '/normal/' in p:
                    p_canonical = p.replace('/normal/', '/crash/')
                else:
                    p_canonical = p

                # Two related files we always want with the geometry:
                #   * .primitives_processed (binary mesh data)
                #   * .visual_processed     (texture / material refs)
                # Both are required for the in-game asset to load.
                # `.model` is legacy / sometimes absent, copy when
                # present.
                related = [p_canonical,
                           p_canonical.replace(
                               '.primitives_processed',
                               '.visual_processed'),
                           p_canonical.replace(
                               '.primitives_processed',
                               '.model')]

                for zp in related:
                    n_ok, n_skip = self._extract_one_file(
                        zp, res_mods, allow_overwrite=True)
                    total_parts_copied  += n_ok
                    total_parts_skipped += n_skip

            # ----- textures (gated by the dialog checkbox) -----
            if not do_textures:
                continue
            for m in meshes_for_comp:
                p = (getattr(m, 'primitives_zip', '') or '').strip()
                if not p:
                    continue
                if loaded_damaged and '/normal/' in p:
                    p = p.replace('/normal/', '/crash/')
                visual_zip = p.replace('.primitives_processed',
                                        '.visual_processed')
                ok, kept, missing = self._extract_textures_for_visual(
                    label, visual_zip, res_mods)
                total_tex_copied  += ok
                total_tex_kept    += kept
                total_tex_missing += missing
                # Each component visits its visual once -- break out
                # after the first mesh contributed it.
                break

        self.log(
            f"Extract: parts copied={total_parts_copied} "
            f"skipped={total_parts_skipped}; "
            f"textures copied={total_tex_copied} "
            f"kept_existing={total_tex_kept} "
            f"missing={total_tex_missing}")

    # ------------------------------------------------------------------
    def _extract_one_file(self, zip_path, res_mods, allow_overwrite):
        """Pull one pkg-internal file into res_mods at its canonical
        relative path.  Returns (copied:int, skipped:int) -- always 1+0
        or 0+1, scaled so the caller can sum across many files.

        When `allow_overwrite=False`, an existing dest file causes
        the copy to be skipped (logged as 'kept existing').  When
        True, the dest is replaced.  Files genuinely missing from
        every pkg are logged + skipped without raising.
        """
        import shutil
        dest_rel = zip_path.replace('/', os.sep).lstrip(os.sep)
        dest = os.path.join(res_mods, dest_rel)

        if not allow_overwrite and os.path.isfile(dest):
            print(f"  [keep] {zip_path}  (already in res_mods)")
            return 0, 1

        local = self._pkg_extractor.extract(zip_path)
        if not local or not os.path.isfile(local):
            # Don't shout for known-optional files like .model
            print(f"  [miss] {zip_path}  (not in any pkg)")
            return 0, 1
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(local, dest)
            print(f"  [ok]   {zip_path}")
            return 1, 0
        except Exception as exc:
            print(f"  [fail] {zip_path}  ({exc})")
            return 0, 1

    # ------------------------------------------------------------------
    def _extract_textures_for_visual(self, label, visual_zip, res_mods):
        """Copy every <Texture> referenced by `visual_zip` into res_mods,
        but ONLY when the destination file doesn't already exist.

        Returns (copied:int, kept_existing:int, missing:int).
        Mirrors `_save_component_textures` for the texture lookup but
        flips the overwrite policy.
        """
        if self._pkg_extractor is None:
            return 0, 0, 0

        local_visual = self._pkg_extractor.extract(visual_zip)
        if not local_visual or not os.path.isfile(local_visual):
            print(f"[extract] {label}: visual not found ({visual_zip})")
            return 0, 0, 0

        try:
            with open(local_visual, 'rb') as fh:
                blob = fh.read()
            from .common import decode_bwxml, is_bwxml
            xml = (decode_bwxml(blob) if is_bwxml(blob)
                   else blob.decode('utf-8', errors='replace'))
        except Exception as exc:
            print(f"[extract] {label}: visual decode failed: {exc}")
            return 0, 0, 0

        import re, shutil
        textures = sorted(set(
            m.group(1).strip()
            for m in re.finditer(r'<Texture>([^<]+)</Texture>', xml)))
        if not textures:
            return 0, 0, 0

        copied = kept = missing = 0
        for tex_zip in textures:
            dest_rel = tex_zip.replace('/', os.sep).lstrip(os.sep)
            dest = os.path.join(res_mods, dest_rel)

            if os.path.isfile(dest):
                print(f"  [keep tex] {tex_zip}  (already in res_mods)")
                kept += 1
                continue

            # Use VisualLoader.resolve_hd_path so we get the same _hd
            # over SD preference WoT itself follows when both ship.
            from .loaders import VisualLoader
            resolved, _used_hd = VisualLoader.resolve_hd_path(
                tex_zip, res_mods, self._pkg_extractor)
            if not resolved or not os.path.isfile(resolved):
                print(f"  [miss tex] {tex_zip}")
                missing += 1
                continue
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(resolved, dest)
                print(f"  [ok tex] {tex_zip}")
                copied += 1
            except Exception as exc:
                print(f"  [fail tex] {tex_zip}  ({exc})")
                missing += 1
        return copied, kept, missing

    # ------------------------------------------------------------------
    def _on_open_extract_loc_clicked(self):
        """Open Explorer at the extracted tank's variant subfolder.

        Path used (in priority order):
            1. <tank_root>/<crash|normal>/  -- the variant the user
               loaded.  Stashed at load time by _stash_extract_paths
               so a tank loaded as damaged opens the crash folder
               and a normal load opens the normal folder.
            2. <tank_root>                  -- whole-tank dir, used
               when the variant subfolder doesn't exist on disk yet
               but the user has previously extracted the OTHER
               variant.
            3. (offer to extract)            -- prompt the user when
               nothing's been extracted at all.

        Greyed-out semantics aren't enforced at the button level --
        we just log a line when the prerequisites aren't met (no
        tank loaded, no res_mods configured, etc.).
        """
        self.log_clear(status='Open Extract Loc')

        # Need at minimum: res_mods configured + a tank loaded.
        if not (self._cfg.get('res_mods') or '').strip():
            self.log_error(
                "Open Extract Loc: res_mods not configured.  "
                "Open Set Paths first.")
            return
        if not (getattr(self, 'source_tank_name', '') or '').strip():
            self.log_error(
                "Open Extract Loc: no tank loaded.  Pick a tank "
                "from the tree first.")
            return

        tank_root   = self._resmods_tank_root()
        variant_dir = self._resmods_variant_dir()
        if tank_root is None:
            self.log_error(
                "Open Extract Loc: can't determine tank folder "
                "(no canonical pkg path on any loaded mesh).")
            return

        # Pick the most specific dir that actually exists on disk.
        # Variant subfolder is the natural target; whole-tank root
        # is a useful fallback when the user previously extracted
        # the OTHER variant; prompt otherwise.
        if variant_dir and os.path.isdir(variant_dir):
            target = variant_dir
        elif os.path.isdir(tank_root):
            target = tank_root
        else:
            target = None

        if target is not None:
            self._open_in_explorer(target)
            variant_word = (
                'damaged' if self._active_set.extract_damaged else 'normal')
            self.log(f"Opened ({variant_word}): {target}")
            return

        # Nothing on disk yet -- offer to extract now.
        try:
            import tkinter as tk
            from tkinter import messagebox
        except ImportError:
            self.log_error(
                f"Open Extract Loc: {tank_root} doesn't exist and "
                f"tkinter isn't available.  Run Extract first.")
            return

        win = tk.Tk()
        win.withdraw()
        try:
            win.attributes('-topmost', True)
        except Exception:
            pass
        # Show the variant-specific path the user will be opening
        # AFTER extract -- not just the tank root -- so the prompt
        # matches what the user expects to see in Explorer.
        prompt_path = variant_dir or tank_root
        ans = messagebox.askyesno(
            'No extract yet',
            f"No extract folder for this tank under res_mods:\n\n"
            f"{prompt_path}\n\n"
            f"Run Extract now?",
            parent=win)
        try:
            win.destroy()
        except Exception:
            pass
        if ans:
            self._on_extract_clicked()
        else:
            self.log("Open Extract Loc: cancelled (no extract).")

    # ------------------------------------------------------------------
    def _open_in_explorer(self, path):
        """Cross-platform 'reveal in file manager' helper.

        Windows: `os.startfile()` -- the canonical way to ask the
                 shell to open a directory.  Equivalent to a
                 double-click in Explorer.  More reliable than
                 `Popen(['explorer', path])` which silently falls
                 back to the user's home/Documents folder when the
                 path argument can't be parsed (forward slashes,
                 missing chars, etc.).
        macOS  : `open` command.
        Linux  : `xdg-open` -- relies on the desktop env's default
                 file manager being registered for inode/directory.

        Normalises the path through abspath + normpath first so
        Windows gets backslashes and any '..' segments are
        flattened.  Also logs the absolute target so when the
        wrong window appears, the user can copy / paste the path
        we tried into Explorer manually.
        """
        import subprocess, sys as _sys
        norm = os.path.normpath(os.path.abspath(path))
        print(f"[open] {norm}  (exists: {os.path.isdir(norm)})")
        try:
            if os.name == 'nt':
                os.startfile(norm)
            elif _sys.platform == 'darwin':
                subprocess.Popen(['open', norm])
            else:
                subprocess.Popen(['xdg-open', norm])
        except Exception as exc:
            self.log_error(f"Open: {exc}")

    # ------------------------------------------------------------------
    def _on_remove_from_resmods_clicked(self):
        """Delete the loaded tank's folder under res_mods.

        Strong confirmation: a Tk dialog asks the user to *type* the
        tank's xml basename to enable the Delete button.  Aborting
        or typing the wrong text leaves the folder untouched.

        Mirror image of Extract -- whatever Extract wrote, this can
        remove.  Doesn't touch anything outside the canonical
        `<res_mods>/vehicles/<nation>/<tank>/` subtree.
        """
        self.log_clear(status='Remove from res_mods')

        root = self._resmods_tank_root()
        if root is None:
            if not (self._cfg.get('res_mods') or '').strip():
                self.log_error(
                    "Remove: res_mods not configured.")
            elif not (getattr(self, 'source_tank_name', '') or '').strip():
                self.log_error("Remove: no tank loaded.")
            else:
                self.log_error("Remove: can't determine tank folder.")
            return

        if not os.path.isdir(root):
            self.log_error(
                f"Remove: nothing to remove -- {root} doesn't exist.")
            return

        # Count what's about to die so the user sees the impact.
        n_files = 0
        for _root, _dirs, files in os.walk(root):
            n_files += len(files)

        tank_name = (self.source_tank_name or '').strip()
        if not self._typed_confirm(
                title='Remove tank from res_mods',
                message=(
                    f"You are about to permanently delete:\n\n"
                    f"   {root}\n\n"
                    f"This will remove {n_files} file"
                    f"{'s' if n_files != 1 else ''} (geometry, "
                    f"visuals, textures).  This action cannot "
                    f"be undone.\n\n"
                    f"Type the tank name exactly to confirm:"),
                expected=tank_name):
            self.log("Remove: cancelled.")
            return

        # Do the deletion.
        import shutil
        try:
            shutil.rmtree(root)
            self.log(f"Removed: {root}  ({n_files} files)")
        except Exception as exc:
            self.log_error(f"Remove failed: {exc}")
            return

        # Walk parent chain and prune any now-empty folders so we
        # don't leave behind `vehicles/<nation>/` skeletons after
        # deleting the only tank in that nation.  Stops as soon as
        # a parent contains anything else, or once we hit res_mods
        # itself.
        res_mods = self._cfg.get('res_mods', '')
        parent = os.path.dirname(root)
        while parent and os.path.normpath(parent) != os.path.normpath(res_mods):
            if not os.path.isdir(parent):
                break
            try:
                # `os.rmdir` only removes empty dirs -- safe.
                os.rmdir(parent)
            except OSError:
                # Not empty; stop walking up.
                break
            parent = os.path.dirname(parent)

    # ------------------------------------------------------------------
    def _typed_confirm(self, title, message, expected):
        """Modal Tk dialog requiring the user to type `expected` to
        enable the OK button.  Returns True iff the user typed an
        exact match and clicked OK.

        Used for destructive operations (Remove from res_mods) where
        a single Yes/No isn't safety enough -- typing the tank name
        prevents misclicks.
        """
        try:
            import tkinter as tk
            from tkinter import ttk
        except ImportError:
            self.log_error("Confirmation dialog: tkinter not available.")
            return False

        win = tk.Tk()
        win.title(title)
        try:
            win.attributes('-topmost', True)
        except Exception:
            pass
        win.geometry('+400+250')
        win.resizable(False, False)

        frame = ttk.Frame(win, padding=12)
        frame.pack(fill='both', expand=True)

        ttk.Label(frame, text=message,
                  justify='left', wraplength=380).pack(anchor='w')
        ttk.Label(frame, text=f"  -> '{expected}'",
                  font=('Consolas', 10, 'bold'),
                  foreground='#a04040').pack(anchor='w', pady=(4, 6))

        entry_var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=entry_var, width=40)
        entry.pack(fill='x', pady=(0, 8))
        entry.focus_set()

        result = {'ok': False}

        def _on_ok():
            if entry_var.get() == expected:
                result['ok'] = True
                win.destroy()

        def _on_cancel():
            win.destroy()

        # Bottom row first so we can parent the OK button straight
        # into it -- avoids a re-pack dance later.  Live-enable the
        # OK button only on exact match so the user gets visible
        # feedback when they've typed it correctly.
        button_row = ttk.Frame(frame)
        button_row.pack(fill='x', pady=(4, 0))
        ok_btn = ttk.Button(
            button_row, text='Delete', command=_on_ok, state='disabled')
        ok_btn.pack(side='right')
        ttk.Button(button_row, text='Cancel', command=_on_cancel
                   ).pack(side='right', padx=(0, 6))

        def _on_keyup(_evt):
            if entry_var.get() == expected:
                ok_btn.configure(state='normal')
            else:
                ok_btn.configure(state='disabled')

        entry.bind('<KeyRelease>', _on_keyup)
        # Enter key fires OK (only effective when match -- because
        # _on_ok itself re-checks); ESC cancels anywhere in the dialog.
        entry.bind('<Return>', lambda _e: _on_ok())
        win.bind('<Escape>', lambda _e: _on_cancel())

        win.protocol('WM_DELETE_WINDOW', _on_cancel)
        win.mainloop()
        return bool(result['ok'])

    # ------------------------------------------------------------------
    def _on_import_clicked(self):
        """Action-button callback for the 'Import' button.

        Workflow mirror of _on_export_clicked:
            1. Verify Blender is reachable.
            2. Show the format-picker form (only one format used on
               import even if the user tries to tick several -- you
               can only open one file at a time).
            3. Open a filtered file dialog and spawn Blender to read it.
        """
        # Fresh console for this action.
        self.log_clear(status='Import')
        try:
            import tkinter as tk
            from tkinter import filedialog
        except ImportError:
            self.log_error("Import: tkinter not available.")
            return
        from .importers import import_vehicle, find_blender_executable
        from . import io_formats

        blender_exe = (self._cfg.get('blender_exe') or '').strip() or None
        if not find_blender_executable(blender_exe):
            self.log_error("Import: Blender not found.  Set blender_exe in "
                           "config or install Blender.")
            return

        # Format picker -- single-select for import (the helper
        # narrows multi-tick down to the first entry).
        chosen_exts = self._show_format_picker('import')
        if not chosen_exts:
            self.log("Import: cancelled.")
            return
        ext = chosen_exts[0]
        entry = io_formats.lookup(ext)
        if not entry or not entry.get('import_'):
            self.log_error(f"Import {ext!r}: not yet implemented.")
            return
        self.log_status(f"Import: {ext.upper()}")
        self.log(f"Import format: {ext}")

        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes('-topmost', True)
        except Exception:
            pass
        in_path = filedialog.askopenfilename(
            parent=root,
            title=f"Import tank -- {entry['name']}",
            filetypes=[(entry['name'], f"*.{ext}"),
                       ('All files', '*.*')],
        )
        try:
            root.destroy()
        except Exception:
            pass

        if not in_path:
            print("[import] cancelled.")
            return

        # ---- FBX-version auto-upgrade ------------------------------------
        # Blender 4.x rejects binary FBX < 7.1; the legacy WoT exporter
        # wrote 6.1, and so does anything else from the FBX SDK 2009 era.
        # Probe the header and, when needed, run Autodesk's free 2013
        # converter to upgrade in place.  See `tankExporterPy.importers.
        # fbx_version` for the version-floor + converter-location logic.
        # `fbx_converter_exe` in tankExporterPy.json overrides the
        # default install-path search.
        if in_path.lower().endswith('.fbx'):
            from .importers.fbx_version import ensure_modern_fbx
            converter_override = (self._cfg.get('fbx_converter_exe')
                                  or '').strip() or None
            new_path, action, message = ensure_modern_fbx(
                in_path, converter_override=converter_override)
            if action == 'converted':
                self.log(f"FBX upgrade: {message}")
                self._show_info_popup("FBX upgraded", message)
                in_path = new_path
            elif action == 'error':
                self.log_error(f"FBX upgrade: {message}")
                self._show_error_popup("FBX import aborted", message)
                return
            # 'no_op' -- modern FBX, ASCII FBX, etc.; proceed silently.

        ok, result = import_vehicle(in_path, blender_exe=blender_exe,
                                     on_log=lambda s: print(f"[import] {s}"))
        if not ok:
            print(f"[import] FAILED -- {result}")
            return
        # `result` is the payload dict
        n = self.load_imported_payload(result)

        # Recover damaged-variant flag from the filename suffix.  The
        # exporter writes `<tank>_damaged.<ext>` for crashed variants
        # (see `_on_export_clicked`); detect that here so a later
        # Save Prim routes the write to res_mods/.../crash/ instead
        # of /normal/.  Case-insensitive so `_DAMAGED` works too.
        in_stem = os.path.splitext(os.path.basename(in_path))[0]
        if in_stem.lower().endswith('_damaged'):
            self._loaded_damaged = True
            self.log(f"[import] detected '_damaged' suffix -- "
                     f"variant=CRASHED")
        else:
            self._loaded_damaged = False

        print(f"[import] DONE -- loaded {n} mesh(es) from "
              f"{os.path.basename(in_path)}")

    # ------------------------------------------------------------------
    def _rebuild_itemlist_now(self):
        """Run the ItemList rebuild + reload in-process, in the
        current thread.  Used by both the user-clicked path
        (`_on_rebuild_itemlist_clicked`) and the auto-rebuild-on-
        missing path (`_ensure_itemlist`) so the diagnostic logging
        is identical between them.

        Returns True on full success, False on any failure (caller
        decides whether to keep going or bail).  Logs to the in-app
        console either way -- the user sees what happened.
        """
        ext = self._pkg_extractor
        if ext is None:
            self.log_error("ItemList: PkgExtractor not configured -- "
                           "open Set Paths first.")
            return False

        pkg_dir  = getattr(ext, 'pkg_dir', None)
        out_path = getattr(ext, '_lookup_xml_path', None)
        if not pkg_dir or not os.path.isdir(pkg_dir):
            self.log_error(f"ItemList: pkg dir not found: {pkg_dir}")
            return False
        if not out_path:
            self.log_error("ItemList: no lookup XML path on PkgExtractor.")
            return False

        try:
            from cust_tools.rebuild_itemlist import (
                list_pkgs, build_index, write_itemlist)
        except Exception as exc:
            self.log_error(f"ItemList: rebuild module import failed: {exc}")
            return False

        self.log(f"Rebuilding {os.path.basename(out_path)} from "
                 f"{pkg_dir} ...")
        try:
            pkgs = list_pkgs(pkg_dir, include_all=False)
            self.log(f"  scanning {len(pkgs)} pkg archive(s) "
                     f"(map / event / audio bundles excluded)")
            entries, stats = build_index(pkgs, allowed_exts=None,
                                         verbose=False)
            self.log(f"  pkgs scanned : {stats['pkgs_scanned']}/"
                     f"{stats['pkgs_total']}")
            self.log(f"  entries seen : {stats['entries_seen']:,}")
            self.log(f"  unique files : {len(entries):,}  "
                     f"({stats['duplicates']:,} cross-pkg collisions)")
            self.log(f"  scan time    : {stats['elapsed_s']:.2f} s")

            self.log(f"  writing {os.path.basename(out_path)} ...")
            import time as _t
            t0 = _t.perf_counter()
            write_itemlist(entries, out_path)
            wsec = _t.perf_counter() - t0
            size_mb = os.path.getsize(out_path) / (1024.0 * 1024.0)
            self.log(f"  -> {size_mb:.1f} MB in {wsec:.2f} s")
        except Exception as exc:
            self.log_error(f"ItemList: rebuild failed: {exc}")
            return False

        # Refresh the running PkgExtractor's in-memory lookup so the
        # next extract() call sees the freshly-indexed entries without
        # a session restart.
        try:
            ext.reload_lookup()
            self.log(f"PkgExtractor reloaded: "
                     f"{len(ext._file_to_pkg):,} entries indexed")
        except Exception as exc:
            self.log_error(f"ItemList: lookup reload failed: {exc}")
            return False

        # Companion artefact: a flat tank-list file written next to
        # TheItemList.xml.  Drives the FBX-import fuzzy-match
        # fallback in `_resolve_tank_nation`, plus serves as a
        # human-readable index of every tank def XML the user's
        # current game install carries.  Rebuilt every time the
        # ItemList rebuilds so the two stay in sync.  Failure here
        # is non-fatal -- the ItemList itself is the critical
        # artefact, the tank-list file is convenience.
        try:
            self._rebuild_tank_list_now()
        except Exception as exc:
            self.log_error(f"tank list: rebuild failed: {exc}")
            # Don't return False -- ItemList rebuild succeeded.

        # In-memory tank-tree refresh.  After a fresh ItemList
        # rebuild the on-disk catalogue may carry tanks the running
        # session never saw -- a game patch added a new line, the
        # user pointed at a different WoT install, etc.  Without a
        # session restart the tier-tree would still show the stale
        # contents.  Drop `_tanks_display_map` so it re-loads on
        # next access, detach the live `ui.tree` pointer so it
        # doesn't point into a soon-to-be-freed cached tree, and
        # rebuild every tier from the current `list_vehicle_xmls`.
        # Tab bar stays (same 11 tier slots either way) -- only the
        # contents get rebuilt.  Failure here is logged but
        # non-fatal: the on-disk artefacts are the critical
        # outputs; a stale tree is a UI annoyance, not data loss.
        try:
            self._tanks_display_map = {}
            self.ui.tree = None
            self._build_all_tier_trees()
            self.log("tank tree: rebuilt against fresh ItemList")
        except Exception as exc:
            self.log_error(f"tank tree: rebuild failed: {exc}")

        return True

    def _rebuild_tank_list_now(self):
        """Write a comprehensive tank-list file alongside TheItemList.xml.

        Walks every nation's `list.xml` via the live PkgExtractor and
        writes one line per tank in the form

            <nation>\t<tier>\t<basename>

        Output path is `tanks_index.txt` in the project root (sibling
        of TheItemList.xml).  The file is regenerated from scratch
        each call -- atomic write via `<path>.tmp` + os.replace.

        Returns True on success, False on failure.  Logs counts to
        the in-app console so the user can see the catalogue size.
        """
        ext = self._pkg_extractor
        if ext is None:
            self.log_error("tank list: PkgExtractor not configured.")
            return False
        try:
            nations = ext.list_vehicle_xmls(with_tier=True)
        except Exception as exc:
            self.log_error(f"tank list: list_vehicle_xmls failed: {exc}")
            return False

        # Sort by nation then by tier then by basename so diffs
        # between game versions stay readable.  Friendly name
        # comes from WoT's own gettext catalogs (`lc_messages/*.mo`)
        # via WoTLocalizer; falls back to the bare key portion of
        # the userString when the catalog isn't available, then to
        # the basename when there's no userString at all.
        rows = []
        for nat in sorted(nations.keys()):
            for entry in nations[nat]:
                xml_name = entry.get('xml', '')
                if not xml_name.lower().endswith('.xml'):
                    continue
                base = xml_name[:-4]
                tier = entry.get('tier')
                tier_s = str(tier) if tier is not None else '-'
                user_str = entry.get('user_string')
                friendly = (self._localizer.lookup_basename(user_str)
                            if user_str else '') or base
                rows.append((nat, tier_s, base, friendly))
        # Stable secondary sort: nation -> tier (numeric) -> name
        def _sort_key(r):
            nat, tier_s, base, _friendly = r
            try:
                tier_n = int(tier_s)
            except ValueError:
                tier_n = 99
            return (nat, tier_n, base.lower())
        rows.sort(key=_sort_key)

        out_dir  = os.path.dirname(os.path.abspath(self.TANKS_TXT))
        out_path = os.path.join(out_dir, 'tanks_index.txt')
        tmp_path = out_path + '.tmp'
        try:
            with open(tmp_path, 'w', encoding='utf-8') as fh:
                fh.write('# Auto-generated by TEPY -- regenerated '
                         'on every ItemList rebuild.\n')
                fh.write('# Format: <nation>\\t<tier>\\t<basename>'
                         '\\t<friendly_name>\n')
                fh.write(f'# Tanks: {len(rows)}  '
                         f'across {len(nations)} nation(s)\n')
                fh.write('# Friendly names come from WoT\'s own '
                         'localization (.mo) catalogs.\n')
                for nat, tier_s, base, friendly in rows:
                    fh.write(f'{nat}\t{tier_s}\t{base}\t{friendly}\n')
            os.replace(tmp_path, out_path)
        except Exception as exc:
            self.log_error(f"tank list: write {out_path} failed: {exc}")
            try:
                if os.path.isfile(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass
            return False

        self.log(f"tank list: wrote {len(rows):,} entries -> "
                 f"{os.path.basename(out_path)}")
        return True

    def _on_rebuild_itemlist_clicked(self):
        """Action-button callback for the 'ItemList' tool button.

        Clears the console for a fresh trace and runs the rebuild.
        Same exact code path as the auto-rebuild that fires on a
        missing TheItemList.xml at startup -- see
        `_rebuild_itemlist_now`.
        """
        self.log_clear(status='ItemList')
        self._rebuild_itemlist_now()

    # ------------------------------------------------------------------
    def _on_pick_tri_clicked(self):
        """Action-button callback for the 'Pick Tri' tool button.

        Toggles the triangle picker.  The button's `active` flag is
        the source of truth -- the wheat locked-on border renders
        when active, and `picker.enabled` mirrors it.  Clearing the
        picker's last hit + the console on toggle-OFF gives a clean
        end-of-session state instead of a stale highlight hanging
        around.
        """
        btn = getattr(self, '_pick_tri_btn', None)
        new_state = not (btn and btn.active)
        if btn is not None:
            btn.active = bool(new_state)
        self.picker.enabled = bool(new_state)
        if new_state:
            self.log_clear(status='Pick Tri')
            self.log("Pick Tri: hover the loaded tank to see "
                     "per-vertex bone data.")
        else:
            self.picker.last_hit = None
            self.log_clear(status='')

    # ------------------------------------------------------------------
    def _on_picker_hit_change(self, mesh_idx, tri_idx):
        """Picker hover-change callback.

        Called from `picker.update_pass` when the triangle under the
        cursor changes between frames.  Clears the console and prints
        three colour-coded lines (one per vertex) showing that
        vertex's bone indices and weights.  If `mesh_idx == -1` the
        cursor moved off any geometry -- we just blank the console
        header instead of dumping data.
        """
        from .picker import format_vertex_line
        if mesh_idx < 0 or mesh_idx >= len(self.meshes):
            # Pointer left the geometry.  Reset the status so the
            # console header reads cleanly; leave the previous lines
            # in place so the user can still scroll up to read them.
            self.log_status('Pick Tri  (hover off-mesh)')
            return

        mesh = self.meshes[mesh_idx]
        try:
            base = tri_idx * 3
            v_ids = (
                int(mesh.indices[base    ]),
                int(mesh.indices[base + 1]),
                int(mesh.indices[base + 2]),
            )
        except (IndexError, TypeError):
            self.log_status(
                f'Pick Tri  (mesh {mesh_idx} tri {tri_idx} -- '
                f'index out of range)')
            return

        # Fresh dump for this triangle.
        self.log_clear(
            status=f"Pick Tri  -- '{mesh.name}'  tri {tri_idx}  "
                   f"verts {v_ids[0]}/{v_ids[1]}/{v_ids[2]}")
        for i, vid in enumerate(v_ids):
            text, color = format_vertex_line('#', i, mesh, vid)
            self.log(text, color=color)

    def _ensure_itemlist(self):
        """Auto-rebuild the lookup table if it's missing or empty.

        Called once at the end of `__init__`, AFTER the UI has been
        built, so the user sees the rebuild progress stream into the
        in-app console rather than wondering why the splash is hung.
        Skipped silently when the lookup is healthy.

        Triggers in two cases:
          * the lookup XML path doesn't exist on disk (fresh checkout
            -- the file is .gitignored because it's machine-specific
            and 70+ MB, so first-run users always hit this branch),
          * the file exists but parsed to zero entries (corrupted /
            wrong-version file, anything that means the runtime dict
            is empty).
        """
        ext = self._pkg_extractor
        if ext is None:
            return

        out_path = getattr(ext, '_lookup_xml_path', None)
        if not out_path:
            return

        has_file    = os.path.isfile(out_path)
        has_entries = bool(getattr(ext, '_file_to_pkg', None))

        if has_file and has_entries:
            return

        self.log_status('ItemList missing -- auto-rebuild')
        if not has_file:
            self.log(f"{os.path.basename(out_path)} not found -- "
                     f"building from pkgs (one-time, ~3 s).")
        else:
            self.log(f"{os.path.basename(out_path)} parsed empty -- "
                     f"rebuilding.")
        self._rebuild_itemlist_now()

    # ------------------------------------------------------------------
    def _on_compare_clicked(self):
        """Action-button callback for the 'Compare' button.

        Opens a side-by-side stats window listing every mesh in both
        the FBX set and the PKG set: face count, vertex count, index
        count, 2nd-UV presence, and vertex-format string.

        Uses tkinter (already a dependency for the Export / Import
        file dialogs) so we don't have to invent OpenGL widgets for a
        scrollable monospace grid.  Always opens fresh -- closing the
        compare window doesn't free anything since tk owns the lifecycle.
        """
        # Fresh in-app console buffer for this action.
        self.log_clear(status='Compare')

        fbx = self._fbx_set
        pkg = self._pkg_set
        if not fbx.is_loaded() and not pkg.is_loaded():
            self.log_error("Compare: both sets are empty -- load a tank "
                           "or import an FBX first.")
            return

        try:
            import tkinter as tk
            from tkinter import ttk, font as tkfont
        except ImportError:
            self.log_error("Compare: tkinter not available -- can't show "
                           "window.  Falling back to console dump only.")
            # Fall back to console-only dump
            self._print_compare_to_console()
            return

        rows = self._build_compare_rows()

        win = tk.Tk()
        win.title("Compare: FBX (imported) vs PKG (WoT-loaded)")
        try:
            win.attributes('-topmost', True)
        except Exception:
            pass
        win.geometry('1100x600')

        mono = tkfont.Font(family='Consolas', size=10)

        # Header line that sits ABOVE the grid -- summarises the two
        # sets by source-tank name and total counts.  Read from the
        # MeshSets directly rather than the rows so an empty side
        # still gets a meaningful "(empty)" label.
        def _summary(meshset, label):
            if not meshset.is_loaded():
                return f"{label}: (empty)"
            v = sum(len(m.positions) for m in meshset.meshes)
            i = sum(len(m.indices)   for m in meshset.meshes)
            tk_name = meshset.source_tank_name or '?'
            return (f"{label}: {tk_name}  "
                    f"meshes={len(meshset.meshes)}  "
                    f"verts={v}  inds={i}")
        hdr_text = (_summary(fbx, 'FBX')
                    + '    |    '
                    + _summary(pkg, 'PKG'))
        tk.Label(win, text=hdr_text, font=mono,
                 anchor='w', padx=8, pady=4).pack(fill='x')

        # Use a Treeview for the row grid -- gives us auto-stretching
        # columns, hover highlighting, and a built-in vertical scrollbar.
        cols = ('idx',
                'fbx_name',  'fbx_v', 'fbx_f', 'fbx_i',
                'fbx_uv2',   'fbx_col', 'fbx_fmt',
                'pkg_name',  'pkg_v', 'pkg_f', 'pkg_i',
                'pkg_uv2',   'pkg_col', 'pkg_fmt')
        tv = ttk.Treeview(win, columns=cols, show='headings', height=24)
        widths = {
            'idx':       40,
            'fbx_name': 170, 'fbx_v': 60, 'fbx_f': 60, 'fbx_i': 60,
            'fbx_uv2':   40, 'fbx_col': 40, 'fbx_fmt': 90,
            'pkg_name': 170, 'pkg_v': 60, 'pkg_f': 60, 'pkg_i': 60,
            'pkg_uv2':   40, 'pkg_col': 40, 'pkg_fmt': 90,
        }
        headings = {
            'idx':      '#',
            'fbx_name': 'FBX name', 'fbx_v': 'verts',  'fbx_f':   'faces',
            'fbx_i':    'inds',     'fbx_uv2': 'UV2',  'fbx_col': 'Col',
            'fbx_fmt':  'fmt',
            'pkg_name': 'PKG name', 'pkg_v': 'verts',  'pkg_f':   'faces',
            'pkg_i':    'inds',     'pkg_uv2': 'UV2',  'pkg_col': 'Col',
            'pkg_fmt':  'fmt',
        }
        for c in cols:
            tv.heading(c, text=headings[c])
            tv.column(c, width=widths[c], anchor='w')

        sb = ttk.Scrollbar(win, orient='vertical', command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        tv.pack(side='left', fill='both', expand=True)

        # Stripe rows where the two sides MISMATCH on counts -- gives
        # the user a quick visual scan for ordering errors before
        # writing back to .primitives_processed.
        tv.tag_configure('mismatch', background='#fff0e0')

        for row in rows:
            tag = 'mismatch' if row.get('mismatch') else ''
            tv.insert('', 'end', tags=(tag,), values=(
                row['idx'],
                row['fbx_name'], row['fbx_v'],   row['fbx_f'], row['fbx_i'],
                row['fbx_uv2'],  row['fbx_col'], row['fbx_fmt'],
                row['pkg_name'], row['pkg_v'],   row['pkg_f'], row['pkg_i'],
                row['pkg_uv2'],  row['pkg_col'], row['pkg_fmt'],
            ))

        # Also dump to console for the lazy / for log capture.
        self._print_compare_to_console(rows)

        # Tk's modal-vs-non-modal dance: this is non-modal so the user
        # can keep flipping the viewer.  win.mainloop() would block
        # the main pygame loop -- update() in a Tk after()-loop instead.
        def _pump():
            try:
                win.update()
                win.after(33, _pump)
            except tk.TclError:
                pass   # window was closed
        win.after(33, _pump)

    def _build_compare_rows(self):
        """Return a list of dicts -- one per side-by-side row -- each
        carrying both the FBX-side and PKG-side stats for that index.

        When one set has more meshes than the other, the shorter side
        is padded with '-' placeholders so the grid stays rectangular.
        Mark a row mismatch=True when the two sides disagree on vertex
        or index count; the dialog highlights those for the user.
        """
        def _mesh_stats(mesh):
            """vert / face / index / uv2 / col / fmt-string for one Mesh.

            UV2 and colour presence are reported from the parsed data
            on the mesh (mesh.uv1, mesh.colour) -- not the format
            string -- so we know each was both DECLARED in the source
            AND parsed successfully.  Imported FBX rows show UV2 = Y
            when the FBX carried a 2nd UV layer; Col stays N for FBX
            until we add per-vertex-colour round-trip via Blender.
            """
            v = len(mesh.positions) if mesh.positions is not None else 0
            i = len(mesh.indices)   if mesh.indices   is not None else 0
            f = i // 3
            uv2 = 'Y' if getattr(mesh, 'uv1',    None) is not None else 'N'
            col = 'Y' if getattr(mesh, 'colour', None) is not None else 'N'
            fmt = getattr(mesh, 'format', '') or ''
            # Cap at 22 chars to match the console-print column.  WoT's
            # longest catalogued format string (BPVTxyznuvuviiiwwtb) is
            # 20 chars, so this trims only set-3-prefixed legacy stuff.
            short_fmt = fmt[:22] if fmt else '-'
            return v, f, i, uv2, col, short_fmt

        def _name(mesh, idx):
            return (getattr(mesh, 'identifier', '')
                    or getattr(mesh, 'name', '')
                    or f'mesh_{idx}')

        fbx_meshes = self._fbx_set.meshes
        pkg_meshes = self._pkg_set.meshes
        n = max(len(fbx_meshes), len(pkg_meshes))
        rows = []
        for i in range(n):
            fbx = fbx_meshes[i] if i < len(fbx_meshes) else None
            pkg = pkg_meshes[i] if i < len(pkg_meshes) else None

            if fbx is not None:
                fv, ff, fi, fuv, fcol, ffmt = _mesh_stats(fbx)
                fname = _name(fbx, i)
            else:
                fv = ff = fi = '-'
                fuv = fcol = ffmt = '-'
                fname = '-'

            if pkg is not None:
                pv, pf, pi, puv, pcol, pfmt = _mesh_stats(pkg)
                pname = _name(pkg, i)
            else:
                pv = pf = pi = '-'
                puv = pcol = pfmt = '-'
                pname = '-'

            mismatch = (fbx is not None and pkg is not None
                        and (fv != pv or fi != pi))

            rows.append({
                'idx':      i,
                'fbx_name': fname, 'fbx_v': fv, 'fbx_f': ff, 'fbx_i': fi,
                'fbx_uv2':  fuv,   'fbx_col': fcol, 'fbx_fmt': ffmt,
                'pkg_name': pname, 'pkg_v': pv, 'pkg_f': pf, 'pkg_i': pi,
                'pkg_uv2':  puv,   'pkg_col': pcol, 'pkg_fmt': pfmt,
                'mismatch': mismatch,
            })
        return rows

    def _print_compare_to_console(self, rows=None):
        """Dump the compare grid into the in-app console panel.

        Layout (one mesh per BLOCK, two lines + blank):
            #N  FBX  <name>   v=...  f=...  inds=...  UV2=Y  Col=N  fmt=...
            #N  PKG  <name>   v=...  f=...  inds=...  UV2=Y  Col=N  fmt=...
            <blank>

        When the FBX and PKG counts disagree on that row, the FBX
        line gets a burnt-orange background (white text) so the
        user can scan for misalignment without reading numbers.
        Burnt orange matches the rest of the app's accent colour.
        """
        if rows is None:
            rows = self._build_compare_rows()

        # Burnt orange (#CC5500 = 204,85,0) normalised to 0..1 for the
        # bg quad.  White foreground reads cleanly against it.
        BG_BURNT = (204 / 255.0, 85 / 255.0, 0 / 255.0)
        FG_WHITE = (255, 255, 255)

        self.log("=" * 80)
        self.log("COMPARE: FBX (imported) vs PKG (WoT-loaded)")
        self.log("=" * 80)

        n_mismatch = 0
        n_total    = len(rows)
        for r in rows:
            mismatch = bool(r.get('mismatch'))

            def _fmt(side, name, v, f, i, uv2, col, fmt):
                return (f"#{r['idx']:>2}  {side}  "
                        f"{str(name)[:22]:22}  "
                        f"v={str(v):>6}  f={str(f):>6}  inds={str(i):>7}  "
                        f"UV2={uv2}  Col={col}  fmt={str(fmt)[:22]}")

            fbx_line = _fmt('FBX',
                            r['fbx_name'], r['fbx_v'], r['fbx_f'],
                            r['fbx_i'],    r['fbx_uv2'], r['fbx_col'],
                            r['fbx_fmt'])
            pkg_line = _fmt('PKG',
                            r['pkg_name'], r['pkg_v'], r['pkg_f'],
                            r['pkg_i'],    r['pkg_uv2'], r['pkg_col'],
                            r['pkg_fmt'])

            # FBX row: highlight on mismatch.  PKG row stays default.
            if mismatch:
                self.log(fbx_line, color=FG_WHITE, bg_color=BG_BURNT)
                n_mismatch += 1
            else:
                self.log(fbx_line)
            self.log(pkg_line)
            self.log('')   # blank separator between mesh blocks

        self.log("=" * 80)
        # Header status reflects the result at a glance.
        if n_mismatch:
            self.log_status(f"Compare: {n_mismatch}/{n_total} mismatch")
        else:
            self.log_status(f"Compare: {n_total} rows, all match")

    def _on_paths_saved(self, values):
        """Called when the user clicks Save in the paths dialog.

        Persists the new values, rebuilds the PkgExtractor, repopulates
        the tank-browser tree, and clears the cached display-name +
        thumbnail-resolution maps so they get rebuilt against the new
        installation.
        """
        # Update + persist config
        for k in ('pkg_dir', 'res_mods', 'lookup_xml'):
            self._cfg[k] = (values.get(k) or '').strip()
        try:
            _config.save(self._cfg)
        except Exception as exc:
            print(f"[viewer] config save failed: {exc}")

        # Reset caches that depend on the paths
        self._thumb_basenames   = None
        self._thumb_path_cache  = {}
        self._tanks_display_map = {}

        # Tear down the old extractor (frees temp dir) and build a fresh one
        if self._pkg_extractor:
            try:
                self._pkg_extractor.cleanup()
            except Exception:
                pass
            self._pkg_extractor = None
        self._init_pkg_extractor_early()

        # Re-bind the armor-colour loader to the new extractor and
        # discard its cache so the next load_vehicle re-parses
        # base_paints.xml from the freshly configured WoT install.
        from .armor_colors import ArmorColorLoader
        self._armor_loader = ArmorColorLoader(self._pkg_extractor)

        # Rebuild the tier-tree array against the new installation.
        # _build_all_tier_trees handles cleanup of the previously-cached
        # trees internally, so we only drop the live pointer + tab bar
        # here.  (self.ui.tree references a cached tree -- don't .cleanup()
        # it directly or the cache would double-free.)
        self.ui.tree = None
        if self.ui.tab_bar:
            self.ui.tab_bar.cleanup()
            self.ui.tab_bar = None
        self._build_all_tier_trees()

    # Vehicle-class icons live in gui-part*.pkg.  The same logical
    # /gui/maps/icons/vehicleTypes/24x24/<class>.png is split across
    # 4 archives, so we ask PkgExtractor to try each one in turn.
    _CLASS_ICON_PKGS = (
        'gui-part1.pkg',
        'gui-part2.pkg',
        'gui-part3.pkg',
        'gui-part4.pkg',
    )
    _CLASS_ICON_NAMES = (
        'lightTank',
        'mediumTank',
        'heavyTank',
        'AT-SPG',
        'SPG',
    )

    def _populate_class_icons(self, tree=None):
        """Extract the 5 vehicle-class icons and upload them as GL textures.

        Source: gui/maps/icons/vehicleTypes/white/36x36/<vclass>.png
        (white silhouettes, the largest stock size — they downscale to
        the row height with the bilinear filter set in _load_thumb_texture).

        Each icon ends up at tree.class_icons[vclass] = (tex_id, w, h).
        Missing icons or extraction failures are silently skipped --
        _render_tree falls back to an empty icon slot in that case.

        Args:
            tree: optional explicit UITreeView to populate.  Defaults
                to self.ui.tree.  Passed explicitly when building the
                per-tier tree array (each tree gets its own icon copies).
        """
        if tree is None:
            tree = self.ui.tree
        if tree is None or self._pkg_extractor is None:
            return
        loaded = 0
        for vclass in self._CLASS_ICON_NAMES:
            internal = f'gui/maps/icons/vehicleTypes/white/36x36/{vclass}.png'
            local = self._pkg_extractor.extract_from_pkg_group(
                self._CLASS_ICON_PKGS, internal)
            if not local:
                continue
            tex_id, w, h = self._load_thumb_texture(local)
            if tex_id:
                tree.class_icons[vclass] = (tex_id, w, h)
                loaded += 1
        if loaded:
            print(f"[viewer] tree class icons: loaded {loaded}/"
                  f"{len(self._CLASS_ICON_NAMES)}")

    def _load_tanks_txt(self, valid_basenames):
        """Parse the Tank Exporter tanks.txt export.

        File format (fixed-width 30-char ID column):

            -= TANKS FOUND IN GAME =-
            ---- Tier : 01 ----
            A01_T1_Cunningham             :T1
            ...

        Long XML basenames are truncated at 30 chars, so any name that
        doesn't match a list.xml entry directly is resolved by prefix
        against *valid_basenames*.

        Args:
            valid_basenames (set[str]): every XML basename present in
                list.xml across all nations -- used as the resolution
                target for truncated names.

        Returns:
            dict[str, str]  -- {full_xml_basename: display_name}
        """
        result = {}
        if not os.path.isfile(self.TANKS_TXT):
            print(f"[viewer] tanks.txt not found at {self.TANKS_TXT} "
                  "-- tree filter disabled")
            return result

        tier_re = re.compile(r'----\s*Tier\s*:\s*(\d+)\s*----')
        try:
            with open(self.TANKS_TXT, 'r', encoding='utf-8',
                      errors='replace') as fh:
                for line in fh:
                    line = line.rstrip()
                    if not line or line.startswith('-='):
                        continue
                    if tier_re.match(line):
                        continue
                    if ':' not in line:
                        continue
                    colon = line.find(':')
                    name  = line[:colon].rstrip()
                    disp  = line[colon + 1:].strip()
                    if not name:
                        continue
                    if name in valid_basenames:
                        result[name] = disp
                    else:
                        # Truncated to 30 chars -- resolve by prefix
                        for v in valid_basenames:
                            if v.startswith(name):
                                result[v] = disp
        except Exception as exc:
            print(f"[viewer] tanks.txt parse failed: {exc}")
            return {}
        return result

    def _build_tab_bar(self):
        """Create the tier-filter tab strip (idempotent -- safe to call
        multiple times, e.g. after a window resize or path change)."""
        if self.ui.tab_bar is not None:
            self.ui.tab_bar.cleanup()
            self.ui.tab_bar = None
        bar = UITabBar(
            x=self.width - self.TREE_PANEL_W,
            y=self.ui.BAR_HEIGHT,
            w=self.TREE_PANEL_W,
            labels=self.TREE_TIER_TABS,
        )
        # Default to tier 1 (or wherever active_index lands)
        bar.active_index = 0
        bar.on_change    = self._on_tier_tab_changed
        self.ui.tab_bar  = bar

    def _on_tier_tab_changed(self, new_index):
        """Tab-bar callback: rebuild the tree filtered to the picked tier."""
        # Cached trees: one UITreeView per tier tab, built once at startup
        # (or after a path change).  Tab clicks just swap pointers; no
        # rebuild, no GPU texture churn, and selection/scroll/hover state
        # persists per-tab.  Out-of-range / unbuilt entries fall through
        # to a build-on-demand fallback below.
        if (0 <= new_index < len(self._tier_tree_cache)
                and self._tier_tree_cache[new_index] is not None):
            self.ui.tree = self._tier_tree_cache[new_index]
            return
        try:
            tier = int(self.TREE_TIER_TABS[new_index])
        except (ValueError, IndexError):
            return
        # Cold build for missing entry (shouldn't normally happen --
        # _build_all_tier_trees fills the array at startup).
        tree = self._build_tier_tree(tier)
        if 0 <= new_index < len(self._tier_tree_cache):
            self._tier_tree_cache[new_index] = tree
        self.ui.tree = tree

    def _build_tier_tree(self, tier_filter):
        """Build and return a fresh UITreeView filtered to *tier_filter*.

        Doesn't touch self.ui.tree.  Caller is responsible for caching
        and pointer-swapping.  Used by _build_all_tier_trees at startup
        and as the cold-build fallback in _on_tier_tab_changed.
        """
        tab_h = UITabBar.HEIGHT if self.ui.tab_bar else 0
        tree = UITreeView(
            x=self.width - self.TREE_PANEL_W,
            y=tab_h,
            w=self.TREE_PANEL_W,
            h=max(1, self.height - tab_h - self.RIGHT_CONTROLS_H),
        )
        tree.on_select       = self._on_tree_tank_selected
        tree.on_hover_change = self._on_tree_hover_change

        if self._pkg_extractor is None:
            return tree

        # Per-tree class icons (cheap; PkgExtractor caches the file
        # extractions, the per-tree cost is just 5 small GPU uploads).
        self._populate_class_icons(tree)

        # Reuse the cached tank list / tanks.txt map across all tiers
        nations = self._pkg_extractor.list_vehicle_xmls(with_tier=True)
        all_basenames = set()
        for entries in nations.values():
            for e in entries:
                all_basenames.add(e['xml'][:-4])
        if not self._tanks_display_map:
            self._tanks_display_map = self._load_tanks_txt(all_basenames)
            print(f"[viewer] tanks.txt: "
                  f"{len(self._tanks_display_map)} active tanks")
        tanks_txt_map = self._tanks_display_map
        # Fallback: if tanks.txt is missing OR parsed empty (file
        # got deleted, fresh checkout where the tracked file is
        # gone, etc.), don't collapse the tree to nothing.  Treat
        # EVERY list.xml entry as in-scope and resolve each tank's
        # `userString` ref against WoT's gettext catalogs to get a
        # friendly localized name -- same source the in-game UI
        # uses, so the tree reads identically to what the user sees
        # in their WoT client (in their chosen language).  When the
        # localizer isn't available (no WoT path, missing .mo) we
        # fall back to the bare basename.
        no_tanks_txt = not tanks_txt_map
        if no_tanks_txt:
            if self._localizer.is_available:
                print(f"[viewer] tanks.txt missing/empty -- "
                      f"resolving display names via WoT's "
                      f"localization catalogs (lc_messages/*.mo)")
            else:
                print(f"[viewer] tanks.txt missing/empty AND no "
                      f"localization catalogs available -- using "
                      f"basenames for tree labels")

        skipped = 0
        for nation in sorted(nations.keys()):
            entries = nations[nation]
            children = []
            for entry in entries:
                xml_name = entry['xml']
                base     = xml_name[:-4]
                if not no_tanks_txt and base not in tanks_txt_map:
                    skipped += 1
                    continue
                tier = entry.get('tier')
                if tier_filter is not None and tier != tier_filter:
                    continue
                # tanks.txt label > localized userString > basename.
                # The localizer auto-falls-through to the bare key
                # when its catalog is missing, so we never get None
                # back -- worst case we get the same string the
                # basename branch would have returned.
                if base in tanks_txt_map:
                    display = tanks_txt_map[base]
                else:
                    user_str = entry.get('user_string')
                    display  = (
                        self._localizer.lookup_basename(user_str)
                        if user_str else base
                    ) or base
                vclass  = entry.get('vclass')
                tier_str   = f"T{tier:>2}" if tier is not None else "T--"
                tank_label = f"{tier_str}  {base}"
                children.append(UITreeNode(
                    label=tank_label,
                    payload={
                        'nation':  nation,
                        'xml':     xml_name,
                        'tier':    tier,
                        'vclass':  vclass,
                        'display': display,
                    },
                ))
            if not children:
                continue
            nation_node = UITreeNode(
                label=f"{nation}  ({len(children)})",
                children=children,
                payload={'nation': nation},
            )
            tree.add_root(nation_node)
        if skipped and tier_filter is None:
            # Only log the "skipped tanks not in tanks.txt" once; it
            # would repeat for every tier otherwise.
            print(f"[viewer] tree: skipped {skipped} tanks "
                  f"(not in tanks.txt)")
        return tree

    def _build_all_tier_trees(self):
        """Pre-build one UITreeView per tier tab and cache them in
        self._tier_tree_cache.  Renders a frame per tier so the tab
        currently being built shows up amber/yellow -- the user gets
        progress feedback during what would otherwise be a 1-2 second
        startup freeze.

        Idempotent: cleans up any previously cached trees first, so
        the same method handles startup AND path-change rebuilds.
        """
        # Clear the live pointer first -- render() loops below would
        # otherwise draw a mid-cleanup tree (labels nullified, class_icons
        # emptied) which both looks wrong and forces wasteful texture
        # rebuilds via UITreeView.ensure_textures.
        self.ui.tree = None

        # Drop any previously cached trees (frees their GPU textures).
        old = getattr(self, '_tier_tree_cache', None)
        if old:
            for t in old:
                if t is None:
                    continue
                try:
                    t.cleanup()
                except Exception as exc:
                    print(f"[viewer] tier-tree cleanup failed: {exc}")

        self._tier_tree_cache = [None] * len(self.TREE_TIER_TABS)

        # Lazy tab bar so the build-progress highlight has somewhere to draw
        if self.ui.tab_bar is None:
            self._build_tab_bar()

        if self._pkg_extractor is None:
            print("[viewer] No PKG extractor -- tier trees will be empty")
            return

        print(f"[viewer] building {len(self.TREE_TIER_TABS)} tier trees...")
        # Roman-numeral tier labels for the splash log -- much more
        # readable than '1' / '2' / ... and matches the in-app tab labels.
        _ROMAN = ['I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII',
                  'IX', 'X', 'XI']
        for i, tier_str in enumerate(self.TREE_TIER_TABS):
            try:
                tier = int(tier_str)
            except ValueError:
                continue
            # Visual progress: amber-highlight this tab while we build it
            self.ui.tab_bar.building_index = i
            try:
                pygame.event.pump()
            except Exception:
                pass
            try:
                self.render()
            except Exception as exc:
                print(f"[viewer] progress render failed: {exc}")

            # Splash status: per-tier line so the user sees the build
            # advancing one tier at a time.
            roman = _ROMAN[i] if 0 <= i < len(_ROMAN) else str(tier)
            self._splash_status(f"  Tier {roman} ({tier_str}) -- "
                                f"scanning vehicle XMLs...")

            tree = self._build_tier_tree(tier)
            self._tier_tree_cache[i] = tree

            # Count leaf rows so the user sees a real "found N tanks"
            # number rather than a generic "done" -- helps spot when a
            # tier is suspiciously empty (e.g. mod messed something up).
            leaf_count = self._count_tree_leaves(tree)
            self._splash_status(f"  Tier {roman}: {leaf_count} tank(s)")

        # Done -- clear the indicator and snap to the active tab's tree
        self.ui.tab_bar.building_index = -1
        idx = self.ui.tab_bar.active_index
        if 0 <= idx < len(self._tier_tree_cache):
            self.ui.tree = self._tier_tree_cache[idx]
        print("[viewer] tier trees ready")
        # Final splash summary
        total = sum(self._count_tree_leaves(t)
                    for t in self._tier_tree_cache if t is not None)
        self._splash_status(f"Tank tree built: {total} tanks across "
                            f"{len(self.TREE_TIER_TABS)} tiers")

    # ------------------------------------------------------------------
    @staticmethod
    def _count_tree_leaves(tree):
        """Count terminal leaves (tank rows) in a UITreeView -- recursive
        walk over node.children since branches (nation rows) aren't
        themselves loadable.  Used by the splash log to surface a real
        per-tier tank count instead of a generic "done".  Returns 0 on
        any failure (defensive -- splash status should never crash startup).
        """
        if tree is None:
            return 0
        try:
            def _walk(node):
                kids = getattr(node, 'children', None) or []
                if not kids:
                    return 1
                return sum(_walk(c) for c in kids)
            roots = getattr(tree, 'roots', None)
            if roots is None:
                # Some trees expose .nodes instead of .roots; fall back.
                roots = getattr(tree, 'nodes', None) or []
            return sum(_walk(r) for r in roots)
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Left info panel (tank stats / description, collapsible sections)
    # ------------------------------------------------------------------

    # Order + display configuration of the info-panel sections.
    # Each entry: (section_label, dict_key, [(field_label, dict_key), ...]).
    # Fields whose dict_key isn't present in the data dict are skipped.
    _INFO_SECTIONS = [
        ('Hull', 'hull', [
            ('HP',           'maxHealth'),
            ('Weight',       'weight'),
            ('Primary armor','primaryArmor'),
        ]),
        ('Chassis', 'chassis', [
            ('Tier',         'level'),
            ('Price',        'price'),
            ('HP',           'maxHealth'),
            ('Weight',       'weight'),
            ('Rotation',     'rotationSpeed'),
            ('Brake force',  'brakeForce'),
            ('Climb angle',  'maxClimbAngle'),
            ('Terrain',      'terrainResistance'),
            ('Repair cost',  'repairCost'),
            ('Repair time',  'repairTime'),
        ]),
        ('Turret', 'turret', [
            ('Tier',         'level'),
            ('Price',        'price'),
            ('HP',           'maxHealth'),
            ('Weight',       'weight'),
            ('Rotation',     'rotationSpeed'),
            ('View range',   'circularVisionRadius'),
            ('Camo',         'invisibilityFactor'),
        ]),
        ('Gun', 'gun', [
            ('Tier',         'level'),
            ('Price',        'price'),
            ('HP',           'maxHealth'),
            ('Weight',       'weight'),
            ('Reload',       'reloadTime'),
            ('Aim time',     'aimingTime'),
            ('Dispersion',   'shotDispersionRadius'),
            ('Ammo',         'maxAmmo'),
            ('Pitch min',    'minPitch'),
            ('Pitch max',    'maxPitch'),
            ('Yaw limits',   'turretYawLimits'),
            ('Camo on shot', 'invisibilityFactorAtShot'),
        ]),
        ('Engine', 'engine', [
            ('Tier',         'level'),
            ('Price',        'price'),
            ('Power',        'power'),
            ('Type',         'tags'),
            ('Weight',       'weight'),
            ('HP',           'maxHealth'),
            ('Fire chance',  'fireStartingChance'),
            ('RPM min',      'rpm_min'),
            ('RPM max',      'rpm_max'),
            ('Repair cost',  'repairCost'),
        ]),
        ('Radio', 'radio', [
            ('Tier',         'level'),
            ('Price',        'price'),
            ('Range',        'distance'),
            ('Weight',       'weight'),
            ('HP',           'maxHealth'),
            ('Repair cost',  'repairCost'),
        ]),
        ('Fuel tank', 'fueltank', [
            ('Price',        'price'),
            ('Weight',       'weight'),
            ('HP',           'maxHealth'),
        ]),
    ]

    def _build_info_panel(self, info):
        """(Re)build the left-hand info panel for the just-loaded tank.

        Sections are built as collapsible UITreeView branches; their
        children are leaf rows of the form 'label: value'.  All sections
        start collapsed so the panel is compact -- click a section
        header to expand it.

        Args:
            info (dict): output of VehicleXMLLoader.parse_info(), or None
                         to clear the panel.
        """
        # Tear down old panel if any
        if self.ui.info_panel is not None:
            self.ui.info_panel.cleanup()
            self.ui.info_panel = None

        # The info panel sits BELOW the left-side control block (buttons,
        # lighting sliders, NMap/AO).  Using LEFT_CONTROLS_H here is
        # important because tank loads call _build_info_panel without a
        # follow-up _on_resize -- if we used BAR_HEIGHT (=0) the freshly
        # built panel would render at y=0, on top of the control buttons.
        panel = UITreeView(
            x=0,
            y=self.LEFT_CONTROLS_H,
            w=self.INFO_PANEL_W,
            h=max(1, self.height - self.LEFT_CONTROLS_H),
            show_thumb_area=False,
        )
        self.ui.info_panel = panel

        if not info:
            return

        # ---- Top-level header rows (tank id + nation, always visible) --
        hdr = info.get('header', {})
        if hdr.get('xml'):
            xml_basename = hdr['xml']
            if xml_basename.lower().endswith('.xml'):
                xml_basename = xml_basename[:-4]
            display = self._tanks_display_map.get(xml_basename, xml_basename)
            panel.add_root(UITreeNode(label=f"Tank: {display}"))
            panel.add_root(UITreeNode(label=f"Nation: {hdr.get('nation','')}"))
            panel.add_root(UITreeNode(label=f"ID: {xml_basename}"))

        # ---- Stat sections ---------------------------------------------
        for section_label, key, fields in self._INFO_SECTIONS:
            data = info.get(key) or {}
            children = []
            for field_label, field_key in fields:
                val = data.get(field_key, '')
                if not val:
                    continue
                children.append(UITreeNode(
                    label=f"  {field_label}: {val}"))
            if not children:
                continue
            section = UITreeNode(
                label=f"{section_label}  ({len(children)})",
                children=children,
            )
            panel.add_root(section)

        # ---- Shells (variable-length list) -----------------------------
        shells = info.get('shells') or []
        if shells:
            shell_children = []
            for s in shells:
                # Build a small sub-branch per shell so each shell's stats
                # collapse independently.
                stats = []
                for label, key in (
                    ('Kind',         'kind'),
                    ('Caliber',      'caliber'),
                    ('Damage',       'damageArmor'),
                    ('Module dmg',   'damageDevices'),
                    ('Penetration',  'piercingPower'),
                    ('Speed',        'speed'),
                    ('Max distance', 'maxDistance'),
                    ('Price',        'price'),
                    ('Ricochet',     'ricochetAngle'),
                ):
                    v = s.get(key, '')
                    if v:
                        stats.append(UITreeNode(label=f"  {label}: {v}"))
                shell_children.append(UITreeNode(
                    label=s.get('tag', '<shell>'),
                    children=stats,
                ))
            shells_section = UITreeNode(
                label=f"Shells  ({len(shell_children)})",
                children=shell_children,
            )
            panel.add_root(shells_section)

        # (Mesh visibility toggles live in the dedicated UIMeshWindow,
        # opened via the "Meshes" bar button -- see _toggle_mesh_window
        # / _populate_mesh_window below.  Keeping them out of this read-
        # only stats panel avoids accidental clicks while reading specs.)

    # ------------------------------------------------------------------
    # Tree-view callbacks
    # ------------------------------------------------------------------

    def _clear_selection_in_other_trees(self):
        """Null `selected_node` on every cached per-tier tree EXCEPT
        the currently-active one.

        The tree panel highlights its `selected_node` in burnt
        orange so the user can spot the loaded tank's row at a
        glance.  Each tier tab owns its own UITreeView though, so
        without this helper a stale highlight lingered on every
        other tab -- last week's tier-5 pick still glowing while a
        tier-7 tank sat in the viewport.  Now the highlight is
        guaranteed to be on the same tab as the tank that's
        actually loaded.

        Called once per successful load (from `_on_tree_tank_selected`'s
        `_on_load` closure) so a cancelled modal leaves the previous
        load's highlight untouched -- the loaded tank IS still the
        previous one, the indicator should match.
        """
        active = self.ui.tree
        cache  = getattr(self, '_tier_tree_cache', None) or ()
        for tree in cache:
            if tree is None or tree is active:
                continue
            tree.selected_node = None

    def _on_tree_tank_selected(self, node):
        """User clicked a leaf (tank) row -- pull options + show the
        per-skin Load picker dialog."""
        if self._pkg_extractor is None:
            print("[viewer] No PKG extractor available -- cannot load tank")
            return

        # Re-entrancy guard.  Drops clicks while a load is already
        # in flight so the user can't stack a second load on top of
        # the first.  Flag is cleared (success OR error) by
        # `_load_tank_with_options`'s try/finally below.
        if self._tank_loading:
            try:
                self.log_status('Load in progress -- click ignored')
            except Exception:
                print("[viewer] tree click ignored -- load in progress")
            return

        info       = node.payload or {}
        nation     = info.get('nation', '')
        xml_name   = info.get('xml',    '')
        tank_label = node.label
        if not (nation and xml_name):
            return

        # Extract the vehicle XML once now so list_options can read it,
        # and we can hand the local path to the load callback later.
        zip_path = f"scripts/item_defs/vehicles/{nation}/{xml_name}"
        local_path = self._pkg_extractor.extract_from_pkg(
            'scripts.pkg', zip_path)
        if not local_path:
            print(f"[viewer] Failed to extract {zip_path}")
            return

        try:
            options = VehicleXMLLoader.list_options(local_path)
        except Exception as exc:
            print(f"[viewer] list_options failed: {exc}")
            return

        # Closure binding the local_path so the dialog callback can call
        # load_vehicle with the chosen overrides.
        def _on_load(skin, chassis_tag, turret_tag, gun_tag, damaged):
            # Status callback flushes one frame per update so the user
            # sees the dialog's bottom strip change while the load runs.
            def _status(msg):
                self.ui.load_dialog.set_status(msg, make_tex=self.ui._make_tex)
                try:
                    self.render()
                except Exception:
                    pass

            # Pull the res_mods preference off the dialog at the
            # moment the user clicks Load (might differ from when
            # the dialog opened).  Persist it so the next dialog
            # opens with the same toggle state.
            prefer_rm = bool(self.ui.load_dialog.prefer_res_mods)
            if bool(self._cfg.get('load_from_res_mods', True)) != prefer_rm:
                self._cfg['load_from_res_mods'] = prefer_rm
                try:
                    _config.save(self._cfg)
                except Exception as exc:
                    print(f"[viewer] config save failed: {exc}")

            # Fresh console for this load action.  Header tag tells the
            # user which tank we're crunching on right now.
            self.log_clear(status=f'Load Tank: {tank_label}')
            _status(f"Loading {tank_label}...")
            # Strip the burnt-orange row highlight from every OTHER
            # tier tab's tree so the loaded-tank indicator only ever
            # appears on the tab that actually owns the loaded tank.
            # Doing it BEFORE the (synchronous) load means the visual
            # update lands during the load's first frame redraw
            # rather than after the full load has finished.
            self._clear_selection_in_other_trees()
            self._load_tank_with_options(
                local_path,
                skin=skin,
                chassis_tag=chassis_tag,
                turret_tag=turret_tag,
                gun_tag=gun_tag,
                damaged=damaged,
                status_callback=_status,
                prefer_res_mods=prefer_rm,
            )

        self.ui.load_dialog.show(
            title=f"{nation} / {tank_label}",
            options=options,
            on_load=_on_load,
            make_tex=self.ui._make_tex,
            prefer_res_mods=bool(self._cfg.get('load_from_res_mods', True)),
        )

    def _load_tank_with_options(self, xml_path, skin=None,
                                 chassis_tag=None, turret_tag=None,
                                 gun_tag=None, damaged=False,
                                 status_callback=None,
                                 prefer_res_mods=True):
        """Trigger load_vehicle with explicit skin / part overrides chosen
        from the load dialog.  status_callback is forwarded to the load
        so progress messages reach the dialog's bottom status line.

        Sets `self._tank_loading = True` for the duration so any
        tree clicks the user makes while the load runs are silently
        dropped (see `_on_tree_tank_selected`).  Cleared in `finally`
        so an exception still re-enables the tree -- the user
        recovers by retrying instead of being permanently locked
        out.

        prefer_res_mods (bool): forwarded to load_vehicle.  When
            False, res_mods overrides are bypassed and the loader
            reads textures + visuals straight from pkg.  Default
            True (match the load-dialog checkbox default).  This
            argument is FBX-import-blind; FBX import calls
            load_vehicle elsewhere with its own policy (always pkg).
        """
        self._tank_loading = True
        try:
            self.load_vehicle(
                xml_path,
                damaged=damaged,
                skin=skin,
                chassis_tag=chassis_tag,
                turret_tag=turret_tag,
                gun_tag=gun_tag,
                status_callback=status_callback,
                prefer_res_mods=prefer_res_mods,
            )
        finally:
            self._tank_loading = False

    # ------------------------------------------------------------------
    # Tank-thumbnail helpers
    # ------------------------------------------------------------------

    def _load_thumb_texture(self, png_path):
        """Decode a PNG and upload it as a 2D texture.

        No vertical flip is applied -- the UI overlay's ortho draws PIL's
        top-down byte order right-side-up (same convention as _make_tex
        for font glyphs).

        Returns:
            (tex_id, width, height)  -- (None, 0, 0) if the file is missing
                                        or PIL is unavailable.
        """
        if not os.path.isfile(png_path):
            return (None, 0, 0)
        try:
            from PIL import Image
        except ImportError:
            return (None, 0, 0)
        try:
            img  = Image.open(png_path).convert('RGBA')
            data = img.tobytes('raw', 'RGBA')
        except Exception as exc:
            print(f"[viewer] thumb load failed for {png_path}: {exc}")
            return (None, 0, 0)

        tex_id = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, tex_id)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, img.width, img.height, 0,
                     GL_RGBA, GL_UNSIGNED_BYTE, data)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        glBindTexture(GL_TEXTURE_2D, 0)
        return (tex_id, img.width, img.height)

    def _thumb_path_for_xml(self, xml_name):
        """Resolve a tank XML name to its thumbnail PNG path.

        Match strategy:
            1. Exact filename match  (Ch19_121.xml -> Ch19_121.png).
            2. Progressively trim the last '_TOKEN' off the basename and
               retry, e.g. Ch19_121_IGR -> Ch19_121, F10_AMX_50B_fallout
               -> F10_AMX_50B.  Special / variant suffixes (_IGR, _SH,
               _7x7, _siege_mode, ...) collapse onto the base tank.

        Results are cached -- the on-disk PNG list is enumerated once on
        first call, and every (xml_basename -> path|None) lookup is
        memoised so repeated hover events don't hit the filesystem.
        """
        if not xml_name:
            return None
        base = xml_name[:-4] if xml_name.endswith('.xml') else xml_name

        # Memoised resolution (None caches "no thumb" too)
        if base in self._thumb_path_cache:
            return self._thumb_path_cache[base]

        # Lazily enumerate the thumb folder
        if self._thumb_basenames is None:
            try:
                self._thumb_basenames = {
                    os.path.splitext(f)[0]
                    for f in os.listdir(self.THUMB_DIR)
                    if f.lower().endswith('.png')
                }
            except OSError:
                self._thumb_basenames = set()

        # Direct hit, then progressive trim
        cur   = base
        found = None
        while True:
            if cur in self._thumb_basenames:
                found = os.path.join(self.THUMB_DIR, cur + '.png')
                break
            if '_' not in cur:
                break
            cur = cur.rsplit('_', 1)[0]

        self._thumb_path_cache[base] = found
        return found

    # ------------------------------------------------------------------
    def _splash_status(self, text):
        """Update the splash-screen status line and immediately
        repaint so the user sees the change.  Pumps the OS event
        queue too -- without that, Windows can mark the app as
        "Not Responding" mid-startup if a single phase takes too long.

        No-op once the splash has been torn down (after run() starts),
        so it's safe to leave these calls in any code path; they only
        do work during the init window.
        """
        if not self.splash:
            return
        try:
            self.splash.set_status(text)
            self.splash.render()
            pygame.display.flip()
            # Drain pending OS messages so the window stays interactive.
            # event.pump() is the no-handler equivalent of event.get()
            # and is exactly what's recommended for long-running init.
            pygame.event.pump()
        except Exception as exc:
            print(f"[viewer] splash status update failed: {exc}")

    def _set_loaded_thumbnail(self, xml_basename):
        """Replace the persistent (loaded-tank) thumbnail + display label.

        Args:
            xml_basename (str|None): tank XML base name (no extension).
                                     None clears the thumbnail and label.
        """
        tree = self.ui.tree
        if tree is None:
            return
        if tree.loaded_thumb_tex:
            glDeleteTextures(1, [tree.loaded_thumb_tex])
            tree.loaded_thumb_tex = None
            tree.loaded_thumb_w   = 0
            tree.loaded_thumb_h   = 0
        # Display name comes from tanks.txt; fall back to the XML
        # basename if we don't have a TE entry for it.
        tree.loaded_thumb_name = (
            self._tanks_display_map.get(xml_basename, xml_basename)
            if xml_basename else '')
        path = self._thumb_path_for_xml(xml_basename)
        if path:
            tex_id, w, h = self._load_thumb_texture(path)
            if tex_id:
                tree.loaded_thumb_tex = tex_id
                tree.loaded_thumb_w   = w
                tree.loaded_thumb_h   = h

    def _on_tree_hover_change(self, node):
        """UITreeView fired a hover-change event.  Swap the hover thumbnail
        + display label.  Hover takes priority over the loaded slot on
        screen, so this is what the user sees while moving the cursor.

        node is None when the cursor is off the rows or hovering a branch.
        """
        tree = self.ui.tree
        if tree is None:
            return
        # Drop old hover texture (don't touch the loaded one)
        if tree.hover_thumb_tex:
            glDeleteTextures(1, [tree.hover_thumb_tex])
            tree.hover_thumb_tex = None
            tree.hover_thumb_w   = 0
            tree.hover_thumb_h   = 0
        tree.hover_thumb_name = ''

        if node is None:
            return
        info     = node.payload or {}
        xml_name = info.get('xml', '')
        # Tree leaves carry their display name in payload['display']
        tree.hover_thumb_name = info.get('display') or (
            xml_name[:-4] if xml_name.endswith('.xml') else xml_name)
        path = self._thumb_path_for_xml(xml_name)
        if not path:
            return
        tex_id, w, h = self._load_thumb_texture(path)
        if tex_id:
            tree.hover_thumb_tex = tex_id
            tree.hover_thumb_w   = w
            tree.hover_thumb_h   = h

    # ------------------------------------------------------------------
    # Scene reset (called at the start of every load_* to free old GPU state)
    # ------------------------------------------------------------------

    def _clear_scene(self):
        """Free every per-mesh GPU object (VBOs, EBO, VAO, textures) and
        reset scene-level state for the ACTIVE set so a subsequent
        load_mesh / load_vehicle / load_imported_payload on the same set
        starts with a clean slate.  Safe to call when nothing is loaded.

        The INACTIVE set is left untouched -- this lets the user load an
        FBX, then load a WoT pkg, then flip back to the FBX without
        having to re-import.  Each set has independent geometry and
        independent source-tank metadata.
        """
        # MeshSet.cleanup() walks its own mesh list, releases GPU
        # objects, and resets every per-load attribute (source_type,
        # exhaust_points, etc.) -- equivalent to the old in-place
        # property writes but scoped to a single set.
        self._active_set.cleanup()
        # Drop any lingering exhaust direction-vector lines from the
        # previously loaded vehicle so they don't render attached to
        # nothing while the next load is in progress.
        self.hp_lines.update([])
        # Clear the persistent thumbnail too -- the next successful load
        # will install the new tank's thumbnail.  Hover thumb (if any) is
        # left alone since it tracks cursor state, not load state.
        self._set_loaded_thumbnail(None)
        # Also clear the left info panel so stale stats don't linger
        # if the next load fails or is a single-mesh load_mesh().
        self._build_info_panel(None)

    # ------------------------------------------------------------------
    # Set-order alignment (foundation for primitives_processed export)
    # ------------------------------------------------------------------

    def _align_fbx_to_pkg_order(self):
        """Reorder the FBX set's meshes to match the PKG set's order.

        The primitive (.primitives_processed) format we eventually
        write back out expects components in a specific order
        (hull, gun, turret, chassis sub-meshes, ...) -- the same order
        PkgExtractor parses out of the WoT install.  A user's FBX
        edit may shuffle that order in Blender; this method puts it
        back so an export-side serialiser can iterate fbx_set.meshes
        in lock-step with the original primitive-group layout.

        Match strategy
        --------------
        Our exporter writes mesh names as f"{display}_{i}", where
        display = (mesh.identifier or mesh.name or 'mesh') and i is
        the original index.  After a Blender round-trip, the FBX
        importer hands the string back verbatim (sometimes with a
        '.001' duplicate-name suffix).  For every PKG slot we look up
        the FBX mesh whose name -- with .001 stripped -- matches the
        expected `f"{display}_{i}"` exactly.  Case insensitive.

        Unmatched PKG slots are reported (so the user can fix names
        in Blender and re-import).  Unmatched FBX meshes are appended
        at the tail in their original order so nothing is lost.

        Returns
        -------
        (matched, total_fbx, total_pkg)  -- counts only; the side
        effect is that self._fbx_set.meshes is reordered in place.
        """
        fbx = self._fbx_set
        pkg = self._pkg_set
        if not fbx.is_loaded() or not pkg.is_loaded():
            print("[align] need both FBX and PKG sets loaded -- skipped")
            return (0, len(fbx.meshes), len(pkg.meshes))

        # Build the expected-prefix list from the PKG side, mirroring
        # exporters/common.py exactly.  identifier-or-name-or-fallback,
        # then "_<original index>".
        expected = []
        for i, m in enumerate(pkg.meshes):
            display = (getattr(m, 'identifier', '')
                       or getattr(m, 'name', '')
                       or f'mesh_{i}')
            expected.append(f"{display}_{i}")

        fbx_list = list(fbx.meshes)
        consumed = [False] * len(fbx_list)

        def _norm(name):
            # Strip Blender's '.001' duplicate suffix if any, lowercase
            # for case-insensitive equality.
            return (name or '').split('.')[0].lower()

        ordered = []
        misses  = []
        for slot, prefix in enumerate(expected):
            target = prefix.lower()
            hit = None
            for j, fm in enumerate(fbx_list):
                if consumed[j]:
                    continue
                if _norm(getattr(fm, 'name', '')) == target:
                    hit = j
                    break
            if hit is None:
                misses.append((slot, prefix))
            else:
                consumed[hit] = True
                ordered.append(fbx_list[hit])

        leftover = [m for j, m in enumerate(fbx_list) if not consumed[j]]
        ordered.extend(leftover)

        fbx.meshes = ordered

        matched = len(fbx_list) - len(leftover)
        if misses:
            print(f"[align] {len(misses)} PKG slot(s) had no FBX match:")
            for slot, prefix in misses[:10]:
                print(f"        PKG[{slot}]  expected '{prefix}'")
            if len(misses) > 10:
                print(f"        ... and {len(misses) - 10} more")
        if leftover:
            print(f"[align] {len(leftover)} FBX mesh(es) had no PKG slot "
                  f"-- appended at the tail")
        print(f"[align] FBX reordered: matched={matched}, "
              f"fbx_total={len(fbx_list)}, pkg_total={len(pkg.meshes)}")
        return (matched, len(fbx_list), len(pkg.meshes))

    # ------------------------------------------------------------------
    # Active-set flip
    # ------------------------------------------------------------------

    def _flip_active_set(self):
        """Toggle the active MeshSet between FBX and PKG and rebuild the
        UI surfaces that read from it (mesh-visibility window, info
        panel, thumbnail, exhaust direction lines, smoke emitters,
        camera fit).

        No-op when the OTHER set is empty -- there's nothing to flip
        TO.  Flipping does NOT free GPU resources; both sets stay
        resident until a fresh load on either side.
        """
        other = 'fbx' if self._active_set_name == 'pkg' else 'pkg'
        target = self._fbx_set if other == 'fbx' else self._pkg_set
        if not target.is_loaded():
            print(f"[viewer] flip: {other!r} set is empty -- nothing to flip to")
            return

        self._active_set_name = other
        s = self._active_set
        print(f"[viewer] flipped active set -> {other!r} "
              f"(meshes={len(s.meshes)}, source_type={s.source_type!r}, "
              f"tank={s.source_tank_name!r})")

        # Rebuild the UI surfaces that read per-load state.
        self._populate_mesh_window()
        self._build_info_panel(s.tank_info)
        self._set_loaded_thumbnail(s.source_tank_name)

        # Re-fit the camera to the new set's bounds (if known).  Keep
        # current camera distance/orientation if no bbox -- avoids a
        # surprising jump on a half-loaded set.
        if s.scene_bbox is not None:
            mn, mx = s.scene_bbox
            self.camera.fit_to_bounds(mn, mx)
            mesh_radius = float(np.linalg.norm((mx - mn) / 2.0))
            self._min_zoom_distance = max(0.05, mesh_radius * 0.05)

        # Rebuild exhaust direction-vector lines for the new set.
        EXHAUST_VECTOR_LEN = 0.5
        cyan = (0.20, 1.00, 1.00)
        segments = []
        for hp in s.exhaust_points:
            start = tuple(float(v) for v in hp['pos'])
            end   = tuple(float(v) for v in
                          (hp['pos'] + hp['fwd'] * EXHAUST_VECTOR_LEN))
            segments.append((start, end, cyan))
        self.hp_lines.update(segments)

        # Re-point smoke emitters at the new set's exhaust hardpoints.
        if self.smoke_particles is not None:
            self.smoke_particles.reset()
            self.smoke_particles.set_emitters(s.exhaust_points)

    # ------------------------------------------------------------------
    # Mesh loading
    # ------------------------------------------------------------------

    def load_mesh(self, filepath):
        """Parse a .primitives_processed file and create GPU meshes.

        Also parses the companion .visual_processed for texture paths and
        per-material flags (alphaReference, alphaTestEnable, doubleSided).

        Args:
            filepath (str): path to the .primitives_processed file
        """
        # Wipe the console so the next-load output is uncluttered
        os.system('cls' if os.name == 'nt' else 'clear')

        # Native WoT loads always populate the PKG set.  Switch FIRST so
        # _clear_scene() only frees the previous PKG geometry; any
        # currently-loaded FBX import in the FBX set is preserved.
        self._active_set_name = 'pkg'
        self._clear_scene()        # free any previously-loaded meshes/textures
        self.source_type  = 'wot'  # primitive-loaded
        self._armor_color = None   # no nation context for single-file loads
        print(f"Loading {filepath}...")
        try:
            mesh_dir = os.path.dirname(filepath)

            # res_mods root: config override -> auto-detect from file path
            cfg_res_mods = self._cfg.get('res_mods', '').strip()
            if cfg_res_mods and os.path.isdir(cfg_res_mods):
                res_mods_root = cfg_res_mods
            else:
                parts        = mesh_dir.split(os.sep)
                res_mods_idx = next((i for i, p in enumerate(parts) if 'res_mods' in p), -1)
                if res_mods_idx >= 0:
                    res_mods_root = os.sep.join(parts[:res_mods_idx + 2])
                else:
                    res_mods_root = os.path.dirname(os.path.dirname(mesh_dir))

            # PKG extractor (lazily created, shared across load_mesh calls)
            if self._pkg_extractor is None:
                cfg_pkg_dir    = self._cfg.get('pkg_dir',    '').strip() or None
                cfg_lookup_xml = self._cfg.get('lookup_xml', '').strip() or None
                wot_root       = os.path.dirname(os.path.dirname(res_mods_root))
                self._pkg_extractor = PkgExtractor(
                    wot_root,
                    pkg_dir=cfg_pkg_dir,
                    lookup_xml=cfg_lookup_xml,
                )

            # Parse geometry
            parsed_groups = MeshParser.parse_primitives_processed(filepath)
            group_names   = [g['name'] for g in parsed_groups]

            # Parse material / texture data from visual_processed
            visual_file   = filepath.replace('.primitives_processed', '.visual_processed')
            group_textures = VisualLoader.parse_textures(visual_file, group_names)

            print("\n" + "=" * 80)
            print("MESH -> TEXTURE ASSIGNMENT DEBUG")
            print("=" * 80)
            print(f"Res_mods root   : {res_mods_root}")
            print(f"Primitive groups: {len(parsed_groups)}")

            self.meshes    = []
            all_positions  = []

            for group in parsed_groups:
                group_name = group['name']
                materials  = group_textures.get(group_name, [])
                pkg        = self._pkg_extractor   # shorthand

                print(f"\n  [{group_name}]  {len(materials)} sub-mesh material(s)")

                # Build one Mesh per sub-mesh (see _split_into_submeshes).
                for sub_idx, (synth, tex) in enumerate(
                        _split_into_submeshes(group, materials)):
                    mesh = Mesh(synth)
                    label_prefix = f"    sub#{sub_idx}"

                    # -- Diffuse texture
                    if 'diffuse' in tex:
                        resolved, used_hd = VisualLoader.resolve_hd_path(
                            tex['diffuse'], res_mods_root, pkg)
                        if resolved:
                            mesh.diffuse_tex_id = TextureLoader.load_texture(resolved)
                            mesh.diffuse_path   = resolved
                            print(f"{label_prefix} diffuse: {os.path.basename(resolved)}"
                                  f"{'[HD]' if used_hd else '[SD]'}  (id={mesh.diffuse_tex_id})")
                        else:
                            mesh.diffuse_tex_id = TextureLoader.create_placeholder(0.7)
                            print(f"{label_prefix} diffuse: [missing] {tex['diffuse']}")
                    else:
                        mesh.diffuse_tex_id = TextureLoader.create_placeholder(0.7)
                        print(f"{label_prefix} diffuse: [placeholder -- no path in visual]")

                    # -- Normal map
                    if 'normal' in tex:
                        resolved, used_hd = VisualLoader.resolve_hd_path(
                            tex['normal'], res_mods_root, pkg)
                        if resolved:
                            mesh.normal_tex_id = TextureLoader.load_texture(resolved, is_normal=True)
                            mesh.normal_path   = resolved
                            print(f"{label_prefix} normal:  {os.path.basename(resolved)}"
                                  f"{'[HD]' if used_hd else '[SD]'}  (id={mesh.normal_tex_id})")
                        else:
                            mesh.normal_tex_id = TextureLoader.create_placeholder(0.5)
                            print(f"{label_prefix} normal:  [missing] {tex['normal']}")
                    else:
                        mesh.normal_tex_id = TextureLoader.create_placeholder(0.5)
                        print(f"{label_prefix} normal:  [placeholder -- no path in visual]")

                    # -- AO map
                    if 'ao' in tex:
                        resolved, used_hd = VisualLoader.resolve_hd_path(
                            tex['ao'], res_mods_root, pkg)
                        if resolved:
                            mesh.ao_tex_id = TextureLoader.load_texture(resolved)
                            mesh.ao_path   = resolved
                            print(f"{label_prefix} ao:      {os.path.basename(resolved)}"
                                  f"{'[HD]' if used_hd else '[SD]'}  (id={mesh.ao_tex_id})")
                        else:
                            print(f"{label_prefix} ao:      [missing] {tex['ao']}")

                    # -- GMM map
                    if 'gmm' in tex:
                        resolved, used_hd = VisualLoader.resolve_hd_path(
                            tex['gmm'], res_mods_root, pkg)
                        if resolved:
                            mesh.gmm_tex_id = TextureLoader.load_texture(resolved)
                            mesh.gmm_path   = resolved
                            print(f"{label_prefix} gmm:     {os.path.basename(resolved)}"
                                  f"{'[HD]' if used_hd else '[SD]'}  (id={mesh.gmm_tex_id})")
                        else:
                            print(f"{label_prefix} gmm:     [missing] {tex['gmm']}")

                    # -- Detail map (shared scratch noise; drives sSpec)
                    # First call resolves via _resolve_detail_map_path which
                    # checks resources/ first and only extracts from the
                    # WoT pkg if we don't have a local copy.  GPU upload is
                    # cached in self._shared_tex_cache so all sub-meshes
                    # share one texture object.  Mesh.cleanup will NOT free
                    # this texture -- viewer cleanup owns it.
                    if 'detail' in tex:
                        resolved = self._resolve_detail_map_path(
                            tex['detail'], res_mods_root)
                        if resolved:
                            key = os.path.abspath(resolved)
                            cached = self._shared_tex_cache.get(key)
                            if cached is None:
                                cached = TextureLoader.load_texture(resolved)
                                self._shared_tex_cache[key] = cached
                                print(f"{label_prefix} detail:  "
                                      f"{os.path.basename(resolved)}  "
                                      f"(id={cached}) [load]")
                            mesh.detail_tex_id = cached
                    mesh.detail_tiling = tex.get('detail_tiling', (1.0, 1.0))

                    # -- Damage layer (PBS_tank_crash.fx materials).
                    # See load_vehicle() for the full discussion of
                    # crashTileMap.  Standalone .primitives_processed
                    # loads can land on a crash visual too, so we
                    # honour the property here as well.
                    mesh.fx = tex.get('fx', '')
                    if 'crash_tile' in tex:
                        resolved, _ = VisualLoader.resolve_hd_path(
                            tex['crash_tile'], res_mods_root, pkg)
                        if resolved:
                            key = os.path.abspath(resolved)
                            cached = self._shared_tex_cache.get(key)
                            if cached is None:
                                cached = TextureLoader.load_texture(resolved)
                                self._shared_tex_cache[key] = cached
                            mesh.crash_tile_tex_id = cached
                            mesh.crash_tile_path   = resolved
                            print(f"{label_prefix} crash:   "
                                  f"{os.path.basename(resolved)}  "
                                  f"(id={cached})")
                        else:
                            print(f"{label_prefix} crash:   [missing] "
                                  f"{tex['crash_tile']}")
                    mesh.crash_uv_tiling = tex.get(
                        'crash_uv_tiling', (1.0, 1.0, 0.0, 0.0))
                    mesh.crash_coefficient = float(
                        tex.get('crash_coefficient', 1.0))

                    # -- Material flags
                    mesh.alpha_reference   = tex.get('alpha_reference',   0)
                    mesh.alpha_test_enable = tex.get('alpha_test_enable', False)
                    # Alpha-tested materials (camo nets, antennas, foliage,
                    # tarps, leaves) are always cutout and almost always
                    # need to be visible from the back too -- force
                    # double-sided whenever alpha test is on, regardless
                    # of what the visual file claims.
                    mesh.double_sided      = (tex.get('double_sided', False)
                                              or bool(mesh.alpha_test_enable))
                    mesh.identifier        = tex.get('identifier',        '')

                    # -- Winding and alpha-source rules based on bone presence
                    vfmt      = synth.get('format', '')
                    has_bones = 'iii' in vfmt

                    # Always flip winding CW->CCW for OpenGL front-face convention.
                    idx = mesh.indices.copy()
                    for i in range(0, len(idx) - 2, 3):
                        idx[i], idx[i + 2] = idx[i + 2], idx[i]
                    mesh.indices = idx

                    # Alpha-test mask routing.
                    #   * Skinned meshes (always HD): mask in ANM.R
                    #   * Static HD meshes with alpha test enabled (e.g. camo
                    #     nets, foliage, tarpaulins): also in ANM.R
                    #   * Static meshes without alpha test: routing is
                    #     irrelevant -- the shader skips the alpha test
                    mesh.alpha_in_normal_red = (
                        has_bones or bool(mesh.alpha_test_enable))
                    # AO routing is independent of the alpha-test routing:
                    # skinned meshes pack AO in AM.A; everything else has
                    # a dedicated <ao_map>.
                    mesh.ao_in_diffuse_alpha = has_bones

                    mesh.build_vao()

                    alpha_src = 'ANM.R' if mesh.alpha_in_normal_red else 'AM.A'
                    print(f"{label_prefix} identifier='{mesh.identifier}'  format='{vfmt}'  "
                          f"bones={has_bones}  flip_winding={not has_bones}  alpha_src={alpha_src}")
                    print(f"{label_prefix} alpha_ref={mesh.alpha_reference}/255 "
                          f"({mesh.alpha_reference / 255.0:.2f})  "
                          f"alpha_test={mesh.alpha_test_enable}  "
                          f"double_sided={mesh.double_sided}")

                    self.meshes.append(mesh)
                    all_positions.append(mesh.positions)

            print("\n" + "=" * 80)

            # Fit camera and remember bbox for 'R' reset.  Only
            # fit-to-bounds on the FIRST load this session -- after
            # that the orbit-camera state (yaw / pitch / distance)
            # sticks across tank reloads.
            #
            # `camera_mode` is intentionally NOT touched here.  Per
            # 2026-05-08 user request, only `load_vehicle` (the tank-
            # tree pick path) and the C-key cycle handler may write
            # to it -- this single-file FBX/GLB import path leaves
            # the current mode alone.
            all_pos  = np.concatenate(all_positions, axis=0)
            bbox_min = np.min(all_pos, axis=0)
            bbox_max = np.max(all_pos, axis=0)
            self._scene_bbox = (bbox_min, bbox_max)
            if not getattr(self, '_camera_fit_once', False):
                self.camera.fit_to_bounds(bbox_min, bbox_max)
                self._camera_fit_once = True

            # Minimum zoom = 5 % of mesh radius (prevents camera entering mesh)
            mesh_radius = float(np.linalg.norm((bbox_max - bbox_min) / 2.0))
            self._min_zoom_distance = max(0.05, mesh_radius * 0.05)

            total_v = sum(len(m.positions) for m in self.meshes)
            total_i = sum(len(m.indices)   for m in self.meshes)
            print(f"Loaded {len(self.meshes)} mesh(es): {total_v} verts, {total_i} indices")
            print(f"Bounds: min={bbox_min}, max={bbox_max}")

            # Repopulate the mesh-visibility window for the new meshes
            self._populate_mesh_window()

        except Exception as exc:
            print(f"Error loading mesh: {exc}")
            import traceback
            traceback.print_exc()

    # ------------------------------------------------------------------
    # FBX / GLB / GLTF / OBJ import (Blender-bridge round trip)
    # ------------------------------------------------------------------

    def load_imported_payload(self, payload):
        """Rebuild self.meshes from the dict produced by
        tankExporterPy.exporters.import_vehicle.

        The payload's positions / normals / tangents / binormals are in
        Blender Z-up (the FBX importer leaves them there); we swizzle
        back to the viewer's native OpenGL Y-up via (x, y, z) -> (x, z, -y),
        which is the exact reverse of the swizzle our exporter applies.

        Args:
            payload (dict): see exporters.import_vehicle docstring for the
                            schema.

        Returns:
            number of Mesh objects added.
        """
        # FBX/GLB/OBJ imports always populate the FBX set.  Switch FIRST
        # so _clear_scene() only frees the previous FBX geometry; any
        # currently-loaded WoT pkg tank in the PKG set is preserved.
        self._active_set_name = 'fbx'
        self._clear_scene()
        self.source_type      = 'fbx'
        self.source_tank_name = payload.get('source_tank') or payload.get('name')
        self._armor_color = None
        meshes_in = payload.get('meshes', [])
        if not meshes_in:
            print("[viewer] import payload had no meshes")
            return 0

        # Try to recover engine-exhaust hardpoints from the WoT install
        # using the tank-name embedded in the payload (or guessed from
        # the FBX filename).  Fails silently when the name doesn't map
        # to a known WoT vehicle XML -- the import still works without
        # smoke emitters.
        if self.source_tank_name:
            self._populate_exhaust_for_tank(self.source_tank_name)

        all_positions = []

        for m in meshes_in:
            positions_bl = m.get('positions') or []
            if not positions_bl:
                continue

            # Blender Z-up -> OpenGL Y-up:  (x, y, z) -> (x, z, -y)
            positions = np.array(
                [(p[0], p[2], -p[1]) for p in positions_bl],
                dtype=np.float32)
            all_positions.append(positions)

            normals_bl = m.get('normals')
            normals = (np.array([(n[0], n[2], -n[1]) for n in normals_bl],
                                dtype=np.float32)
                       if normals_bl else
                       np.tile([0.0, 1.0, 0.0],
                               (len(positions), 1)).astype(np.float32))

            tangents_bl  = m.get('tangents')
            tangents = (np.array([(t[0], t[2], -t[1]) for t in tangents_bl],
                                 dtype=np.float32)
                        if tangents_bl else None)

            binormals_bl = m.get('binormals')
            binormals = (np.array([(b[0], b[2], -b[1]) for b in binormals_bl],
                                  dtype=np.float32)
                         if binormals_bl else None)

            uvs = m.get('uvs') or []
            uv0 = (np.array(uvs, dtype=np.float32) if uvs else
                   np.zeros((len(positions), 2), dtype=np.float32))

            # Optional 2nd UV channel.  None when the FBX/glTF source
            # had only a single UVMap layer, which is most common; the
            # WoT-side .uv2 sidecar sections only show up on track /
            # equipment / lightmap-using parts.
            uvs2 = m.get('uvs2')
            uv1 = (np.array(uvs2, dtype=np.float32)
                   if uvs2 else None)

            indices_in = m.get('indices') or []
            indices = np.asarray(indices_in, dtype=np.uint32)

            bone_indices_in = m.get('bone_indices')
            bone_indices = (np.asarray(bone_indices_in, dtype=np.uint8)
                            if bone_indices_in else None)
            bone_weights_in = m.get('bone_weights')
            bone_weights = (np.asarray(bone_weights_in, dtype=np.float32)
                            if bone_weights_in else None)

            parsed_group = {
                'name':   m.get('name', 'imported'),
                'format': 'imported',
                'vertices': {
                    'positions':    positions,
                    'normals':      normals,
                    'tangents':     tangents,
                    'binormals':    binormals,
                    'uv0':          uv0,
                    # Mesh.__init__ does .get('uv1') so None is fine
                    # when the import had no second UV layer.
                    'uv1':          uv1,
                    'bone_indices': bone_indices,
                    'bone_weights': bone_weights,
                },
                'indices':     indices,
                'prim_groups': [],
            }
            mesh = Mesh(parsed_group)

            # Per-mesh world placement: comes in from Blender as a
            # Z-up row-major 4x4.  Convert to viewer's Y-up convention
            # via similarity transform M_y = B^-1 M_z B  where B is the
            # change-of-basis matrix that maps Y-up vectors to Z-up:
            #     B (x, y, z)_yup = (x, -z, y)_zup
            # Sanity check: a pure translation (5, 2, 7)_yup is exported
            # as (5, -7, 2)_zup; this similarity transform recovers
            # (5, 2, 7)_yup on the way back in.
            mm = m.get('model_matrix')
            if mm and len(mm) == 16:
                M_z = np.array(mm, dtype=np.float32).reshape(4, 4)
                B = np.array([
                    [1, 0,  0, 0],
                    [0, 0, -1, 0],
                    [0, 1,  0, 0],
                    [0, 0,  0, 1],
                ], dtype=np.float32)
                # B is orthonormal so B^-1 = B^T.
                M_y = B.T @ M_z @ B
                mesh.model_matrix = M_y

            # Diffuse texture (best-effort).  Other materials default
            # to grey placeholders -- enough to render and verify
            # geometry / UVs are intact.
            diffuse_path = m.get('diffuse_path')
            if diffuse_path and os.path.isfile(diffuse_path):
                mesh.diffuse_tex_id = TextureLoader.load_texture(diffuse_path)
                mesh.diffuse_path   = diffuse_path
            else:
                mesh.diffuse_tex_id = TextureLoader.create_placeholder(0.7)
            mesh.normal_tex_id = TextureLoader.create_placeholder(0.5)

            # Imported meshes don't carry alpha/AO routing flags from
            # the original WoT material -- leave them at defaults.
            mesh.alpha_test_enable   = False
            mesh.double_sided        = False
            mesh.alpha_in_normal_red = False
            mesh.ao_in_diffuse_alpha = False

            self.meshes.append(mesh)

        # Camera fit: same path that load_mesh / load_vehicle use
        if all_positions:
            cat = np.concatenate(all_positions, axis=0)
            mn  = cat.min(axis=0)
            mx  = cat.max(axis=0)
            self._scene_bbox = (mn, mx)
            self.camera.fit_to_bounds(mn, mx)

        # Refresh the mesh-visibility window
        self._populate_mesh_window()

        # Stamp the info panel with a placeholder (no full WoT stats here)
        self._build_info_panel(None)

        # Cache mesh count for the return value -- the auto-load-pkg
        # block below briefly switches active sets and would otherwise
        # report the PKG count when we want to report what was imported.
        n_imported = len(self.meshes)

        # If we resolved a WoT tank name from the import, also load the
        # matching pkg-side geometry into the PKG set so the user can
        # immediately Flip / Compare without re-loading.  The import
        # remains the active set on screen; we only briefly flip to
        # 'pkg' for the load_vehicle call.  Failures are non-fatal --
        # the import view still works without a paired PKG load.
        if (self.source_tank_name and self._pkg_extractor is not None
                and not self._pkg_set.is_loaded()):
            nation, tank_basename = self._resolve_tank_nation(self.source_tank_name)
            if nation:
                xml_zip = f'scripts/item_defs/vehicles/{nation}/{tank_basename}.xml'
                xml_local = self._pkg_extractor.extract(xml_zip)
                if xml_local:
                    print(f"[viewer] auto-loading PKG twin for "
                          f"'{tank_basename}' from {xml_zip}")
                    try:
                        # load_vehicle flips active->'pkg' internally and
                        # populates self._pkg_set.  We then flip back to
                        # 'fbx' so the user keeps seeing the import they
                        # just opened.  Project policy: when an FBX is
                        # imported, the paired PKG twin is loaded with
                        # prefer_res_mods=False so the comparison shows
                        # the vanilla game asset, never the user's
                        # res_mods overrides.  Otherwise a half-built
                        # mod could mislead the round-trip diff.
                        self.load_vehicle(xml_local, prefer_res_mods=False)
                    except Exception as exc:
                        print(f"[viewer] auto-load PKG twin failed: {exc}")
                    finally:
                        self._active_set_name = 'fbx'
                        # With both sets now populated, re-order the
                        # FBX side so its mesh indices line up with
                        # the PKG side -- the foundation for a future
                        # write-back to .primitives_processed.  No-op
                        # if either side is empty.
                        try:
                            self._align_fbx_to_pkg_order()
                        except Exception as exc:
                            print(f"[viewer] auto-align failed: {exc}")
                        # Rebuild the mesh window + info panel for the
                        # FBX side now that we're back on it.  Camera
                        # was last fit to whichever set load_vehicle
                        # left active -- re-fit to FBX bounds here.
                        self._populate_mesh_window()
                        self._build_info_panel(self._fbx_set.tank_info)
                        if self._fbx_set.scene_bbox is not None:
                            mn, mx = self._fbx_set.scene_bbox
                            self.camera.fit_to_bounds(mn, mx)
                        # Smoke / hp_lines belong to the active set;
                        # restore them from the FBX side.
                        EXHAUST_VECTOR_LEN = 0.5
                        cyan = (0.20, 1.00, 1.00)
                        segments = []
                        for hp in self._fbx_set.exhaust_points:
                            start = tuple(float(v) for v in hp['pos'])
                            end   = tuple(float(v) for v in
                                          (hp['pos'] + hp['fwd'] * EXHAUST_VECTOR_LEN))
                            segments.append((start, end, cyan))
                        self.hp_lines.update(segments)
                        if self.smoke_particles is not None:
                            self.smoke_particles.reset()
                            self.smoke_particles.set_emitters(
                                self._fbx_set.exhaust_points)

        return n_imported

    # ------------------------------------------------------------------
    # Vehicle XML loader
    # ------------------------------------------------------------------

    def load_vehicle(self, xml_path, damaged=False,
                     skin=None, chassis_tag=None,
                     turret_tag=None, gun_tag=None,
                     status_callback=None,
                     prefer_res_mods=True):
        """Load a complete tank from a WoT vehicle XML definition file."""
        # Reset camera mode to 1 (chase) on every tank load -- so a
        # session that ended in commander view doesn't reopen with
        # the turret hidden.  Press C to cycle to chase / commander
        # again.  (Was previously only being reset in load_mesh,
        # which is the single-file FBX/GLB import path -- not this
        # one, the tank-tree pick path.)
        self.camera_mode = 1
        return self._load_vehicle_impl(
            xml_path, damaged=damaged, skin=skin,
            chassis_tag=chassis_tag, turret_tag=turret_tag,
            gun_tag=gun_tag, status_callback=status_callback,
            prefer_res_mods=prefer_res_mods)

    def _load_vehicle_impl(self, xml_path, damaged=False,
                           skin=None, chassis_tag=None,
                           turret_tag=None, gun_tag=None,
                           status_callback=None,
                           prefer_res_mods=True):
        """Implementation body for `load_vehicle`.

        Picks the best (highest-price) turret and the top gun (last listed)
        by default; pass *_tag overrides to swap any component, and *skin*
        to pull models from <models>/<sets>/<skin>/... instead of the
        default paths.

        Each component (hull, chassis, turret, gun) is loaded as a set of
        meshes with the correct world-space translation applied via
        mesh.model_matrix.

        Args:
            xml_path     (str) : absolute path to the vehicle XML
            damaged      (bool): load destroyed/crashed model variant when True
            skin         (str | None): skin name from <models>/<sets>; None=default
            chassis_tag  (str | None): override chassis selection by element tag
            turret_tag   (str | None): override turret selection
            gun_tag      (str | None): override gun selection (under chosen turret)
            status_callback (callable(str) | None): receives short progress
                          strings as the load advances ("Parsing XML...",
                          "Loading Hull...", etc.).  Wired to the load
                          dialog's bottom status line so the user sees
                          something while the (synchronous) load runs.
            prefer_res_mods (bool): when True (default), the loader walks
                          res_mods/ + res/ before falling back to pkg --
                          so any user mod overrides win.  When False,
                          texture / visual lookups skip the disk roots
                          and go straight to pkg, giving vanilla bytes
                          regardless of what's mounted in res_mods.
                          Driven by the "Load from res_mods" checkbox at
                          the top of the load dialog.  FBX import calls
                          this method elsewhere with prefer_res_mods=False
                          unconditionally (project policy: imported FBX
                          tanks are always paired with pkg textures).
        """
        # Local helper -- safe even when status_callback is None
        def _status(msg):
            if status_callback is not None:
                try:
                    status_callback(msg)
                except Exception as exc:
                    print(f"[viewer] status callback failed: {exc}")

        # Wipe the console so the next-load output is uncluttered
        os.system('cls' if os.name == 'nt' else 'clear')
        _status("Parsing vehicle XML...")

        # Reset PkgExtractor timing counters so the summary at the end
        # shows JUST this load (instead of cumulative across the session).
        # Cheap; just clears a Python list.
        if self._pkg_extractor is not None:
            self._pkg_extractor.reset_timing()
        # Wall-clock timing of the full load_vehicle so we can compare
        # the PkgExtractor portion against the rest of the work
        # (texture decode + GPU upload + GL VAO build).
        import time as _time
        _load_t0 = _time.perf_counter()

        # Native WoT loads always populate the PKG set.  Switch FIRST so
        # _clear_scene() only frees the previous PKG geometry; any
        # currently-loaded FBX in the FBX set is preserved.
        self._active_set_name = 'pkg'
        self._clear_scene()        # free any previously-loaded meshes/textures
        self.source_type = 'wot'   # vehicle XML / primitive load path
        # Capture tank xml basename for later cross-referencing -- e.g.
        # an FBX exported from this session can embed it in a custom
        # property so a future re-import knows which WoT def XML to
        # consult for engine-exhaust hardpoints.
        self.source_tank_name = os.path.splitext(
            os.path.basename(xml_path))[0]
        # Stash the variant so the FBX exporter can tag the default
        # output filename, the FBX importer can recover the flag from
        # that tag on a round trip, and Save Prim can route the
        # write to res_mods/.../crash/ instead of /normal/.  Without
        # this the variant info gets dropped at the first hand-off
        # and writes silently land in the wrong folder.
        self._loaded_damaged = bool(damaged)
        variant = 'CRASHED' if damaged else 'undamaged'
        print(f"\nLoading vehicle from {xml_path} ({variant}) ...")

        # Detect nation from path + look up armor colour.  Pulls from
        # WoT's own base_paints.xml via ArmorColorLoader -- DEFAULTS
        # only, not the player-customisation paints.  Tanks listed in
        # the base-paint exclude filter (Type 59 Gold, Skorpion BF,
        # etc.) get None back from .get() and render untinted because
        # their unique colour is baked into the texture itself.
        nation = _nation_from_xml_path(xml_path)
        tank_basename = os.path.splitext(os.path.basename(xml_path))[0]
        self._armor_color = self._armor_loader.get(nation, tank_basename)
        if self._armor_color:
            print(f"  nation='{nation}'  tank='{tank_basename}'  "
                  f"armor_color(sRGB norm)="
                  f"{tuple(f'{v:.4f}' for v in self._armor_color)}")
        else:
            print(f"  nation='{nation}'  tank='{tank_basename}'  "
                  f"(no armor tint -- excluded or unknown nation)")

        try:
            # --- res_mods root: config override -> auto-detect from XML path ----
            # When `prefer_res_mods=False` (load-dialog checkbox off,
            # or any FBX-import call), we deliberately blank the root
            # so VisualLoader.resolve_hd_path skips its disk-walk and
            # goes straight to PkgExtractor.  That gives the user
            # vanilla pkg bytes regardless of what's mounted in
            # res_mods/ -- useful when they want to confirm the game
            # original or are about to overwrite a half-built mod.
            if not prefer_res_mods:
                res_mods_root = ''
                print(f"  res_mods         : (skipped -- pkg-only load)")
            else:
                cfg_res_mods = self._cfg.get('res_mods', '').strip()
                if cfg_res_mods and os.path.isdir(cfg_res_mods):
                    res_mods_root = cfg_res_mods
                    print(f"  res_mods (config) : {res_mods_root}")
                else:
                    parts        = xml_path.split(os.sep)
                    res_mods_idx = next((i for i, p in enumerate(parts)
                                         if 'res_mods' in p.lower()), -1)
                    if res_mods_idx >= 0:
                        res_mods_root = os.sep.join(parts[:res_mods_idx + 2])
                    else:
                        res_mods_root = os.path.dirname(xml_path)
                    print(f"  res_mods (auto)   : {res_mods_root}")

            # --- PKG extractor: config overrides -> auto-detect from wot_root ---
            if self._pkg_extractor is None:
                cfg_pkg_dir    = self._cfg.get('pkg_dir',    '').strip() or None
                cfg_lookup_xml = self._cfg.get('lookup_xml', '').strip() or None
                wot_root       = os.path.dirname(os.path.dirname(res_mods_root))
                self._pkg_extractor = PkgExtractor(
                    wot_root,
                    pkg_dir=cfg_pkg_dir,
                    lookup_xml=cfg_lookup_xml,
                )

            _status("Resolving component paths...")
            components = VehicleXMLLoader.parse(
                xml_path, res_mods_root, self._pkg_extractor,
                damaged=damaged,
                chassis_tag=chassis_tag,
                turret_tag=turret_tag,
                gun_tag=gun_tag,
                skin=skin,
            )

            self.meshes   = []
            all_positions = []
            self._exhaust_points = []   # filled per-component below
            # Fire/damage spawn points reset to empty too -- only get
            # populated below when `damaged=True` and the per-component
            # walk finds HP_Fire_* nodes.
            self._fire_points    = []

            # Engine-exhaust spec from the def XML.  Tells us which named
            # nodes WoT actually treats as the engine exhaust (vs HP_Fire,
            # which is for damage / burning visuals) and the pixie preset
            # (gas_medium / diesel_large / ...) we'll later use to pick a
            # smoke style.  Falls through gracefully if the block is absent.
            exhaust_spec = VehicleXMLLoader.find_engine_exhaust(xml_path)
            self._exhaust_pixie = (exhaust_spec or {}).get('pixie')
            # Auto-wire the per-engine-class smoke / fire sliders to
            # this tank's class.  See _set_active_engine_class / cleanup for
            # the persistence side.
            self._set_active_engine_class(self._exhaust_pixie)
            exhaust_node_names  = set(
                n.lower() for n in (exhaust_spec or {}).get('nodes', []))
            if exhaust_spec:
                print(f"  exhaust spec: pixie={self._exhaust_pixie!r}  "
                      f"nodes={(exhaust_spec or {}).get('nodes', [])}")
            else:
                print("  exhaust spec: <none in def XML>")

            for comp in components:
                label  = comp['label']
                prim   = comp['primitives']
                vis    = comp['visual']
                offset = comp['offset']

                if prim is None:
                    print(f"  [{label}] primitives_processed not found -- skipped")
                    continue

                # User-visible progress: each component takes ~0.3-0.5s
                # so the dialog's status line ticks Hull -> Turret -> Gun
                # as the load advances.
                _status(f"Loading {label}...")

                print(f"\n  [{label}]  offset={offset}")
                print(f"    primitives: {os.path.basename(prim)}")

                # Discover engine-exhaust hardpoints on this component
                # (chiefly Hull).  Positions are in COMPONENT-local
                # BigWorld space; we add the component offset (already in
                # OpenGL handedness) and then flip Z to convert the node
                # values from BigWorld (+Z forward, left-handed) into
                # OpenGL (-Z forward, right-handed) -- same gl_z = -bw_z
                # rule used by the mesh / turret-position loaders.
                # Forward vectors get the same Z negation so the smoke
                # plume direction stays consistent with the geometry.
                #
                # When the def XML lists explicit node names
                # (exhaust_node_names) we filter the keyword matches down
                # to that authoritative set; otherwise we accept all
                # keyword matches.
                if vis:
                    keyword_hits = VisualLoader.find_exhaust_nodes(vis)
                    if exhaust_node_names:
                        kept = [(n, p, f) for (n, p, f) in keyword_hits
                                if n in exhaust_node_names]
                    else:
                        kept = keyword_hits
                    if kept:
                        print(f"    exhaust nodes ({len(kept)}):")
                        for name, pos_bw, fwd_bw in kept:
                            # BW -> GL: negate Z on both position and direction
                            pos_gl = np.array([pos_bw[0], pos_bw[1], -pos_bw[2]],
                                              dtype=np.float32)
                            fwd_gl = np.array([fwd_bw[0], fwd_bw[1], -fwd_bw[2]],
                                              dtype=np.float32)
                            world_pos = pos_gl + np.asarray(offset, dtype=np.float32)
                            print(f"      {name:24s}"
                                  f"  pos=({world_pos[0]:+.3f}, {world_pos[1]:+.3f}, {world_pos[2]:+.3f})"
                                  f"  fwd=({fwd_gl[0]:+.2f}, {fwd_gl[1]:+.2f}, {fwd_gl[2]:+.2f})")
                            self._exhaust_points.append({
                                'component': label,
                                'name':      name,
                                'pos':       world_pos,
                                'fwd':       fwd_gl,
                            })

                # ---- Fire / damage spawn points (damaged tanks only) ----
                # HP_Fire_* nodes only matter when the user loaded the
                # crashed variant; on a normal-variant load we walk for
                # them anyway (cheap; one substring filter on the node
                # name list) but the fire ParticleSystem stays silent
                # because we don't call set_emitters with anything until
                # the damaged check below.
                if vis and damaged:
                    fire_hits = VisualLoader.find_fire_nodes(vis)
                    if fire_hits:
                        print(f"    fire nodes ({len(fire_hits)}):")
                        for name, pos_bw, _fwd_bw in fire_hits:
                            # Same BW -> GL Z-flip on position.  Forward
                            # vector is REPLACED with world-up (0, 1, 0)
                            # -- fire goes UP regardless of the artist's
                            # node orientation in 3DS Max.  This is the
                            # whole point of the fire path: there's no
                            # meaningful direction other than up.
                            pos_gl = np.array([pos_bw[0], pos_bw[1], -pos_bw[2]],
                                              dtype=np.float32)
                            world_pos = pos_gl + np.asarray(offset, dtype=np.float32)
                            up_gl = np.array([0.0, 1.0, 0.0],
                                             dtype=np.float32)
                            print(f"      {name:24s}"
                                  f"  pos=({world_pos[0]:+.3f}, "
                                  f"{world_pos[1]:+.3f}, {world_pos[2]:+.3f})"
                                  f"  fwd=(0, 1, 0)  [forced up]")
                            self._fire_points.append({
                                'component': label,
                                'name':      name,
                                'pos':       world_pos,
                                'fwd':       up_gl,
                            })

                # Build translation matrix for this component
                model_mat = np.eye(4, dtype=np.float32)
                model_mat[0, 3] = offset[0]
                model_mat[1, 3] = offset[1]
                model_mat[2, 3] = offset[2]

                # Parse geometry
                parsed_groups = MeshParser.parse_primitives_processed(prim)
                group_names   = [g['name'] for g in parsed_groups]

                # Parse material / texture data
                group_textures = {}
                # Bone palette per primitive group, declaration order.
                # Plumbed onto each Mesh as `mesh.bone_palette` so
                # downstream consumers (currently `tank_physics.
                # from_chassis_meshes`) can resolve raw `iii` byte
                # values into bone NAMES and filter accordingly.
                # Empty dict when this is a non-skinned component
                # (hull / turret / gun on most tanks) -- the lookup
                # below just returns None for those meshes.
                group_bones = {}
                if vis:
                    group_textures = VisualLoader.parse_textures(vis, group_names)
                    group_bones    = VisualLoader.parse_renderset_bones(vis)

                for group in parsed_groups:
                    group_name = group['name']
                    materials  = group_textures.get(group_name, [])
                    pkg        = self._pkg_extractor   # shorthand

                    # Build one Mesh per sub-mesh range so multi-material
                    # primitive groups (e.g. BVII_3DSt hull = body + 6
                    # equipment / skirt overlays) get their correct textures.
                    for synth, tex in _split_into_submeshes(group, materials):
                        mesh = Mesh(synth)

                        # Diffuse
                        if 'diffuse' in tex:
                            resolved, _ = VisualLoader.resolve_hd_path(
                                tex['diffuse'], res_mods_root, pkg)
                            if resolved:
                                mesh.diffuse_tex_id = TextureLoader.load_texture(resolved)
                                mesh.diffuse_path   = resolved
                            else:
                                mesh.diffuse_tex_id = TextureLoader.create_placeholder(0.7)
                                self.log_error(f"missing diffuse: "
                                               f"{os.path.basename(tex['diffuse'])}")
                        else:
                            mesh.diffuse_tex_id = TextureLoader.create_placeholder(0.7)

                        # Normal map
                        if 'normal' in tex:
                            resolved, _ = VisualLoader.resolve_hd_path(
                                tex['normal'], res_mods_root, pkg)
                            if resolved:
                                mesh.normal_tex_id = TextureLoader.load_texture(resolved, is_normal=True)
                                mesh.normal_path   = resolved
                            else:
                                mesh.normal_tex_id = TextureLoader.create_placeholder(0.5)
                                self.log_error(f"missing normal: "
                                               f"{os.path.basename(tex['normal'])}")
                        else:
                            mesh.normal_tex_id = TextureLoader.create_placeholder(0.5)

                        # AO
                        if 'ao' in tex:
                            resolved, _ = VisualLoader.resolve_hd_path(
                                tex['ao'], res_mods_root, pkg)
                            if resolved:
                                mesh.ao_tex_id = TextureLoader.load_texture(resolved)
                                mesh.ao_path   = resolved

                        # GMM
                        if 'gmm' in tex:
                            resolved, _ = VisualLoader.resolve_hd_path(
                                tex['gmm'], res_mods_root, pkg)
                            if resolved:
                                mesh.gmm_tex_id = TextureLoader.load_texture(resolved)
                                mesh.gmm_path   = resolved

                        # Detail map (shared scratch noise; drives sSpec).
                        # Goes through _resolve_detail_map_path so we hit
                        # the resources/ cache before falling back to
                        # the slow PkgExtractor path.
                        if 'detail' in tex:
                            resolved = self._resolve_detail_map_path(
                                tex['detail'], res_mods_root)
                            if resolved:
                                key = os.path.abspath(resolved)
                                cached = self._shared_tex_cache.get(key)
                                if cached is None:
                                    cached = TextureLoader.load_texture(resolved)
                                    self._shared_tex_cache[key] = cached
                                mesh.detail_tex_id = cached
                        mesh.detail_tiling = tex.get('detail_tiling', (1.0, 1.0))

                        # Damage layer (PBS_tank_crash.fx materials).
                        # Single shared crashTileMap holds scorch /
                        # dirt / exposed-metal in RGB + blend mask
                        # in A; the renderer mixes it over the base
                        # diffuse using crash_uv_tiling and
                        # crash_coefficient.  We share the GL
                        # texture across every mesh that references
                        # it via _shared_tex_cache (it's literally
                        # the same file -- 52 of 52 sampled crash
                        # materials use the same path) so a tank
                        # with 30 crash materials only uploads one
                        # 4MB texture.
                        mesh.fx = tex.get('fx', '')
                        if 'crash_tile' in tex:
                            resolved, _ = VisualLoader.resolve_hd_path(
                                tex['crash_tile'], res_mods_root, pkg)
                            if resolved:
                                key = os.path.abspath(resolved)
                                cached = self._shared_tex_cache.get(key)
                                if cached is None:
                                    cached = TextureLoader.load_texture(resolved)
                                    self._shared_tex_cache[key] = cached
                                mesh.crash_tile_tex_id = cached
                                mesh.crash_tile_path   = resolved
                            else:
                                # crash_tile.dds lives in particles.pkg --
                                # if TheItemList hasn't been rebuilt
                                # since damage support landed, the
                                # lookup will miss and the resolver
                                # falls back to scan-fallback.  Log
                                # so the user can spot it.
                                print(f"  [damage] crash_tile.dds not "
                                      f"found ({tex['crash_tile']}) -- "
                                      f"rebuild ItemList?  No damage "
                                      f"layer for this material.")
                        mesh.crash_uv_tiling = tex.get(
                            'crash_uv_tiling', (1.0, 1.0, 0.0, 0.0))
                        mesh.crash_coefficient = float(
                            tex.get('crash_coefficient', 1.0))

                        # Material flags
                        mesh.alpha_reference   = tex.get('alpha_reference',   0)
                        mesh.alpha_test_enable = tex.get('alpha_test_enable', False)
                        # Alpha-tested = cutout = needs to be drawn from
                        # both sides (camo nets, antennas, leaves, etc.).
                        # Force double-sided whenever alpha test is on.
                        mesh.double_sided      = (tex.get('double_sided', False)
                                                  or bool(mesh.alpha_test_enable))
                        mesh.identifier        = tex.get('identifier',        '')

                        # Winding / alpha source
                        vfmt      = synth.get('format', '')
                        has_bones = 'iii' in vfmt

                        # Always flip winding CW->CCW for OpenGL front-face convention.
                        idx = mesh.indices.copy()
                        for i in range(0, len(idx) - 2, 3):
                            idx[i], idx[i + 2] = idx[i + 2], idx[i]
                        mesh.indices = idx

                        # Alpha-test mask routing (see load_mesh for details).
                        # ANM.R for skinned OR alpha-tested meshes; AM.A otherwise.
                        mesh.alpha_in_normal_red = (
                            has_bones or bool(mesh.alpha_test_enable))
                        mesh.ao_in_diffuse_alpha = has_bones

                        # Apply component world offset
                        mesh.model_matrix = model_mat

                        # Tag with the WoT component this mesh belongs to.
                        # Read by the .primitives_processed writer to
                        # group meshes back into per-component output
                        # files (Hull / Chassis / Turret / Gun).  The
                        # label here comes straight from the components
                        # loop above, lower-cased so the picker dialog
                        # and the writer share one canonical key.
                        mesh.component = label.lower()

                        # Bone palette in declaration order.  Sub-meshes
                        # split off the same group all share one
                        # vertex stream, so they all share one palette.
                        # `None` when the visual carries no skinned
                        # renderSet for this group (hull / turret /
                        # gun on the vast majority of tanks).
                        mesh.bone_palette = group_bones.get(group_name)
                        # Remember the in-pkg path so Save-Prim can
                        # write the output to res_mods/<version>/<same
                        # path>, making the file an automatic override
                        # the game picks up at next launch.  All
                        # meshes belonging to a given component share
                        # one source file -- they all get the same
                        # path here.
                        mesh.primitives_zip = comp.get('primitives_zip', '')

                        mesh.build_vao()
                        self.meshes.append(mesh)
                        all_positions.append(mesh.positions + offset)

            if not self.meshes:
                print("No meshes loaded.")
                return

            # Stash res_mods Extract paths now that nation, xml
            # basename, and damaged are all settled.  Captures the
            # variant subdir (`/normal/` vs `/crash/`) in one shot
            # so subsequent Open Extract Loc / Remove clicks don't
            # re-derive it -- and so a tank loaded as `damaged`
            # opens to the crash subfolder rather than the normal
            # one (paths matter).
            self._stash_extract_paths()

            # Parse the vehicle gameplay XML EARLY so the chassis
            # block (suspension envelope + road-wheel radius +
            # render-model-offset) is available BEFORE the
            # `from_chassis_meshes` call below.  Re-used by the
            # info-panel build later (we cache on
            # `_pending_chassis_info` + the active set's tank_info
            # so the parse only happens once per load).
            self._pending_chassis_info = None
            try:
                _info_early = VehicleXMLLoader.parse_info(
                    xml_path, self._pkg_extractor)
                self._active_set.tank_info = _info_early
                self._pending_chassis_info = (
                    (_info_early or {}).get('chassis') or {})
            except Exception as exc:
                print(f"[viewer] early info parse failed: {exc}")
                _info_early = None

            # Auto-extract the wheel rig from the freshly-loaded
            # chassis sub-meshes so tank physics works on ANY tank
            # rather than just T110E4.  Heuristic: group every
            # vertex of every `component == 'chassis'` mesh by its
            # dominant `iii` byte, take centroids of groups whose
            # mean Y is below ~0.75 m (the road-wheel band -- this
            # excludes drive sprockets, idlers, and return rollers
            # which sit higher up).  Falls back to the T110E4
            # hardcoded rig if no groups match (very rare).
            try:
                from .tank_physics import TankPhysics
                chass = [m for m in self.meshes
                         if getattr(m, 'component', '') == 'chassis']
                # Per-tank physics inputs, parsed from the SELECTED
                # chassis's gameplay XML (`info['chassis']` block).
                # Falls through to TankPhysics ctor defaults when
                # the field is missing (rare; some modded tanks
                # drop the wheelGroups / groundNodes blocks).
                # Only the BEST chassis gets parsed in
                # `VehicleXMLLoader.parse_info`; if the user ever
                # loads a non-top chassis variant, we'd need a
                # per-load chassis-name resolver -- TODO.
                _chassis_kwargs = {}
                _ci = (getattr(self, '_pending_chassis_info', None)
                       or {})
                if 'groupRadius_road' in _ci:
                    _chassis_kwargs['radius'] = float(_ci['groupRadius_road'])
                if 'minOffset' in _ci:
                    _chassis_kwargs['min_offset'] = float(_ci['minOffset'])
                if 'maxOffset' in _ci:
                    _chassis_kwargs['max_offset'] = float(_ci['maxOffset'])
                if 'renderModelOffset' in _ci:
                    _chassis_kwargs['track_thickness'] = float(_ci['renderModelOffset'])
                self.tank_physics = TankPhysics.from_chassis_meshes(
                    chass, **_chassis_kwargs)
                # Reset position state on tank reload so old
                # positions don't carry over.
                self.tank_physics.pos[:]   = 0.0
                # Force the hull-AABB overlay to rebuild from the
                # new tank's hull meshes on the next render.
                self._hull_box_local = None
                self.tank_physics.yaw_deg  = 0.0
                self.tank_physics.pitch_deg = 0.0
                self.tank_physics.roll_deg  = 0.0
                self.tank_physics.vy        = 0.0
                # Re-arm the drive-key hint so the next time Susp
                # is toggled on the controls get re-printed.
                self._drive_keys_seen = False
                # Print the wired chassis params for the new tank.
                if _chassis_kwargs:
                    parts = []
                    if 'radius' in _chassis_kwargs:
                        parts.append(f"r={_chassis_kwargs['radius']:.3f}")
                    if 'min_offset' in _chassis_kwargs:
                        parts.append(f"min={_chassis_kwargs['min_offset']:+.3f}")
                    if 'max_offset' in _chassis_kwargs:
                        parts.append(f"max={_chassis_kwargs['max_offset']:+.3f}")
                    if 'track_thickness' in _chassis_kwargs:
                        parts.append(f"track={_chassis_kwargs['track_thickness']:.3f}")
                    print(f"  chassis params from XML: {', '.join(parts)}")
            except Exception as exc:
                print(f"[viewer] tank-physics auto-extract failed: {exc}")
            er = self._active_set.extract_variant_dir
            if er:
                variant_word = ('damaged' if self._active_set.extract_damaged
                                else 'normal')
                print(f"  extract dir ({variant_word})  : {er}")

            all_pos  = np.concatenate(all_positions, axis=0)
            bbox_min = np.min(all_pos, axis=0)
            bbox_max = np.max(all_pos, axis=0)
            self._scene_bbox = (bbox_min, bbox_max)
            # Same camera-stickiness rule as load_mesh -- fit only
            # on the first load this session.  R key explicitly
            # resets + re-fits when the user wants it.
            if not getattr(self, '_camera_fit_once', False):
                self.camera.fit_to_bounds(bbox_min, bbox_max)
                self._camera_fit_once = True

            mesh_radius = float(np.linalg.norm((bbox_max - bbox_min) / 2.0))
            self._min_zoom_distance = max(0.05, mesh_radius * 0.05)

            total_v = sum(len(m.positions) for m in self.meshes)
            total_i = sum(len(m.indices)   for m in self.meshes)
            xml_basename_for_log = os.path.basename(xml_path).rsplit('.', 1)[0]
            print(f"\nLoaded {len(self.meshes)} mesh groups: {total_v} verts, {total_i} indices")
            # In-app console: short / human-readable summary
            wall_ms = (_time.perf_counter() - _load_t0) * 1000.0
            self.log(f"Loaded {xml_basename_for_log}: "
                     f"{len(self.meshes)} meshes, {total_v} verts, "
                     f"{total_i // 3} faces  ({wall_ms:.0f} ms)")

            # PkgExtractor timing breakdown -- shows lookup hits vs scan
            # fallbacks + the slowest individual ops.  Helps explain
            # the "first chassis is slow" pattern: the slow ones are
            # almost always scan-fallback hits where TheItemList.xml
            # didn't index that file, so we have to open one or more
            # pkg namelists to find it.  Subsequent loads hit the
            # in-memory namelist cache and are essentially free.
            if self._pkg_extractor is not None:
                summary, totals = self._pkg_extractor.summarize_timing(top_n=8)
                for line in summary:
                    print(f"[load:timing] {line}")
                    self.log(line)
                # Flush any queued TheItemList.xml additions in ONE
                # batched read+write.  Without this batching we used
                # to rewrite the 15 MB lookup XML once per scan-
                # fallback discovery -- 20-30 times during a fresh
                # tank load, accounting for most of the multi-second
                # first-load slowness.  After the flush, every newly-
                # discovered file is also persisted across sessions.
                self._pkg_extractor.flush_persisted_entries()

            # Install the persistent thumbnail for the now-loaded tank
            xml_basename = os.path.basename(xml_path).rsplit('.', 1)[0]
            self._set_loaded_thumbnail(xml_basename)

            # Populate the left info panel with parsed stats.  Also
            # cache the parsed dict on the active set so a subsequent
            # flip back to this PKG view can rebuild the panel without
            # re-parsing the XML.
            try:
                # Reuse the EARLY parse from above (skip the second
                # parse).  If the early parse failed (`_info_early`
                # is None), fall back to a fresh parse here.
                info = _info_early
                if info is None:
                    info = VehicleXMLLoader.parse_info(
                        xml_path, self._pkg_extractor)
                    self._active_set.tank_info = info
                self._build_info_panel(info)
                # Pull the per-tank max forward speed for the drive
                # speed-step controller.  Vehicle XMLs carry this as
                # `<speedLimits><forward>` in kph already; loader
                # passes it through unchanged.  Default 50 kph if
                # the XML didn't carry the block (rare).
                fwd_kph = ((info.get('speed') or {}).get('forward_kph')
                           or 50.0)
                self._top_speed_kph = float(fwd_kph)
                # Speed step persists across loads now (was being
                # reset to 0 here).  If you had cruise on at step
                # 5 and load a new tank, the new tank inherits the
                # step + cruise state.  Speed magnitude rescales
                # automatically to the new tank's `_top_speed_kph`
                # via `_kph_for_step`.
                self.log(
                    f"top speed: {self._top_speed_kph:.1f} kph  "
                    f"(use 1-9 to drive, 0 to stop)",
                    color=(180, 220, 255))
                # Total tank weight = sum of every component's
                # `<weight>` (hull + chassis + turret + gun + engine
                # + radio + fueltank).  Surface to the in-app
                # console; cached on `_total_weight_kg` for the
                # mass-inertia model when we wire it back in.
                total_kg = float(info.get('total_weight_kg', 0.0) or 0.0)
                self._total_weight_kg = total_kg
                if total_kg > 0:
                    self.log(
                        f"total tank weight: {total_kg / 1000.0:.1f} t  "
                        f"({total_kg:.0f} kg)",
                        color=(180, 220, 255))
            except Exception as exc:
                print(f"[viewer] info-panel build failed: {exc}")

            # Refresh the mesh-visibility window so its rows match the
            # newly-loaded meshes (all reset to visible).  Window stays
            # closed/open as the user left it; only its contents change.
            self._populate_mesh_window()

            # Snapshot bind_pos / bind_fwd RIGHT NOW (at load time)
            # for every exhaust + fire hardpoint.  The lazy snapshot
            # in `_update_emitters_for_chassis_pose` was supposed to
            # cover this but had a subtle hole: any code that touched
            # `hp['pos']` between load and the first physics tick
            # would corrupt the bind reference.  This explicit
            # snapshot at load time makes bind_pos authoritative.
            #
            # Also dumps the captured bind values to the in-app
            # console so we can see at a glance whether all HPs got
            # captured at the SAME chassis-relative coordinates --
            # if one HP shows a wildly different bind_pos, that's
            # the framing-bug culprit.
            for hp in self._exhaust_points:
                hp['bind_pos'] = np.asarray(hp['pos'], dtype=np.float32).copy()
                hp['bind_fwd'] = np.asarray(hp['fwd'], dtype=np.float32).copy()
            for hp in self._fire_points:
                hp['bind_pos'] = np.asarray(hp['pos'], dtype=np.float32).copy()
                hp['bind_fwd'] = np.asarray(hp['fwd'], dtype=np.float32).copy()
            if self._exhaust_points:
                print(f"  Exhaust HPs ({len(self._exhaust_points)}) "
                      f"bind-pose snapshot:")
                for hp in self._exhaust_points:
                    bp = hp['bind_pos']; bf = hp['bind_fwd']
                    print(f"    {hp.get('name', '?'):24s}  "
                          f"comp={hp.get('component', '?'):8s}  "
                          f"pos=({bp[0]:+.3f}, {bp[1]:+.3f}, {bp[2]:+.3f})  "
                          f"fwd=({bf[0]:+.2f}, {bf[1]:+.2f}, {bf[2]:+.2f})")

            # Build exhaust direction-vector lines: 0.5-unit ray from each
            # hardpoint along its (already-flipped) forward vector.
            EXHAUST_VECTOR_LEN = 0.5
            cyan = (0.20, 1.00, 1.00)
            segments = []
            for hp in self._exhaust_points:
                start = tuple(float(v) for v in hp['pos'])
                end   = tuple(float(v) for v in (hp['pos'] + hp['fwd'] * EXHAUST_VECTOR_LEN))
                segments.append((start, end, cyan))
            self.hp_lines.update(segments)

            # Engine-exhaust smoke -- hardpoints from the running
            # engine.  Reset kills any in-flight particles from the
            # previous tank so we don't see ghost smoke.
            if self.smoke_particles is not None:
                self.smoke_particles.reset()
                self.smoke_particles.set_emitters(self._exhaust_points)

            # Fire-point smoke is currently DISABLED -- the smoke
            # plume rising off each HP_Fire flame looked wrong over
            # the painted-flame billboards (white/grey wisps in front
            # of the orange flame washed out the colour).  The
            # particle system stays in place so flipping the
            # `enable_fire_smoke` flag below back on is a one-line
            # change; keeping it off for now.
            enable_fire_smoke = False
            if self.fire_smoke_particles is not None:
                self.fire_smoke_particles.reset()
                fire_smoke_emitters = []
                if enable_fire_smoke and damaged and self._fire_points:
                    for hp in self._fire_points:
                        fire_smoke_emitters.append({
                            'component': hp.get('component', ''),
                            'name':      hp.get('name', '') + '_smoke',
                            'pos':       hp['pos'],
                            'fwd':       np.array([0.0, 1.0, 0.0],
                                                  dtype=np.float32),
                        })
                # Always set the emitter list (possibly empty) so a
                # previous damaged tank's emitters don't leak into
                # this load.
                self.fire_smoke_particles.set_emitters(fire_smoke_emitters)
                if fire_smoke_emitters:
                    print(f"  Smoke emitters: {len(self._exhaust_points)} "
                          f"engine exhaust + {len(fire_smoke_emitters)} "
                          f"fire-point smoke (cap=400)")

            # Fire emitters -- only populated when the damaged variant
            # is loaded (see the per-component fire-walk above).  When
            # the user loads a normal-variant tank, _fire_points stays
            # empty and the fire system goes idle (no emitters means
            # the billboard render call short-circuits).
            if self.fire_billboards is not None:
                self.fire_billboards.reset()
                self.fire_billboards.set_emitters(self._fire_points)
                if self._fire_points:
                    print(f"  Fire enabled: {len(self._fire_points)} "
                          f"HP_Fire billboard emitter(s)")

            _status("Done.")

        except Exception as exc:
            print(f"Error loading vehicle: {exc}")
            import traceback
            traceback.print_exc()
            _status(f"Error: {exc}")

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    def _sync_button_state(self, attr, value):
        """Push a viewer flag back to the matching button's active state."""
        for btn in self.ui.buttons:
            if btn.attr == attr:
                btn.active = value
                break

    def _apply_button_action(self, btn):
        """Propagate a button click to the corresponding viewer attribute."""
        attr = btn.attr
        if attr is None:
            return
        setattr(self, attr, btn.active)
        if attr == 'wireframe':
            # Just flip the flag -- the next render() pass picks it up
            # and handles the overlay-vs-solid logic.  We DON'T call
            # glPolygonMode here any more because the wireframe is now
            # an overlay pass on top of the solid render, not a global
            # rasteriser-mode replacement.
            pass

    # ------------------------------------------------------------------
    def _layout_widgets(self):
        """Position every menu-bar widget inside its side panel.

        Layout
        ------
        Left panel (x=0, w=INFO_PANEL_W, top block height = LEFT_CONTROLS_H):
            row 0 (y=8)   Grid       Axes       Light
            row 1 (y=34)  Orbit      Skybox     Wireframe
            row 2 (y=60)  Set Paths            Meshes
            row 3 (y=92)  Light slider              [NMap]
            row 4 (y=117) Ambient slider            [AO]

        Right panel (bottom block, height = RIGHT_CONTROLS_H):
            Sm Start, Sm End, Sm Speed, Sm Fade sliders
            Show HP checkbox

        Slider tracks are sized to fit each panel's width; label/value
        text is positioned per-instance via sl.label_x / sl.value_x.
        """
        # ---- LEFT PANEL --------------------------------------------------
        BTN_W, BTN_H = 70, 22
        BTN_PAD_X    = 8
        BTN_PAD_Y    = 8
        BTN_GAP_X    = 6
        BTN_GAP_Y    = 4

        # Three columns of buttons grouped by category.  Each group has
        # a small header label drawn above its row(s) so the user can
        # tell at a glance where to look for "show / hide stuff" vs
        # "read / write files" vs "rebuild caches and other utilities".
        #
        # Group ordering (top -> bottom):
        #   UI    -- display toggles + windows the user opens to look
        #            at the loaded tank from a different angle.
        #   IO    -- everything that touches the disk: the Set Paths
        #            configuration dialog, FBX/glTF/OBJ Import / Export,
        #            and the WoT-native Save Prim writer.
        #   Tools -- batch / one-shot utilities that don't fit the other
        #            two buckets.  Currently just the ItemList rebuild;
        #            future round-trip self-test, diagnostics, etc. land
        #            here.
        #
        # Each row is (label, col, span).  span=3 means full-width
        # (cols 0..2), span=2 spans cols 0..1.  Group order in the
        # dict determines vertical stacking.
        # i18n: section names AND button labels resolve through `_()`
        # so the lookup keys match the translated strings the buttons
        # were registered under in `_build_ui`.  When the language
        # doesn't change mid-session, `_()` is deterministic so the
        # `btn_by_label` lookup below finds every button.
        button_groups = [
            # res_mods I/O.  Lives at the top so it's the first
            # thing the user reaches when they want to mod a tank.
            # Section header reads `res_mods` (the destination, not
            # the verb) so the bar makes it obvious at a glance
            # which folder these three buttons act on.  Hidable:
            # section-header chevron toggles the group's collapse
            # state (persisted in config under `section_collapsed`).
            (_('res_mods'), [
                (_('Extract'),              0, 3),
                (_('Open Extract Loc'),     0, 3),
                (_('Remove from res_mods'), 0, 3),
            ]),
            # UI toggles -- viewport rendering flags.  Olive
            # accent applied per-button in `_build_ui`.  Six
            # toggles fill 3-3 across two rows; Wireframe and
            # Shaded moved to the Model group below since they
            # mutate how the model is rendered, not the viewport
            # decoration.  Terrain claims Wireframe's old slot so
            # the row still lays out as a clean 3-cell row.
            (_('UI'), [
                (_('Grid'),      0, 1),
                (_('Axes'),      1, 1),
                (_('Light'),     2, 1),
                (_('Orbit'),     0, 1),
                (_('Skybox'),    1, 1),
                (_('Terrain'),   2, 1),
            ]),
            # Model tools -- act on the loaded tank rather than
            # the viewport.  Burnt-orange accent so they match
            # Meshes / Flip / Compare.  Wireframe and Shaded are
            # toggle-style; Meshes / Flip / Compare are action-
            # style.  Both flavours land here because they all
            # operate on the loaded tank.
            (_('Model'), [
                (_('Meshes'),    0, 1),
                (_('Flip'),      1, 1),
                (_('Compare'),   2, 1),
                (_('Wireframe'), 0, 1),
                (_('Shaded'),    1, 1),
            ]),
            (_('IO'), [
                (_('Set Paths'), 0, 3),
                # Export + Import share one row at equal widths.
                # The list-form entry triggers the even-split
                # packing path in the loop below -- each gets
                # (full_row - gap) / 2 pixels, so they're
                # guaranteed identical sized regardless of how
                # the 3-col grid would have packed them.
                # Accent colours (burnt-orange Export, olive
                # Import) set in `_build_ui`.
                [_('Export'), _('Import')],
                (_('Save Prim'), 0, 3),
                (_('Language'),  0, 3),
                (_('Theme'),     0, 3),
            ]),
            (_('Tools'), [
                (_('ItemList'),  0, 3),
                (_('Pick Tri'),  0, 3),
            ]),
        ]

        # Section-header label: rendered as a single line of soft-grey
        # text above each group's first row, with a thin gap below.
        SECTION_LABEL_H   = 16    # vertical reserve for the header text
        SECTION_GAP_AFTER = 2     # tight tuck under the label
        FULL_ROW_W        = BTN_W * 3 + BTN_GAP_X * 2

        # Re-create the section labels each layout pass -- text textures
        # are owned by UIManager.panel_labels and freed in clear_panel_labels.
        self.ui.clear_panel_labels()

        # Persisted per-section collapse state.  Defaults to {} so an
        # older config (pre-section-collapse) reads as "everything
        # expanded".  Section names are matched by their UNTRANSLATED
        # English key so a language switch doesn't desync the state.
        section_collapsed = self._cfg.get('section_collapsed') or {}

        # English keys for each group, in `button_groups` order.
        # Index-aligned with `button_groups`.  Used for the
        # `section_collapsed` dict so a language switch doesn't
        # invalidate the user's collapse picks (the displayed label
        # changes via `_()`, but the storage key stays stable).
        section_keys = ['res_mods', 'UI', 'Model', 'IO', 'Tools']

        btn_by_label = {b.label: b for b in self.ui.buttons}
        # Default every button to visible; the collapse loop below
        # toggles it off for any section whose body should hide.
        for b in self.ui.buttons:
            b.visible = True
        # Walk groups from top to bottom, packing rows into the column grid
        # within each group.  Tracks an explicit "next_y" so we can
        # interleave headers + button rows without computing a global
        # row index.
        next_y = BTN_PAD_Y
        for grp_idx, (section_name, rows) in enumerate(button_groups):
            section_key = (section_keys[grp_idx]
                           if grp_idx < len(section_keys)
                           else section_name)
            collapsed   = bool(section_collapsed.get(section_key, False))

            # Header label: chevron + section name.  The chevron is a
            # wide-headed barb (Supplemental Arrows-C, U+1F86A/B);
            # they need Segoe UI Symbol to render -- but our default
            # _make_tex font is Calibri which doesn't carry the
            # block.  Workaround: use ▶ / ▼ (BLACK RIGHT/DOWN-POINTING
            # TRIANGLE, U+25B6 / U+25BC) which Calibri DOES carry.
            # Visually similar -- chunky filled triangle -- and a
            # one-codepoint sub doesn't pull a second font into the
            # panel-label render path.
            chevron = '▶' if collapsed else '▼'  # ▶ / ▼
            header_text = f"{chevron}  {section_name}"
            self.ui.add_panel_label(
                header_text,
                x=BTN_PAD_X, y=next_y,
                color=(150, 165, 200),
                section_key=section_key,
                # Wide click target: full row width so the user can
                # hit the bar anywhere across the panel, not just on
                # the few-pixel-wide chevron+text.
                click_w=FULL_ROW_W,
            )
            next_y += SECTION_LABEL_H + SECTION_GAP_AFTER

            if collapsed:
                # Hide every button that belongs to this section.
                # Geometry stays whatever it was; `visible=False`
                # short-circuits both the renderer and click hits.
                for item in rows:
                    labels = (item if isinstance(item, list)
                              else [item[0]])
                    for lbl in labels:
                        b = btn_by_label.get(lbl)
                        if b is not None:
                            b.visible = False
                # No body height -- next group renders right under
                # the (just-rendered) header.
                continue

            # Pack rows: each entry is one of:
            #   (label, col, span)    -- a single button at a 3-col
            #                            grid slot (existing layout)
            #   [label_a, label_b]    -- two-or-more buttons that
            #                            split a full-width row
            #                            EVENLY (each gets the same
            #                            pixel width).  Used for
            #                            Import / Export which the
            #                            user wants on the same row
            #                            at identical widths -- the
            #                            3-col grid can't quite do
            #                            that with integer spans.
            #
            # When a span would overflow the next column boundary,
            # that row sits on its own line; consecutive single-cell
            # entries stack left-to-right and only bump y when the
            # column wraps.
            row_y    = next_y
            cur_col  = 0
            full_w   = FULL_ROW_W
            for item in rows:
                if isinstance(item, list):
                    # Even-split row.  Always lands on its own line;
                    # close any partial row above, then walk the
                    # labels and place them with equal width.
                    if cur_col != 0:
                        row_y += BTN_H + BTN_GAP_Y
                        cur_col = 0
                    labels = [s for s in item]
                    n = max(1, len(labels))
                    each_w = (full_w - BTN_GAP_X * (n - 1)) // n
                    cx = BTN_PAD_X
                    for lbl in labels:
                        b = btn_by_label.get(lbl)
                        if b is None:
                            cx += each_w + BTN_GAP_X
                            continue
                        b.x = cx
                        b.y = row_y
                        b.w = each_w
                        cx += each_w + BTN_GAP_X
                    row_y += BTN_H + BTN_GAP_Y
                    cur_col = 0
                    continue

                # Standard (label, col, span) tuple.
                label, col, span = item
                btn = btn_by_label.get(label)
                if not btn:
                    continue
                # If this entry's start column is to the LEFT of where
                # the cursor currently sits, that means the previous
                # entry filled the row -- start a new row.
                if col < cur_col:
                    row_y += BTN_H + BTN_GAP_Y
                    cur_col = 0
                btn.x = BTN_PAD_X + col * (BTN_W + BTN_GAP_X)
                btn.y = row_y
                btn.w = BTN_W * span + BTN_GAP_X * (span - 1)
                cur_col = col + span
                # Saturated row -> next entry will be on a new line.
                if cur_col >= 3:
                    row_y += BTN_H + BTN_GAP_Y
                    cur_col = 0

            # `row_y` is now pointing at the line *after* the last row
            # if cur_col == 0, or AT the last row's line if not.  Make
            # sure we land at the start of the next free line either
            # way before adding the inter-group gap.
            if cur_col != 0:
                row_y += BTN_H + BTN_GAP_Y
            next_y = row_y + BTN_GAP_Y * 2     # extra breathing room
                                                # between groups

        # Lighting sliders inside the left panel.  Slider value text sits
        # to the right of the track.  NMap/AO checkboxes get their own
        # row below the Ambient slider so the slider tracks can run wider.
        #
        # Geometry: track is centered horizontally in the info panel
        # (so the empty space on either side of the track is equal),
        # and the value-text column sits one comfortable gap to the
        # right of the track end so the digits don't crowd the
        # handle when the slider is at value_max.
        L_TRACK_W = 128                                  # track width
        L_TRACK_X = (self.INFO_PANEL_W - L_TRACK_W) // 2 # centered in panel
        L_VAL_GAP = 10                                   # bar->number gap
        L_VAL_X   = L_TRACK_X + L_TRACK_W + L_VAL_GAP
        # Sliders sit below the grouped button block.  `next_y` from the
        # group-packing loop above is already pointing at the first free
        # line after the Tools group, so we just add a small breathing
        # gap and use it directly.
        slider_y0 = next_y + 12
        ROW_H     = 25

        if self._metal_slider:
            self._metal_slider.track_x  = L_TRACK_X
            self._metal_slider.track_cy = slider_y0
            self._metal_slider.track_w  = L_TRACK_W
            self._metal_slider.label_x  = 6
            self._metal_slider.value_x  = L_VAL_X
        if self._shine_slider:
            self._shine_slider.track_x  = L_TRACK_X
            self._shine_slider.track_cy = slider_y0 + ROW_H
            self._shine_slider.track_w  = L_TRACK_W
            self._shine_slider.label_x  = 6
            self._shine_slider.value_x  = L_VAL_X
        # Checkbox row below Ambient -- two checkboxes side by side
        cb_row_cy = slider_y0 + 2 * ROW_H
        if self._invert_metal_cb:
            self._invert_metal_cb.x = 16
            self._invert_metal_cb.y = cb_row_cy - self._invert_metal_cb.size // 2
        if self._invert_shine_cb:
            self._invert_shine_cb.x = 110
            self._invert_shine_cb.y = cb_row_cy - self._invert_shine_cb.size // 2

        # Update LEFT_CONTROLS_H to fit whatever the layout above
        # actually produced.  The button-group structure has expanded
        # over time (display toggles -> + UI / IO / Tools section
        # headers -> + ItemList button), and a fixed constant kept
        # going stale and pushing the lighting sliders into the info-
        # tree below.  Now we measure: bottom of the last checkbox
        # row + a small bottom margin = the height the controls block
        # needs to claim.  The info-panel positioning in _on_resize
        # reads `self.LEFT_CONTROLS_H` AFTER this method runs, so the
        # tree below sits cleanly under whatever we computed here.
        cb_size_default = 14    # UICheckbox default; tolerable approx
        cb_bottom = cb_row_cy + cb_size_default // 2 + 1
        BOTTOM_MARGIN = 10
        self.LEFT_CONTROLS_H = max(self.__class__.LEFT_CONTROLS_H,
                                    cb_bottom + BOTTOM_MARGIN)

        # ---- RIGHT PANEL -------------------------------------------------
        # Sliders + checkboxes laid out in three labelled groups:
        #
        #     Smoke   (5 sliders -- Sm Start / Sm End / Sm Speed /
        #              Sm FadeS / Sm FadeE)
        #     Fire    (1 slider  -- Fire Size)
        #     Normals (1 slider  -- Normals; PerVtx + Debug
        #              checkboxes share the row below)
        #
        # Each group gets a small grey sub-header above its sliders
        # so the user can tell at a glance which knob affects what.
        # Tighter row spacing (22 px vs the old 25) + wider tracks
        # (165 vs 135) trades a little vertical for a lot of
        # horizontal -- no more 70 px of dead space on the right
        # edge.
        SMOKE_SLIDERS = [
            (self._smoke_start_slider,    'Sm Start'),
            (self._smoke_end_slider,      'Sm End'),
            (self._smoke_speed_slider,    'Sm Speed'),
            (self._smoke_fade_slider,     'Sm FadeS'),
            (self._smoke_fade_end_slider, 'Sm FadeE'),
        ]
        FIRE_SLIDERS    = [(self._fire_size_slider, 'Fire Size')]
        NORMALS_SLIDERS = [(self._normals_slider,   'Normals')]
        slider_groups = [
            (_('Smoke'),   SMOKE_SLIDERS),
            (_('Fire'),    FIRE_SLIDERS),
            (_('Normals'), NORMALS_SLIDERS),
        ]

        # Geometry constants -- tightened from the v1.71.x baseline
        # so the panel claims less vertical for the same widgets.
        R_TOP_PAD            = 8
        R_SUB_HEADER_H       = 14
        R_SUB_HEADER_GAP     = 2
        R_GROUP_GAP_AFTER    = 4         # extra breathing between groups
        R_ROW_H              = 22        # was 25
        R_CB_ROW_H           = 18
        R_BOTTOM_MARGIN      = 8
        # Debug section header sits above the Smoke / Fire /
        # Normals sub-headers and acts as the master collapse
        # toggle for everything below it.  Same row height as the
        # sub-headers so the header strip math stays simple.
        R_DEBUG_HEADER_H     = 14
        R_DEBUG_HEADER_GAP   = 4

        # Section state -- 'Debug' key in section_collapsed.
        section_collapsed = self._cfg.get('section_collapsed') or {}
        debug_collapsed   = bool(section_collapsed.get('Debug', False))

        n_sliders = sum(1 for g, lst in slider_groups
                          for sl, _lbl in lst if sl)
        n_groups  = len(slider_groups)

        # Sub-header skip rule: when a group has exactly ONE slider
        # AND the slider's own label matches the group label, the
        # group sub-header would duplicate text that the slider
        # already renders one line below.  Caught while staring at
        # the Normals group, where both group and the only slider
        # were `_('Normals')`, producing a stacked-text bug.
        # Comparison is on the live `slider.label` so it works
        # across translations -- both group label and slider label
        # come out of `gettext` so their translated strings match
        # iff the source strings did.
        def _group_header_redundant(grp, slis):
            if len(slis) != 1:
                return False
            sl, _lbl = slis[0]
            return sl is not None and sl.label == grp
        n_headers = sum(1 for grp, slis in slider_groups
                          if not _group_header_redundant(grp, slis))

        # Required panel height -- depends on collapse state.
        # Collapsed: just the header strip + paddings.
        # Expanded: header + groups + sliders + checkbox row + bottom.
        if debug_collapsed:
            self.RIGHT_CONTROLS_H = (R_TOP_PAD + R_DEBUG_HEADER_H
                                      + R_BOTTOM_MARGIN)
        else:
            required_h = (R_TOP_PAD
                          + R_DEBUG_HEADER_H + R_DEBUG_HEADER_GAP
                          + n_headers * (R_SUB_HEADER_H + R_SUB_HEADER_GAP)
                          + n_groups  * R_GROUP_GAP_AFTER
                          + n_sliders * R_ROW_H
                          + R_CB_ROW_H + R_BOTTOM_MARGIN)
            self.RIGHT_CONTROLS_H = max(self.__class__.RIGHT_CONTROLS_H,
                                         required_h)

        right_x   = self.width - self.TREE_PANEL_W
        right_top = self.height - self.RIGHT_CONTROLS_H

        # Slider geometry -- track width matches the v1.71.x
        # baseline (135 px); user-confirmed as the right size.
        # The wasted right margin past the value column is
        # acceptable -- room to grow if a future longer value
        # string needs the space.
        R_TRACK_X = right_x + 72
        R_TRACK_W = 135
        R_VAL_X   = R_TRACK_X + R_TRACK_W + 6
        R_LABEL_X = right_x + 6

        # ---- Debug section header (always rendered, clickable) ----
        # Chevron + label.  The full panel width (minus the spine
        # margin) is the click target so the user can hit the bar
        # anywhere along its length.
        debug_chev = '▶' if debug_collapsed else '▼'
        self.ui.add_panel_label(
            f"{debug_chev}  {_('Debug')}",
            x=R_LABEL_X,
            y=right_top + R_TOP_PAD,
            color=(150, 165, 200),
            section_key='Debug',
            click_w=self.TREE_PANEL_W - 8,
        )

        # When collapsed, skip the body entirely -- widgets keep
        # their last-known positions (we set visible=False at the
        # END of _layout_widgets, after the final unconditional
        # True-loop, so collapsed widgets stay hidden until the
        # next un-collapse).
        if not debug_collapsed:
            cur_y = (right_top + R_TOP_PAD
                     + R_DEBUG_HEADER_H + R_DEBUG_HEADER_GAP)
            # Walk the groups, painting sub-headers via
            # add_panel_label and positioning each slider in turn.
            # Sub-headers are skipped for redundant single-slider
            # groups (see `_group_header_redundant` above) so we
            # don't double-print the same name.
            for grp_label, sliders in slider_groups:
                if not _group_header_redundant(grp_label, sliders):
                    self.ui.add_panel_label(
                        grp_label,
                        x=R_LABEL_X,
                        y=cur_y,
                        color=(150, 165, 200),
                    )
                    cur_y += R_SUB_HEADER_H + R_SUB_HEADER_GAP
                for sl, _lbl in sliders:
                    if not sl:
                        continue
                    sl.track_x  = R_TRACK_X
                    sl.track_cy = cur_y + R_ROW_H // 2
                    sl.track_w  = R_TRACK_W
                    sl.label_x  = R_LABEL_X
                    sl.value_x  = R_VAL_X
                    sl.visible  = True
                    cur_y += R_ROW_H
                cur_y += R_GROUP_GAP_AFTER

            # PerVtx + Debug + Susp checkboxes sit on a shared row
            # at the bottom of the Normals group.  Three columns at
            # +8 / +90 / +172 px so labels don't run together inside
            # the 286-px panel.  Checkboxes don't have track_x; we
            # use the centroid of their box so y aligns with the
            # slider tracks above visually.
            cb_row_cy = cur_y + R_CB_ROW_H // 2
            if self._normals_mode_cb:
                self._normals_mode_cb.x = right_x + 8
                self._normals_mode_cb.y = cb_row_cy - self._normals_mode_cb.size // 2
                self._normals_mode_cb.visible = True
            if self._debug_cb:
                self._debug_cb.x = right_x + 90
                self._debug_cb.y = cb_row_cy - self._debug_cb.size // 2
                self._debug_cb.visible = True
            if self._suspension_cb:
                self._suspension_cb.x = right_x + 172
                self._suspension_cb.y = cb_row_cy - self._suspension_cb.size // 2
                self._suspension_cb.visible = True

        # All other widgets stay visible
        for w in self.ui.sliders + self.ui.checkboxes:
            if not hasattr(w, 'visible'):
                continue
            w.visible = True

        # When the Debug section is collapsed, override the final
        # True-loop above and force every right-panel widget back
        # to visible=False.  Done as a tail-block so the
        # left-panel-targeted True-loop stays untouched -- we just
        # undo what it did to the right-panel widgets when
        # collapse demands it.  Keeps the change scoped to the
        # right side; left panel never knows this exists.
        if debug_collapsed:
            for sl, _lbl in (SMOKE_SLIDERS + FIRE_SLIDERS
                              + NORMALS_SLIDERS):
                if sl:
                    sl.visible = False
            if self._normals_mode_cb:
                self._normals_mode_cb.visible = False
            if self._debug_cb:
                self._debug_cb.visible = False
            if self._suspension_cb:
                self._suspension_cb.visible = False

    # ------------------------------------------------------------------
    # Logging helpers (mirror to stdout AND the in-app console)
    # ------------------------------------------------------------------

    def log(self, msg, color=None):
        """Print to stdout AND append a line to the bottom console.

        Args:
            msg (str): the line to log.
            color (tuple | None): optional (r, g, b) 0-255; None falls
                back to the console's default light-grey.
        """
        try:
            print(msg)
        except Exception:
            pass
        try:
            if self.ui and getattr(self.ui, 'console', None):
                self.ui.console.add_line(msg, color)
        except Exception:
            # Don't let a logging failure ever crash the running app.
            pass

    def log_error(self, msg):
        """Same as log() but red-tinted -- for missing textures, parse
        failures, etc.
        """
        self.log(msg, color=(255, 110, 110))

    def log_status(self, status):
        """Update the console header's status string (the "what we're
        currently doing" tag).  Doesn't add a line; pair it with a
        clear() at the start of a fresh action and add_line() calls
        below.
        """
        try:
            if self.ui and getattr(self.ui, 'console', None):
                self.ui.console.set_status(status)
        except Exception:
            pass

    def log_clear(self, status=None):
        """Empty the console buffer + optionally retag the status.
        Called at the start of every user-driven action (tank load,
        Import, Export, Save Prim) so the resulting log is focused
        on that action only.
        """
        try:
            if self.ui and getattr(self.ui, 'console', None):
                self.ui.console.clear(status=status)
        except Exception:
            pass

    def handle_input(self):
        """Process all queued SDL events and mouse state."""
        for event in pygame.event.get():
            if event.type == QUIT:
                self.running = False

            elif event.type == pygame.WINDOWENTER:
                # Mouse just crossed INTO the TEPY window.  Save
                # whichever window currently holds the foreground
                # so we can return focus when the cursor leaves,
                # then bring TEPY to the foreground.  Eliminates
                # the "first click only focuses, second click
                # actually does the action" double-tap that's
                # standard Windows behaviour for unfocused apps.
                self._maybe_steal_focus_on_enter()

            elif event.type == pygame.WINDOWLEAVE:
                # Cursor left the TEPY window -- hand focus back
                # to whatever was in front before, so we don't
                # silently rob another app of its input the moment
                # the user mouses away.
                self._maybe_restore_focus_on_leave()

            elif event.type == VIDEORESIZE:
                # Carry vsync=1 through every set_mode call -- SDL
                # treats each set_mode as a fresh window-create on
                # some platforms and resets the swap interval to 0
                # if we don't ask for vsync explicitly here too.
                pygame.display.set_mode(
                    (event.w, event.h),
                    DOUBLEBUF | OPENGL | RESIZABLE,
                    vsync=1,
                )
                self._on_resize(event.w, event.h)

            elif event.type == KEYDOWN:
                if event.key == K_ESCAPE:
                    self.running = False
                elif event.key == K_F11:
                    # Fullscreen toggle.  After splash teardown
                    # the window is already fullscreen; F11 brings
                    # it back to the windowed dimensions we
                    # remembered at startup, F11 again returns to
                    # fullscreen.  Standard convention across
                    # most desktop GL apps.
                    self._toggle_fullscreen()
                elif event.key == K_F2:
                    # Wireframe toggle.  Was W until v1.94 -- moved
                    # to F2 because W is now the forward-drive key
                    # in the new WASD-style tank-handles scheme.
                    # F1 is reserved for the help overlay (future).
                    self.wireframe = not self.wireframe
                    self._sync_button_state('wireframe', self.wireframe)
                elif event.key == K_n:
                    self.use_normal_map = not self.use_normal_map
                elif event.key == K_o:
                    # Toggle auto-circle drive.  Tank pulls a steady
                    # arc of radius `_auto_circle_radius` at the
                    # current speed-step kph; yaw rate auto-derived
                    # to face the tangent.  Mutually exclusive with
                    # cruise (S) -- toggling either turns the other
                    # off.
                    self._auto_circle = not self._auto_circle
                    self.log(f"auto-circle: "
                             f"{'on' if self._auto_circle else 'off'}  "
                             f"(R={self._auto_circle_radius:.1f} m)",
                             color=(180, 220, 255) if self._auto_circle
                             else (180, 180, 180))
                elif event.key == K_c:
                    # Cycle camera mode: orbit -> chase -> commander -> orbit.
                    #   0 (orbit / free cam) -> 1 (chase)
                    #   1 (chase)            -> 2 (commander)
                    #   2 (commander)        -> 0 (orbit)
                    #
                    # On the 2 -> 0 hop (commander -> free cam),
                    # snapshot commander's CURRENT eye + look-at into
                    # the orbit camera state.  Why: commander view is
                    # locked to the chassis -- as the tank moves, the
                    # commander-view eye moves with it.  Without this
                    # save, free cam would yank back to a stale
                    # pre-commander orbit pose, leaving the camera
                    # somewhere the tank used to be.  By copying
                    # commander's now-position into self.camera, free
                    # cam picks up exactly where commander was looking
                    # at the moment of the press.  User request
                    # 2026-05-08.
                    if (self.camera_mode == 2
                            and self.tank_physics is not None):
                        chassis = np.asarray(
                            self.tank_physics.chassis_matrix(),
                            dtype=np.float64)
                        # Use the SAME eye + head-rotated target the
                        # commander view is actually looking at -- so
                        # free cam picks up at the current head
                        # rotation, not the forward-locked default.
                        # Mirror of _anchored_view_matrix's C2 branch.
                        head_yaw   = math.radians(self._head_yaw_deg)
                        head_pitch = math.radians(self._head_pitch_deg)
                        cy_, sy_ = math.cos(head_yaw),   math.sin(head_yaw)
                        cp_, sp_ = math.cos(head_pitch), math.sin(head_pitch)
                        fwd = np.array(
                            [-cp_ * sy_, sp_, -cp_ * cy_],
                            dtype=np.float64)
                        eye_local    = np.array(
                            [0.0, 1.95, 0.0, 1.0], dtype=np.float64)
                        target_local = np.array(
                            [eye_local[0] + 10.0 * fwd[0],
                             eye_local[1] + 10.0 * fwd[1],
                             eye_local[2] + 10.0 * fwd[2],
                             1.0], dtype=np.float64)
                        eye_world    = (chassis @ eye_local)[:3]
                        target_world = (chassis @ target_local)[:3]
                        # offset points from look-at BACK to eye --
                        # which is exactly the orbit camera's
                        # eye-relative-to-center vector.
                        offset = eye_world - target_world
                        dist   = float(np.linalg.norm(offset))
                        if dist > 1e-6:
                            self.camera.center   = (
                                target_world.astype(np.float32))
                            self.camera.distance = max(0.5, dist)
                            self.camera.yaw     = float(np.degrees(
                                np.arctan2(offset[0], offset[2])))
                            self.camera.pitch   = float(np.degrees(
                                np.arctan2(
                                    offset[1],
                                    np.hypot(offset[0], offset[2]))))
                    self.camera_mode = (
                        self.camera_mode + 1) % self._N_CAMERA_MODES
                    # Entering commander mode: reset head rotation to
                    # straight-forward + level so the user always
                    # starts the seat ride facing where the tank is
                    # facing.  Mouse drag rotates from there.
                    if self.camera_mode == 2:
                        self._head_yaw_deg   = 0.0
                        self._head_pitch_deg = 0.0
                    label = ('orbit', 'chase (driver side)',
                             'commander (turret POV)')[self.camera_mode]
                    self.log(f"camera: {label}",
                             color=(180, 220, 255))
                elif event.key == K_h:
                    # Toggle the contact-wheel red highlight.
                    # Independent of Susp so the user can keep the
                    # physics running while turning the visual aid
                    # on/off.  Default ON because the highlight is
                    # the whole point of the per-wheel-anchor visual
                    # debug -- you want to SEE which 4 wheels the
                    # plane fit is leaning on.
                    self._highlight_contacts = not self._highlight_contacts
                    self.log(f"contact highlight: "
                             f"{'on' if self._highlight_contacts else 'off'}",
                             color=(255, 80, 80) if self._highlight_contacts
                             else (180, 180, 180))
                elif event.key == K_r:
                    # Full camera reset -- restore yaw/pitch and re-fit to mesh bounds.
                    # Matches Camera.__init__ defaults (yaw 225 = 45-deg
                    # front-left 3/4 view).
                    self.camera.yaw      = 225.0
                    self.camera.pitch    = 30.0
                    self.camera.center   = np.array([0.0, 0.0, 0.0], dtype=np.float32)
                    self.camera.distance = 10.0
                    if self._scene_bbox is not None:
                        self.camera.fit_to_bounds(*self._scene_bbox)
                # Speed step selector: 0 stops the tank, 1 picks the
                # current tank's max forward speed, 9 picks creep
                # (0.1 kph), 2..8 linearly interpolate.  Driven by
                # _kph_for_step / _speed_yards_per_sec; consumed by
                # the arrow-key drive logic in handle_input.  Number
                # row only -- numpad keys are NOT bound so we don't
                # collide with future zoom / camera shortcuts.
                elif event.key == K_0:
                    self._set_speed_step(0)
                elif event.key == K_1:
                    self._set_speed_step(1)
                elif event.key == K_2:
                    self._set_speed_step(2)
                elif event.key == K_3:
                    self._set_speed_step(3)
                elif event.key == K_4:
                    self._set_speed_step(4)
                elif event.key == K_5:
                    self._set_speed_step(5)
                elif event.key == K_6:
                    self._set_speed_step(6)
                elif event.key == K_7:
                    self._set_speed_step(7)
                elif event.key == K_8:
                    self._set_speed_step(8)
                elif event.key == K_9:
                    self._set_speed_step(9)

            elif event.type == MOUSEWHEEL:
                # Tree panel scroll has priority over camera zoom
                mx, my = pygame.mouse.get_pos()
                if self.ui.handle_mouse_wheel(mx, my, event.y):
                    pass  # consumed by the tree
                else:
                    # Mode-aware zoom: chase has its own
                    # `_chase_distance` (chassis-local orbit radius);
                    # commander is fixed-position and ignores wheel
                    # zoom; orbit/free uses the standard
                    # `self.camera.distance`.
                    if event.y > 0:
                        zoom = 0.9    # wheel forward -> zoom in
                    elif event.y < 0:
                        zoom = 1.1
                    else:
                        zoom = 1.0
                    if self.camera_mode == 1:
                        self._chase_distance = max(
                            0.5, self._chase_distance * zoom)
                    elif self.camera_mode != 2:
                        self.camera.distance *= zoom

            elif event.type == MOUSEBUTTONDOWN:
                mx, my = event.pos
                if event.button == 1 and self.ui.is_pointer_over_ui(mx, my):
                    # Section-header clicks come first -- they're
                    # wider hit zones than buttons (full row width)
                    # and folding the section out from under a click
                    # would otherwise consume the same coords twice.
                    # 'Debug' is the right-panel section and uses a
                    # dedicated handler that avoids touching
                    # left-panel state; every other section_key
                    # (left panel) routes through the generic
                    # handler.
                    section_key = self.ui.section_header_at(mx, my)
                    if section_key == 'Debug':
                        self._toggle_debug_collapsed()
                    elif section_key is not None:
                        self._toggle_section_collapsed(section_key)
                    else:
                        widget = self.ui.handle_mouse_down(mx, my)
                        if widget is not None:    # UIButton returned
                            self._apply_button_action(widget)

            elif event.type == MOUSEBUTTONUP:
                if event.button == 1:
                    # Snapshot whether a slider was being dragged BEFORE
                    # handle_mouse_up clears _active_slider.  If yes,
                    # the user just released after a tweak -- snapshot
                    # the active engine class's slider values into
                    # self._cfg and write the JSON so the change
                    # persists right now (instead of waiting for the
                    # window to close).
                    slider_was_active = self.ui._active_slider is not None
                    self.ui.handle_mouse_up()
                    if slider_was_active:
                        self._persist_all_sliders(write_json=True)

            elif event.type == MOUSEMOTION:
                self.ui.update_hover(*event.pos)
                if event.buttons[0]:    # left button held -- update slider drag
                    self.ui.handle_mouse_drag(*event.pos)

        # Continuous mouse-button camera controls
        btns      = pygame.mouse.get_pressed()
        mouse_pos = pygame.mouse.get_pos()

        # Crosshair visibility: any time the user is actively
        # moving the look-at point.  Captured here (outside the
        # cursor-over-UI gate so it stays correct even when the
        # pointer briefly grazes a UI panel mid-drag), consumed by
        # `render`.  Shift held (Y-axis lift) OR middle-mouse
        # button (the "wheel" button -- XZ pan).  Right-click
        # orbit doesn't move the look-at, so it doesn't trigger
        # the cue.
        shift_held = bool(pygame.key.get_mods() & pygame.KMOD_SHIFT)
        # Crosshair shows whenever the user is moving the look-at
        # point.  After the 2026-05-08 mouse rebind:
        #   RIGHT-button held  -> XZ pan  -> show crosshair
        #   MIDDLE-button held -> Y-lift (with Shift) -> show crosshair
        # Either chord turns it on so the user always sees where
        # they're aiming during a pan / Y-lift.  Drawn with depth
        # test ENABLED (see render() near `lookat_lines.render`)
        # so the lines occlude correctly behind the tank / terrain
        # -- the look-at point inside a piece of geometry only shows
        # the parts of the cross that emerge to the surface, which
        # reads as the natural "your aim is here" cue.
        self._show_lookat_lines = bool(btns[1] or btns[2])

        # Tank-driving keys: arrow keys move the tank around the
        # terrain (XZ only -- physics handles Y / pitch / roll).
        # Useful for watching the suspension respond to the heightmap
        # in real time.  Speed scales with frame dt so movement feels
        # uniform regardless of FPS.  Q / E rotate the chassis yaw.
        #
        # Speeds NEGATED from the natural mathematical convention
        # because TEPY's rendered world Z direction runs opposite
        # to the chassis-mesh-local Z (skinned chassis is not
        # Z-flipped on load while the hull/turret/gun are, so the
        # visible front of the tank ends up at world -Z).  This
        # negation makes UP=forward, LEFT=strafe-left, Q=yaw-left
        # match the visible tank.
        if (self.tank_physics_enabled and self.tank_physics
                and self.show_terrain and self.terrain):
            keys = pygame.key.get_pressed()
            dt   = max(1e-3, getattr(self, '_frame_dt', 1.0 / 60.0))
            # Stepped speed selector.  `_speed_yards_per_sec()`
            # returns the per-step rate in scene units (= yards) per
            # second; we negate to match TEPY's rendered Z direction
            # (visible front of the tank lives at world -Z because
            # the chassis is not Z-flipped on load while the hull /
            # turret / gun are).  Default step is 0 (stopped) so the
            # tank doesn't move until the user picks a speed via
            # keys 1-9.
            move_speed = -self._speed_yards_per_sec()   # m/s, sign flipped
            yaw_speed  = -60.0  # deg/s, sign flipped (yaw rate is
                                # speed-step-independent for now;
                                # could scale with move_speed later
                                # but turn-in-place has its own feel
                                # and Professor Coffee can flag if
                                # it needs a step ladder too)
            tp = self.tank_physics
            yaw = math.radians(tp.yaw_deg)
            cyy, syy = math.cos(yaw), math.sin(yaw)
            moved = False

            # ---- Decide TARGET forward speed -------------------------
            # `move_speed` is already negated for TEPY's render-Z
            # convention (so `pos[2] += cyy * move_speed * dt` moves
            # the tank in its visible-forward direction).  We compute
            # a TARGET signed velocity for the current input set, then
            # ramp `self._current_forward` toward it using accel /
            # decel rates -- so W press doesn't snap to full speed,
            # release doesn't stop instantly.
            #
            # Auto-circle, cruise, and W all want forward (full step).
            # Z wants backward (negate).  No keys = decel toward 0.
            target_forward = 0.0
            if self._auto_circle:
                target_forward = move_speed
            elif keys[pygame.K_w] or keys[pygame.K_s]:
                # W and S both drive forward (momentary -- release
                # to stop, same as any other drive key).  S used
                # to be a cruise toggle but Professor Coffee
                # preferred straight hold-to-go behaviour.
                target_forward = move_speed
            elif keys[pygame.K_z] or keys[pygame.K_x]:
                # Z and X both drive backward (hold).  X is the
                # duplicate added at Professor Coffee's request --
                # different finger position, same effect.
                target_forward = -move_speed

            # ---- Ramp toward target ----------------------------------
            # ACCEL: tank spools up to step speed in ~2 seconds at
            #   default values -- a real heavy tank takes longer but
            #   3-5 m/s^2 reads as snappy-but-not-instant on screen.
            # DECEL: ~2x ACCEL so braking + direction reversal feel
            #   responsive (real tanks can brake harder than they
            #   accelerate; matches tank handling intuition).
            DRIVE_ACCEL = 5.0    # m/s^2, spool-up
            DRIVE_DECEL = 10.0   # m/s^2, braking + reversal
            cur   = float(self._current_forward)
            delta = target_forward - cur
            # Same direction AND target larger in magnitude = accel.
            # Opposite direction OR target smaller in magnitude = decel.
            if (target_forward * cur >= 0.0
                    and abs(target_forward) > abs(cur)):
                rate = DRIVE_ACCEL
            else:
                rate = DRIVE_DECEL
            step = rate * dt
            if abs(delta) <= step:
                cur = target_forward
            else:
                cur += step if delta > 0 else -step
            self._current_forward = cur

            # ---- Auto circle drive ---------------------------------
            # When toggled ON via `O`, the tank ignores manual drive
            # keys and instead pulls a steady arc of radius
            # `_auto_circle_radius` at the current ramped speed.
            # Yaw rate omega = v / R is exactly what's needed to
            # keep the chassis facing along the circle's tangent
            # at every point.  Using `cur` (ramped) instead of
            # `move_speed` (instant target) means the circle starts
            # tight and spirals out as the tank accelerates.
            if self._auto_circle:
                R    = max(0.1, float(self._auto_circle_radius))
                v    = abs(cur)   # current ramped m/s
                if v > 1e-6:
                    omega_deg = math.degrees(v / R)
                    tp.yaw_deg -= omega_deg * dt
                    yaw = math.radians(tp.yaw_deg)
                    cyy, syy = math.cos(yaw), math.sin(yaw)
            elif not self._auto_circle:
                # Manual yaw input.  Yaw rate is INSTANT (no ramp)
                # because tanks pivot quickly when stopped and
                # ramping the yaw was distracting in testing.
                if keys[pygame.K_a] or keys[pygame.K_q]:
                    tp.yaw_deg -= yaw_speed * dt
                    moved = True
                if keys[pygame.K_d] or keys[pygame.K_e]:
                    tp.yaw_deg += yaw_speed * dt
                    moved = True

            # ---- Apply ramped forward velocity to position ---------
            if abs(cur) > 1e-6:
                tp.pos[0] += syy * cur * dt
                tp.pos[2] += cyy * cur * dt
                moved = True
            # One-shot diagnostic so we can tell whether the issue
            # is "keys not detected at all" (focus) vs "detected
            # but tank not visibly responding".  Logged once per
            # tank load.
            if moved and not getattr(self, '_drive_keys_seen', False):
                self.log(
                    f"drive: tank pos = "
                    f"({tp.pos[0]:+.2f}, _, {tp.pos[2]:+.2f})  "
                    f"yaw = {tp.yaw_deg:+.1f}",
                    color=(180, 220, 255))
                self._drive_keys_seen = True

        # Block the camera path entirely while a UI slider is being
        # dragged.  The slider's MOUSEMOTION+buttons[0] handler at
        # line ~8806 fires regardless of whether the cursor wandered
        # outside the panel mid-drag -- without this guard, dragging
        # a slider off the panel would simultaneously update the
        # slider AND orbit the camera (since LEFT-button now does
        # both).
        slider_active = self.ui._active_slider is not None
        if (not self.ui.is_pointer_over_ui(*mouse_pos)
                and not slider_active):
            dx = mouse_pos[0] - self.mouse_last[0]
            dy = mouse_pos[1] - self.mouse_last[1]

            # Mouse-button bindings (2026-05-08 user request):
            #   LEFT   (btns[0]) -> orbit (yaw / pitch the camera)
            #   RIGHT  (btns[2]) -> pan on the XZ ground plane
            #   MIDDLE (btns[1]) -> show look-at crosshair; +Shift = Y-lift
            #   WHEEL scroll      -> zoom (handled in MOUSEBUTTONDOWN above)
            #
            # The `is_pointer_over_ui` gate above means LEFT-click on a
            # UI button / slider goes to the UI handler instead of orbit,
            # so the rebind doesn't break panel interaction.  The
            # `not slider_active` extra gate handles the case where the
            # user drags a slider OUT of the panel mid-drag (still want
            # the slider to win, not have orbit kick in).
            #
            # Shift + MIDDLE keeps Y-lift on its own dedicated chord so
            # an accidental Shift during typing doesn't yank the camera.
            if shift_held and btns[1] and (dx or dy):
                # Half the pan-XZ speed so the Y lift feels deliberate
                # and lands precisely instead of overshooting.  The
                # 0.10 factor (vs the pan's 0.20) is the only knob --
                # adjust here if it ever feels too sluggish or too
                # twitchy.
                speed_y = 0.01 * self.camera.distance * 0.10
                # Sign reversed from the obvious "drag-up = rise"
                # convention: pygame y grows downward, so adding
                # `dy * speed` directly means drag-DOWN raises the
                # look-at and drag-UP drops it -- matches the user's
                # mental model of "push the world down to lift my
                # gaze" / "pull the world up to look down".
                self.camera.center[1] += dy * speed_y
            elif btns[0]:   # LEFT-drag
                if self.camera_mode == 2:
                    # Commander mode: drag rotates the HEAD in
                    # chassis-local space, not the orbit camera.
                    # Lets the user look around from the commander
                    # seat (left, right, up, down) while the
                    # chassis-locked anchor preserves "tracks the
                    # car's turn" -- a head rotated 90 deg right
                    # in chassis-local stays right-of-tank as the
                    # tank yaws.  Pitch clamped to keep the head
                    # from flipping over.
                    self._head_yaw_deg   += dx * 0.5
                    self._head_pitch_deg += dy * 0.5
                    self._head_pitch_deg  = float(np.clip(
                        self._head_pitch_deg, -85.0, +85.0))
                elif self.camera_mode == 1:
                    # Chase mode: drag rotates the orbit angle in
                    # CHASSIS-LOCAL space (so chase tracks the
                    # tank's yaw automatically -- same kid-out-
                    # the-window behavior as commander).
                    self._chase_yaw_deg   += dx * 0.5
                    self._chase_pitch_deg += dy * 0.5
                    self._chase_pitch_deg  = float(np.clip(
                        self._chase_pitch_deg, -85.0, +85.0))
                else:
                    # Free cam (orbit / mode 0): drag the world-
                    # frame orbit camera.
                    self.camera.yaw   += dx * 0.5
                    self.camera.pitch += dy * 0.5
                    self.camera.pitch  = np.clip(self.camera.pitch, -89, 89)

            if btns[2]:   # RIGHT-drag -> pan on XZ ground plane
                # Pan speed (formerly a UI slider, baked at 0.20)
                speed = 0.01 * self.camera.distance * 0.20
                view  = self.camera.get_view_matrix()

                # Camera right and forward vectors, flattened to XZ (no Y lift)
                right_xz = np.array([view[0, 0], 0.0, view[0, 2]], dtype=np.float32)
                fwd_xz   = np.array([-view[2, 0], 0.0, -view[2, 2]], dtype=np.float32)

                # Normalise (safe -- zero-length only when looking straight down)
                r_len = np.linalg.norm(right_xz)
                f_len = np.linalg.norm(fwd_xz)
                if r_len > 1e-6: right_xz /= r_len
                if f_len > 1e-6: fwd_xz   /= f_len

                self.camera.center -= right_xz * (dx * speed)   # flipped X: drag right → camera right
                self.camera.center += fwd_xz   * (dy * speed)

        self.mouse_last = list(mouse_pos)

        # If the console's collapsed state OR drag-resize height
        # changed during this tick, re-run the layout pass so the 3D
        # camera viewport reflects the new console size.  Cheap (just
        # re-computes a few rects); only fires when the flag is set.
        if getattr(self.ui, '_console_geometry_dirty', False):
            self.ui._console_geometry_dirty = False
            self._on_resize(self.width, self.height)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self):
        """Draw one frame: scene meshes + scene helpers + UI overlay."""
        # Splash screen short-circuit: while the splash is up (during
        # init), every render() call paints it instead of the regular
        # scene + UI.  Cleared by run() once startup is complete.
        if self.splash is not None:
            self.splash.render()
            pygame.display.flip()
            return

        # Mirror the Debug + Susp checkboxes into the master flags
        # at the TOP of render() so every gate downstream reads
        # the CURRENT-frame state.  Was being mirrored at the
        # bottom of render(), which left every debug-overlay
        # gate reading the previous frame's value -- visible as
        # "Debug checkbox shows no markers / no wheel colors"
        # when the toggle and the gate disagree by one frame
        # (worst case: gate never sees the True transition
        # because some other code path changes the checkbox
        # state mid-frame).
        if self._debug_cb is not None:
            self._debug = bool(self._debug_cb.checked)
        if self._suspension_cb is not None:
            new_susp = bool(self._suspension_cb.checked)
            # Susp ON -> OFF transition: chase / commander cameras
            # read `tank_physics.chassis_matrix()` for their anchor.
            # If we leave the physics state at its previously-settled
            # pose (pos.y = -10 cm, pitch / roll / vy from the last
            # solve) but RESTORE the meshes to bind below, the
            # chassis_matrix and the rendered mesh disagree -- camera
            # anchors to "below origin" while the tank renders at
            # world origin.  Result: pressing Susp visibly yanks the
            # camera even though nothing else changed.
            #
            # Fix: snap the physics pose back to bind on every OFF
            # transition so chassis_matrix() returns identity and
            # the camera sits where the tank actually is.
            if (not new_susp and getattr(self, 'tank_physics_enabled', False)
                    and self.tank_physics is not None):
                self.tank_physics.pos[:]    = 0.0
                self.tank_physics.pitch_deg = 0.0
                self.tank_physics.roll_deg  = 0.0
                self.tank_physics.vy        = 0.0
            self.tank_physics_enabled = new_susp

        # ---- GPU timer query: begin -------------------------------------
        # Lazy-create the two-query pool the first time we render the
        # real scene.  Failure (old driver, missing GL_TIME_ELAPSED)
        # leaves _gl_query_ids None and every later GPU-timing call
        # short-circuits on the same `is None` check.
        if self._gl_query_ids is None:
            try:
                self._gl_query_ids = list(glGenQueries(2))
            except Exception as exc:
                print(f"[viewer] GPU timer queries unavailable: {exc}")
                self._gl_query_ids = []   # truthy distinct from None so
                                          # we don't retry on every frame
        if self._gl_query_ids:
            try:
                qid = self._gl_query_ids[self._gl_query_idx]
                glBeginQuery(GL_TIME_ELAPSED, qid)
            except Exception:
                # Driver hiccup -- disable for the rest of the session.
                self._gl_query_ids = []

        # Full-window clear (so the right-side panel area is also wiped)
        glViewport(0, 0, self.width, self.height)
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)

        # 3D scene draws into the area between the two side panels and
        # ABOVE the bottom console panel.  When the info panel is
        # collapsed, only the spine (~18 px) is reserved on the left.
        # UI pass at the end resets the viewport.
        scene_x = self.ui.info_left_inset(self.INFO_PANEL_W)
        scene_w = max(1, self.width - scene_x - self.TREE_PANEL_W)
        console_h = (self.ui.console.height_for_layout()
                     if self.ui.console else 0)
        scene_h = max(1, self.height - console_h)
        # GL viewport y is bottom-up: the console sits at y=0..console_h
        # at the BOTTOM of the window in screen-space, so the 3D scene
        # starts at GL y = console_h and extends up to the top.
        glViewport(scene_x, console_h, scene_w, scene_h)

        # 3D pass always starts in GL_FILL.  When the Wireframe toggle
        # is on we run TWO passes in sequence:
        #   1) solid pass with GL_POLYGON_OFFSET_FILL enabled at
        #      `(4.0, 4.0)`, pushing every filled triangle AWAY from
        #      the camera in depth-buffer space.  Pixels cover the
        #      same screen area; only their depth values move.
        #   2) line pass at natural Z (no offset), so line draws win
        #      the depth test against the offset-back fills.
        #
        # Offset value tuned empirically -- (1,1) z-fought on dense
        # geometry, (2,2) was still flickering at grazing angles, (4,4)
        # gives clean separation across every angle / distance combo
        # we've tested without producing visible gaps where adjacent
        # tris meet (which is the failure mode if you push too far).
        glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
        # NOTE: GL_POLYGON_OFFSET_FILL is set immediately before the
        # SOLID MESH PASS later in render() (around the
        # `for mesh in self.meshes:` loop) -- not here.  Doing it
        # here meant the offset was active during the intervening
        # passes (terrain, picker FBO, skybox, hull bbox), which
        # could nudge those off where they belonged AND was at risk
        # of being clobbered by intermediate `glDisable` /
        # `glPolygonOffset` calls before the solid pass actually
        # ran.  In-place enable just before the mesh draw is
        # robust against that.

        view = self._anchored_view_matrix()
        proj = self.camera.get_projection_matrix()

        # ---- Tank physics (per-wheel terrain conformance) ------------
        # Runs BEFORE every per-mesh upload below so the picker FBO,
        # the main mesh pass, the wireframe overlay, and the normals
        # debug pass all use the post-physics model_matrix.
        #
        # Recipe: the FIRST frame after each tank load, save each
        # mesh's load-time `model_matrix` to `bind_model_matrix`
        # (the bone-baked rest pose).  Every subsequent frame,
        # rebuild `model_matrix = chassis_pose @ bind_model_matrix`
        # so the chassis pose layers on top of the per-component
        # offsets WITHOUT mutating the rest pose.
        #
        # Disabled paths:
        #   * Terrain off                  -> chassis pose = identity.
        #   * No tank loaded               -> nothing to apply.
        #   * `tank_physics_enabled` False -> identity (debug knob).
        if (self.tank_physics_enabled and self.meshes
                and self.show_terrain and self.terrain
                and self.tank_physics is not None):
            # Save each mesh's load-time matrix once per load.
            for m in self.meshes:
                if not hasattr(m, 'bind_model_matrix'):
                    m.bind_model_matrix = np.array(
                        m.model_matrix, dtype=np.float32, copy=True)
            # Tick the physics with the frame delta tracked in run().
            # Clamp to a reasonable minimum so a paused / single-step
            # frame doesn't make the physics solver jitter.
            dt = max(1e-3, getattr(self, '_frame_dt', 1.0 / 60.0))
            # Time the full tank-math step (physics update + per-mesh
            # model-matrix recompose) so the user can see the
            # physics cost in real time on the grass-green overlay
            # in the upper-left corner.  Uses perf_counter for
            # microsecond resolution; smoothed lightly so the
            # readout is stable.
            import time as _time
            _t_phys0 = _time.perf_counter()
            chassis_pose = self.tank_physics.update(self.terrain, dt)
            # Apply chassis pose to every sub-mesh.
            for m in self.meshes:
                m.model_matrix = (chassis_pose
                                   @ m.bind_model_matrix).astype(np.float32)
            _phys_ms = (_time.perf_counter() - _t_phys0) * 1000.0
            # Exponential smoothing so the overlay value doesn't
            # flicker every frame.  Alpha 0.20 ~= 5-frame trailing
            # average at 60 fps.
            prev_ms = getattr(self, '_physics_ms', 0.0)
            self._physics_ms = 0.20 * _phys_ms + 0.80 * prev_ms

            # Particle hardpoints (engine smoke + fire) live in
            # bind-pose world coords -- they were captured at load
            # time before chassis_pose existed.  As the tank moves
            # / rotates / pitches / rolls, those bind positions go
            # stale and the smoke plumes start emitting from the
            # spot where the tank USED to be.  Snapshot the bind
            # values once on first use, then transform them through
            # chassis_pose every frame and re-publish to the
            # particle systems.
            #
            # Position gets the full 4x4 (rotation + translation);
            # forward direction gets only the 3x3 rotation block --
            # we don't want a hardpoint emit-direction to pick up
            # the chassis's world translation.
            self._update_emitters_for_chassis_pose(chassis_pose)
        else:
            # Restore bind pose if we ever applied physics earlier
            # in this load (e.g. terrain toggled off mid-session).
            for m in self.meshes:
                if hasattr(m, 'bind_model_matrix'):
                    m.model_matrix = m.bind_model_matrix.copy()
            # Same restore for emitters: when physics was on then
            # toggled off, the emitter pos/fwd carry the last
            # transformed values.  Snap them back to bind so the
            # smoke plume re-anchors at the bind-pose hardpoint
            # locations.
            self._update_emitters_for_chassis_pose(np.eye(4, dtype=np.float32))

        # ---- Triangle picker (off-screen colour-pick) ----------------
        # When the Pick Tri tool is on, render the tank into a hidden
        # FBO with the picking shader and read back the pixel under
        # the cursor.  Runs BEFORE the main scene pass: the picking
        # render binds its own FBO + viewport, so we have to re-bind
        # the back buffer + scene viewport afterward to keep the
        # rest of the pipeline pointing at the visible target.
        if (self.picker.enabled and self.meshes
                and not self.ui.is_pointer_over_ui(*pygame.mouse.get_pos())):
            self.picker.update_pass(
                meshes        = self.meshes,
                view          = view,
                proj          = proj,
                mouse_xy      = pygame.mouse.get_pos(),
                window_h      = self.height,
                viewport      = (scene_x, console_h, scene_w, scene_h),
                on_hit_change = self._on_picker_hit_change,
            )
            # Restore the back-buffer + scene viewport for the main
            # render pass below.  picker.update_pass leaves
            # GL_FRAMEBUFFER == 0 already, but we re-bind explicitly
            # in case a future driver bug breaks that contract.
            glBindFramebuffer(GL_FRAMEBUFFER, 0)
            glViewport(scene_x, console_h, scene_w, scene_h)

        # Skybox -- rendered first; depth trick (xyww) places it at far plane
        if self.skybox and self.show_skybox:
            glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
            self.skybox.render(view, proj)
            # Stay in GL_FILL for the main mesh pass; the wireframe
            # overlay (if any) fires after the solid render.
            glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)

        # Procedural terrain.  Drawn AFTER the skybox (so the
        # ground occludes the horizon line) and BEFORE the tank
        # meshes (so the tank renders on top).  Depth-tested
        # against the main scene so the tank tracks meet the
        # ground instead of clipping through.  Toggle is the
        # `Terrain` button in the UI section -- off by default.
        if self.show_terrain and self.terrain and self.terrain_shader:
            glEnable(GL_DEPTH_TEST)
            glDepthMask(GL_TRUE)
            light_dir_world = np.array(
                [0.5, 1.0, 0.3], dtype=np.float32)
            self.terrain.render(self.terrain_shader, view, proj,
                                  light_dir_world)

        # Background helpers (rendered without depth test so they never clip mesh)
        glLineWidth(1.0)
        glDisable(GL_DEPTH_TEST)
        if self.show_grid:
            self.grid.render(self.color_shader, view, proj)
        if self.show_axes:
            self.axes.render(self.color_shader, view, proj)

        # Look-at crosshair was drawn here pre-1.97.2 -- BEFORE
        # the tank mesh pass below -- which meant the tank
        # rendered ON TOP of the crosshair, so the depth-test-
        # disabled crosshair was still hidden behind the hull.
        # Moved to AFTER the mesh passes (just before the UI
        # render) so it draws on top of the rendered tank
        # without being overwritten.

        # Tank physics overlay (markers): drawn DOWN-STREAM after the
        # tank mesh + wireframe + normals + picker passes, so the
        # markers respect the z buffer and occlude correctly behind
        # the chassis / hull (instead of always-on-top).  The block
        # below builds nothing and just runs the per-bone console
        # dump -- the actual segment-build + draw lives later in
        # render() near the look-at crosshair.
        if (self._debug
                and self.tank_physics_enabled and self.tank_physics is not None
                and self.meshes
                and self.show_terrain and self.terrain
                and len(self.tank_physics.last_wheel_world)):
            tp     = self.tank_physics
            # ---- Live console dump of per-bone angles ----------------
            # Clear + reprint every frame so the user gets a real-time
            # readout of what each wheel's suspension is doing.  Slows
            # the frame loop noticeably (one cls + N print() calls per
            # tick) but Professor Coffee explicitly accepted that
            # cost ("i dont care if it slows it down").
            self._dump_bone_angles_to_console(tp)

        # ---- Hull bounding-box overlay -------------------------------
        # Draws a wireframe AABB around every component=='hull' mesh.
        # Built once at tank-load time as 12 line edges in HULL-LOCAL
        # space, transformed by chassis_pose each frame so the box
        # rides with the chassis.  Useful for sizing / collision-
        # debug; doesn't constrain the physics.
        if (self._debug
                and self.meshes and self.tank_physics_enabled
                and self.tank_physics is not None):
            if not hasattr(self, '_hull_box_local') or self._hull_box_local is None:
                self._build_hull_box_local()
            if self._hull_box_local is not None:
                self._render_hull_box(view, proj)

        glEnable(GL_DEPTH_TEST)

        if not self.meshes:
            # Reset viewport to full window for the UI pass
            glViewport(0, 0, self.width, self.height)
            self.ui.render(self.width, self.height)
            pygame.display.flip()
            return

        # Three lights at 120° spacing on a horizontal ring.  Stationary
        # at offsets 0/120/240° by default; "Orbit" button rotates the
        # ring as a whole at 0.5°/frame.
        base_angle = np.radians(self.frame_count * 0.5) if self.orbit_lights else 0.0
        step       = 2.0 * np.pi / float(self.NUM_LIGHTS)
        light_positions = []
        for i in range(self.NUM_LIGHTS):
            a = base_angle + i * step
            light_positions.append((
                self.LIGHT_RADIUS * np.cos(a),
                self.LIGHT_HEIGHT,
                self.LIGHT_RADIUS * np.sin(a),
            ))

        # Pick the shader based on the scene's source.  WoT primitives go
        # through the full PBR pipeline; imported FBX/GLB/OBJ uses the
        # simple diffuse + bump shader.  Both shaders share the same
        # vertex stage and a common uniform set (view / projection /
        # light_pos / view_pos / metal_scale / shine_scale /
        # use_normal_map / is_GA_normal / wireframe_mode), so the
        # existing per-mesh code mostly works for both.  Uniform-misses
        # on PBR-only names against the imported shader are silent
        # no-ops (glUniform at location -1).
        # Shaded toggle (Model-group button) flips the WoT mesh
        # path onto the same simple Phong shader the FBX import
        # path uses.  Useful for diagnostic A/B compare and as a
        # cheap fallback when the PBR pipeline misbehaves on a
        # specific tank's materials.  FBX scenes always use the
        # imported shader regardless of toggle state since they
        # have no GMM / AO / damage layer to feed the PBR path.
        use_simple_shader = (self.source_type == 'fbx'
                             or bool(getattr(self, 'shaded_mode', False)))
        self._active_shader = (self.imported_shader
                               if use_simple_shader
                               else self.shader)
        active = self._active_shader

        # Shared mesh shader state
        active.use()
        active.set_mat4('view',       view)
        active.set_mat4('projection', proj)
        active.set_vec3_array('light_pos', light_positions)

        # ---- Per-mesh skinning upload helper --------------------------
        # Closures over `active` so we don't repeat the bind dance at
        # every draw site (main pass + wireframe pass).  Behaviour:
        #
        #   * If the mesh is skinned AND tank physics is active AND
        #     the mesh carries a bone palette, build a per-bone matrix
        #     array via `TankPhysics.bone_matrix_array(palette)`,
        #     upload as `u_bones`, and set `u_skinned = 1`.  Each
        #     wheel's bone gets a translation in mesh-local Y by the
        #     residual between the rigid plane fit and the actual
        #     terrain target -- so per-wheel deflections show through
        #     the geometry.
        #
        #   * Otherwise set `u_skinned = 0`.  The shader's identity
        #     branch then runs and the mesh draws at its bind pose
        #     (or rigid `chassis_matrix` pose, whichever is composed
        #     into mesh.model_matrix).
        #
        # This keeps non-skinned meshes (hull / turret / gun, every
        # imported FBX) untouched -- they never carried bone data
        # to begin with, and the disabled `iii / ww` attribs are
        # never read by the shader's identity branch.
        # Pre-resolve the per-wheel state (CONTACT / HANGING /
        # OVER_COMP / NONE) once per frame.  Each chassis sub-mesh
        # then maps wheel_bone_names[i] -> its OWN palette slot to
        # build the 64-slot state array uploaded as u_wheel_state.
        per_wheel_state = None
        wheel_names_global = []
        # Wheel-color highlight is now gated by the master Debug
        # checkbox in addition to the H-toggle / tank-physics
        # active flags.  When Debug is OFF, the coloured shader
        # path goes dormant -- wheels render with their normal
        # PBR look regardless of contact state.
        if (self.tank_physics is not None
                and self.tank_physics_enabled
                and self._highlight_contacts
                and self._debug):
            per_wheel_state    = getattr(
                self.tank_physics, 'last_wheel_state', None)
            wheel_names_global = (
                getattr(self.tank_physics, 'wheel_bone_names', None) or [])

        def _upload_skinning(mesh):
            palette = getattr(mesh, 'bone_palette', None)
            has_skin_data = (mesh.bone_indices is not None
                             and mesh.bone_weights is not None
                             and palette)
            if (has_skin_data
                    and self.tank_physics_enabled
                    and self.tank_physics is not None):
                bones = self.tank_physics.bone_matrix_array(palette)
                active.set_mat4_array('u_bones', bones)
                active.set_int('u_skinned', 1)
            else:
                active.set_int('u_skinned', 0)

            # Per-bone state array (size MAX_BONES = 64, must match
            # mesh.vert).  Default 0 (no highlight); each wheel name
            # known to the physics maps to ITS palette slot in this
            # mesh's `bone_palette` and gets the wheel's CONTACT /
            # HANGING / OVER_COMP state code from
            # `tank_physics.last_wheel_state`.  Sub-meshes that
            # don't carry the wheel bones (turret / hull / gun, also
            # the track-ribbon meshes whose renderSet only declares
            # Track_* bones) end up with all-zeros and the shader
            # paints nothing.
            MAX_BONES = 64
            wheel_state_arr = [0] * MAX_BONES
            mode = 0
            if (palette and per_wheel_state is not None
                    and len(wheel_names_global) > 0
                    and has_skin_data):
                for i, nm in enumerate(wheel_names_global):
                    if not nm or nm not in palette:
                        continue
                    pi = palette.index(nm)
                    if pi < MAX_BONES:
                        wheel_state_arr[pi] = int(per_wheel_state[i])
                        mode = 1 if (self._highlight_contacts and self._debug) else 0
            active.set_int_array('u_wheel_state', wheel_state_arr)
            active.set_int('u_contact_mode', mode)

        # Camera world-space eye position (derived from view matrix inverse)
        # The view matrix is R|t; eye = -R^T * t = transpose(R) * (-t)
        R  = view[:3, :3]
        t  = view[:3,  3]
        eye = -R.T @ t
        active.set_vec3('view_pos', float(eye[0]), float(eye[1]), float(eye[2]))

        active.set_int('use_normal_map', 1 if self.use_normal_map else 0)
        # WoT data uses GA-encoded normals; imported FBX has standard RGB
        active.set_int('is_GA_normal',
                       0 if self.source_type == 'fbx' else 1)
        # Main mesh pass is ALWAYS solid -- the wireframe (if enabled)
        # is a separate overlay pass after this one, with polygon-offset
        # so the lines don't z-fight against the surface they ride on.
        active.set_int('wireframe_mode', 0)

        # Flat-colour Shaded mode: when the toggle is on, the
        # imported shader skips the diffuse-texture fetch and uses
        # the active theme's c1 (burnt orange on TEPY Default,
        # whatever the user picked otherwise) at 75% brightness.
        # Tying it to the theme means a Dracula user gets
        # Dracula-orange Shaded; the GLSL contract is just "RGB
        # triple, linear-ish".  The 0.75 darkening keeps the lit
        # side from blowing out past white once the Lambert / Phong
        # / ambient terms add up at the per-frame Light slider
        # default (~10x scale inside imported.frag).  Uniform
        # missing on the PBR shader -> silent no-op so the call is
        # safe to make every frame regardless of which shader is
        # active.
        if bool(getattr(self, 'shaded_mode', False)):
            r, g, b, _a = _theme.c1()
            SHADED_DIM = 0.75   # 25 % darker than raw theme c1
            active.set_int('use_flat_color', 1)
            active.set_vec3('flat_color',
                            float(r) * SHADED_DIM,
                            float(g) * SHADED_DIM,
                            float(b) * SHADED_DIM)
        else:
            active.set_int('use_flat_color', 0)

        # Crash-tile channel rotation.  Per-load offset added to the
        # shader's world-space-hash channel pick, so reloading the
        # same damaged tank cycles which of the three R/G/B grunge
        # variants land where.  Set once per frame here; the shader
        # ignores it on materials without `has_crash_tile=1`.
        active.set_int('crash_channel_offset',
                        int(self._crash_channel_offset) % 3)

        active.set_float('metal_scale',
            self._metal_slider.value if self._metal_slider else 1.0)
        active.set_float('shine_scale',
            self._shine_slider.value if self._shine_slider else 1.0)
        active.set_int('invert_metal',
            1 if (self._invert_metal_cb and self._invert_metal_cb.checked) else 0)
        active.set_int('invert_shine',
            1 if (self._invert_shine_cb and self._invert_shine_cb.checked) else 0)

        # IBL maps -- always bound when skybox exists (IBL works even if sky background
        # is hidden).  Units: 4=irradiance, 5=brdf_lut, 6=prefiltered specular.
        # Only the PBR shader actually consumes them; the imported shader's
        # set_int/set_vec3 calls land on missing uniform locations (silent).
        if self.skybox:
            if self.skybox.irradiance_id:
                glActiveTexture(GL_TEXTURE4)
                glBindTexture(GL_TEXTURE_CUBE_MAP, self.skybox.irradiance_id)
                active.set_int('irradiance_map', 4)
                active.set_int('has_irradiance', 1)
            else:
                active.set_int('has_irradiance', 0)

            if self.skybox.brdf_lut_id:
                glActiveTexture(GL_TEXTURE5)
                glBindTexture(GL_TEXTURE_2D, self.skybox.brdf_lut_id)
                active.set_int('brdf_lut',     5)
                active.set_int('has_brdf_lut', 1)
            else:
                active.set_int('has_brdf_lut', 0)

            if self.skybox.cubemap_id:
                glActiveTexture(GL_TEXTURE6)
                glBindTexture(GL_TEXTURE_CUBE_MAP, self.skybox.cubemap_id)
                active.set_int('prefiltered_map', 6)
                active.set_int('has_prefiltered',  1)
            else:
                active.set_int('has_prefiltered', 0)
        else:
            active.set_int('has_irradiance',  0)
            active.set_int('has_brdf_lut',    0)
            active.set_int('has_prefiltered', 0)

        # Nation armor color -- shared across all meshes in this vehicle.
        # Imported FBX scenes have no nation context (source_type=='fbx');
        # we still push a neutral-tint uniform here to keep the loop
        # uniform across both shaders (imported shader doesn't have
        # `armor_color` so the call is a silent no-op).
        if self._armor_color and self.source_type != 'fbx':
            r, g, b = self._armor_color
            active.set_vec3('armor_color',    r, g, b)
            active.set_int ('has_armor_color', 1)
        else:
            active.set_vec3('armor_color',    1.0, 1.0, 1.0)
            active.set_int ('has_armor_color', 0)

        # Commander camera mode hides the turret + gun so the user
        # can see out from inside the turret -- otherwise the camera
        # is buried in the geometry and you just see the back of
        # the breech.  Mode 0 / 1 leave everything visible.
        hide_turret_gun = (self.camera_mode == 2)

        # Re-assert polygon-offset state IMMEDIATELY before the solid
        # mesh pass.  An earlier enable site at the top of render()
        # was being clobbered by some intervening pass (terrain,
        # picker FBO, hull bbox draws each manage their own polygon
        # state and at least one was leaving GL_POLYGON_OFFSET_FILL
        # disabled by the time the solid mesh pass actually fired --
        # result: the wireframe-overlay z-fighting that came back
        # mid-2026-05-08).  Re-asserting the offset right here
        # guarantees the solid mesh pass below renders with depth
        # pushed back, so the line pass at natural Z reliably wins
        # the depth test.  Value bumped (4 -> 8) for additional
        # margin at far-from-camera distances and grazing angles.
        if self.wireframe:
            glEnable(GL_POLYGON_OFFSET_FILL)
            glPolygonOffset(8.0, 8.0)
        else:
            glDisable(GL_POLYGON_OFFSET_FILL)

        # Draw each primitive group
        for mesh in self.meshes:
            if not getattr(mesh, 'visible', True):
                continue   # user toggled this sub-mesh off in the info panel
            if hide_turret_gun and getattr(mesh, 'component', '') in ('turret', 'gun'):
                continue   # commander POV: hide turret + gun
            # Rubber-band track ribbon (track_LShape* / track_RShape*
            # inside Chassis.primitives_processed) -- temporarily
            # blanked out while the kinematic-bone-driven NURB track
            # replacement (see ARCHITECTURE.md "Track physics roadmap")
            # is built.  Flip RENDER_RUBBER_BAND_TRACK to True to
            # bring the rubber band back for an A/B.
            RENDER_RUBBER_BAND_TRACK = False  # if (false) blank
            if not RENDER_RUBBER_BAND_TRACK:
                _mn = getattr(mesh, 'name', '') or ''
                if _mn.startswith('track_') and 'Shape' in _mn:
                    continue
            # Alpha test
            if mesh.alpha_test_enable:
                active.set_int(  'alpha_test_enable', 1)
                active.set_float('alpha_ref',  mesh.alpha_reference / 255.0)
            else:
                active.set_int(  'alpha_test_enable', 0)
                active.set_float('alpha_ref',  0.0)

            # Alpha and AO channel routing -- only meaningful for the PBR
            # shader.  Imported shader has no such uniforms (silent miss).
            active.set_int('alpha_in_normal_red',
                                1 if mesh.alpha_in_normal_red else 0)
            active.set_int('ao_in_diffuse_alpha',
                                1 if getattr(mesh, 'ao_in_diffuse_alpha',
                                             mesh.alpha_in_normal_red) else 0)
            active.set_int('has_ao_map', 1 if mesh.ao_tex_id else 0)

            # Face culling
            if mesh.double_sided:
                glDisable(GL_CULL_FACE)
            else:
                glEnable(GL_CULL_FACE)
                glCullFace(GL_BACK)

            active.set_mat4('model', mesh.model_matrix)
            _upload_skinning(mesh)
            mesh.render(active)

        glEnable(GL_CULL_FACE)

        # ---- Wireframe overlay (rides on top of the solid pass) -------
        # When the Wireframe toggle is on, do a SECOND pass over every
        # visible mesh in GL_LINE mode using the same shader / VAO
        # setup.  No polygon-offset on this pass -- the SOLID pass
        # was already pushed back via GL_POLYGON_OFFSET_FILL above,
        # so lines drawn at normal depth automatically win the depth
        # test against the offset-back triangles.
        #
        # The shader's `wireframe_mode` uniform is flipped to 1 here
        # so the fragment stage outputs a flat dark colour rather
        # than re-running PBR for the line draws.
        if self.wireframe:
            # Line pass at natural Z.  We disable POLYGON_OFFSET_FILL
            # (which only acted on the solid pass anyway) so the
            # lines render unbiased; the +4 fill push above gives
            # them ~4 depth-buffer-units of clearance to win the
            # z-test against the surface they ride on.
            glDisable(GL_POLYGON_OFFSET_FILL)
            glPolygonOffset(0.0, 0.0)
            glPolygonMode(GL_FRONT_AND_BACK, GL_LINE)
            glLineWidth(1.0)
            # Force the wireframe colour path in the fragment shader.
            active.set_int('wireframe_mode', 1)
            # Disable face culling for the overlay so back-edges are
            # visible too -- a wireframe overlay that hides anything
            # the user can't see "through" the front faces feels broken.
            glDisable(GL_CULL_FACE)
            for mesh in self.meshes:
                if not getattr(mesh, 'visible', True):
                    continue
                if hide_turret_gun and getattr(mesh, 'component', '') in ('turret', 'gun'):
                    continue
                # Skip rubber-band track ribbon (see solid-pass note).
                _mn = getattr(mesh, 'name', '') or ''
                if _mn.startswith('track_') and 'Shape' in _mn:
                    continue
                active.set_mat4('model', mesh.model_matrix)
                _upload_skinning(mesh)
                mesh.render(active)
            # Restore everything we touched.
            glEnable(GL_CULL_FACE)
            glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
            active.set_int('wireframe_mode', 0)

        # Surface-normal debug lines.  Skipped entirely when the
        # slider is at zero -- otherwise we re-bind each mesh's VAO
        # and draw GL_TRIANGLES so the geometry shader can see each
        # face's three vertex normals at once.  Wireframe mode also
        # forces GL_FILL here so the lines render solid even when
        # the main mesh pass is in line mode.
        #
        # Two modes available, driven by the PerVtx checkbox:
        #   * by-face   (default)  -- 1 cyan line per triangle from
        #                             centroid along the AVERAGED
        #                             3-vertex normal
        #   * by-vertex (PerVtx on) -- 3 axes-coloured lines per
        #                             triangle, one per vertex
        normals_len = (self._normals_slider.value
                       if self._normals_slider else 0.0)
        # Mirror the PerVtx checkbox state for the render pass.  Save
        # to instance var so the config-persist path picks it up too.
        if self._normals_mode_cb is not None:
            self._normals_per_vertex = bool(self._normals_mode_cb.checked)
        if normals_len > 0.0 and self.normals_shader is not None:
            glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
            glLineWidth(1.5)
            self.normals_shader.use()
            self.normals_shader.set_mat4('view',       view)
            self.normals_shader.set_mat4('projection', proj)
            self.normals_shader.set_float('u_normal_length', normals_len)
            self.normals_shader.set_int(
                'u_mode', 1 if self._normals_per_vertex else 0)
            for mesh in self.meshes:
                if not getattr(mesh, 'visible', True):
                    continue
                if hide_turret_gun and getattr(mesh, 'component', '') in ('turret', 'gun'):
                    continue
                # Skip rubber-band track ribbon (see solid-pass note).
                _mn = getattr(mesh, 'name', '') or ''
                if _mn.startswith('track_') and 'Shape' in _mn:
                    continue
                if mesh.vao is None:
                    continue
                self.normals_shader.set_mat4('model', mesh.model_matrix)
                glBindVertexArray(mesh.vao)
                # Triangles via the EBO -- the GS gets all 3 vertex
                # positions + normals per primitive and decides
                # itself how many lines to emit (1 for face-mode,
                # 3 for vertex-mode).
                glDrawElements(GL_TRIANGLES, mesh.index_count,
                               GL_UNSIGNED_INT, ctypes.c_void_p(0))
            glBindVertexArray(0)
            # The wireframe overlay (above) already restored GL_FILL,
            # so no global polygon-mode reset is needed here.

        # Light position indicators (one sphere per light)
        if self.show_light:
            for (lx, ly, lz) in light_positions:
                light_model = np.eye(4, dtype=np.float32)
                light_model[0, 3] = lx
                light_model[1, 3] = ly
                light_model[2, 3] = lz
                self.light_sphere.render(self.color_shader, light_model, view, proj)

        # Mirror the Debug checkbox into the master flag so every
        # on-screen debug overlay below respects it without an
        # event callback.  Convention: any new debug-only render
        # should gate on `self._debug`.
        if self._debug_cb is not None:
            self._debug = bool(self._debug_cb.checked)

        # Mirror the Susp checkbox into the tank-physics enable
        # flag.  Cheap to flip on / off mid-session -- physics
        # branch in the early `render()` block reads
        # `tank_physics_enabled` each frame, and its False path
        # restores `mesh.model_matrix = bind_model_matrix.copy()`,
        # so the tank pops back to the world origin instantly.
        if self._suspension_cb is not None:
            new_susp = bool(self._suspension_cb.checked)
            # On the off->on transition, log the drive controls
            # so they're discoverable instead of hidden in the
            # CHANGELOG.  Once per toggle, cleared when Susp goes
            # back off so re-enabling reprints.
            if new_susp and not self._suspension_test:
                self.log("Susp ON -- drive controls:", color=(180, 220, 255))
                self.log("  W / Z      = forward / backward",  color=(160, 200, 230))
                self.log("  A / D      = turn left / right",   color=(160, 200, 230))
                self.log("  S          = cruise toggle",       color=(160, 200, 230))
                self.log("  O          = auto-circle",         color=(160, 200, 230))
                self.log("  0..9       = speed step",          color=(160, 200, 230))
            self._suspension_test     = new_susp
            self.tank_physics_enabled = new_susp

        # Hardpoint markers -- orange sphere at each discovered exhaust
        # node + 0.5-unit cyan direction vector.  Debug-only.
        if self._debug:
            for hp in self._exhaust_points:
                hp_model = np.eye(4, dtype=np.float32)
                hp_model[0, 3] = float(hp['pos'][0])
                hp_model[1, 3] = float(hp['pos'][1])
                hp_model[2, 3] = float(hp['pos'][2])
                self.hp_sphere.render(self.color_shader, hp_model, view, proj)
            self.hp_lines.render(self.color_shader, view, proj)

        # ---- Mirror live slider values back into the active engine
        # class.  Single-source-of-truth sub: routing logic lives
        # inside `_persist_all_sliders`, which uses
        # `self._active_engine_class` (the engine class) as the routing
        # ref.  No JSON write here -- mouse-up handles that.
        self._persist_all_sliders(write_json=False)

        # ---- Smoke particles ----------------------------------------------
        # Update + render the billboard flipbook system.  Update advances
        # the simulation by this frame's dt (computed in run() before
        # render()).  Render uploads the alive particles, builds
        # camera-facing quads in the vertex shader, samples the flipbook
        # for each particle's age, alpha-blends into the scene.
        if self.smoke_particles is not None:
            # Push current slider values onto the system so changes are
            # live -- the actual values are read every update / render.
            if self._smoke_start_slider:
                self.smoke_particles.start_size       = self._smoke_start_slider.value
            if self._smoke_end_slider:
                self.smoke_particles.end_size         = self._smoke_end_slider.value
            if self._smoke_speed_slider:
                self.smoke_particles.speed            = self._smoke_speed_slider.value
            if self._smoke_fade_slider:
                self.smoke_particles.fade_start_frame = self._smoke_fade_slider.value
            if self._smoke_fade_end_slider:
                self.smoke_particles.fade_end_frame   = self._smoke_fade_end_slider.value

            self.smoke_particles.update(self._frame_dt)
            self.smoke_particles.render(self.particle_shader, view, proj)

        # ---- Fire-point smoke (damaged tanks only) -----------------------
        # Independent ParticleSystem with a 400-particle cap (smoke
        # for the engine exhaust runs 1024).  Inherits every slider
        # value from the engine smoke -- the only thing that differs
        # is the per-system pool size.  Empty emitters = idle.
        if self.fire_smoke_particles is not None:
            if self._smoke_start_slider:
                self.fire_smoke_particles.start_size = (
                    self._smoke_start_slider.value)
            if self._smoke_end_slider:
                self.fire_smoke_particles.end_size = (
                    self._smoke_end_slider.value)
            if self._smoke_speed_slider:
                self.fire_smoke_particles.speed = (
                    self._smoke_speed_slider.value)
            if self._smoke_fade_slider:
                self.fire_smoke_particles.fade_start_frame = (
                    self._smoke_fade_slider.value)
            if self._smoke_fade_end_slider:
                self.fire_smoke_particles.fade_end_frame = (
                    self._smoke_fade_end_slider.value)

            self.fire_smoke_particles.update(self._frame_dt)
            self.fire_smoke_particles.render(
                self.particle_shader, view, proj)

        # ---- Fire particles (damaged tanks only) -------------------------
        # Same flipbook-billboard machinery as smoke, separate
        # ParticleSystem instance so size / speed tunables don't fight
        # the smoke ones.  No emitters means update/render are essentially
        # free -- so this block is safe to run unconditionally even when
        # the loaded tank is undamaged.
        if self.fire_billboards is not None:
            if self._fire_size_slider:
                self.fire_billboards.size = self._fire_size_slider.value
            # Fire FPS is fixed -- the user-facing slider was removed
            # (we never wanted it controllable per session).  The
            # initial 30 fps set in __init__ stays put.

            self.fire_billboards.update(self._frame_dt)
            self.fire_billboards.render(self.particle_shader, view, proj)

        # ---- Fire-card outlines (debug) ----------------------------------
        # Gated on the master Debug flag (set above).  Same convention
        # as the HP markers -- if you're adding a new on-screen debug
        # overlay, check `self._debug` here.
        if (self._debug
                and self.fire_billboards is not None
                and self.fire_billboards.emitters):
            # Mirror the bottom-anchored offsets the textured pass uses
            # (see particles.py / _CORNER_OFFSETS_BOTTOM): bottom edge
            # straddles the HP_Fire point horizontally AND vertically,
            # so the box centroid sits exactly on the hardpoint
            # (matching the centered fire billboard in particles.py).
            # view is row-major so row 0 = camera right, row 1 =
            # camera up.  Numbers come from view directly so the
            # rectangle stays camera-facing exactly like the textured
            # quad.
            cam_right = view[0, :3]
            cam_up    = view[1, :3]
            size      = float(self.fire_billboards.size)
            half_w    = 0.5 * size
            half_h    = 0.5 * size
            yellow    = (1.0, 1.0, 0.0)
            segs = []
            for em in self.fire_billboards.emitters:
                pos = np.asarray(em['pos'], dtype=np.float32)
                bl = pos - cam_right * half_w - cam_up * half_h
                br = pos + cam_right * half_w - cam_up * half_h
                tr = pos + cam_right * half_w + cam_up * half_h
                tl = pos - cam_right * half_w + cam_up * half_h
                segs.append((tuple(bl), tuple(br), yellow))
                segs.append((tuple(br), tuple(tr), yellow))
                segs.append((tuple(tr), tuple(tl), yellow))
                segs.append((tuple(tl), tuple(bl), yellow))
            # LineBatch uploads + draws GL_LINES; cheap for the handful
            # of HP_Fire points a tank carries (1-4 typical).
            self.fire_outlines.update(segs)
            self.fire_outlines.render(self.color_shader, view, proj)

        # ---- Triangle-picker overlay ---------------------------------
        # Draw the highlighted triangle + edge lines + vertex point
        # markers on top of the just-rendered tank.  Lives here
        # (before the UI 2-D pass switches viewport / disables depth
        # test) so it composites correctly with the scene depth
        # buffer.  No-op when the picker is disabled or has no hit.
        if self.picker.enabled:
            self.picker.draw_overlay(view, proj,
                                      _theme.c1(), _theme.c2())

        # ---- Tank physics markers (depth-tested, post-tank) ----------
        # Per-wheel ground-contact pink stars + cyan target stars +
        # yellow suspension shaft + blue physics-hit X.  Drawn HERE
        # (after the tank mesh / wireframe / normals / picker passes)
        # with depth test ENABLED so they correctly occlude behind
        # the chassis / hull / terrain.  Pre-2026-05-08 these were
        # drawn earlier in the pass with depth test disabled, which
        # made them ALWAYS visible on top of geometry -- couldn't
        # tell whether a marker was inside or outside the tank.
        # Gated by Debug + Susp + tank loaded + Terrain on, same as
        # before.
        if (self._debug
                and self.tank_physics_enabled and self.tank_physics is not None
                and self.meshes
                and self.show_terrain and self.terrain
                and len(self.tank_physics.last_wheel_world)):
            tp     = self.tank_physics
            PINK   = (1.0, 0.40, 0.65)   # ground-contact dots
            CYAN   = (0.40, 0.95, 0.95)  # target wheel-centre dots
            YELLOW = (1.0, 0.92, 0.20)
            BLUE   = (0.20, 0.45, 1.00)  # physics hit-location X
            # 20 cm asterisk: visible at any zoom level.  Drawn as a
            # 3-axis cross + diagonal so the marker reads as a "star"
            # against the terrain texture even when the camera is
            # far back.
            DOT_R  = 0.20
            DIAG   = DOT_R * 0.71   # 1/sqrt(2) -- equal-length diagonals
            segs   = []
            for i, (wx, _wy, wz) in enumerate(tp.last_wheel_world):
                ty = float(tp.last_terrain_y[i])
                wc = float(tp.last_target_y[i])

                # ---- Ground contact: pink star (XZ + diagonals) ----
                # Render in the XZ plane at terrain Y so it reads as
                # "footprint on the ground".
                segs.append(((wx - DOT_R, ty, wz),
                             (wx + DOT_R, ty, wz), PINK))
                segs.append(((wx, ty, wz - DOT_R),
                             (wx, ty, wz + DOT_R), PINK))
                segs.append(((wx - DIAG, ty, wz - DIAG),
                             (wx + DIAG, ty, wz + DIAG), PINK))
                segs.append(((wx - DIAG, ty, wz + DIAG),
                             (wx + DIAG, ty, wz - DIAG), PINK))

                # ---- Wheel-centre target: cyan star in XY plane ---
                # Use vertical (Y) axis so the marker is always
                # visible regardless of camera angle.
                segs.append(((wx - DOT_R, wc, wz),
                             (wx + DOT_R, wc, wz), CYAN))
                segs.append(((wx, wc - DOT_R, wz),
                             (wx, wc + DOT_R, wz), CYAN))
                segs.append(((wx - DIAG, wc - DIAG, wz),
                             (wx + DIAG, wc + DIAG, wz), CYAN))
                segs.append(((wx - DIAG, wc + DIAG, wz),
                             (wx + DIAG, wc - DIAG, wz), CYAN))

                # ---- Suspension shaft: yellow line ----------------
                segs.append(((wx, ty, wz),
                             (wx, wc, wz), YELLOW))

                # ---- Physics hit-location: blue X -----------------
                # Two diagonal segments at the wheel's terrain
                # sample point.  Lifted 1 mm above the terrain Y
                # so it doesn't z-fight with the ground texture
                # (the pink star sits at exact terrain Y; the X
                # rides 1 mm proud and reads as a separate
                # marker).  Used to visually verify the physics's
                # computed hit location matches what's under the
                # rendered wheel.
                ty_x = ty + 0.001
                segs.append(((wx - DOT_R, ty_x, wz - DOT_R),
                             (wx + DOT_R, ty_x, wz + DOT_R), BLUE))
                segs.append(((wx - DOT_R, ty_x, wz + DOT_R),
                             (wx + DOT_R, ty_x, wz - DOT_R), BLUE))
            self.physics_lines.update(segs)
            # Explicit depth test ON: prior passes (terrain, mesh)
            # left it on, but if any future pass drops it, this
            # local enable keeps the markers from going always-on-top.
            glEnable(GL_DEPTH_TEST)
            self.physics_lines.render(self.color_shader, view, proj)

        # Look-at crosshair: pale-pink lines spanning 10 units along
        # each world axis (X / Y / Z), centred on `camera.center`.
        # Drawn HERE -- after every tank-mesh / wireframe / picker
        # pass -- so the tank doesn't overdraw it.  Depth test
        # explicitly ENABLED below (re-enabled per Professor
        # Coffee's request 2026-05-08): the crosshair occludes
        # correctly behind tank / terrain, so when the look-at
        # point is *inside* a piece of geometry the user sees the
        # lines emerge only at the surface -- which is the natural
        # cue for "your aim point is here".  Visibility gated by
        # `_show_lookat_lines` (RIGHT-button = pan, or MIDDLE-button
        # = Y-lift / crosshair-only).
        if self._show_lookat_lines:
            cx, cy_, cz = (float(self.camera.center[0]),
                           float(self.camera.center[1]),
                           float(self.camera.center[2]))
            H = 5.0   # half-length per axis -> 10 units total
            PINK = (1.0, 0.75, 0.85)
            self.lookat_lines.update([
                ((cx - H, cy_, cz), (cx + H, cy_, cz), PINK),  # X
                ((cx, cy_ - H, cz), (cx, cy_ + H, cz), PINK),  # Y (up in GL)
                ((cx, cy_, cz - H), (cx, cy_, cz + H), PINK),  # Z
            ])
            # Explicit glEnable so the crosshair never accidentally
            # renders in always-on-top mode if some earlier pass
            # left depth test off.  (Most passes restore it, but
            # being explicit here means a future regression in any
            # of those passes can't cause this draw to "float".)
            glEnable(GL_DEPTH_TEST)
            self.lookat_lines.render(self.color_shader, view, proj)

        # 2-D overlay (reset viewport to full window so the tree + dialog
        # can draw outside the 3D scene area)
        glViewport(0, 0, self.width, self.height)
        self.ui.render(self.width, self.height)

        # Physics-timer readout in the upper-left, grass-green text.
        # Drawn AFTER the UI so it sits on top of the scene + any
        # tree / panel that would otherwise overlap.  Smoothed
        # value from `_physics_ms` (5-frame trailing average).
        if self.tank_physics_enabled and self.tank_physics is not None:
            self._render_physics_timer_overlay(self.width, self.height)

        # ---- GPU timer query: end + read previous frame ------------------
        # Close THIS frame's timer query first (everything queued
        # between glBeginQuery and here counts).  Then attempt a
        # non-blocking read of the OTHER query -- by now its frame's
        # GPU work is done, so GL_QUERY_RESULT_AVAILABLE returns true
        # and the result fetch is essentially free.  If the result
        # isn't ready yet (unlikely with vsync but possible on a
        # really fast GPU + low-latency driver), we just skip this
        # frame's accumulation -- one missed sample in a 5-frame
        # block isn't worth the stall a blocking GL_QUERY_RESULT
        # would cost.
        if self._gl_query_ids:
            try:
                glEndQuery(GL_TIME_ELAPSED)
                self._gl_query_in_flight[self._gl_query_idx] = True

                prev_idx = 1 - self._gl_query_idx
                if self._gl_query_in_flight[prev_idx]:
                    prev_qid = self._gl_query_ids[prev_idx]
                    available = glGetQueryObjectiv(
                        prev_qid, GL_QUERY_RESULT_AVAILABLE)
                    if available:
                        # PyOpenGL spells the 64-bit getter with the
                        # trailing `v` (so does the GL spec; many docs
                        # drop it).  Result is in nanoseconds for a
                        # GL_TIME_ELAPSED query.
                        ns = glGetQueryObjectui64v(
                            prev_qid, GL_QUERY_RESULT)
                        self._gpu_accum_ms += ns / 1_000_000.0

                # Swap so next frame uses the slot we just read from.
                self._gl_query_idx = 1 - self._gl_query_idx
            except Exception:
                # Driver hiccup -- disable for the rest of the session.
                self._gl_query_ids = []

        pygame.display.flip()

    # ------------------------------------------------------------------
    # Mouse-enter / leave focus stealing (Windows polite-grab)
    # ------------------------------------------------------------------

    def _maybe_steal_focus_on_enter(self):
        """Bring TEPY to the foreground when the cursor enters our
        window, saving whatever window held foreground before so
        we can hand it back on `WINDOWLEAVE`.

        The point: Windows' default click-to-focus rule eats the
        first click on a backgrounded app (the click only raises
        the window; the inner control sees nothing).  Stealing
        focus on cursor-enter means the very first click on a
        TEPY button does what the user expects.

        Politeness: we save the previous foreground HWND on the
        way IN and restore it on the way OUT (`WINDOWLEAVE`), so
        we're not permanently stealing input from whatever was
        focused before.

        Windows-only.  On Linux / macOS, SDL2 already handles
        cursor-enter focus the way the user expects, so the
        platform check short-circuits and we do nothing.
        """
        if os.name != 'nt':
            return
        # If we already have a saved hwnd, the user re-entered
        # without leaving in between -- don't double-save.  The
        # original prev hwnd is still the right one to restore.
        if self._prev_foreground_hwnd is not None:
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            wm_info = pygame.display.get_wm_info()
            my_hwnd = wm_info.get('window') if wm_info else None
            cur     = user32.GetForegroundWindow()
            if not my_hwnd or cur == my_hwnd:
                return    # already foreground; nothing to steal
            self._prev_foreground_hwnd = int(cur)
            user32.SetForegroundWindow(int(my_hwnd))
        except Exception:
            # ctypes / WM-info / SetForegroundWindow can all fail
            # in edge cases (RDP sessions, screen-locked, security
            # policy).  Reset so the leave handler doesn't try to
            # restore an invalid hwnd.
            self._prev_foreground_hwnd = None

    def _maybe_restore_focus_on_leave(self):
        """Hand focus back to whichever window was foreground
        before our mouse-enter steal.  Called on `WINDOWLEAVE`.

        No-op when we never actually stole focus (cursor crossed
        the window boundary while we already had it, or the
        platform-check short-circuited the steal).
        """
        if os.name != 'nt' or self._prev_foreground_hwnd is None:
            return
        try:
            import ctypes
            ctypes.windll.user32.SetForegroundWindow(
                int(self._prev_foreground_hwnd))
        except Exception:
            pass
        self._prev_foreground_hwnd = None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _set_window_borderless(self, borderless):
        """Toggle the OS window's title-bar / chrome / resize-frame
        without recreating the pygame display surface.

        Pygame's `NOFRAME` flag can't be flipped after `set_mode()`
        without destroying + recreating the window -- which on an
        OpenGL surface drops the GL context and trashes every
        uploaded texture / VBO.  This Win32 path mutates the
        window-style bitmask directly (`SetWindowLong` +
        `SetWindowPos(SWP_FRAMECHANGED)`), so the same HWND keeps
        its GL context.

        No-op on non-Windows (the borderless splash is a Windows
        cosmetic; SDL on Mac / Linux respects `pygame.NOFRAME`
        only at creation time anyway).
        """
        if os.name != 'nt':
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            wm_info = pygame.display.get_wm_info()
            hwnd = wm_info.get('window') if wm_info else None
            if not hwnd:
                return
            GWL_STYLE             = -16
            WS_OVERLAPPEDWINDOW   = 0x00CF0000   # caption + sysmenu + min/max + thick frame
            WS_POPUP              = 0x80000000
            WS_VISIBLE            = 0x10000000
            SWP_NOMOVE            = 0x0002
            SWP_NOSIZE            = 0x0001
            SWP_NOZORDER          = 0x0004
            SWP_FRAMECHANGED      = 0x0020
            # 64-bit-safe getter; falls back to the 32-bit variant
            # for the (rare) 32-bit Python build.
            GetWindowLong = (user32.GetWindowLongPtrW
                             if hasattr(user32, 'GetWindowLongPtrW')
                             else user32.GetWindowLongW)
            SetWindowLong = (user32.SetWindowLongPtrW
                             if hasattr(user32, 'SetWindowLongPtrW')
                             else user32.SetWindowLongW)
            # Simple two-bit toggle: only mask WS_CAPTION (title
            # bar) and WS_THICKFRAME (resize border).  Don't touch
            # WS_SYSMENU / WS_MINIMIZEBOX / WS_MAXIMIZEBOX / WS_VISIBLE
            # / WS_POPUP -- SDL set those up at window creation and
            # the previous "swap WS_OVERLAPPEDWINDOW for WS_POPUP"
            # approach was conflicting with SDL2's own bookkeeping
            # on the restore path, leaving the window chromeless
            # even after a successful SetWindowLong.
            WS_CAPTION    = 0x00C00000
            WS_THICKFRAME = 0x00040000
            current = GetWindowLong(hwnd, GWL_STYLE)
            if borderless:
                new_style = current & ~(WS_CAPTION | WS_THICKFRAME)
            else:
                new_style = current |  (WS_CAPTION | WS_THICKFRAME)
            if new_style == current:
                return   # nothing to do, save the SetWindowPos roundtrip
            SetWindowLong(hwnd, GWL_STYLE, new_style)
            # Add SWP_SHOWWINDOW on the restore path -- some
            # drivers leave the window in a "style changed but
            # not redrawn" state after SetWindowLong unless we
            # explicitly poke it back into the visible set.
            # SWP_DRAWFRAME = SWP_FRAMECHANGED already; combine
            # with SWP_SHOWWINDOW to be belt-and-braces.
            SWP_SHOWWINDOW = 0x0040
            user32.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                | SWP_FRAMECHANGED | SWP_SHOWWINDOW)
            # Belt-and-braces: ShowWindow(SW_SHOW) makes sure the
            # window is in the visible state regardless of what
            # the previous style transition left it as.  Idempotent
            # if already visible.
            SW_SHOW = 5
            user32.ShowWindow(hwnd, SW_SHOW)
        except Exception as exc:
            print(f"[viewer] borderless toggle failed: {exc}")

    def _go_maximized(self):
        """Maximise the pygame window so it fills the screen but
        keeps its title bar / taskbar / chrome.

        Different from fullscreen-exclusive: the desktop's taskbar
        stays visible, the window remains a normal app citizen for
        ALT-TAB / window-snap, and the OS chrome at the top still
        has the [_] / [□] / [X] buttons.  Just bigger.

        Implementation: Win32 ShowWindow(HWND, SW_MAXIMIZE).  The
        resulting WM_SIZE fires SDL_WINDOWEVENT_SIZE_CHANGED ->
        pygame VIDEORESIZE, which our existing handle_input branch
        catches and feeds to _on_resize for the GL viewport +
        side-panel relayout.

        No-op fallback on non-Windows: just stays at the windowed
        dimensions.
        """
        if os.name != 'nt':
            self._is_fullscreen = False
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            wm_info = pygame.display.get_wm_info()
            hwnd = wm_info.get('window') if wm_info else None
            if not hwnd:
                self._is_fullscreen = False
                return
            SW_MAXIMIZE = 3
            user32.ShowWindow(hwnd, SW_MAXIMIZE)
            self._is_fullscreen = True
        except Exception as exc:
            print(f"[viewer] maximize failed: {exc}")
            self._is_fullscreen = False

    def _go_windowed(self):
        """Restore the maximised window back to a normal resizable
        window at its prior dimensions.  Companion to
        `_go_maximized`; reached via F11 mid-session.

        Implementation: Win32 ShowWindow(HWND, SW_RESTORE).  SDL2
        remembers the pre-maximise size + position natively, so
        SW_RESTORE drops back to exactly where the user had it
        (or the launch dimensions if no manual resize happened).
        """
        if os.name != 'nt':
            self._is_fullscreen = False
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            wm_info = pygame.display.get_wm_info()
            hwnd = wm_info.get('window') if wm_info else None
            if not hwnd:
                self._is_fullscreen = False
                return
            SW_RESTORE = 9
            user32.ShowWindow(hwnd, SW_RESTORE)
            self._is_fullscreen = False
        except Exception as exc:
            print(f"[viewer] windowed restore failed: {exc}")

    def _toggle_fullscreen(self):
        """F11 hotkey: flip between windowed and maximised states.
        Method name kept as `_toggle_fullscreen` since F11 is the
        conventional fullscreen-toggle key, but the underlying
        action is OS-window maximise (chrome preserved) -- see
        `_go_maximized` for the rationale.

        After the ShowWindow call, explicitly poll the new client
        rect from Win32 and feed it into `_on_resize` so the GL
        viewport + side panels reshape THIS frame instead of
        waiting on the next pygame event tick.  Belt-and-braces
        for the case where the WM_SIZE / SDL_WINDOWEVENT_SIZE_
        CHANGED / pygame VIDEORESIZE chain has any extra latency.
        """
        if getattr(self, '_is_fullscreen', False):
            self._go_windowed()
        else:
            self._go_maximized()
        # Immediately reshape the GL viewport + UI to the new
        # client size.  The chrome (title bar / resize border) is
        # NOT counted -- GetClientRect returns just the content
        # area, which is what `glViewport` and `_on_resize` want.
        if os.name == 'nt':
            try:
                import ctypes
                user32 = ctypes.windll.user32
                wm_info = pygame.display.get_wm_info()
                hwnd = wm_info.get('window') if wm_info else None
                if hwnd:
                    class _RECT(ctypes.Structure):
                        _fields_ = [('left',   ctypes.c_long),
                                    ('top',    ctypes.c_long),
                                    ('right',  ctypes.c_long),
                                    ('bottom', ctypes.c_long)]
                    rc = _RECT()
                    if user32.GetClientRect(hwnd, ctypes.byref(rc)):
                        new_w = max(1, rc.right - rc.left)
                        new_h = max(1, rc.bottom - rc.top)
                        if (new_w, new_h) != (self.width, self.height):
                            self._on_resize(new_w, new_h)
            except Exception as exc:
                print(f"[viewer] post-F11 resize failed: {exc}")

    def _set_borderless(self, borderless):
        """Toggle the OS title-bar / resize-border on the pygame
        window via Win32 SetWindowLongW.

        Pygame's `NOFRAME` flag can't be flipped after creation
        without recreating the window (which throws away the GL
        context and every shader / texture / VAO bound to it), so
        we mutate the existing HWND's style flags directly via
        Win32.

        Style bits toggled:
            WS_CAPTION    (title bar)
            WS_THICKFRAME (resizable border + corner grips)

        SetWindowPos with SWP_FRAMECHANGED forces the new style to
        take effect without a move/resize/zorder change.

        No-op on non-Windows or when ctypes / the WM info isn't
        available -- the splash just looks the same as the rest of
        the session there, which is the existing behaviour.
        """
        if os.name != 'nt':
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            wm_info = pygame.display.get_wm_info()
            hwnd = wm_info.get('window') if wm_info else None
            if not hwnd:
                return
            GWL_STYLE        = -16
            WS_CAPTION       = 0x00C00000
            WS_THICKFRAME    = 0x00040000
            SWP_NOSIZE       = 0x0001
            SWP_NOMOVE       = 0x0002
            SWP_NOZORDER     = 0x0004
            SWP_FRAMECHANGED = 0x0020
            cur = user32.GetWindowLongW(hwnd, GWL_STYLE)
            if borderless:
                new = cur & ~(WS_CAPTION | WS_THICKFRAME)
            else:
                new = cur |  (WS_CAPTION | WS_THICKFRAME)
            if new != cur:
                user32.SetWindowLongW(hwnd, GWL_STYLE, new)
                user32.SetWindowPos(
                    hwnd, 0, 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE
                    | SWP_NOZORDER | SWP_FRAMECHANGED)
        except Exception as exc:
            print(f"[viewer] borderless toggle failed: {exc}")

    def run(self):
        """Enter the main event loop; returns after the window is closed."""
        # Tear down the startup splash (if any) -- from this point on
        # render() draws the real scene + UI.  Does nothing if Splash
        # construction failed earlier.
        if self.splash is not None:
            try:
                self.splash.cleanup()
            except Exception as exc:
                print(f"[viewer] splash cleanup: {exc}")
            self.splash = None
        # Window chrome was already restored at the END of __init__
        # (after preload).  Doing it here was racing the SW_MAXIMIZE
        # below -- the WM_NCCALCSIZE message hadn't drained, so the
        # maximise took effect on a borderless popup and the chrome
        # never came back.  See the end-of-preload block in __init__
        # for the correct restore site.
        # Maximise the window for the main viewing experience.
        # Splash rendered in the original 1280x720 client area so
        # it stayed centred in a known viewport; now that init is
        # done we want every available pixel for the tank + tree +
        # side panels, but still want the taskbar / title-bar /
        # ALT-TAB to behave like a normal windowed app.  Maximise
        # rather than fullscreen-exclusive does that.  F11 toggles
        # back to the windowed dimensions.
        self._go_maximized()

        while self.running:
            self.handle_input()

            # Particle-integration dt -- the PREVIOUS frame's measured
            # wall-clock time (in seconds).  Standard game-loop
            # pattern: simulate this frame using how long the last one
            # took.  An earlier draft tried `now - _last_frame_end`,
            # but `_last_frame_end` is set AFTER flip() returns, so
            # that delta was just the handle_input() cost (tens of
            # microseconds) -- particles barely moved.  Clamped at
            # 0.1 s so pauses (window drag, debugger, full-tank load)
            # don't spawn a wall of catch-up particles in one frame.
            self._frame_dt = max(0.0, min(self._prev_frame_ms / 1000.0, 0.1))

            # ---- Render the frame ----------------------------------
            # `self.render()` ends with `pygame.display.flip()`, which
            # with vsync on stalls until the next vertical refresh.
            # When flip() returns, the swap has been scheduled (and
            # in practice already committed at the next refresh edge);
            # that's the moment we treat as "frame done" for FPS.
            self.render()
            self.frame_count += 1

            # ---- Frame-time accounting (5-frame block average) -----
            # Add this frame's wall time to the running accumulator.
            # When the 5th frame in the block lands, divide and
            # refresh the title bar; reset for the next block.
            # Until then, just keep accumulating -- caption stays put.
            # `_prev_frame_ms` carries this frame's value forward so
            # the next iteration's particle dt is correct.
            frame_end = time.perf_counter()
            frame_ms  = (frame_end - self._last_frame_end) * 1000.0
            self._last_frame_end = frame_end
            self._prev_frame_ms  = frame_ms

            self._fps_accum_ms    += frame_ms
            self._fps_block_count += 1

            if self._fps_block_count >= 5:
                # Wall-clock side: mean ms / frame and derived FPS.
                avg_ms = self._fps_accum_ms / self._fps_block_count
                self._fps_avg_ms  = avg_ms
                self._fps_display = (1000.0 / avg_ms) if avg_ms > 0.0 else 0.0

                # GPU side: same 5-frame block, lagged by one frame
                # (we read frame N's query on frame N+1).  When timer
                # queries are unavailable (`_gl_query_ids` empty),
                # gpu_accum stays 0 and we just hide the GPU number
                # in the caption below.
                self._gpu_avg_ms = (self._gpu_accum_ms / 5.0
                                     if self._gpu_accum_ms > 0.0 else 0.0)

                self._fps_block_count = 0
                self._fps_accum_ms    = 0.0
                self._gpu_accum_ms    = 0.0

                if self._gpu_avg_ms > 0.0:
                    pygame.display.set_caption(
                        f"TEPY  v{self._app_version} -- "
                        f"{self._fps_display:5.1f} FPS  "
                        f"(cpu {self._fps_avg_ms:5.2f} ms / "
                        f"gpu {self._gpu_avg_ms:5.2f} ms)  |  "
                        f"dist={self.camera.distance:.2f}  "
                        f"meshes={len(self.meshes)}"
                    )
                else:
                    pygame.display.set_caption(
                        f"TEPY  v{self._app_version} -- "
                        f"{self._fps_display:5.1f} FPS  "
                        f"({self._fps_avg_ms:5.2f} ms)  |  "
                        f"dist={self.camera.distance:.2f}  "
                        f"meshes={len(self.meshes)}"
                    )

        # Persist slider values so the next session restores them.
        # Done before GPU cleanup so a crash inside cleanup doesn't lose the
        # settings.  Mouse-up has already been writing the JSON on
        # every drag release (see handle_input + _persist_all_sliders),
        # so this final pass mostly catches non-slider state
        # (checkboxes, debug flag, legacy-key drops).
        try:
            # Snapshot every slider value into self._cfg via the
            # single-source-of-truth sub.  No write here -- we'll
            # save once below after also folding in the checkboxes.
            self._persist_all_sliders(write_json=False)
            # Drop legacy single-slot keys (pre-v1.46 schema).
            for legacy, _new_field in self._LEGACY_SMOKE_KEYS:
                self._cfg.pop(legacy, None)
            for legacy, _new_field in self._LEGACY_FIRE_KEYS:
                self._cfg.pop(legacy, None)
            for legacy in self._LEGACY_DROPPED_KEYS:
                self._cfg.pop(legacy, None)
            # Non-slider widgets that share the same write cycle.
            if self._normals_mode_cb is not None:
                self._cfg['normals_per_vertex']      = bool(self._normals_mode_cb.checked)
            # Master debug-overlay flag (Debug checkbox).  Replaces
            # the pre-v1.50 split keys; both old keys are dropped
            # so the JSON ends up with only `debug`.
            self._cfg['debug']         = bool(self._debug)
            self._cfg['show_terrain']  = bool(self.show_terrain)
            self._cfg['suspension_test'] = bool(self._suspension_test)
            self._cfg.pop('show_hardpoints', None)
            self._cfg.pop('show_fire_cards', None)
            _config.save(self._cfg)
        except Exception as exc:
            print(f"[viewer] config save (sliders) failed: {exc}")

        # Cleanup
        for mesh in self.meshes:
            mesh.cleanup()
        # Shared textures (detail map etc.) are not owned by Mesh -- free here
        for tex_id in self._shared_tex_cache.values():
            try:
                glDeleteTextures(1, [tex_id])
            except Exception:
                pass
        self._shared_tex_cache.clear()
        self.grid.cleanup()
        self.axes.cleanup()
        self.light_sphere.cleanup()
        self.hp_sphere.cleanup()
        self.hp_lines.cleanup()
        self.fire_outlines.cleanup()
        self.lookat_lines.cleanup()
        self.physics_lines.cleanup()
        self.hull_box_lines.cleanup()
        if self.smoke_particles is not None:
            self.smoke_particles.cleanup()
        if self.fire_smoke_particles is not None:
            self.fire_smoke_particles.cleanup()
        if self.smoke_flipbook is not None:
            self.smoke_flipbook.cleanup()
        if self.fire_billboards is not None:
            self.fire_billboards.cleanup()
        if self.fire_flipbook is not None:
            self.fire_flipbook.cleanup()
        if self.skybox:
            self.skybox.cleanup()
        if self.terrain:
            self.terrain.cleanup()
        # Free every cached tier tree (UIManager.cleanup will also try to
        # free self.ui.tree, which is one of these -- UITreeView.cleanup
        # is idempotent so the second pass is a no-op).
        for t in getattr(self, '_tier_tree_cache', []):
            if t is None:
                continue
            try:
                t.cleanup()
            except Exception:
                pass
        self.ui.cleanup()
        if self._pkg_extractor:
            self._pkg_extractor.cleanup()
        # GL timer queries -- only present if the driver supported
        # them and lazy-init succeeded on the first render call.
        if self._gl_query_ids:
            try:
                glDeleteQueries(len(self._gl_query_ids), self._gl_query_ids)
            except Exception:
                pass
        pygame.quit()
