"""
2D UI overlay -- top menu bar with toggle buttons, sliders, and checkboxes.

Layout (top-left origin, y increases downward):
    Row 1  y=0..30   : toggle buttons  (Grid, Axes, Light, Skybox, Wireframe)
    Row 2  y=30..57  : Metal  slider + value + Invert checkbox
    Row 3  y=57..84  : Shine  slider + value + Invert checkbox
    BAR_HEIGHT = 84

Classes:
    UIButton   : labelled toggle button.
                 Methods: contains(mx, my).
    UISlider   : horizontal slider, value 0.0-1.0.
                 Methods: hit(mx,my), set_from_mouse(mx), ensure_value_tex(font).
    UICheckbox : small square toggle with text label.
                 Methods: hit(mx, my).
    UIManager  : owns all widgets, handles events, renders the overlay.
                 Methods:
                     add_button(label,x,y,w,h,active) -> UIButton
                     add_slider(label,track_x,track_cy,track_w,value) -> UISlider
                     add_checkbox(label,x,y,checked) -> UICheckbox
                     handle_mouse_down(mx,my) -> UIButton|None
                     handle_mouse_drag(mx,my)
                     handle_mouse_up()
                     update_hover(mx,my)
                     is_pointer_over_ui(mx,my) -> bool
                     render(width,height)
                     cleanup()
"""

import ctypes

import numpy as np
import pygame
from OpenGL.GL import *

from .shaders import UIShader


# ============================================================================
# UIButton
# ============================================================================

class UIButton:
    """Bar button.

    Two flavours:
      * Toggle button -- has self.attr set; click flips self.active and
        UIManager returns the button so Viewer._apply_button_action can
        mirror the new state into a viewer flag.
      * Action button -- has self.action set (callable, no args); click
        invokes it directly.  self.active is ignored visually (drawn as
        inactive), and UIManager.handle_mouse_down returns None so no
        flag mirroring runs.
    """

    def __init__(self, label, x, y, w, h, active=True, action=None):
        self.label    = label
        self.x, self.y = x, y
        self.w, self.h  = w, h
        self.active   = active
        self.hovered  = False
        self.attr     = None
        self.action   = action       # callable() or None
        self.text_tex = None
        self.text_w = self.text_h = 0
        # Mirrors UISlider / UICheckbox -- skipped by render/click/hover
        # when False.  Set by Viewer._on_resize so the info-panel
        # collapse spine hides the entire left-panel control block.
        self.visible = True

    def contains(self, mx, my):
        return self.x <= mx <= self.x + self.w and self.y <= my <= self.y + self.h


# ============================================================================
# UISlider
# ============================================================================

class UISlider:
    """Horizontal slider, value in [0, value_max].

    Args:
        label     (str)  : left-side label text
        track_x   (int)  : left edge of the track in pixels
        track_cy  (int)  : vertical centre of the track
        track_w   (int)  : track width in pixels
        value     (float): initial value 0.0-value_max
        value_max (float): upper bound (default 1.0)
    """

    TRACK_H  =  6
    HANDLE_W = 12
    HANDLE_H = 18

    def __init__(self, label, track_x, track_cy, track_w, value=0.5, value_max=1.0):
        self.label     = label
        self.track_x   = track_x
        self.track_cy  = track_cy
        self.track_w   = track_w
        self.value_max = float(value_max)
        self.value     = max(0.0, min(self.value_max, float(value)))
        self._dragging = False

        # Label texture (static)
        self.label_tex = None
        self.label_w = self.label_h = 0

        # Value texture (regenerated when value changes)
        self.value_tex = None
        self.value_w = self.value_h = 0
        self._cached_val_str = None

        # Per-instance label/value x positions.  When None, the renderer
        # uses track-relative defaults (label just left of track, value
        # just right of track).  Viewer._layout_widgets sets explicit
        # values when laying out a group inside one of the side panels.
        self.label_x = None
        self.value_x = None

        # Group tag (no longer used for collapsing -- left/right side panels
        # are always visible -- but kept so widgets can still be addressed
        # by group when laying out.
        self.group_id = ''
        self.visible  = True

    # ------------------------------------------------------------------
    @property
    def handle_cx(self):
        """Centre-x of the drag handle in screen pixels."""
        norm = self.value / self.value_max if self.value_max > 0 else 0.0
        return self.track_x + int(norm * self.track_w)

    def hit(self, mx, my):
        """True if (mx,my) is over the track or handle."""
        return (self.track_x - 4 <= mx <= self.track_x + self.track_w + 4 and
                self.track_cy - self.HANDLE_H // 2 - 2 <= my
                                <= self.track_cy + self.HANDLE_H // 2 + 2)

    def set_from_mouse(self, mx):
        """Update value from mouse x coordinate (clamped to track)."""
        t = (mx - self.track_x) / max(1, self.track_w)
        t = max(0.0, min(1.0, t))
        self.value = t * self.value_max

    # ------------------------------------------------------------------
    def ensure_value_tex(self, font):
        """Re-render the value texture if the value has changed."""
        s = f"{self.value:.2f}"
        if s == self._cached_val_str:
            return
        if self.value_tex:
            glDeleteTextures(1, [self.value_tex])
        self._cached_val_str = s
        surf = font.render(s, True, (210, 230, 255))
        data = pygame.image.tostring(surf, 'RGBA', False)
        self.value_w = surf.get_width()
        self.value_h = surf.get_height()
        self.value_tex = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, self.value_tex)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, self.value_w, self.value_h, 0,
                     GL_RGBA, GL_UNSIGNED_BYTE, data)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        glBindTexture(GL_TEXTURE_2D, 0)


# ============================================================================
# UICheckbox
# ============================================================================

class UICheckbox:
    """Small square toggle with a text label to its right.

    Args:
        label   (str)  : label shown to the right of the box
        x, y    (int)  : top-left of the checkbox square
        size    (int)  : pixel side length of the square
        checked (bool) : initial state
    """

    def __init__(self, label, x, y, size=14, checked=False):
        self.label   = label
        self.x, self.y = x, y
        self.size    = size
        self.checked = checked
        self.label_tex = None
        self.label_w = self.label_h = 0
        # Collapsible-group tagging (matches UISlider).
        self.group_id = ''
        self.visible  = True

    def hit(self, mx, my):
        return self.x <= mx <= self.x + self.size and self.y <= my <= self.y + self.size


# ============================================================================
# UITreeView  --  collapsible, scrollable side panel
# ============================================================================

class UITreeNode:
    """One row in a UITreeView.

    A node is either a 'branch' (children list non-empty -- click to expand)
    or a 'leaf' (children empty -- click invokes the tree's on_select).

    Args:
        label    (str)      : visible text
        children (list|None): child UITreeNode list, None or [] -> leaf
        payload  (any)      : opaque user data passed back via on_select
    """

    def __init__(self, label, children=None, payload=None):
        self.label    = label
        self.children = children or []
        self.payload  = payload
        self.expanded = False

        # Lazy-built texture cache
        self.label_tex = None
        self.label_w = self.label_h = 0


class UITreeView:
    """Side panel showing a 2-level (or deeper) collapsible tree of items.

    Layout: anchored to a fixed (x, y) with size (w, h).  Rows are ROW_H
    pixels tall.  Mouse-wheel inside the panel scrolls.  Clicking a branch
    toggles it, clicking a leaf invokes self.on_select(node).

    The panel manages its own label textures (built lazily on first render)
    and stores them on each node.
    """

    ROW_H            = 18      # row height in pixels
    INDENT           = 14      # indent per depth level
    THUMB_AREA_H     = 210     # vertical pixels reserved at the bottom for
                               # the tank thumbnail (see UIManager rendering)
    THUMB_PADDING    = 6       # margin around the thumbnail image

    def __init__(self, x, y, w, h, show_thumb_area=True):
        self.x, self.y = int(x), int(y)
        self.w, self.h = int(w), int(h)
        self.show_thumb_area = bool(show_thumb_area)
        self.roots     = []          # list[UITreeNode]
        self.scroll_y  = 0           # pixels (>=0)
        self.hover_idx = -1          # index into the flat list
        self.on_select        = None  # callable(UITreeNode) -> None
        self.on_hover_change  = None  # callable(UITreeNode_or_None) -> None
        self._last_hover_node = None  # used to fire on_hover_change
        # Persistent selection: the leaf node the user most recently
        # clicked (and on_select was fired for).  Tracked by node
        # reference so collapse/expand/scroll don't lose it.  Drawn
        # with a burnt-orange highlight in _render_tree.  None until
        # the first selection.
        self.selected_node    = None

        # Tank thumbnail state (textures owned/managed by the Viewer).
        # Either pair may be None.  When a hover thumb is set it takes
        # priority over the loaded thumb on screen.
        self.loaded_thumb_tex = None
        self.loaded_thumb_w   = 0
        self.loaded_thumb_h   = 0
        self.hover_thumb_tex  = None
        self.hover_thumb_w    = 0
        self.hover_thumb_h    = 0

        # Vehicle-class icons rendered to the LEFT of each leaf row.
        # Populated by Viewer from gui-part*.pkg (24x24 icons).  Keys are
        # the same strings PkgExtractor.list_vehicle_xmls returns for
        # vclass: 'lightTank' / 'mediumTank' / 'heavyTank' / 'AT-SPG' /
        # 'SPG'.  Values are (tex_id, w, h) triples; missing classes are
        # simply skipped at draw time.
        self.class_icons = {}

        # Display-name labels rendered ABOVE the thumbnail image.
        # Loaded/hover are tracked in lock-step with the textures above so
        # the label changes whenever the picture changes.
        self.loaded_thumb_name = ''
        self.hover_thumb_name  = ''
        # Cached label textures + the strings they were built for, so
        # we only re-render the texture when the string actually changes.
        self._loaded_name_tex = None
        self._loaded_name_str = None
        self._loaded_name_w   = 0
        self._loaded_name_h   = 0
        self._hover_name_tex  = None
        self._hover_name_str  = None
        self._hover_name_w    = 0
        self._hover_name_h    = 0

    # ---- structure ---------------------------------------------------
    def add_root(self, node):
        self.roots.append(node)

    def clear(self):
        self.roots = []
        self.scroll_y = 0
        self.hover_idx = -1
        self.selected_node = None    # selection points at a stale node otherwise

    def _flatten(self):
        """Yield (depth, node) for every currently-visible row."""
        def walk(node, depth):
            yield (depth, node)
            if node.expanded:
                for c in node.children:
                    yield from walk(c, depth + 1)
        for r in self.roots:
            yield from walk(r, 0)

    # ---- geometry / hit-testing -------------------------------------
    def hit(self, mx, my):
        return (self.x <= mx <= self.x + self.w
            and self.y <= my <= self.y + self.h)

    def _rows_h(self):
        """Height of the scrolling-rows region (excludes thumbnail area
        when show_thumb_area is True; otherwise the rows fill the panel)."""
        if not self.show_thumb_area:
            return max(0, self.h)
        return max(0, self.h - self.THUMB_AREA_H)

    def thumbnail_rect(self):
        """(x, y, w, h) of the thumbnail area at the bottom of the panel.
        Returns a zero-height rect when show_thumb_area is False."""
        if not self.show_thumb_area:
            return (self.x, self.y + self.h, self.w, 0)
        return (self.x,
                self.y + self._rows_h(),
                self.w,
                self.THUMB_AREA_H)

    def _hit_rows(self, mx, my):
        """True iff (mx,my) is inside the rows region (above the thumbnail)."""
        return (self.x <= mx <= self.x + self.w
            and self.y <= my <= self.y + self._rows_h())

    def _row_at(self, mx, my):
        """Return flat-list index for (mx,my) or -1.  Restricted to the
        rows region; clicks/hovers over the thumbnail return -1."""
        if not self._hit_rows(mx, my):
            return -1
        rel = (my - self.y) + self.scroll_y
        idx = rel // self.ROW_H
        flat = list(self._flatten())
        return idx if 0 <= idx < len(flat) else -1

    # ---- events ------------------------------------------------------
    def handle_click(self, mx, my):
        """Toggle a branch or invoke on_select for a leaf.  Returns True if
        the click was consumed by the tree."""
        idx = self._row_at(mx, my)
        if idx < 0:
            return self.hit(mx, my)   # eat clicks on empty tree area too
        flat = list(self._flatten())
        _depth, node = flat[idx]
        if node.children:
            node.expanded = not node.expanded
        elif self.on_select:
            # Persist the selection so the row stays highlighted in
            # burnt orange after on_select fires (the load may take a
            # while -- the user wants visual confirmation of which row
            # they clicked).
            self.selected_node = node
            self.on_select(node)
        return True

    def handle_scroll(self, mx, my, dy):
        """Mouse-wheel inside the panel scrolls the rows.
        dy > 0 = wheel up = scroll up.  Returns True if consumed."""
        if not self.hit(mx, my):
            return False
        self.scroll_y -= int(dy) * self.ROW_H * 3
        # Clamp against the rows-only region (thumbnail area is fixed)
        total_rows = sum(1 for _ in self._flatten())
        max_scroll = max(0, total_rows * self.ROW_H - self._rows_h())
        self.scroll_y = max(0, min(self.scroll_y, max_scroll))
        return True

    def update_hover(self, mx, my):
        self.hover_idx = self._row_at(mx, my)
        # Fire on_hover_change when the *node* identity changes (None when
        # we're off the rows or hovering a branch).
        node = None
        if self.hover_idx >= 0:
            flat = list(self._flatten())
            _depth, node = flat[self.hover_idx]
            if node.children:
                node = None       # only leaves drive the thumbnail preview
        if node is not self._last_hover_node:
            self._last_hover_node = node
            if self.on_hover_change:
                self.on_hover_change(node)

    # ---- rendering helpers (called by UIManager) --------------------
    def ensure_textures(self, make_tex):
        """Build label_tex on every node lazily.

        Args:
            make_tex: callable(text, color) -> (tex_id, w, h)  -- supplied
                      by UIManager so we share its font/_make_tex helper.
        """
        for _depth, node in self._iter_all():
            if node.label_tex is None:
                col = (235, 235, 240) if node.children else (200, 215, 235)
                node.label_tex, node.label_w, node.label_h = make_tex(
                    node.label, col)

    def _iter_all(self):
        """Yield every (depth, node) regardless of expanded state.
        Used for texture pre-build only."""
        def walk(node, depth):
            yield (depth, node)
            for c in node.children:
                yield from walk(c, depth + 1)
        for r in self.roots:
            yield from walk(r, 0)

    def cleanup(self):
        for _depth, node in self._iter_all():
            if node.label_tex:
                glDeleteTextures(1, [node.label_tex])
                node.label_tex = None
        # Thumbnail textures (uploaded by Viewer, ownership released here)
        for attr in ('loaded_thumb_tex', 'hover_thumb_tex',
                     '_loaded_name_tex', '_hover_name_tex'):
            tex = getattr(self, attr, None)
            if tex:
                glDeleteTextures(1, [tex])
                setattr(self, attr, None)
        # Class icons (uploaded by Viewer, ownership released here)
        for tex_id, _, _ in self.class_icons.values():
            if tex_id:
                glDeleteTextures(1, [tex_id])
        self.class_icons = {}


