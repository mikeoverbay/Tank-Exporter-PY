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
import os
import re
import shutil
import time
from collections import deque

import numpy as np
import pygame
from pygame.locals import (DOUBLEBUF, KEYDOWN, MOUSEBUTTONDOWN, MOUSEBUTTONUP,
                            MOUSEMOTION, MOUSEWHEEL, OPENGL, QUIT, RESIZABLE,
                            VIDEORESIZE, K_ESCAPE, K_n, K_r, K_w)
from OpenGL.GL import *

from .loaders  import MeshParser, VisualLoader, TextureLoader, VehicleXMLLoader, PkgExtractor
from .         import config as _config
from .mesh     import Mesh
from .scene    import Camera, Grid, Axes, Sphere, LineBatch
from .shaders  import (ShaderProgram, SimpleColorShader,
                       ParticleShader, ImportedShader, NormalsShader)
from .skybox   import Skybox
from .particles import FlipbookTexture, ParticleSystem
from .ui       import UIManager, UITreeView, UITreeNode, UITabBar


# ---------------------------------------------------------------------------
# Nation armor colours
# ---------------------------------------------------------------------------
# The per-nation default tint colour used to live here as a hardcoded
# table guessed from the legacy VB Tank Exporter source.  It now comes
# from `tankviewer.armor_colors.ArmorColorLoader`, which parses the
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
    RIGHT_CONTROLS_H = 200      # 5 smoke sliders + Normals slider + Show HP checkbox

    # Tier-filter tab labels (strings).  '1' .. '11' for WoT tiers I-XI.
    TREE_TIER_TABS = [str(t) for t in range(1, 12)]

    # Width of the left-hand info panel (collapsible tank stats).
    INFO_PANEL_W = 280

    # Folder of tank-thumbnail PNGs (filename = <tank_xml_basename>.png).
    # Located at the project root, sibling to the tankviewer package.
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
        # Merge supplied config with defaults so callers can omit it
        self._cfg = _config.load()
        if cfg:
            self._cfg.update(cfg)

        pygame.init()
        pygame.font.init()

        self.width  = 1280
        self.height = 720

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

        glEnable(GL_DEPTH_TEST)
        glClearColor(0.1, 0.1, 0.12, 1.0)

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
            _splash_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'resources', 'splash.png')
            if os.path.isfile(_splash_path):
                _welcome = f"Welcome to version {_APP_VERSION}"
                self.splash = Splash(_splash_path, self.width, self.height,
                                     welcome_text=_welcome)
                self.splash.render()
                pygame.display.flip()
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

        # ---- Particle system (engine smoke, billboard flipbook) ------------
        # Flipbook is 91 PNG frames (256x256 RGBA) at resources/smoke/.
        # Loaded once at startup and held for the life of the viewer.
        # ParticleSystem owns the per-particle CPU state + dynamic VBO;
        # set_emitters() is called every load_vehicle to point it at the
        # current tank's HP_Track_Exhaus_* nodes.
        self._splash_status('Loading smoke flipbook (91 frames)...')
        self.particle_shader = None
        self.smoke_flipbook  = None
        self.smoke_particles = None
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
            self.smoke_particles.fade_start_frame = float(
                self._cfg.get('smoke_fade_start_frame', 75.0))
            self.smoke_particles.fade_end_frame   = float(
                self._cfg.get('smoke_fade_end_frame',   91.0))
        except Exception as exc:
            print(f"[viewer] Smoke particles disabled: {exc}")
            self.particle_shader = None
            self.smoke_flipbook  = None
            self.smoke_particles = None

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

        # Toggleable display flags
        self.show_grid   = False
        self.show_axes   = False
        self.show_skybox = False
        self.show_light  = True
        self.wireframe   = False
        self.use_normal_map = True
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
        self._normals_slider        = None
        self._normals_mode_cb       = None
        self._invert_metal_cb    = None
        self._invert_shine_cb    = None
        self._show_hp_cb         = None

        # New: hardpoint-marker visibility (orange spheres + cyan
        # direction vectors at exhaust nodes).  Persisted in config.
        # Default off -- the data is still populated on every load so
        # the toggle just gates the rendering.
        self._show_hardpoints = bool(self._cfg.get('show_hardpoints', False))

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

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        """Create the top-bar toggle buttons, PBR sliders, and invert checkboxes."""
        # --- Row 1: toggle buttons (fixed at y=4 regardless of bar height) ---
        x = self.ui.BUTTON_PADDING
        y = 4
        h = 22
        for label, attr in [
            ('Grid',      'show_grid'),
            ('Axes',      'show_axes'),
            ('Light',     'show_light'),
            ('Orbit',     'orbit_lights'),
            ('Skybox',    'show_skybox'),
            ('Wireframe', 'wireframe'),
        ]:
            initial = getattr(self, attr, False)
            btn      = self.ui.add_button(label, x, y, 70, h, active=initial)
            btn.attr = attr
            x       += btn.w + self.ui.BUTTON_SPACING

        # --- Action buttons (one-shot, no toggle attr) -------------------
        x       += self.ui.BUTTON_SPACING   # extra gap from toggle group

        self.ui.add_button('Set Paths', x, y, 84, h, active=False,
                           action=self._show_paths_dialog)
        x       += 84 + self.ui.BUTTON_SPACING

        # 'Meshes' opens / closes the mesh-visibility window.  Always
        # available; population happens at load time.
        self.ui.add_button('Meshes', x, y, 70, h, active=False,
                           action=self._toggle_mesh_window)
        x       += 70 + self.ui.BUTTON_SPACING

        # 'Export' opens a Save dialog and spawns Blender (--background)
        # to write FBX / GLB / GLTF / OBJ.  Disabled visually when no
        # tank is loaded -- the action callback also short-circuits.
        self.ui.add_button('Export', x, y, 70, h, active=False,
                           action=self._on_export_clicked)
        x       += 70 + self.ui.BUTTON_SPACING

        # 'Import' opens a file picker and spawns Blender to read an
        # FBX/GLB/OBJ back into the viewer's scene.  Round-trip companion
        # to Export -- decodes WoT* color attributes and reconstructs the
        # original per-vertex stream.
        self.ui.add_button('Import', x, y, 70, h, active=False,
                           action=self._on_import_clicked)
        x       += 70 + self.ui.BUTTON_SPACING

        # 'Flip' toggles the active mesh set between FBX (imported) and
        # PKG (WoT-loaded).  No-op when the other set is empty.  Lets
        # the user A/B-compare an import against the in-game data
        # before exporting back out to .primitives_processed.
        self.ui.add_button('Flip', x, y, 70, h, active=False,
                           action=self._flip_active_set)
        x       += 70 + self.ui.BUTTON_SPACING

        # 'Compare' opens the side-by-side per-part stats window
        # (face/vert/index counts, UV2 presence, vertex format) so the
        # user can verify the imported FBX matches the PKG data slot
        # for slot before re-export.
        self.ui.add_button('Compare', x, y, 70, h, active=False,
                           action=self._on_compare_clicked)
        x       += 70 + self.ui.BUTTON_SPACING

        # 'Save Prim' opens a component picker (Hull / Chassis /
        # Turret / Gun) and writes the chosen parts back out as
        # WoT-native .primitives_processed files.  Companion to
        # Export (which writes FBX/GLB/OBJ) -- this one targets the
        # game's own format.
        self.ui.add_button('Save Prim', x, y, 70, h, active=False,
                           action=self._on_save_prim_clicked)
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
        self.ui.add_button('ItemList', x, y, 70, h, active=False,
                           action=self._on_rebuild_itemlist_clicked)
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
        light_init   = float(self._cfg.get('light_value',   0.10))
        ambient_init = float(self._cfg.get('ambient_value', 0.50))
        self._metal_slider = self.ui.add_slider('Light',   tx, cy1, tw,
                                                value=light_init,   value_max=0.25,
                                                group_id='lighting')
        self._shine_slider = self.ui.add_slider('Ambient', tx, cy2, tw,
                                                value=ambient_init, value_max=1.0,
                                                group_id='lighting')

        # Smoke particle tunables (drive the smoke ParticleSystem each
        # frame).  Defaults pulled from config so iterations persist
        # across sessions.  Fade-end is also persisted but not exposed as
        # a slider yet -- once a fade-start value is dialled in, both
        # values continue to apply via the shader uniforms even after
        # the slider widget is removed.
        smoke_start_init      = float(self._cfg.get('smoke_start_size',       0.10))
        smoke_end_init        = float(self._cfg.get('smoke_end_size',         0.25))
        smoke_speed_init      = float(self._cfg.get('smoke_speed',            2.0))
        smoke_fade_start_init = float(self._cfg.get('smoke_fade_start_frame', 30.0))
        smoke_fade_end_init   = float(self._cfg.get('smoke_fade_end_frame',   60.0))
        self._smoke_start_slider = self.ui.add_slider(
            'Sm Start', tx, cy3, tw, value=smoke_start_init, value_max=0.5,
            group_id='smoke')
        self._smoke_end_slider   = self.ui.add_slider(
            'Sm End',   tx, cy4, tw, value=smoke_end_init,   value_max=1.0,
            group_id='smoke')
        self._smoke_speed_slider = self.ui.add_slider(
            'Sm Speed', tx, cy5, tw, value=smoke_speed_init, value_max=8.0,
            group_id='smoke')
        # Fade range: alpha begins ramping at FadeS, hits zero at FadeE.
        # FadeE < 91 makes the smoke disappear before the flipbook ends
        # (which is what "fade earlier" means).  Default 30 -> 60 fades
        # over the middle third of the particle's life.
        self._smoke_fade_slider     = self.ui.add_slider(
            'Sm FadeS', tx, cy6,        tw, value=smoke_fade_start_init,
            value_max=91.0, group_id='smoke')
        self._smoke_fade_end_slider = self.ui.add_slider(
            'Sm FadeE', tx, cy6 + 25,   tw, value=smoke_fade_end_init,
            value_max=91.0, group_id='smoke')

        # Surface-normal debug lines.  Slider drives world-space line
        # length; 0 = off.  Default loaded from the persisted config
        # so the user's preferred setting survives across sessions.
        # Range max 0.5 is plenty for tank-scale meshes -- tracks +
        # smaller equipment look right around 0.05-0.15, hull faces
        # around 0.20-0.40.  Final on-screen position is set in
        # _layout_widgets alongside the smoke sliders.
        normals_init = float(self._cfg.get('normals_length', 0.0))
        self._normals_slider = self.ui.add_slider(
            'Normals', tx, cy6 + 50,   tw, value=normals_init,
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
            'PerVtx', cx, cy6 + 50 - 7, 14,
            checked=self._normals_per_vertex, group_id='smoke')

        cb_size = 14
        self._invert_metal_cb = self.ui.add_checkbox(
            'NMap', cx, cy1 - cb_size // 2, cb_size, checked=True,
            group_id='lighting')
        self._invert_shine_cb = self.ui.add_checkbox(
            'AO',   cx, cy2 - cb_size // 2, cb_size, checked=True,
            group_id='lighting')
        # Hardpoint-marker visibility toggle, lives in the smoke control
        # block on the right panel.  Position is finalised in _layout_widgets.
        self._show_hp_cb = self.ui.add_checkbox(
            'Show HP', cx, cy6 - cb_size // 2, cb_size,
            checked=self._show_hardpoints, group_id='smoke')

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

        # Position every widget inside its panel BEFORE we set the
        # info-panel rect below -- _layout_widgets computes the actual
        # height the button + slider stack needs and writes it back
        # into self.LEFT_CONTROLS_H, so the info-tree positioning
        # that follows reads the freshly computed value.  Moving this
        # call earlier avoids a fixed-constant LEFT_CONTROLS_H going
        # stale every time the button-group structure changes.
        self._layout_widgets()

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
        # Identifies "left panel" widgets by their x position falling
        # inside the panel's x range (post-layout).
        left_visible = not self.ui.info_collapsed
        for btn in self.ui.buttons:
            if btn.x < self.INFO_PANEL_W:
                btn.visible = left_visible
        for sl in self.ui.sliders:
            if sl.track_x < self.INFO_PANEL_W:
                sl.visible = left_visible
        for cb in self.ui.checkboxes:
            if cb.x < self.INFO_PANEL_W:
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
    def _resolve_tank_nation(self, tank_name):
        """Look up which nation owns a tank XML basename.

        Args:
            tank_name (str): tank XML basename, e.g. 'It21_Lion' (case
                             insensitive; trailing extension is stripped).

        Returns:
            (nation, tank_basename) on success, or (None, tank_basename)
            when the name doesn't match any list.xml entry across all
            nations.  PkgExtractor must already be initialised; returns
            (None, '') when it isn't.
        """
        if not tank_name or self._pkg_extractor is None:
            return (None, '')
        tank_basename = os.path.splitext(os.path.basename(tank_name))[0]
        try:
            nations = self._pkg_extractor.list_vehicle_xmls(with_tier=True)
        except Exception as exc:
            print(f"[viewer] resolve-tank-nation: list_vehicle_xmls failed: {exc}")
            return (None, tank_basename)
        target = f"{tank_basename.lower()}.xml"
        for nat, entries in nations.items():
            for e in entries:
                if e.get('xml', '').lower() == target:
                    return (nat, tank_basename)
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
                # see tankviewer/exporters/common.py).  This is the
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

        skipped = 0
        for nation in sorted(nations.keys()):
            entries = nations[nation]
            children = []
            for entry in entries:
                xml_name = entry['xml']
                base     = xml_name[:-4]
                if base not in tanks_txt_map:
                    skipped += 1
                    continue
                tier = entry.get('tier')
                if tier_filter is not None and tier != tier_filter:
                    continue
                display = tanks_txt_map[base]
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

    def _on_tree_tank_selected(self, node):
        """User clicked a leaf (tank) row -- pull options + show the
        per-skin Load picker dialog."""
        if self._pkg_extractor is None:
            print("[viewer] No PKG extractor available -- cannot load tank")
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

            # Fresh console for this load action.  Header tag tells the
            # user which tank we're crunching on right now.
            self.log_clear(status=f'Load Tank: {tank_label}')
            _status(f"Loading {tank_label}...")
            self._load_tank_with_options(
                local_path,
                skin=skin,
                chassis_tag=chassis_tag,
                turret_tag=turret_tag,
                gun_tag=gun_tag,
                damaged=damaged,
                status_callback=_status,
            )

        self.ui.load_dialog.show(
            title=f"{nation} / {tank_label}",
            options=options,
            on_load=_on_load,
            make_tex=self.ui._make_tex,
        )

    def _load_tank_with_options(self, xml_path, skin=None,
                                 chassis_tag=None, turret_tag=None,
                                 gun_tag=None, damaged=False,
                                 status_callback=None):
        """Trigger load_vehicle with explicit skin / part overrides chosen
        from the load dialog.  status_callback is forwarded to the load
        so progress messages reach the dialog's bottom status line."""
        self.load_vehicle(
            xml_path,
            damaged=damaged,
            skin=skin,
            chassis_tag=chassis_tag,
            turret_tag=turret_tag,
            gun_tag=gun_tag,
            status_callback=status_callback,
        )

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

            # Fit camera and remember bbox for 'R' reset
            all_pos  = np.concatenate(all_positions, axis=0)
            bbox_min = np.min(all_pos, axis=0)
            bbox_max = np.max(all_pos, axis=0)
            self._scene_bbox = (bbox_min, bbox_max)
            self.camera.fit_to_bounds(bbox_min, bbox_max)

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
        tankviewer.exporters.import_vehicle.

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
                        # just opened.
                        self.load_vehicle(xml_local)
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
                     status_callback=None):
        """Load a complete tank from a WoT vehicle XML definition file.

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

            # Engine-exhaust spec from the def XML.  Tells us which named
            # nodes WoT actually treats as the engine exhaust (vs HP_Fire,
            # which is for damage / burning visuals) and the pixie preset
            # (gas_medium / diesel_large / ...) we'll later use to pick a
            # smoke style.  Falls through gracefully if the block is absent.
            exhaust_spec = VehicleXMLLoader.find_engine_exhaust(xml_path)
            self._exhaust_pixie = (exhaust_spec or {}).get('pixie')
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
                if vis:
                    group_textures = VisualLoader.parse_textures(vis, group_names)

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

            all_pos  = np.concatenate(all_positions, axis=0)
            bbox_min = np.min(all_pos, axis=0)
            bbox_max = np.max(all_pos, axis=0)
            self._scene_bbox = (bbox_min, bbox_max)
            self.camera.fit_to_bounds(bbox_min, bbox_max)

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
                info = VehicleXMLLoader.parse_info(xml_path,
                                                   self._pkg_extractor)
                self._active_set.tank_info = info
                self._build_info_panel(info)
            except Exception as exc:
                print(f"[viewer] info-panel build failed: {exc}")

            # Refresh the mesh-visibility window so its rows match the
            # newly-loaded meshes (all reset to visible).  Window stays
            # closed/open as the user left it; only its contents change.
            self._populate_mesh_window()

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

            # Point the smoke particle system at the same hardpoints.
            # Resetting kills any in-flight particles from the previous
            # tank so we don't see ghost smoke during a load.
            if self.smoke_particles is not None:
                self.smoke_particles.reset()
                self.smoke_particles.set_emitters(self._exhaust_points)

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
        button_groups = [
            ('UI', [
                ('Grid',      0, 1),
                ('Axes',      1, 1),
                ('Light',     2, 1),
                ('Orbit',     0, 1),
                ('Skybox',    1, 1),
                ('Wireframe', 2, 1),
                ('Meshes',    0, 1),
                ('Flip',      1, 1),
                ('Compare',   2, 1),
            ]),
            ('IO', [
                ('Set Paths', 0, 3),
                ('Import',    0, 2),
                ('Export',    2, 1),
                ('Save Prim', 0, 3),
            ]),
            ('Tools', [
                ('ItemList',  0, 3),
            ]),
        ]

        # Section-header label: rendered as a single line of soft-grey
        # text above each group's first row, with a thin gap below.
        SECTION_LABEL_H   = 16    # vertical reserve for the header text
        SECTION_GAP_AFTER = 2     # tight tuck under the label

        # Re-create the section labels each layout pass -- text textures
        # are owned by UIManager.panel_labels and freed in clear_panel_labels.
        self.ui.clear_panel_labels()

        btn_by_label = {b.label: b for b in self.ui.buttons}
        # Walk groups from top to bottom, packing rows into the column grid
        # within each group.  Tracks an explicit "next_y" so we can
        # interleave headers + button rows without computing a global
        # row index.
        next_y = BTN_PAD_Y
        for section_name, rows in button_groups:
            # Header label: small, slightly indented to align with the
            # first column.  Drawn at the current cursor; cursor then
            # advances past it before the first row.
            self.ui.add_panel_label(section_name,
                                    x=BTN_PAD_X, y=next_y,
                                    color=(150, 165, 200))
            next_y += SECTION_LABEL_H + SECTION_GAP_AFTER

            # Pack rows: each entry is (label, col, span).  When a span
            # would overflow the next column boundary, that row sits on
            # its own line; consecutive single-cell entries stack
            # left-to-right and only bump y when the column wraps.
            row_y    = next_y
            cur_col  = 0
            for label, col, span in rows:
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
        L_TRACK_X = 56
        L_TRACK_W = 160   # ~10% narrower for cleaner panel fit
        L_VAL_X   = L_TRACK_X + L_TRACK_W + 6
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
        right_x   = self.width - self.TREE_PANEL_W
        right_top = self.height - self.RIGHT_CONTROLS_H
        # Right panel slider geometry.  Label sits at `right_x + 6`;
        # we need ~60+ px for the longest labels ('Sm FadeE',
        # 'Normals') at 13pt Calibri Bold.  Track starts at +72 so the
        # label has room to breathe without overlapping the track.
        # Track width unchanged; value text follows automatically.
        R_TRACK_X = right_x + 72
        R_TRACK_W = 135
        R_VAL_X   = R_TRACK_X + R_TRACK_W + 6
        smoke_y0  = right_top + 14

        smoke_sliders = [
            self._smoke_start_slider,
            self._smoke_end_slider,
            self._smoke_speed_slider,
            self._smoke_fade_slider,
            self._smoke_fade_end_slider,
            self._normals_slider,
        ]
        for i, sl in enumerate(smoke_sliders):
            if not sl:
                continue
            sl.track_x  = R_TRACK_X
            sl.track_cy = smoke_y0 + i * ROW_H
            sl.track_w  = R_TRACK_W
            sl.label_x  = right_x + 6
            sl.value_x  = R_VAL_X
            sl.visible  = True

        # PerVtx + Show HP share one row BELOW the smoke sliders.
        # PerVtx sits on the LEFT (where Show HP used to be); Show HP
        # moves OVER to the right side of the same row so both
        # toggles fit without adding another row to the panel.
        cb_row_y = (smoke_y0 + len(smoke_sliders) * ROW_H
                    - 14 // 2)    # 14 = checkbox size
        if self._normals_mode_cb:
            self._normals_mode_cb.x = right_x + 8
            self._normals_mode_cb.y = cb_row_y
            self._normals_mode_cb.visible = True
        if self._show_hp_cb:
            # Right side of the row -- past the slider value column so
            # the labels don't crowd each other.
            self._show_hp_cb.x = right_x + 150
            self._show_hp_cb.y = cb_row_y
            self._show_hp_cb.visible = True

        # All other widgets stay visible
        for w in self.ui.sliders + self.ui.checkboxes:
            if not hasattr(w, 'visible'):
                continue
            w.visible = True

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
                elif event.key == K_w:
                    self.wireframe = not self.wireframe
                    # Don't call glPolygonMode here -- the next render
                    # pass owns the overlay-vs-solid switch.  The W key
                    # just flips the flag.
                    self._sync_button_state('wireframe', self.wireframe)
                elif event.key == K_n:
                    self.use_normal_map = not self.use_normal_map
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

            elif event.type == MOUSEWHEEL:
                # Tree panel scroll has priority over camera zoom
                mx, my = pygame.mouse.get_pos()
                if self.ui.handle_mouse_wheel(mx, my, event.y):
                    pass  # consumed by the tree
                elif event.y > 0:
                    # SDL2 / pygame 2: event.y > 0 = wheel forward = zoom in
                    self.camera.distance *= 0.9
                elif event.y < 0:
                    self.camera.distance *= 1.1

            elif event.type == MOUSEBUTTONDOWN:
                mx, my = event.pos
                if event.button == 1 and self.ui.is_pointer_over_ui(mx, my):
                    widget = self.ui.handle_mouse_down(mx, my)
                    if widget is not None:          # UIButton returned
                        self._apply_button_action(widget)

            elif event.type == MOUSEBUTTONUP:
                if event.button == 1:
                    self.ui.handle_mouse_up()

            elif event.type == MOUSEMOTION:
                self.ui.update_hover(*event.pos)
                if event.buttons[0]:    # left button held -- update slider drag
                    self.ui.handle_mouse_drag(*event.pos)

        # Continuous mouse-button camera controls
        btns      = pygame.mouse.get_pressed()
        mouse_pos = pygame.mouse.get_pos()

        if not self.ui.is_pointer_over_ui(*mouse_pos):
            dx = mouse_pos[0] - self.mouse_last[0]
            dy = mouse_pos[1] - self.mouse_last[1]

            if btns[2]:   # right-click -> orbit
                self.camera.yaw   += dx * 0.5
                self.camera.pitch += dy * 0.5
                self.camera.pitch  = np.clip(self.camera.pitch, -89, 89)

            if btns[1]:   # middle-click -> pan on XZ ground plane
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
        #   1) solid pass with GL_POLYGON_OFFSET_FILL enabled, which
        #      nudges every filled triangle SLIGHTLY AWAY from the
        #      camera in depth.  The pixels still cover the same
        #      screen area; only their depth values move.
        #   2) line pass at normal Z (no offset), so the line draws
        #      win the depth test against the offset-back fills.
        # Standard "wireframe over solid" recipe -- one polygon-offset
        # call before solid draws, disable + GL_LINE for the overlay.
        glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
        if self.wireframe:
            glEnable(GL_POLYGON_OFFSET_FILL)
            # Positive factor / units pushes filled tris AWAY from
            # camera in depth-buffer space.  Tuned to a stable
            # value across Intel/NVIDIA/AMD; bigger numbers can
            # produce visible gaps where adjacent tris meet.
            glPolygonOffset(1.0, 1.0)

        view = self.camera.get_view_matrix()
        proj = self.camera.get_projection_matrix()

        # Skybox -- rendered first; depth trick (xyww) places it at far plane
        if self.skybox and self.show_skybox:
            glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
            self.skybox.render(view, proj)
            # Stay in GL_FILL for the main mesh pass; the wireframe
            # overlay (if any) fires after the solid render.
            glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)

        # Background helpers (rendered without depth test so they never clip mesh)
        glLineWidth(1.0)
        glDisable(GL_DEPTH_TEST)
        if self.show_grid:
            self.grid.render(self.color_shader, view, proj)
        if self.show_axes:
            self.axes.render(self.color_shader, view, proj)
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
        self._active_shader = (self.imported_shader
                               if self.source_type == 'fbx'
                               else self.shader)
        active = self._active_shader

        # Shared mesh shader state
        active.use()
        active.set_mat4('view',       view)
        active.set_mat4('projection', proj)
        active.set_vec3_array('light_pos', light_positions)

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

        # Draw each primitive group
        for mesh in self.meshes:
            if not getattr(mesh, 'visible', True):
                continue   # user toggled this sub-mesh off in the info panel
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
            # First, disable the FILL offset we enabled at the start
            # of the solid pass.  Lines render at their natural Z so
            # they sit IN FRONT of the offset-back surface.
            glDisable(GL_POLYGON_OFFSET_FILL)
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
                active.set_mat4('model', mesh.model_matrix)
                mesh.render(active)
            # Restore everything we touched
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

        # Mirror the Show HP checkbox into the visibility flag so the
        # marker rendering below respects it without an event callback.
        if self._show_hp_cb is not None:
            self._show_hardpoints = bool(self._show_hp_cb.checked)

        # Hardpoint markers -- orange sphere at each discovered exhaust
        # node + 0.5-unit cyan direction vector.  Gated by the "Show HP"
        # checkbox in the smoke panel (mirrors self._show_hardpoints).
        if self._show_hardpoints:
            for hp in self._exhaust_points:
                hp_model = np.eye(4, dtype=np.float32)
                hp_model[0, 3] = float(hp['pos'][0])
                hp_model[1, 3] = float(hp['pos'][1])
                hp_model[2, 3] = float(hp['pos'][2])
                self.hp_sphere.render(self.color_shader, hp_model, view, proj)
            self.hp_lines.render(self.color_shader, view, proj)

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

        # 2-D overlay (reset viewport to full window so the tree + dialog
        # can draw outside the 3D scene area)
        glViewport(0, 0, self.width, self.height)
        self.ui.render(self.width, self.height)

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
    # Main loop
    # ------------------------------------------------------------------

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
        # settings.  Other persisted keys (paths, info-panel state) were
        # written eagerly when changed; sliders only flush on exit.
        try:
            if self._metal_slider:
                self._cfg['light_value']      = float(self._metal_slider.value)
            if self._shine_slider:
                self._cfg['ambient_value']    = float(self._shine_slider.value)
            if self._smoke_start_slider:
                self._cfg['smoke_start_size']        = float(self._smoke_start_slider.value)
            if self._smoke_end_slider:
                self._cfg['smoke_end_size']          = float(self._smoke_end_slider.value)
            if self._smoke_speed_slider:
                self._cfg['smoke_speed']             = float(self._smoke_speed_slider.value)
            if self._smoke_fade_slider:
                self._cfg['smoke_fade_start_frame']  = float(self._smoke_fade_slider.value)
            if self._smoke_fade_end_slider:
                self._cfg['smoke_fade_end_frame']    = float(self._smoke_fade_end_slider.value)
            if self._normals_slider:
                self._cfg['normals_length']          = float(self._normals_slider.value)
            if self._normals_mode_cb is not None:
                self._cfg['normals_per_vertex']      = bool(self._normals_mode_cb.checked)
            # Hardpoint marker visibility (Show HP checkbox in smoke panel)
            self._cfg['show_hardpoints']     = bool(self._show_hardpoints)
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
        if self.smoke_particles is not None:
            self.smoke_particles.cleanup()
        if self.smoke_flipbook is not None:
            self.smoke_flipbook.cleanup()
        if self.skybox:
            self.skybox.cleanup()
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