# ============================================================================
# UIConfirmDialog  --  modal "Load tank?" prompt with 'crashed' checkbox
# ============================================================================

class UIConfirmDialog:
    """Centred modal dialog for confirming a tank load.

    Public API:
        show(title, on_confirm)  -- title is a short label (e.g. tank name);
                                    on_confirm(crashed: bool) is called when
                                    the user clicks Load.  Click Cancel or
                                    outside the dialog box -> dismissed.
        active                   -- True while visible
        handle_click(mx,my)      -- returns True if the dialog consumed the
                                    click (always True while active = modal)
        render(...)              -- draws the backdrop + box + widgets
    """

    BOX_W = 360
    BOX_H = 170
    BTN_W = 90
    BTN_H = 28

    def __init__(self):
        self.active        = False
        self.title         = ''
        self.title_tex     = None
        self.title_w = self.title_h = 0
        self.crashed       = False
        self.on_confirm    = None

        # Hover flags (set during update_hover)
        self.hover_load    = False
        self.hover_cancel  = False

        # Cached geometry from last render (window-relative, recomputed each frame)
        self._box_x = 0
        self._box_y = 0

    # ---- show / hide -------------------------------------------------
    def show(self, title, on_confirm, make_tex):
        self.active     = True
        self.title      = title
        self.crashed    = False
        self.on_confirm = on_confirm
        # Title texture is rebuilt every show (titles change)
        if self.title_tex:
            glDeleteTextures(1, [self.title_tex])
        self.title_tex, self.title_w, self.title_h = make_tex(
            title, (240, 245, 255))

    def hide(self):
        self.active = False
        self.on_confirm = None

    # ---- geometry ----------------------------------------------------
    def _layout(self, window_w, window_h):
        self._box_x = (window_w - self.BOX_W) // 2
        self._box_y = (window_h - self.BOX_H) // 2

    def _box_rect(self):
        return (self._box_x, self._box_y, self.BOX_W, self.BOX_H)

    def _crashed_cb_rect(self):
        return (self._box_x + 24, self._box_y + 70, 14, 14)

    def _load_btn_rect(self):
        return (self._box_x + self.BOX_W - self.BTN_W * 2 - 24,
                self._box_y + self.BOX_H - self.BTN_H - 16,
                self.BTN_W, self.BTN_H)

    def _cancel_btn_rect(self):
        return (self._box_x + self.BOX_W - self.BTN_W - 16,
                self._box_y + self.BOX_H - self.BTN_H - 16,
                self.BTN_W, self.BTN_H)

    @staticmethod
    def _hit(rect, mx, my):
        x, y, w, h = rect
        return x <= mx <= x + w and y <= my <= y + h

    # ---- events ------------------------------------------------------
    def handle_click(self, mx, my):
        if not self.active:
            return False
        if self._hit(self._crashed_cb_rect(), mx, my):
            self.crashed = not self.crashed
            return True
        if self._hit(self._load_btn_rect(), mx, my):
            cb = self.on_confirm
            crashed = self.crashed
            self.hide()
            if cb:
                cb(crashed)
            return True
        if self._hit(self._cancel_btn_rect(), mx, my):
            self.hide()
            return True
        # Click outside the dialog box -> cancel
        if not self._hit(self._box_rect(), mx, my):
            self.hide()
        return True   # always consume clicks while modal

    def update_hover(self, mx, my):
        if not self.active:
            return
        self.hover_load   = self._hit(self._load_btn_rect(),   mx, my)
        self.hover_cancel = self._hit(self._cancel_btn_rect(), mx, my)

    def cleanup(self):
        if self.title_tex:
            glDeleteTextures(1, [self.title_tex])
            self.title_tex = None


# ============================================================================
# UILoadTankDialog  --  per-skin Load picker with chassis/turret/gun radios
# ============================================================================

class UILoadTankDialog:
    """Modal dialog shown when the user clicks a tank in the tree.

    The dialog lists one block per available skin (a Default block is
    always present; additional blocks come from <hull>/<models>/<sets>).
    Each block has three radio columns -- Chassis / Turret / Gun -- with
    the tank's "best" pick pre-selected, and two action buttons:
        [Load]          -- load undamaged with this skin + selection
        [Load Damaged]  -- load destroyed/crashed variant
    A single Cancel button at the bottom dismisses the dialog.

    Public API:
        show(title, options, best, on_load, make_tex)
            options : dict from VehicleXMLLoader.list_options()
            best    : same dict (so 'best' fields stay in one place)
            on_load : callback(skin_or_None, chassis, turret, gun, damaged)
        hide()
        active                -- True while visible
        handle_click(mx, my)  -- always consumes while modal
        update_hover(mx, my)
        render(...)
    """

    # Layout constants
    BOX_W            = 760
    TITLE_H          = 28
    BLOCK_HEADER_H   = 22       # 'Default' / 'Skin: <name>' strip
    COL_HEADER_H     = 18       # 'Chassis' / 'Turret' / 'Gun' label row
    BLOCK_PAD        = 10
    BLOCK_GAP        = 8
    RADIO_SIZE       = 12
    RADIO_ROW_H      = 18
    BTN_W            = 110
    BTN_H            = 26
    CANCEL_W         = 80
    PAD              = 14
    COL_GAP          = 8

    def __init__(self):
        self.active     = False
        self.title      = ''
        self.on_load    = None        # callback(skin, chassis, turret, gun, damaged)

        # Per-skin block data, populated by show().
        # Each entry: {
        #   'skin':       str|None,       # None = Default
        #   'chassis':    [tag, ...],
        #   'turrets':    [tag, ...],
        #   'guns':       {turret_tag: [gun_tag, ...]},
        #   'sel_ch':     str,            # currently-selected chassis tag
        #   'sel_tu':     str,            # currently-selected turret tag
        #   'sel_gu':     str,            # currently-selected gun tag
        # }
        self.blocks = []

        # Hover state
        self.hover_cancel    = False
        self.hover_load_idx  = -1     # index of block whose Load is hovered
        self.hover_dmg_idx   = -1     # index of block whose Load-Damaged is hovered

        # Cached label textures (built lazily)
        self._title_tex = None
        self._title_w = self._title_h = 0
        self._cancel_tex = None
        self._cancel_w = self._cancel_h = 0
        self._load_tex = None
        self._load_w = self._load_h = 0
        self._dmg_tex = None
        self._dmg_w = self._dmg_h = 0
        self._col_label_tex = {}      # 'Chassis'/'Turret'/'Gun' -> (tex, w, h)
        self._block_header_tex = []   # per-block: tex, w, h
        self._radio_label_tex = []    # per-block: dict[(group, tag)] -> (tex,w,h)

        # Status line shown along the bottom strip while a load is in
        # progress.  Updated by Viewer.set_load_status() during
        # load_vehicle so the user gets visible feedback ("Parsing
        # Hull primitives...") instead of a frozen dialog.
        self.status_text     = ''
        self._status_tex     = None
        self._status_w       = 0
        self._status_h       = 0
        self._status_str     = None     # cache: last text the texture was built for

        # Layout cache
        self._box_x = self._box_y = self._box_h = 0

    # ---- show / hide -------------------------------------------------
    def show(self, title, options, on_load, make_tex):
        """Open the dialog.

        Args:
            title    (str): dialog title bar text (e.g. 'usa / A14_T30')
            options  (dict): VehicleXMLLoader.list_options() output
            on_load  (callable): (skin_or_None, chassis_tag, turret_tag,
                                  gun_tag, damaged) -> None
            make_tex (callable): UIManager._make_tex (for label rendering)
        """
        self.active  = True
        self.title   = title
        self.on_load = on_load

        # Free any per-show cached label textures
        self._free_per_show_tex()

        # Build blocks.  The Default block is always first; then one block
        # per skin name in options['skins'].
        skin_names = [None] + list(options.get('skins') or [])
        chassis    = list(options.get('chassis') or [])
        turrets    = list(options.get('turrets') or [])
        guns_map   = options.get('guns_per_turret') or {}
        best_ch    = options.get('best_chassis') or (chassis[-1] if chassis else '')
        best_tu    = options.get('best_turret')  or (turrets[-1] if turrets else '')
        best_gu_pt = options.get('best_gun_per_turret') or {}
        best_gu    = best_gu_pt.get(best_tu, '')

        self.blocks = []
        for skin in skin_names:
            self.blocks.append({
                'skin':    skin,
                'chassis': chassis,
                'turrets': turrets,
                'guns':    guns_map,
                'sel_ch':  best_ch,
                'sel_tu':  best_tu,
                'sel_gu':  best_gu,
            })
        self._block_header_tex = [None] * len(self.blocks)
        self._radio_label_tex  = [{} for _ in self.blocks]

        # Title texture
        self._title_tex, self._title_w, self._title_h = make_tex(
            f"Load: {title}", (235, 240, 250))
        self._make_tex = make_tex   # stash for lazy radio-label rebuilds

    def hide(self):
        self.active  = False
        self.on_load = None

    # ---- status -------------------------------------------------------
    def set_status(self, text, make_tex=None):
        """Update the bottom-strip status text.

        Args:
            text     (str): new status (empty string clears the line)
            make_tex (callable | None): UIManager._make_tex; required on
                first call after a value change.  Falls back to the
                stash from show() when None.
        """
        text = text or ''
        if text == self._status_str:
            return
        if self._status_tex:
            try:
                glDeleteTextures(1, [self._status_tex])
            except Exception:
                pass
            self._status_tex = None
        self.status_text = text
        self._status_str = text
        if not text:
            return
        builder = make_tex or getattr(self, '_make_tex', None)
        if builder is None:
            return
        self._status_tex, self._status_w, self._status_h = builder(
            text, (220, 220, 230))

    def _free_per_show_tex(self):
        """Free textures that vary per show() call."""
        for tex in [self._title_tex] \
                + [t for t in self._block_header_tex if t] \
                + [t for d in self._radio_label_tex
                     for t,_,_ in d.values() if t]:
            if tex:
                glDeleteTextures(1, [tex])
        self._title_tex = None
        self._block_header_tex = []
        self._radio_label_tex  = []

    # ---- geometry ----------------------------------------------------
    def _block_rows(self, block):
        """Number of radio rows in a block (= max of #chassis, #turrets, #guns
        for the SELECTED turret)."""
        nc = len(block['chassis'])
        nt = len(block['turrets'])
        ng = len(block['guns'].get(block['sel_tu'], []))
        return max(nc, nt, ng, 1)

    def _block_h(self, block):
        return (self.BLOCK_HEADER_H
                + self.COL_HEADER_H
                + self._block_rows(block) * self.RADIO_ROW_H
                + self.BTN_H + self.BLOCK_PAD * 2 + 6)

    def _layout(self, window_w, window_h):
        body_h = sum(self._block_h(b) for b in self.blocks) \
                 + (len(self.blocks) - 1) * self.BLOCK_GAP \
                 + self.TITLE_H + self.PAD * 2 + self.BTN_H + 12
        # Cap height to window; user can't scroll yet -- if too many skins
        # appear in some future tank we'll just clip the bottom blocks.
        self._box_h = max(180, min(body_h, window_h - 40))
        self._box_x = (window_w - self.BOX_W) // 2
        self._box_y = (window_h - self._box_h) // 2

    def _box_rect(self):
        return (self._box_x, self._box_y, self.BOX_W, self._box_h)

    def _block_rect(self, idx):
        """Outer rect of block idx within the dialog body."""
        x = self._box_x + self.PAD
        w = self.BOX_W - self.PAD * 2
        y = self._box_y + self.TITLE_H + self.PAD
        for i in range(idx):
            y += self._block_h(self.blocks[i]) + self.BLOCK_GAP
        return (x, y, w, self._block_h(self.blocks[idx]))

    def _column_x(self, block_idx, col):
        """X position of column 0/1/2 (chassis / turret / gun)."""
        bx, _, bw, _ = self._block_rect(block_idx)
        col_w = (bw - self.BLOCK_PAD * 2 - self.COL_GAP * 2) // 3
        return bx + self.BLOCK_PAD + col * (col_w + self.COL_GAP), col_w

    def _radio_rect(self, block_idx, col, row):
        """Rect of the radio-row hit area (full row, not just the box)."""
        bx, by, bw, bh = self._block_rect(block_idx)
        cx, cw = self._column_x(block_idx, col)
        ry = (by + self.BLOCK_HEADER_H + self.COL_HEADER_H
              + row * self.RADIO_ROW_H)
        return (cx, ry, cw, self.RADIO_ROW_H)

    def _load_btn_rect(self, block_idx):
        bx, by, bw, bh = self._block_rect(block_idx)
        x = bx + bw - self.BTN_W * 2 - self.BLOCK_PAD - 6
        y = by + bh - self.BTN_H - self.BLOCK_PAD
        return (x, y, self.BTN_W, self.BTN_H)

    def _dmg_btn_rect(self, block_idx):
        bx, by, bw, bh = self._block_rect(block_idx)
        x = bx + bw - self.BTN_W - self.BLOCK_PAD
        y = by + bh - self.BTN_H - self.BLOCK_PAD
        return (x, y, self.BTN_W, self.BTN_H)

    def _cancel_rect(self):
        return (self._box_x + self.BOX_W - self.CANCEL_W - self.PAD,
                self._box_y + self._box_h - self.BTN_H - 10,
                self.CANCEL_W, self.BTN_H)

    @staticmethod
    def _hit(rect, mx, my):
        x, y, w, h = rect
        return x <= mx <= x + w and y <= my <= y + h

    # ---- events ------------------------------------------------------
    def handle_click(self, mx, my):
        if not self.active:
            return False

        # Cancel
        if self._hit(self._cancel_rect(), mx, my):
            self.hide()
            return True

        # Per-block hit testing
        for idx, block in enumerate(self.blocks):
            # Load
            if self._hit(self._load_btn_rect(idx), mx, my):
                self._fire_load(idx, damaged=False)
                return True
            # Load Damaged
            if self._hit(self._dmg_btn_rect(idx), mx, my):
                self._fire_load(idx, damaged=True)
                return True
            # Radio rows -- chassis (col 0), turrets (col 1), guns (col 2)
            for row, tag in enumerate(block['chassis']):
                if self._hit(self._radio_rect(idx, 0, row), mx, my):
                    block['sel_ch'] = tag
                    return True
            for row, tag in enumerate(block['turrets']):
                if self._hit(self._radio_rect(idx, 1, row), mx, my):
                    if block['sel_tu'] != tag:
                        block['sel_tu'] = tag
                        # Reset gun selection to last child of this turret
                        gun_list = block['guns'].get(tag, [])
                        block['sel_gu'] = gun_list[-1] if gun_list else ''
                        # Layout depends on max(chassis,turret,gun)
                    return True
            gun_list = block['guns'].get(block['sel_tu'], [])
            for row, tag in enumerate(gun_list):
                if self._hit(self._radio_rect(idx, 2, row), mx, my):
                    block['sel_gu'] = tag
                    return True

        # Click outside the box -> cancel; click anywhere inside but not on a
        # widget is a no-op (don't dismiss accidentally).
        if not self._hit(self._box_rect(), mx, my):
            self.hide()
        return True   # always consume while modal

    def _fire_load(self, idx, damaged):
        block = self.blocks[idx]
        cb = self.on_load
        skin    = block['skin']
        chassis = block['sel_ch']
        turret  = block['sel_tu']
        gun     = block['sel_gu']
        # Stay ACTIVE during the callback so the bottom status line is
        # visible while the load runs.  Hide afterwards so the dialog
        # disappears once the tank is on screen.
        if cb:
            try:
                cb(skin, chassis, turret, gun, damaged)
            finally:
                self.hide()
        else:
            self.hide()

    def update_hover(self, mx, my):
        if not self.active:
            return
        self.hover_cancel   = self._hit(self._cancel_rect(), mx, my)
        self.hover_load_idx = -1
        self.hover_dmg_idx  = -1
        for idx in range(len(self.blocks)):
            if self._hit(self._load_btn_rect(idx), mx, my):
                self.hover_load_idx = idx
            if self._hit(self._dmg_btn_rect(idx), mx, my):
                self.hover_dmg_idx = idx

    def cleanup(self):
        self._free_per_show_tex()
        for tex in (self._cancel_tex, self._load_tex, self._dmg_tex):
            if tex:
                glDeleteTextures(1, [tex])
        self._cancel_tex = self._load_tex = self._dmg_tex = None
        for tex, _, _ in self._col_label_tex.values():
            if tex:
                glDeleteTextures(1, [tex])
        self._col_label_tex = {}


# ============================================================================
# UIPathsDialog  --  modal "Set Paths" form (game packages / res_mods / lookup)
# ============================================================================

class UIPathsDialog:
    """Centred modal dialog for editing the three persistent paths.

    Path values are owned by the dialog while it's active; on Save the
    on_confirm callback fires with a dict and the dialog hides.  Editing
    is done via system folder/file pickers (tkinter.filedialog), invoked
    when the user clicks the per-row Browse button.

    Public API:
        show(initial, on_confirm)
            initial      -- dict {'pkg_dir', 'res_mods', 'lookup_xml'}
            on_confirm   -- callable(values_dict) -> None
        hide()
        active           -- True while visible
        handle_click(mx, my)  -- returns True if the click was consumed
        update_hover(mx, my)
        render(...)
    """

    BOX_W       = 620
    BOX_H       = 280
    ROW_H       = 38
    LABEL_W     = 150
    BROWSE_W    = 76
    BTN_W       = 90
    BTN_H       = 28
    PAD         = 16

    # Field definitions: key, label, picker mode (folder|file)
    _FIELDS = [
        ('pkg_dir',    'Game packages:',     'folder'),
        ('res_mods',   'Res_mods/<version>:', 'folder'),
        ('lookup_xml', 'TheItemList.xml:',   'file'),
    ]

    def __init__(self):
        self.active     = False
        self.values     = {k: '' for k, _, _ in self._FIELDS}
        self.on_confirm = None

        # Hover flags
        self.hover_save     = False
        self.hover_cancel   = False
        self.hover_browse   = [False, False, False]   # one per row

        # Cached label / value textures (rebuilt when string changes)
        self._label_tex   = [None] * len(self._FIELDS)
        self._label_w     = [0]    * len(self._FIELDS)
        self._label_h     = [0]    * len(self._FIELDS)
        self._value_tex   = [None] * len(self._FIELDS)
        self._value_str   = [None] * len(self._FIELDS)
        self._value_w     = [0]    * len(self._FIELDS)
        self._value_h     = [0]    * len(self._FIELDS)

        self._title_tex = None
        self._title_w = self._title_h = 0

        # Cached static button text textures
        self._save_tex = self._cancel_tex = self._browse_tex = None
        self._save_w = self._save_h = 0
        self._cancel_w = self._cancel_h = 0
        self._browse_w = self._browse_h = 0

        # Layout cache (recomputed each frame)
        self._box_x = self._box_y = 0

    # ---- show / hide -------------------------------------------------
    def show(self, initial, on_confirm):
        self.active     = True
        self.on_confirm = on_confirm
        self.values     = {k: (initial.get(k) or '') for k, _, _ in self._FIELDS}
        # Force value textures to rebuild on next render
        self._value_str = [None] * len(self._FIELDS)

    def hide(self):
        self.active = False
        self.on_confirm = None

    # ---- geometry ----------------------------------------------------
    def _layout(self, window_w, window_h):
        self._box_x = (window_w - self.BOX_W) // 2
        self._box_y = (window_h - self.BOX_H) // 2

    def _box_rect(self):
        return (self._box_x, self._box_y, self.BOX_W, self.BOX_H)

    def _row_rect(self, idx):
        """Rect of the value-display field for row idx (left of Browse)."""
        x  = self._box_x + self.PAD + self.LABEL_W
        y0 = self._box_y + 50    # below title strip + title pad
        y  = y0 + idx * self.ROW_H
        w  = self.BOX_W - self.PAD * 2 - self.LABEL_W - self.BROWSE_W - 6
        return (x, y, w, 22)

    def _browse_rect(self, idx):
        rx, ry, rw, rh = self._row_rect(idx)
        return (rx + rw + 6, ry, self.BROWSE_W, rh)

    def _save_btn_rect(self):
        return (self._box_x + self.BOX_W - self.BTN_W * 2 - self.PAD - 8,
                self._box_y + self.BOX_H - self.BTN_H - 14,
                self.BTN_W, self.BTN_H)

    def _cancel_btn_rect(self):
        return (self._box_x + self.BOX_W - self.BTN_W - self.PAD,
                self._box_y + self.BOX_H - self.BTN_H - 14,
                self.BTN_W, self.BTN_H)

    @staticmethod
    def _hit(rect, mx, my):
        x, y, w, h = rect
        return x <= mx <= x + w and y <= my <= y + h

    # ---- system folder / file picker --------------------------------
    @staticmethod
    def _pick_path(mode, current):
        """Open a tkinter file/folder picker.  Returns the chosen path
        or '' if the user cancelled.  Tk root is created hidden and
        destroyed immediately so the pygame window stays focused."""
        try:
            import tkinter as tk
            from tkinter import filedialog
        except ImportError:
            print("[ui] tkinter not available -- cannot open file picker")
            return ''

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        try:
            if mode == 'folder':
                chosen = filedialog.askdirectory(
                    initialdir=current or None,
                    title='Select folder',
                    mustexist=True,
                )
            else:
                chosen = filedialog.askopenfilename(
                    initialdir=(current and __import__('os').path.dirname(current)) or None,
                    title='Select file',
                    filetypes=[('XML files', '*.xml'), ('All files', '*.*')],
                )
        finally:
            root.destroy()
        return chosen or ''

    # ---- events ------------------------------------------------------
    def handle_click(self, mx, my):
        if not self.active:
            return False

        # Browse buttons (per row)
        for i, (key, _, mode) in enumerate(self._FIELDS):
            if self._hit(self._browse_rect(i), mx, my):
                chosen = self._pick_path(mode, self.values.get(key, ''))
                if chosen:
                    self.values[key] = chosen
                    # Force this row's value texture to rebuild
                    self._value_str[i] = None
                return True

        # Save
        if self._hit(self._save_btn_rect(), mx, my):
            cb     = self.on_confirm
            values = dict(self.values)
            self.hide()
            if cb:
                cb(values)
            return True

        # Cancel
        if self._hit(self._cancel_btn_rect(), mx, my):
            self.hide()
            return True

        # Click outside the box -> dismiss (treated as cancel)
        if not self._hit(self._box_rect(), mx, my):
            self.hide()
        return True   # always consume while modal

    def update_hover(self, mx, my):
        if not self.active:
            return
        self.hover_save   = self._hit(self._save_btn_rect(),   mx, my)
        self.hover_cancel = self._hit(self._cancel_btn_rect(), mx, my)
        for i in range(len(self._FIELDS)):
            self.hover_browse[i] = self._hit(self._browse_rect(i), mx, my)

    def cleanup(self):
        for tex in ([self._title_tex, self._save_tex,
                     self._cancel_tex, self._browse_tex]
                    + self._label_tex + self._value_tex):
            if tex:
                glDeleteTextures(1, [tex])
        self._title_tex = self._save_tex = None
        self._cancel_tex = self._browse_tex = None
        self._label_tex = [None] * len(self._FIELDS)
        self._value_tex = [None] * len(self._FIELDS)


# ============================================================================
# UITabBar  --  horizontal strip of clickable numeric tabs
# ============================================================================

class UITabBar:
    """Strip of fixed-width tabs with a single active selection.

    Used above the right-hand tank-browser tree to filter by tier
    (labels '1' .. '11').  Tab labels are arbitrary strings; the widget
    just renders them and reports the active index via on_change.

    Public:
        labels       -- list[str], read-only after construction
        active_index -- int, currently-selected tab
        on_change    -- callable(new_index) -> None, fired on click change
        hit(mx, my)  -- True iff the cursor is in the bar's bounds
    """

    HEIGHT = 22

    def __init__(self, x, y, w, labels):
        self.x      = int(x)
        self.y      = int(y)
        self.w      = int(w)
        self.labels = list(labels)
        self.active_index = 0
        self.on_change    = None

        # Lazy label textures, hover state per tab
        self._label_tex = [None] * len(self.labels)
        self._label_w   = [0]    * len(self.labels)
        self._label_h   = [0]    * len(self.labels)
        self.hover_idx  = -1

        # Index of the tab whose tree is currently being built at startup.
        # Highlighted in yellow during the build loop in
        # Viewer._build_all_tier_trees so the user sees progress.  -1 = idle.
        self.building_index = -1

    # ---- geometry ---------------------------------------------------
    def hit(self, mx, my):
        return (self.x <= mx <= self.x + self.w
            and self.y <= my <= self.y + self.HEIGHT)

    def _tab_rect(self, idx):
        """Rect of tab `idx` -- tabs are evenly distributed across w."""
        n  = max(1, len(self.labels))
        # Use floats so rounding error doesn't accumulate; clamp last.
        x0 = self.x + (idx     * self.w) // n
        x1 = self.x + ((idx+1) * self.w) // n
        return (x0, self.y, x1 - x0, self.HEIGHT)

    def _hit_tab(self, mx, my):
        if not self.hit(mx, my):
            return -1
        for i in range(len(self.labels)):
            x, y, w, h = self._tab_rect(i)
            if x <= mx < x + w:
                return i
        return -1

    # ---- events -----------------------------------------------------
    def handle_click(self, mx, my):
        i = self._hit_tab(mx, my)
        if i < 0:
            return False
        if i != self.active_index:
            self.active_index = i
            if self.on_change:
                try:
                    self.on_change(i)
                except Exception as exc:
                    print(f"[ui] tab on_change failed: {exc}")
        return True

    def update_hover(self, mx, my):
        self.hover_idx = self._hit_tab(mx, my)

    def cleanup(self):
        for tex in self._label_tex:
            if tex:
                glDeleteTextures(1, [tex])
        self._label_tex = [None] * len(self.labels)


# ============================================================================
# UIMeshWindow  --  persistent floating panel for per-mesh visibility toggles
# ============================================================================

class UIMeshRow:
    """One row in the mesh-visibility window.  Owns its label texture."""

    def __init__(self, mesh_index, label):
        self.mesh_index = int(mesh_index)
        self.label      = label
        self.visible    = True       # default when newly populated
        self.label_tex  = None
        self.label_w    = 0
        self.label_h    = 0


class UIMeshWindow:
    """Persistent (non-modal) window listing every loaded mesh as a
    checkbox.  Clicking a checkbox flips that mesh's visibility flag --
    the renderer skips invisible meshes, so the change is immediate.

    Always reset to all-visible when populate() is called (called from
    the viewer at the end of load_vehicle / load_mesh).  The window's
    open/closed state survives across loads, but the row contents are
    rebuilt each time.

    Public API:
        populate(rows, make_tex)
            rows     : list of (mesh_index, label) tuples
            make_tex : UIManager._make_tex (used to render row labels)
        toggle()             -- show/hide the window
        active               -- True when shown
        on_toggle            -- callable(mesh_index, new_visible) set by
                                Viewer to mirror the flag onto the Mesh
        handle_click(mx, my) -- True if click consumed
        handle_scroll(mx, my, dy) -- True if scroll consumed
        render(...)          -- draws the window
    """

    BOX_W            = 300
    TITLE_H          = 24
    ROW_H            = 20
    CHECKBOX_SIZE    = 14
    PAD              = 8
    MAX_VISIBLE_ROWS = 28      # vertical-scroll above this count

    def __init__(self, x=0, y=0):
        self.active   = False
        self.x, self.y = int(x), int(y)
        self.rows     = []          # list[UIMeshRow]
        self.scroll_y = 0
        self.on_toggle = None

        # Cached static textures
        self._title_tex = None
        self._title_w   = 0
        self._title_h   = 0
        self._close_tex = None
        self._close_w   = 0
        self._close_h   = 0
        self.hover_close = False

    # ---- show / hide / populate -------------------------------------
    def populate(self, rows, make_tex):
        """Replace the row list with fresh entries.  All rows start
        visible.  Free old textures, build new ones."""
        # Free outgoing labels
        for r in self.rows:
            if r.label_tex:
                glDeleteTextures(1, [r.label_tex])
        self.rows     = []
        self.scroll_y = 0
        for mesh_index, label in rows:
            row = UIMeshRow(mesh_index, label)
            row.label_tex, row.label_w, row.label_h = make_tex(
                label, (220, 225, 235))
            self.rows.append(row)
        # Static title / close-glyph textures (built once, reused)
        if self._title_tex is None:
            self._title_tex, self._title_w, self._title_h = make_tex(
                "Meshes (visibility)", (240, 245, 255))
        if self._close_tex is None:
            self._close_tex, self._close_w, self._close_h = make_tex(
                "x", (240, 240, 245))

    def toggle(self):
        self.active = not self.active

    def show(self):  self.active = True
    def hide(self):  self.active = False

    # ---- geometry ---------------------------------------------------
    def _content_h(self):
        n = min(len(self.rows), self.MAX_VISIBLE_ROWS)
        return self.TITLE_H + max(1, n) * self.ROW_H + self.PAD * 2

    def hit(self, mx, my):
        if not self.active:
            return False
        return (self.x <= mx <= self.x + self.BOX_W
            and self.y <= my <= self.y + self._content_h())

    def _close_rect(self):
        sz = self.TITLE_H - 6
        return (self.x + self.BOX_W - sz - 4, self.y + 3, sz, sz)

    def _row_rect(self, idx):
        """Rect for the whole clickable row at flat index `idx`."""
        y0 = self.y + self.TITLE_H + self.PAD
        return (self.x + self.PAD, y0 + idx * self.ROW_H,
                self.BOX_W - self.PAD * 2, self.ROW_H)

    @staticmethod
    def _hit(rect, mx, my):
        x, y, w, h = rect
        return x <= mx <= x + w and y <= my <= y + h

    # ---- events -----------------------------------------------------
    def handle_click(self, mx, my):
        """Toggle a row checkbox or close the window.  Returns True if
        the click was consumed (caller should stop processing)."""
        if not self.active:
            return False
        if not self.hit(mx, my):
            return False

        if self._hit(self._close_rect(), mx, my):
            self.hide()
            return True

        # Row hit-test (account for scroll)
        rel_y = (my - self.y - self.TITLE_H - self.PAD) + self.scroll_y
        if rel_y < 0:
            return True   # title strip click -- consume but do nothing
        idx = rel_y // self.ROW_H
        if 0 <= idx < len(self.rows):
            row = self.rows[idx]
            row.visible = not row.visible
            if self.on_toggle:
                try:
                    self.on_toggle(row.mesh_index, row.visible)
                except Exception as exc:
                    print(f"[ui] mesh-window on_toggle failed: {exc}")
        return True

    def handle_scroll(self, mx, my, dy):
        if not self.active or not self.hit(mx, my):
            return False
        self.scroll_y -= int(dy) * self.ROW_H * 3
        max_scroll = max(0,
            len(self.rows) * self.ROW_H - self.MAX_VISIBLE_ROWS * self.ROW_H)
        self.scroll_y = max(0, min(self.scroll_y, max_scroll))
        return True

    def update_hover(self, mx, my):
        self.hover_close = (self.active
                            and self._hit(self._close_rect(), mx, my))

    def cleanup(self):
        for r in self.rows:
            if r.label_tex:
                glDeleteTextures(1, [r.label_tex])
        self.rows = []
        for tex in (self._title_tex, self._close_tex):
            if tex:
                glDeleteTextures(1, [tex])
        self._title_tex = self._close_tex = None


# ============================================================================
# UIManager
# ============================================================================

class UIManager:
    """Renders the full top-bar overlay (buttons, sliders, checkboxes).

    Constants
    ---------
    BAR_HEIGHT     = 84   total bar height in pixels
    BUTTON_PADDING =  8   left margin for first button
    BUTTON_SPACING =  6   gap between buttons

    Slider column positions (pixels from left edge of window):
    SLIDER_LABEL_X =  8   left edge of label text
    SLIDER_TRACK_X = 58   left edge of slider track
    SLIDER_TRACK_W = 160  track width
    SLIDER_VALUE_X = 226  left edge of value text
    SLIDER_CB_X    = 278  left edge of checkbox square
    SLIDER_CBLBL_X = 296  left edge of "Invert" label
    """

    # No top menu bar in the new layout -- all controls live in the
    # left and right side panels, the 3D viewport spans the full
    # window height between them.
    BAR_HEIGHT     = 0
    BUTTON_PADDING =  8
    BUTTON_SPACING =  6

    # Legacy slider column constants (still referenced in some places;
    # actual widget positions are now set per-instance by Viewer._layout_widgets).
    SLIDER_LABEL_X = 8
    SLIDER_TRACK_X = 58
    SLIDER_TRACK_W = 160
    SLIDER_VALUE_X = 226
    SLIDER_CB_X    = 278
    SLIDER_CBLBL_X = 296
    _SLIDER_CY     = [44, 69, 94, 119, 144, 169]

    # Width (px) of the always-visible spine on the left edge that holds
    # the info-panel collapse/expand chevron.  When the info panel is
    # collapsed, the panel itself becomes 0 px wide and only this spine
    # is visible -- click it anywhere to expand again.
    COLLAPSE_TAB_W = 18

    def __init__(self):
        self.shader     = UIShader()
        self.buttons    = []
        self.sliders    = []
        self.checkboxes = []
        self.font       = pygame.font.SysFont('Segoe UI', 13)
        self._active_slider = None

        # Side panels + modal dialogs (created on demand by Viewer)
        self.tree         = None     # right-hand tank-browser UITreeView
        self.tab_bar      = None     # tier-filter tabs above the tree
        self.info_panel   = None     # left-hand stats/description UITreeView
        self.mesh_window  = UIMeshWindow()          # persistent mesh visibility toggle window
        self.dialog       = UIConfirmDialog()       # legacy, kept for compat
        self.load_dialog  = UILoadTankDialog()      # tank-load picker
        self.paths_dialog = UIPathsDialog()

        # Info-panel collapse state.  Viewer initialises this from config
        # at startup and registers `on_info_toggle` so a click on the
        # spine can re-run the layout pass and persist the new state.
        self.info_collapsed     = False
        self.info_panel_full_w  = 0      # set by Viewer when the panel is built
        self.on_info_toggle     = None   # callable() -- viewer-level relayout hook

        # Chevron glyph texture (lazy: built on first render, rebuilt
        # when info_collapsed flips).  '<' shown when expanded (click to
        # collapse), '>' when collapsed (click to expand).
        self._chevron_tex   = None
        self._chevron_w     = 0
        self._chevron_h     = 0
        self._chevron_glyph = ''
        self._spine_hovered = False

        # Last-known render size (used by click/hit handlers that don't
        # receive height as a parameter -- the spine rect is height-aware).
        self._last_render_w = 0
        self._last_render_h = 0

        # Control-panel rectangles in absolute window coordinates.  Set by
        # Viewer._on_resize.  Used to (a) draw the dark control-panel
        # backgrounds in render() and (b) treat clicks/drags inside those
        # rects as UI interaction (so the camera doesn't pan when the user
        # is clicking around the controls).  Default zero means "no panel".
        self.left_controls_rect  = (0, 0, 0, 0)   # (x, y, w, h)
        self.right_controls_rect = (0, 0, 0, 0)

        # Dynamic quad VAO
        self.quad_vao = glGenVertexArrays(1)
        self.quad_vbo = glGenBuffers(1)
        glBindVertexArray(self.quad_vao)
        glBindBuffer(GL_ARRAY_BUFFER, self.quad_vbo)
        glBufferData(GL_ARRAY_BUFFER, 6 * 4 * 4, None, GL_DYNAMIC_DRAW)
        glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(0))
        glEnableVertexAttribArray(0)
        glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(8))
        glEnableVertexAttribArray(1)
        glBindVertexArray(0)

    # ------------------------------------------------------------------
    # Widget factories
    # ------------------------------------------------------------------

    def _make_tex(self, text, color=(240, 240, 240)):
        """Render text with self.font -> GLuint, (w, h)."""
        surf = self.font.render(text, True, color)
        data = pygame.image.tostring(surf, 'RGBA', False)
        w, h = surf.get_width(), surf.get_height()
        tid  = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, tid)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, data)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        glBindTexture(GL_TEXTURE_2D, 0)
        return tid, w, h

    def add_button(self, label, x, y, w, h, active=True, action=None):
        """Create and register a bar button.

        Args:
            label   (str)        : visible text
            x, y, w, h (int)     : screen-space rect
            active  (bool)       : initial toggle state (ignored for action btns)
            action  (callable|None):
                * None  -> toggle button.  Caller sets btn.attr; click flips
                          btn.active and the click is reported back.
                * callable -> action button.  Click calls action() directly.

        Returns:
            UIButton
        """
        btn = UIButton(label, x, y, w, h, active, action=action)
        btn.text_tex, btn.text_w, btn.text_h = self._make_tex(label)
        self.buttons.append(btn)
        return btn

    def add_slider(self, label, track_x, track_cy, track_w, value=0.5,
                   value_max=1.0, group_id=''):
        """Create and register a horizontal slider.

        Args:
            label     (str)  : static label shown to the left of the track
            track_x   (int)  : left edge of the track
            track_cy  (int)  : vertical centre of the track
            track_w   (int)  : track width in pixels
            value     (float): initial value 0.0-value_max
            value_max (float): upper bound of the slider (default 1.0)
            group_id  (str)  : collapsible-group tag.  Sliders with the
                               same group_id are shown/hidden together
                               by Viewer._layout_groups.

        Returns:
            UISlider
        """
        sl = UISlider(label, track_x, track_cy, track_w, value, value_max)
        sl.label_tex, sl.label_w, sl.label_h = self._make_tex(label, (200, 200, 200))
        sl.group_id = group_id
        self.sliders.append(sl)
        return sl

    def add_checkbox(self, label, x, y, size=14, checked=False, group_id=''):
        """Create and register a checkbox.

        Args:
            label    (str)  : text shown to the right of the box
            x, y     (int)  : top-left of the checkbox square
            size     (int)  : side length in pixels
            checked  (bool) : initial state
            group_id (str)  : collapsible-group tag (paired with sliders).

        Returns:
            UICheckbox
        """
        cb = UICheckbox(label, x, y, size, checked)
        cb.label_tex, cb.label_w, cb.label_h = self._make_tex(label, (200, 200, 200))
        cb.group_id = group_id
        self.checkboxes.append(cb)
        return cb

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def handle_mouse_down(self, mx, my):
        """Process a left-button-down event inside the bar.

        Order of priority:
            1. Modal dialog (if open, eats every click)
            2. Tree-view panel (if click is inside it)
            3. Bar widgets (checkboxes, sliders, buttons) as before

        Returns the toggled UIButton (so the caller can apply GL side-effects),
        or None for everything else.
        """
        # 1. Modal dialogs (paths first, then load, then legacy confirm)
        #    -- always eat the click while any modal is up.
        if self.paths_dialog.active:
            self.paths_dialog.handle_click(mx, my)
            return None
        if self.load_dialog.active:
            self.load_dialog.handle_click(mx, my)
            return None
        if self.dialog.active:
            self.dialog.handle_click(mx, my)
            return None

        # 2. Mesh-visibility window (non-modal but always wins inside its rect)
        if self.mesh_window.handle_click(mx, my):
            return None

        # 3. Tab bar above the tree -- check before the tree itself
        if self.tab_bar and self.tab_bar.hit(mx, my):
            self.tab_bar.handle_click(mx, my)
            return None

        # 4. Tree panel (right) / info panel (left)
        if self.tree and self.tree.hit(mx, my):
            self.tree.handle_click(mx, my)
            return None
        # Info-panel spine -- click anywhere on the strip to toggle.
        # Checked BEFORE info_panel.hit so a click at the spine's column
        # never falls into the panel content (which sits at the same x
        # range when expanded).
        if (self.info_panel_full_w > 0 and
                self.info_spine_hit(mx, my, self._last_render_h)):
            self.info_collapsed = not self.info_collapsed
            if self.on_info_toggle is not None:
                try:
                    self.on_info_toggle()
                except Exception as exc:
                    print(f"[ui] info-panel toggle hook failed: {exc}")
            return None
        if (self.info_panel and not self.info_collapsed
                and self.info_panel.hit(mx, my)):
            self.info_panel.handle_click(mx, my)
            return None

        # Checkboxes first (they're small targets).  Hidden ones (group
        # currently collapsed) are skipped so collapsed-section clicks
        # don't toggle phantom widgets.
        for cb in self.checkboxes:
            if not getattr(cb, 'visible', True):
                continue
            if cb.hit(mx, my):
                cb.checked = not cb.checked
                return None

        # Sliders -- clicking anywhere on the track jumps the handle
        for sl in self.sliders:
            if not getattr(sl, 'visible', True):
                continue
            if sl.hit(mx, my):
                sl._dragging    = True
                self._active_slider = sl
                sl.set_from_mouse(mx)
                return None

        # Bar buttons (toggle + action)
        for btn in self.buttons:
            if not getattr(btn, 'visible', True):
                continue
            if btn.contains(mx, my):
                if btn.action is not None:
                    # Action button -- invoke the callback directly,
                    # don't toggle, don't bubble up to caller.
                    try:
                        btn.action()
                    except Exception as exc:
                        print(f"[ui] button '{btn.label}' action failed: {exc}")
                    return None
                btn.active = not btn.active
                return btn

        return None

    def handle_mouse_wheel(self, mx, my, dy):
        """Forward a vertical wheel tick to the tree if the cursor is over it.

        Returns True if the wheel was consumed (caller should not zoom the
        camera), False otherwise.  While the modal dialog is active we
        always consume the event so the camera doesn't zoom in/out behind it.
        """
        if (self.dialog.active or self.paths_dialog.active
                or self.load_dialog.active):
            return True
        if self.mesh_window.handle_scroll(mx, my, dy):
            return True
        if self.tree and self.tree.handle_scroll(mx, my, dy):
            return True
        if (self.info_panel and not self.info_collapsed
                and self.info_panel.handle_scroll(mx, my, dy)):
            return True
        return False

    def handle_mouse_drag(self, mx, my):
        """Update the active slider while the mouse button is held."""
        if self._active_slider:
            self._active_slider.set_from_mouse(mx)

    def handle_mouse_up(self):
        """Release the active slider drag."""
        if self._active_slider:
            self._active_slider._dragging = False
        self._active_slider = None

    def update_hover(self, mx, my):
        """Refresh hover state on all buttons / tree rows / dialog widgets."""
        for btn in self.buttons:
            btn.hovered = (getattr(btn, 'visible', True)
                           and btn.contains(mx, my))
        if self.tab_bar:
            self.tab_bar.update_hover(mx, my)
        if self.tree:
            self.tree.update_hover(mx, my)
        if self.info_panel and not self.info_collapsed:
            self.info_panel.update_hover(mx, my)
        # Spine hover -- highlights the toggle strip when the cursor is over it
        self._spine_hovered = (self.info_panel_full_w > 0 and
                               self.info_spine_hit(mx, my, self._last_render_h))
        self.mesh_window.update_hover(mx, my)
        self.dialog.update_hover(mx, my)
        self.load_dialog.update_hover(mx, my)
        self.paths_dialog.update_hover(mx, my)

    def is_pointer_over_ui(self, mx, my):
        # Bar gone in the new layout; this branch only triggers if a
        # caller bumps BAR_HEIGHT back up for some reason.
        if self.BAR_HEIGHT > 0 and my <= self.BAR_HEIGHT:
            return True
        # Control panels (left = display+lighting, right = tank list+smoke)
        for rx, ry, rw, rh in (self.left_controls_rect, self.right_controls_rect):
            if rw > 0 and rh > 0 and rx <= mx < rx + rw and ry <= my < ry + rh:
                return True
        if self.tab_bar and self.tab_bar.hit(mx, my):
            return True
        if self.tree and self.tree.hit(mx, my):
            return True
        # Spine + (when expanded) info panel
        if (self.info_panel_full_w > 0 and
                self.info_spine_hit(mx, my, self._last_render_h)):
            return True
        if (self.info_panel and not self.info_collapsed
                and self.info_panel.hit(mx, my)):
            return True
        if self.mesh_window.hit(mx, my):
            return True
        if (self.dialog.active or self.paths_dialog.active
                or self.load_dialog.active):
            return True
        return False

    # ------------------------------------------------------------------
    # Internal rendering helpers
    # ------------------------------------------------------------------

    def _ortho(self, width, height):
        l, r = 0.0, float(width)
        t, b = 0.0, float(height)
        m = np.zeros((4, 4), dtype=np.float32)
        m[0, 0] = 2.0 / (r - l)
        m[1, 1] = 2.0 / (t - b)
        m[2, 2] = -1.0
        m[0, 3] = -(r + l) / (r - l)
        m[1, 3] = -(t + b) / (t - b)
        m[3, 3] = 1.0
        return m

    def _draw_quad(self, x, y, w, h):
        verts = np.array([
            x,     y,     0.0, 0.0,
            x + w, y,     1.0, 0.0,
            x + w, y + h, 1.0, 1.0,
            x,     y,     0.0, 0.0,
            x + w, y + h, 1.0, 1.0,
            x,     y + h, 0.0, 1.0,
        ], dtype=np.float32)
        glBindBuffer(GL_ARRAY_BUFFER, self.quad_vbo)
        glBufferSubData(GL_ARRAY_BUFFER, 0, verts.nbytes, verts)
        glDrawArrays(GL_TRIANGLES, 0, 6)

    def _draw_tex(self, tid, x, y, w, h):
        """Draw a font/text texture: samples its alpha, RGB comes from u_color
        (white in this code path).  See ui.frag mode 1."""
        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, tid)
        self.shader.set_int('u_tex', 0)
        self.shader.set_int('u_use_tex', 1)
        self.shader.set_vec4('u_color', 1.0, 1.0, 1.0, 1.0)
        self._draw_quad(x, y, w, h)
        self.shader.set_int('u_use_tex', 0)

    def _draw_image_tex(self, tid, x, y, w, h):
        """Draw a true RGB(A) image texture (e.g. tank thumbnail).
        Samples full RGBA from the texture.  See ui.frag mode 2."""
        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, tid)
        self.shader.set_int('u_tex', 0)
        self.shader.set_int('u_use_tex', 2)
        self.shader.set_vec4('u_color', 1.0, 1.0, 1.0, 1.0)
        self._draw_quad(x, y, w, h)
        self.shader.set_int('u_use_tex', 0)

    def _solid(self, r, g, b, a, x, y, w, h):
        self.shader.set_int('u_use_tex', 0)
        self.shader.set_vec4('u_color', r, g, b, a)
        self._draw_quad(x, y, w, h)

    # ------------------------------------------------------------------
    # Info-panel collapse helpers
    # ------------------------------------------------------------------

    def info_left_inset(self, expanded_w):
        """Effective left-side inset that the 3D viewport must avoid.
        Returns COLLAPSE_TAB_W when collapsed, expanded_w otherwise.
        """
        return self.COLLAPSE_TAB_W if self.info_collapsed else expanded_w

    def info_spine_rect(self, height):
        """Rect of the always-visible spine (toggle strip) on the panel's
        right edge.  Width is COLLAPSE_TAB_W; spans full panel height
        below the menu bar so the user always has a big click target.
        """
        # Spine sits at the right edge of whatever inset is currently active
        x = self.info_left_inset(self.info_panel_full_w) - self.COLLAPSE_TAB_W
        y = self.BAR_HEIGHT
        return (x, y, self.COLLAPSE_TAB_W, max(1, height - self.BAR_HEIGHT))

    def info_spine_hit(self, mx, my, height):
        x, y, w, h = self.info_spine_rect(height)
        return (x <= mx < x + w) and (y <= my < y + h)

    def _ensure_chevron(self):
        """Build (or rebuild) the chevron texture for the current state."""
        glyph = '>' if self.info_collapsed else '<'
        if glyph == self._chevron_glyph and self._chevron_tex is not None:
            return
        if self._chevron_tex is not None:
            glDeleteTextures(1, [self._chevron_tex])
        # Slightly larger font for the glyph so it's readable on a 16-px-wide spine
        big = pygame.font.SysFont('Segoe UI', 16, bold=True)
        surf = big.render(glyph, True, (220, 220, 230))
        data = pygame.image.tostring(surf, 'RGBA', False)
        w, h = surf.get_width(), surf.get_height()
        tid = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, tid)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, data)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        glBindTexture(GL_TEXTURE_2D, 0)
        self._chevron_tex   = tid
        self._chevron_w     = w
        self._chevron_h     = h
        self._chevron_glyph = glyph

    def _render_info_spine(self, height):
        """Draw the toggle strip + centered chevron.  Caller must already
        have set up the ortho projection and quad VAO (we're inside
        `render`)."""
        self._ensure_chevron()
        x, y, w, h = self.info_spine_rect(height)
        # Background -- subtle highlight on hover so it reads as clickable
        if self._spine_hovered:
            self._solid(0.28, 0.30, 0.36, 1.0, x, y, w, h)
        else:
            self._solid(0.18, 0.18, 0.22, 1.0, x, y, w, h)
        # 1-px right-edge accent so the spine pops against the dark scene
        self._solid(0.32, 0.32, 0.38, 1.0, x + w - 1, y, 1, h)
        # Chevron glyph centred horizontally; vertically near the top third
        # so it doesn't fight with anything else on screen
        if self._chevron_tex:
            cx = x + (w - self._chevron_w) // 2
            cy = y + max(0, (h // 3) - self._chevron_h // 2)
            self._draw_tex(self._chevron_tex, cx, cy, self._chevron_w, self._chevron_h)

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def render(self, width, height):
        """Draw the full bar overlay.

        Forces GL_FILL so wireframe mode does not affect the UI.

        Args:
            width, height (int): current framebuffer size in pixels
        """
        # Cache for click/hit-test handlers that don't take height as a param
        self._last_render_w = width
        self._last_render_h = height
        # Refresh value textures (no-op when value hasn't changed)
        for sl in self.sliders:
            sl.ensure_value_tex(self.font)

        glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
        glDisable(GL_DEPTH_TEST)
        glDisable(GL_CULL_FACE)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

        self.shader.use()
        self.shader.set_mat4('projection', self._ortho(width, height))
        glBindVertexArray(self.quad_vao)

        # ---- Control panel backgrounds (left + right) ------------------
        # Drawn first so widgets render on top.  The big tank-list /
        # info-tree panels paint their own backgrounds inside their
        # own rects below the control sections.  The divider between
        # the controls block and the panel below it is drawn 2 px tall
        # in a brighter accent so it reads as a separator bar.
        for rect in (self.left_controls_rect, self.right_controls_rect):
            rx, ry, rw, rh = rect
            if rw > 0 and rh > 0:
                self._solid(0.12, 0.12, 0.14, 0.96, rx, ry, rw, rh)
                # Top edge accent
                self._solid(0.25, 0.25, 0.28, 1.0, rx, ry,           rw, 1)
                # Bottom divider -- 2 px, brighter, so it visually
                # separates the controls slab from whatever sits below
                # (left: info tree.  right: nothing immediately, but
                # consistent styling looks better).
                self._solid(0.45, 0.45, 0.55, 1.0, rx, ry + rh - 2,  rw, 2)

        # ---- Toggle buttons (row 1, y=4) ------------------------------
        for btn in self.buttons:
            if not getattr(btn, 'visible', True):
                continue
            if btn.active:
                # Burnt orange (TE colour scheme) -- matches the selected
                # tree-row highlight so the user gets a consistent
                # "this is on" cue across the whole UI.
                col = (0.92, 0.45, 0.10, 1.0) if btn.hovered else (0.78, 0.33, 0.05, 1.0)
            else:
                col = (0.32, 0.32, 0.36, 1.0) if btn.hovered else (0.22, 0.22, 0.25, 1.0)
            self._solid(*col, btn.x, btn.y, btn.w, btn.h)
            if btn.text_tex:
                tx = btn.x + (btn.w - btn.text_w) // 2
                ty = btn.y + (btn.h - btn.text_h) // 2
                self._draw_tex(btn.text_tex, tx, ty, btn.text_w, btn.text_h)

        # ---- Sliders (rows 2 and 3) -----------------------------------
        for sl in self.sliders:
            if not getattr(sl, 'visible', True):
                continue
            cy  = sl.track_cy
            ty  = cy - sl.TRACK_H // 2       # track top y
            hy  = cy - sl.HANDLE_H // 2      # handle top y
            hcx = sl.handle_cx               # handle centre x

            # Label position: per-slider override, else track-relative default
            label_x = (sl.label_x if sl.label_x is not None
                       else sl.track_x - sl.label_w - 6)
            value_x = (sl.value_x if sl.value_x is not None
                       else sl.track_x + sl.track_w + 8)

            # Label
            if sl.label_tex:
                self._draw_tex(sl.label_tex,
                               label_x,
                               cy - sl.label_h // 2,
                               sl.label_w, sl.label_h)

            # Track background
            self._solid(0.18, 0.18, 0.20, 1.0, sl.track_x, ty, sl.track_w, sl.TRACK_H)

            # Track filled (left of handle) -- blue tint
            filled = max(0, hcx - sl.track_x)
            if filled > 0:
                self._solid(0.30, 0.50, 0.85, 1.0, sl.track_x, ty, filled, sl.TRACK_H)

            # Handle
            handle_col = (0.80, 0.90, 1.0, 1.0) if sl._dragging else (0.60, 0.75, 0.95, 1.0)
            self._solid(*handle_col,
                        hcx - sl.HANDLE_W // 2, hy,
                        sl.HANDLE_W, sl.HANDLE_H)

            # Value text
            if sl.value_tex:
                self._draw_tex(sl.value_tex,
                               value_x,
                               cy - sl.value_h // 2,
                               sl.value_w, sl.value_h)

        # ---- Checkboxes ----------------------------------------------
        for cb in self.checkboxes:
            if not getattr(cb, 'visible', True):
                continue   # skip checkboxes in collapsed groups
            # Border
            self._solid(0.40, 0.40, 0.45, 1.0, cb.x - 1, cb.y - 1, cb.size + 2, cb.size + 2)
            # Background
            bg = (0.30, 0.50, 0.85, 1.0) if cb.checked else (0.18, 0.18, 0.20, 1.0)
            self._solid(*bg, cb.x, cb.y, cb.size, cb.size)
            # Check mark inner square
            if cb.checked:
                pad = 3
                self._solid(0.85, 0.95, 1.0, 1.0,
                            cb.x + pad, cb.y + pad,
                            cb.size - pad * 2, cb.size - pad * 2)
            # Label text
            if cb.label_tex:
                self._draw_tex(cb.label_tex,
                               cb.x + cb.size + 4,
                               cb.y + (cb.size - cb.label_h) // 2,
                               cb.label_w, cb.label_h)

        # ---- Side panels ---------------------------------------------
        if self.tab_bar:
            self._render_tab_bar(self.tab_bar)
        if self.tree:
            self._render_tree(self.tree, height)
        # Info panel is hidden when collapsed -- only the spine shows.
        if self.info_panel and not self.info_collapsed:
            self._render_tree(self.info_panel, height)
        # Spine (toggle strip) -- always visible, regardless of collapse state.
        # Drawn AFTER the info panel so the chevron sits on top of the panel's
        # right edge.
        if self.info_panel_full_w > 0:
            self._render_info_spine(height)

        # ---- Mesh-visibility window (above the panels, below modals) -
        if self.mesh_window.active:
            self._render_mesh_window(self.mesh_window)

        # ---- Modal dialogs (rendered last so they sit on top) --------
        if self.dialog.active:
            self._render_dialog(self.dialog, width, height)
        if self.load_dialog.active:
            self._render_load_dialog(self.load_dialog, width, height)
        if self.paths_dialog.active:
            self._render_paths_dialog(self.paths_dialog, width, height)

        glBindVertexArray(0)
        glDisable(GL_BLEND)
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_CULL_FACE)

    # ------------------------------------------------------------------
    # Tree-view + dialog rendering
    # ------------------------------------------------------------------

    def _render_tree(self, tree, window_h):
        """Draw the right-side collapsible tree panel + bottom thumbnail."""
        # Build any missing label textures lazily
        tree.ensure_textures(self._make_tex)

        # Panel background
        self._solid(0.10, 0.10, 0.12, 0.96, tree.x, tree.y, tree.w, tree.h)
        # 1px left border
        self._solid(0.25, 0.25, 0.28, 1.0, tree.x, tree.y, 1, tree.h)

        rows_h = tree._rows_h()

        # Scissor to clip rows inside the rows region (NOT the thumbnail area)
        # GL scissor uses bottom-left origin.
        glEnable(GL_SCISSOR_TEST)
        glScissor(tree.x, window_h - (tree.y + rows_h), tree.w, rows_h)

        flat = list(tree._flatten())
        y_cur = tree.y - tree.scroll_y
        for idx, (depth, node) in enumerate(flat):
            row_y = y_cur
            y_cur += tree.ROW_H

            # Skip rows wholly outside the rows region
            if row_y + tree.ROW_H < tree.y or row_y > tree.y + rows_h:
                continue

            row_x = tree.x + 4

            # Persistent selection -- burnt orange (TE colour scheme).
            # Drawn before hover so a hovered selected row gets a
            # subtle blue tint on top of the orange.
            if tree.selected_node is not None and node is tree.selected_node:
                self._solid(0.78, 0.33, 0.05, 0.65,
                            tree.x + 1, row_y, tree.w - 1, tree.ROW_H)

            # Hover highlight
            if idx == tree.hover_idx:
                self._solid(0.20, 0.32, 0.55, 0.55,
                            tree.x + 1, row_y, tree.w - 1, tree.ROW_H)

            # Indent + glyph (branch) or vclass icon (leaf) + label
            label_x = row_x + depth * tree.INDENT
            if node.children:
                # Branch: expand/collapse chip
                chip_y = row_y + (tree.ROW_H - 5) // 2
                col = (0.85, 0.90, 1.0, 1.0) if node.expanded else (0.55, 0.65, 0.85, 1.0)
                self._solid(*col, label_x, chip_y, 5, 5)
                label_x += 10
            else:
                # Leaf: vehicle-class icon (if available for this vclass)
                vclass    = (node.payload or {}).get('vclass')
                icon_info = tree.class_icons.get(vclass) if vclass else None
                if icon_info:
                    tex_id, iw, ih = icon_info
                    # Fit into a square that matches the row height (with 1px margin).
                    icon_h = tree.ROW_H - 2
                    icon_w = (int(round(iw * icon_h / ih))
                              if ih > 0 else icon_h)
                    icon_y = row_y + (tree.ROW_H - icon_h) // 2
                    self._draw_image_tex(tex_id, label_x, icon_y,
                                         icon_w, icon_h)
                    label_x += icon_w + 4
                else:
                    # Reserve the icon slot so labels stay aligned even when
                    # a vclass is missing or its icon hasn't loaded yet.
                    label_x += (tree.ROW_H - 2) + 4

            if node.label_tex:
                self._draw_tex(node.label_tex,
                               label_x,
                               row_y + (tree.ROW_H - node.label_h) // 2,
                               node.label_w, node.label_h)

        glDisable(GL_SCISSOR_TEST)

        # ---- Thumbnail strip at the bottom of the panel -----------------
        if tree.show_thumb_area:
            self._render_tree_thumbnail(tree)

    # Height of the name strip above the thumbnail image.
    THUMB_NAME_STRIP_H = 24

    def _render_tree_thumbnail(self, tree):
        """Render the loaded/hover tank thumbnail in the reserved area.

        Layout (top → bottom):
            divider line          (1 px)
            display-name label    (THUMB_NAME_STRIP_H px, centred)
            tank thumbnail image  (letterboxed in remaining space)

        Hover wins over loaded when both are set; the name label and the
        image are always drawn from the same source so they update in
        lock-step.
        """
        tx, ty, tw, th = tree.thumbnail_rect()

        # Top divider line + dark background
        self._solid(0.25, 0.25, 0.28, 1.0, tx, ty, tw, 1)
        self._solid(0.06, 0.06, 0.08, 1.0, tx, ty + 1, tw, th - 1)

        # ---- Pick the active source (hover wins) -----------------------
        if tree.hover_thumb_tex:
            tex_id   = tree.hover_thumb_tex
            iw, ih   = tree.hover_thumb_w, tree.hover_thumb_h
            name_str = tree.hover_thumb_name
            slot     = 'hover'
        elif tree.loaded_thumb_tex:
            tex_id   = tree.loaded_thumb_tex
            iw, ih   = tree.loaded_thumb_w, tree.loaded_thumb_h
            name_str = tree.loaded_thumb_name
            slot     = 'loaded'
        else:
            return

        if not tex_id or iw <= 0 or ih <= 0:
            return

        # ---- Lazily (re)build the name texture if its string changed ---
        cached_str = getattr(tree, f'_{slot}_name_str')
        if cached_str != name_str:
            old_tex = getattr(tree, f'_{slot}_name_tex')
            if old_tex:
                glDeleteTextures(1, [old_tex])
            if name_str:
                n_tex, n_w, n_h = self._make_tex(name_str, (245, 245, 250))
            else:
                n_tex, n_w, n_h = None, 0, 0
            setattr(tree, f'_{slot}_name_tex', n_tex)
            setattr(tree, f'_{slot}_name_w',   n_w)
            setattr(tree, f'_{slot}_name_h',   n_h)
            setattr(tree, f'_{slot}_name_str', name_str)
        name_tex = getattr(tree, f'_{slot}_name_tex')
        name_w   = getattr(tree, f'_{slot}_name_w')
        name_h   = getattr(tree, f'_{slot}_name_h')

        # ---- Draw the label, centred in the top strip -----------------
        strip_h = self.THUMB_NAME_STRIP_H
        if name_tex:
            self._draw_tex(name_tex,
                           tx + (tw    - name_w) // 2,
                           ty + 1 + (strip_h - name_h) // 2,
                           name_w, name_h)

        # ---- Letterbox-fit the image into the remaining area -----------
        pad      = tree.THUMB_PADDING
        img_top  = ty + 1 + strip_h
        img_h    = th - 1 - strip_h
        avail_w  = max(1, tw    - pad * 2)
        avail_h  = max(1, img_h - pad * 2)

        img_aspect   = iw / ih
        avail_aspect = avail_w / avail_h
        if img_aspect >= avail_aspect:
            draw_w = avail_w
            draw_h = int(round(draw_w / img_aspect))
        else:
            draw_h = avail_h
            draw_w = int(round(draw_h * img_aspect))
        draw_x = tx       + (tw    - draw_w) // 2
        draw_y = img_top  + (img_h - draw_h) // 2

        self._draw_image_tex(tex_id, draw_x, draw_y, draw_w, draw_h)

    # ------------------------------------------------------------------
    # Tab-bar rendering
    # ------------------------------------------------------------------

    def _render_tab_bar(self, bar):
        """Draw the tier-filter tab strip above the tree."""
        # Lazy build label textures
        for i, label in enumerate(bar.labels):
            if bar._label_tex[i] is None:
                tex, w, h = self._make_tex(label, (235, 240, 250))
                bar._label_tex[i] = tex
                bar._label_w[i]   = w
                bar._label_h[i]   = h

        # Strip background (matches dark panel chrome)
        self._solid(0.10, 0.10, 0.12, 1.0, bar.x, bar.y, bar.w, bar.HEIGHT)
        # Bottom 1px separator
        self._solid(0.25, 0.25, 0.28, 1.0,
                    bar.x, bar.y + bar.HEIGHT - 1, bar.w, 1)

        for i, label in enumerate(bar.labels):
            tx, ty, tw, th = bar._tab_rect(i)
            is_active = (i == bar.active_index)
            is_hover  = (i == bar.hover_idx)

            is_building = (i == bar.building_index)
            if is_building:
                col = (0.95, 0.75, 0.10, 1.0)        # amber/yellow -- in progress
            elif is_active:
                col = (0.78, 0.33, 0.05, 1.0)        # burnt orange (TE)
            elif is_hover:
                col = (0.25, 0.27, 0.32, 1.0)        # subtle hover tint
            else:
                col = (0.16, 0.17, 0.20, 1.0)        # inactive
            self._solid(*col, tx + 1, ty + 1, tw - 1, th - 2)

            # Label centred
            tex = bar._label_tex[i]
            if tex:
                lw = bar._label_w[i]
                lh = bar._label_h[i]
                self._draw_tex(tex,
                               tx + (tw - lw) // 2,
                               ty + (th - lh) // 2,
                               lw, lh)

    def _render_dialog(self, dlg, window_w, window_h):
        """Draw the modal load-confirm dialog."""
        dlg._layout(window_w, window_h)
        bx, by, bw, bh = dlg._box_rect()

        # Backdrop dim
        self._solid(0.0, 0.0, 0.0, 0.55, 0, 0, window_w, window_h)
        # Dialog box border + body
        self._solid(0.35, 0.40, 0.50, 1.0, bx - 1, by - 1, bw + 2, bh + 2)
        self._solid(0.16, 0.17, 0.20, 1.0, bx, by, bw, bh)
        # Title strip
        self._solid(0.22, 0.28, 0.38, 1.0, bx, by, bw, 28)

        # "Load tank?" header text
        if dlg.title_tex is None:
            # Fallback if show() wasn't called -- shouldn't happen
            return
        # Header label
        header = "Load tank?"
        # Cache header texture on the dialog itself (rebuild only if missing)
        if not hasattr(dlg, '_header_tex') or dlg._header_tex is None:
            dlg._header_tex, dlg._header_w, dlg._header_h = self._make_tex(
                header, (235, 240, 250))
        self._draw_tex(dlg._header_tex,
                       bx + 12, by + (28 - dlg._header_h) // 2,
                       dlg._header_w, dlg._header_h)

        # Tank-name label  (centred horizontally inside box, below the title strip)
        name_x = bx + (bw - dlg.title_w) // 2
        name_y = by + 38
        self._draw_tex(dlg.title_tex, name_x, name_y, dlg.title_w, dlg.title_h)

        # 'Crashed' checkbox
        cx, cy, cs, _ = dlg._crashed_cb_rect()
        self._solid(0.40, 0.40, 0.45, 1.0, cx - 1, cy - 1, cs + 2, cs + 2)
        bg = (0.30, 0.50, 0.85, 1.0) if dlg.crashed else (0.18, 0.18, 0.20, 1.0)
        self._solid(*bg, cx, cy, cs, cs)
        if dlg.crashed:
            pad = 3
            self._solid(0.85, 0.95, 1.0, 1.0,
                        cx + pad, cy + pad, cs - pad * 2, cs - pad * 2)

        # Crashed checkbox label
        if not hasattr(dlg, '_crashed_label_tex') or dlg._crashed_label_tex is None:
            dlg._crashed_label_tex, dlg._crashed_label_w, dlg._crashed_label_h = \
                self._make_tex("Load crashed (destroyed) variant", (215, 220, 230))
        self._draw_tex(dlg._crashed_label_tex,
                       cx + cs + 6, cy + (cs - dlg._crashed_label_h) // 2,
                       dlg._crashed_label_w, dlg._crashed_label_h)

        # Load button
        lx, ly, lw, lh = dlg._load_btn_rect()
        col = (0.45, 0.65, 0.95, 1.0) if dlg.hover_load else (0.30, 0.50, 0.85, 1.0)
        self._solid(*col, lx, ly, lw, lh)
        if not hasattr(dlg, '_load_btn_tex') or dlg._load_btn_tex is None:
            dlg._load_btn_tex, dlg._load_btn_w, dlg._load_btn_h = self._make_tex(
                "Load", (245, 250, 255))
        self._draw_tex(dlg._load_btn_tex,
                       lx + (lw - dlg._load_btn_w) // 2,
                       ly + (lh - dlg._load_btn_h) // 2,
                       dlg._load_btn_w, dlg._load_btn_h)

        # Cancel button
        cnx, cny, cnw, cnh = dlg._cancel_btn_rect()
        col = (0.40, 0.40, 0.45, 1.0) if dlg.hover_cancel else (0.28, 0.28, 0.32, 1.0)
        self._solid(*col, cnx, cny, cnw, cnh)
        if not hasattr(dlg, '_cancel_btn_tex') or dlg._cancel_btn_tex is None:
            dlg._cancel_btn_tex, dlg._cancel_btn_w, dlg._cancel_btn_h = self._make_tex(
                "Cancel", (220, 220, 225))
        self._draw_tex(dlg._cancel_btn_tex,
                       cnx + (cnw - dlg._cancel_btn_w) // 2,
                       cny + (cnh - dlg._cancel_btn_h) // 2,
                       dlg._cancel_btn_w, dlg._cancel_btn_h)

    # ------------------------------------------------------------------
    # Paths-dialog rendering
    # ------------------------------------------------------------------

    @staticmethod
    def _truncate_path_left(s, max_chars):
        """Trim long path strings from the LEFT so the file/folder name
        at the end stays visible.  Prepends '...' when shortened."""
        if len(s) <= max_chars:
            return s
        return '...' + s[-(max_chars - 3):]

    def _ensure_static_dialog_textures(self, dlg):
        """Build the dialog's title + button text textures if missing."""
        if dlg._title_tex is None:
            dlg._title_tex, dlg._title_w, dlg._title_h = self._make_tex(
                "Set Paths", (235, 240, 250))
        if dlg._save_tex is None:
            dlg._save_tex, dlg._save_w, dlg._save_h = self._make_tex(
                "Save", (245, 250, 255))
        if dlg._cancel_tex is None:
            dlg._cancel_tex, dlg._cancel_w, dlg._cancel_h = self._make_tex(
                "Cancel", (220, 220, 225))
        if dlg._browse_tex is None:
            dlg._browse_tex, dlg._browse_w, dlg._browse_h = self._make_tex(
                "Browse...", (235, 240, 250))
        for i, (_, label, _) in enumerate(dlg._FIELDS):
            if dlg._label_tex[i] is None:
                tex, w, h = self._make_tex(label, (215, 220, 230))
                dlg._label_tex[i] = tex
                dlg._label_w[i]   = w
                dlg._label_h[i]   = h

    def _render_paths_dialog(self, dlg, window_w, window_h):
        """Draw the modal Set-Paths dialog."""
        dlg._layout(window_w, window_h)
        bx, by, bw, bh = dlg._box_rect()

        # Backdrop dim
        self._solid(0.0, 0.0, 0.0, 0.55, 0, 0, window_w, window_h)
        # Box border + body
        self._solid(0.35, 0.40, 0.50, 1.0, bx - 1, by - 1, bw + 2, bh + 2)
        self._solid(0.16, 0.17, 0.20, 1.0, bx, by, bw, bh)
        # Title strip
        self._solid(0.22, 0.28, 0.38, 1.0, bx, by, bw, 28)

        self._ensure_static_dialog_textures(dlg)

        # Title
        self._draw_tex(dlg._title_tex,
                       bx + 12, by + (28 - dlg._title_h) // 2,
                       dlg._title_w, dlg._title_h)

        # ---- Per-row label, value field, browse button -----------------
        for i, (key, _, _mode) in enumerate(dlg._FIELDS):
            # Label
            label_x = bx + dlg.PAD
            row_x, row_y, row_w, row_h = dlg._row_rect(i)
            self._draw_tex(dlg._label_tex[i],
                           label_x,
                           row_y + (row_h - dlg._label_h[i]) // 2,
                           dlg._label_w[i], dlg._label_h[i])

            # Value field background
            self._solid(0.22, 0.22, 0.26, 1.0, row_x - 1, row_y - 1,
                        row_w + 2, row_h + 2)
            self._solid(0.10, 0.11, 0.13, 1.0, row_x, row_y, row_w, row_h)

            # Value text (truncated from the left to keep the tail visible)
            current = dlg.values.get(key, '') or ''
            display = self._truncate_path_left(current, 60) if current \
                      else '<not set>'
            if dlg._value_str[i] != display:
                old = dlg._value_tex[i]
                if old:
                    glDeleteTextures(1, [old])
                col = (220, 225, 235) if current else (140, 140, 150)
                tex, w, h = self._make_tex(display, col)
                dlg._value_tex[i] = tex
                dlg._value_w[i]   = w
                dlg._value_h[i]   = h
                dlg._value_str[i] = display
            if dlg._value_tex[i]:
                self._draw_tex(dlg._value_tex[i],
                               row_x + 6,
                               row_y + (row_h - dlg._value_h[i]) // 2,
                               dlg._value_w[i], dlg._value_h[i])

            # Browse button
            bxr, byr, bwr, bhr = dlg._browse_rect(i)
            col = ((0.45, 0.55, 0.75, 1.0) if dlg.hover_browse[i]
                   else (0.30, 0.38, 0.55, 1.0))
            self._solid(*col, bxr, byr, bwr, bhr)
            self._draw_tex(dlg._browse_tex,
                           bxr + (bwr - dlg._browse_w) // 2,
                           byr + (bhr - dlg._browse_h) // 2,
                           dlg._browse_w, dlg._browse_h)

        # ---- Save / Cancel buttons ------------------------------------
        sx, sy, sw, sh = dlg._save_btn_rect()
        col = (0.45, 0.65, 0.95, 1.0) if dlg.hover_save else (0.30, 0.50, 0.85, 1.0)
        self._solid(*col, sx, sy, sw, sh)
        self._draw_tex(dlg._save_tex,
                       sx + (sw - dlg._save_w) // 2,
                       sy + (sh - dlg._save_h) // 2,
                       dlg._save_w, dlg._save_h)

        cx, cy, cw, ch = dlg._cancel_btn_rect()
        col = (0.40, 0.40, 0.45, 1.0) if dlg.hover_cancel else (0.28, 0.28, 0.32, 1.0)
        self._solid(*col, cx, cy, cw, ch)
        self._draw_tex(dlg._cancel_tex,
                       cx + (cw - dlg._cancel_w) // 2,
                       cy + (ch - dlg._cancel_h) // 2,
                       dlg._cancel_w, dlg._cancel_h)

    # ------------------------------------------------------------------
    # Mesh-visibility window rendering
    # ------------------------------------------------------------------

    def _render_mesh_window(self, w):
        """Draw the persistent mesh-visibility window."""
        ch = w._content_h()
        # Border + body
        self._solid(0.35, 0.40, 0.50, 1.0,
                    w.x - 1, w.y - 1, w.BOX_W + 2, ch + 2)
        self._solid(0.16, 0.17, 0.20, 1.0,
                    w.x, w.y, w.BOX_W, ch)
        # Title strip
        self._solid(0.22, 0.28, 0.38, 1.0,
                    w.x, w.y, w.BOX_W, w.TITLE_H)
        if w._title_tex:
            self._draw_tex(w._title_tex,
                           w.x + 8,
                           w.y + (w.TITLE_H - w._title_h) // 2,
                           w._title_w, w._title_h)
        # Close glyph
        cx, cy, cw, ck = w._close_rect()
        col = (0.55, 0.30, 0.30, 1.0) if w.hover_close else (0.35, 0.20, 0.22, 1.0)
        self._solid(*col, cx, cy, cw, ck)
        if w._close_tex:
            self._draw_tex(w._close_tex,
                           cx + (cw - w._close_w) // 2,
                           cy + (ck - w._close_h) // 2,
                           w._close_w, w._close_h)

        # Scissor-clip the rows region so scrolled rows don't bleed
        # outside the window box.
        # Row region in window coords: y0..y0+rows_h
        rows_y = w.y + w.TITLE_H + w.PAD
        rows_h = max(1, ch - w.TITLE_H - w.PAD * 2)
        # Convert to GL coords (bottom-left origin)
        # We don't have window_h here; use the saved last-render height
        # Actually simpler: just clip rendering by Python skip.
        for i, row in enumerate(w.rows):
            ry = rows_y - w.scroll_y + i * w.ROW_H
            if ry + w.ROW_H < rows_y or ry > rows_y + rows_h:
                continue   # outside visible rows region

            # Checkbox
            cb_size = w.CHECKBOX_SIZE
            cb_x = w.x + w.PAD
            cb_y = ry + (w.ROW_H - cb_size) // 2
            # Border
            self._solid(0.40, 0.40, 0.45, 1.0,
                        cb_x - 1, cb_y - 1, cb_size + 2, cb_size + 2)
            # Background -- blue when checked (visible), dark otherwise
            bg = ((0.30, 0.50, 0.85, 1.0) if row.visible
                  else (0.18, 0.18, 0.20, 1.0))
            self._solid(*bg, cb_x, cb_y, cb_size, cb_size)
            if row.visible:
                pad = 3
                self._solid(0.85, 0.95, 1.0, 1.0,
                            cb_x + pad, cb_y + pad,
                            cb_size - pad * 2, cb_size - pad * 2)

            # Label
            if row.label_tex:
                self._draw_tex(row.label_tex,
                               cb_x + cb_size + 6,
                               ry + (w.ROW_H - row.label_h) // 2,
                               row.label_w, row.label_h)

    # ------------------------------------------------------------------
    # Load-tank dialog rendering (per-skin blocks with radio groups)
    # ------------------------------------------------------------------

    def _ensure_load_dialog_static_tex(self, dlg):
        """Build static button + column-header textures once per session."""
        if dlg._load_tex is None:
            dlg._load_tex, dlg._load_w, dlg._load_h = self._make_tex(
                "Load", (245, 250, 255))
        if dlg._dmg_tex is None:
            dlg._dmg_tex, dlg._dmg_w, dlg._dmg_h = self._make_tex(
                "Load Damaged", (255, 230, 220))
        if dlg._cancel_tex is None:
            dlg._cancel_tex, dlg._cancel_w, dlg._cancel_h = self._make_tex(
                "Cancel", (220, 220, 225))
        for col_label in ('Chassis', 'Turret', 'Gun'):
            if col_label not in dlg._col_label_tex:
                dlg._col_label_tex[col_label] = self._make_tex(
                    col_label, (200, 215, 235))

    def _ensure_radio_label(self, dlg, block_idx, group, tag):
        """Lazily build the label texture for one radio row."""
        cache = dlg._radio_label_tex[block_idx]
        key = (group, tag)
        if key not in cache:
            cache[key] = self._make_tex(tag, (220, 225, 235))
        return cache[key]

    def _render_load_dialog(self, dlg, window_w, window_h):
        """Draw the modal Load-Tank dialog with per-skin blocks."""
        dlg._layout(window_w, window_h)
        bx, by, bw, bh = dlg._box_rect()

        # Backdrop dim
        self._solid(0.0, 0.0, 0.0, 0.55, 0, 0, window_w, window_h)
        # Box border + body
        self._solid(0.35, 0.40, 0.50, 1.0, bx - 1, by - 1, bw + 2, bh + 2)
        self._solid(0.16, 0.17, 0.20, 1.0, bx, by, bw, bh)
        # Title strip
        self._solid(0.22, 0.28, 0.38, 1.0, bx, by, bw, dlg.TITLE_H)

        self._ensure_load_dialog_static_tex(dlg)
        if dlg._title_tex:
            self._draw_tex(dlg._title_tex,
                           bx + 12,
                           by + (dlg.TITLE_H - dlg._title_h) // 2,
                           dlg._title_w, dlg._title_h)

        # ---- Per-skin blocks ------------------------------------------
        for idx, block in enumerate(dlg.blocks):
            self._render_load_block(dlg, idx, block)

        # ---- Status line (left of Cancel) -----------------------------
        # Drawn before the Cancel button so a long status string can
        # extend toward the right margin without poking through the btn.
        cx, cy, cw, ch = dlg._cancel_rect()
        if dlg._status_tex and dlg._status_w > 0:
            status_x  = bx + dlg.PAD
            status_y  = cy + (ch - dlg._status_h) // 2
            # Burnt-orange tint so it visually reads as the same kind
            # of "in progress" cue as the active toggle buttons.
            self._solid(0.78, 0.33, 0.05, 0.20,
                        status_x - 4, cy + 2,
                        cx - status_x, ch - 4)
            self._draw_tex(dlg._status_tex,
                           status_x, status_y,
                           dlg._status_w, dlg._status_h)

        # ---- Cancel button --------------------------------------------
        col = (0.40, 0.40, 0.45, 1.0) if dlg.hover_cancel \
              else (0.28, 0.28, 0.32, 1.0)
        self._solid(*col, cx, cy, cw, ch)
        self._draw_tex(dlg._cancel_tex,
                       cx + (cw - dlg._cancel_w) // 2,
                       cy + (ch - dlg._cancel_h) // 2,
                       dlg._cancel_w, dlg._cancel_h)

    def _render_load_block(self, dlg, idx, block):
        """Draw one skin block: header strip + 3 radio columns + 2 buttons."""
        bx, by, bw, bh = dlg._block_rect(idx)

        # Block bg + border
        self._solid(0.45, 0.50, 0.62, 1.0, bx - 1, by - 1, bw + 2, bh + 2)
        self._solid(0.20, 0.21, 0.25, 1.0, bx, by, bw, bh)

        # Header strip
        self._solid(0.26, 0.32, 0.44, 1.0, bx, by, bw, dlg.BLOCK_HEADER_H)
        if dlg._block_header_tex[idx] is None:
            label = ('Default' if block['skin'] is None
                     else f"Skin: {block['skin']}")
            dlg._block_header_tex[idx] = self._make_tex(label, (240, 245, 255))
        h_tex, h_w, h_h = dlg._block_header_tex[idx]
        if h_tex:
            self._draw_tex(h_tex, bx + 8,
                           by + (dlg.BLOCK_HEADER_H - h_h) // 2, h_w, h_h)

        # Column-header strip just below the block header.  Subtly tinted
        # so the headers visually separate from the radio rows below.
        col_y = by + dlg.BLOCK_HEADER_H
        self._solid(0.16, 0.18, 0.22, 1.0,
                    bx, col_y, bw, dlg.COL_HEADER_H)

        # Column headers (Chassis / Turret / Gun)
        for col, label in enumerate(('Chassis', 'Turret', 'Gun')):
            cx, cw = dlg._column_x(idx, col)
            tex, tw, th = dlg._col_label_tex[label]
            if tex:
                self._draw_tex(tex, cx + 2,
                               col_y + (dlg.COL_HEADER_H - th) // 2,
                               tw, th)

        # Radio rows -- chassis (col 0), turrets (col 1), guns (col 2 for sel turret)
        groups = (
            (0, 'chassis', block['chassis'], block['sel_ch']),
            (1, 'turret',  block['turrets'], block['sel_tu']),
            (2, 'gun',     block['guns'].get(block['sel_tu'], []),
                           block['sel_gu']),
        )
        for col, group, tags, selected in groups:
            for row, tag in enumerate(tags):
                rx, ry, rw, rh = dlg._radio_rect(idx, col, row)
                # Optional row hover highlight (no flag tracked but cheap to add)
                # ...skipped for now to keep behaviour predictable.

                # Radio circle: filled when selected
                box_size = dlg.RADIO_SIZE
                box_x = rx + 2
                box_y = ry + (rh - box_size) // 2
                self._solid(0.40, 0.40, 0.45, 1.0,
                            box_x - 1, box_y - 1, box_size + 2, box_size + 2)
                bg = ((0.30, 0.50, 0.85, 1.0) if tag == selected
                      else (0.18, 0.18, 0.20, 1.0))
                self._solid(*bg, box_x, box_y, box_size, box_size)
                if tag == selected:
                    pad = 3
                    self._solid(0.85, 0.95, 1.0, 1.0,
                                box_x + pad, box_y + pad,
                                box_size - pad * 2, box_size - pad * 2)

                # Label
                tex, lw, lh = self._ensure_radio_label(dlg, idx, group, tag)
                if tex:
                    label_x = box_x + box_size + 4
                    label_y = ry + (rh - lh) // 2
                    self._draw_tex(tex, label_x, label_y, lw, lh)

        # Load button
        lx, ly, lw, lh = dlg._load_btn_rect(idx)
        col = ((0.45, 0.65, 0.95, 1.0) if dlg.hover_load_idx == idx
               else (0.30, 0.50, 0.85, 1.0))
        self._solid(*col, lx, ly, lw, lh)
        self._draw_tex(dlg._load_tex,
                       lx + (lw - dlg._load_w) // 2,
                       ly + (lh - dlg._load_h) // 2,
                       dlg._load_w, dlg._load_h)

        # Load Damaged button
        dx, dy, dw, dh = dlg._dmg_btn_rect(idx)
        col = ((0.85, 0.50, 0.45, 1.0) if dlg.hover_dmg_idx == idx
               else (0.60, 0.32, 0.30, 1.0))
        self._solid(*col, dx, dy, dw, dh)
        self._draw_tex(dlg._dmg_tex,
                       dx + (dw - dlg._dmg_w) // 2,
                       dy + (dh - dlg._dmg_h) // 2,
                       dlg._dmg_w, dlg._dmg_h)

    # ------------------------------------------------------------------
    def cleanup(self):
        """Delete all GPU resources."""
        for btn in self.buttons:
            if btn.text_tex:
                glDeleteTextures(1, [btn.text_tex])
        for sl in self.sliders:
            if sl.label_tex: glDeleteTextures(1, [sl.label_tex])
            if sl.value_tex: glDeleteTextures(1, [sl.value_tex])
        for cb in self.checkboxes:
            if cb.label_tex: glDeleteTextures(1, [cb.label_tex])
        if self._chevron_tex:
            glDeleteTextures(1, [self._chevron_tex])
            self._chevron_tex = None

        # Tree + dialogs
        if self.tree:
            self.tree.cleanup()
        if self.info_panel:
            self.info_panel.cleanup()
        if self.tab_bar:
            self.tab_bar.cleanup()
        if self.dialog:
            # The confirm dialog caches a few label textures lazily on itself
            for attr in ('_header_tex', '_crashed_label_tex',
                         '_load_btn_tex', '_cancel_btn_tex'):
                tex = getattr(self.dialog, attr, None)
                if tex:
                    glDeleteTextures(1, [tex])
                    setattr(self.dialog, attr, None)
            self.dialog.cleanup()
        if self.paths_dialog:
            self.paths_dialog.cleanup()
        if self.load_dialog:
            self.load_dialog.cleanup()
        if self.mesh_window:
            self.mesh_window.cleanup()

        if self.quad_vbo: glDeleteBuffers(1, [self.quad_vbo])
        if self.quad_vao: glDeleteVertexArrays(1, [self.quad_vao])
