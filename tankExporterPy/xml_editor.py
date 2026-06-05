"""ImGui-backed XML editor windows.

A thin wrapper around Dear ImGui's `input_text_multiline`
widget (via imgui-bundle), scoped specifically to XML editing
for "The Xml Files" tab bar in `viewer.py`.

History note (2026-05-24):  this module originally wrapped the
imgui-bundle binding of ImGuiColorTextEdit -- the rewrite by
Johan Goossens which adds syntax highlighting, multi-cursor,
find/replace, mini-map, line folding, etc.  That widget turned
out to have unfixable upstream bugs in this build of imgui-
bundle (1.92.801): clicking the horizontal scrollbar selected
text to EOF, dragged the view down, and leaked text drawing
past the window's own clip rect.  Reproducible in the bundled
demo_text_edit.py with no application code involved -- not
something we can patch around.  We dropped back to ImGui's
built-in `input_text_multiline`, which:

  *   has working scrollbars (it's the core widget every ImGui
      demo has used for a decade -- stable)
  *   has copy/cut/paste/undo via the usual ctrl shortcuts
  *   does NOT have syntax highlighting or line numbers

The XML files we edit are small enough that the loss of
colorization doesn't hurt much; correctness wins over polish.

The imgui context + GL renderer are still initialised LAZILY,
and open_tab returns False if `imgui_bundle` isn't installed.

Public surface (consumed by `viewer.py`):

    XMLEditor()                         -- construct once
    .open_tab(idx, label, path, text,
              save_cb, width, height)   -- pop the editor for a
                                           tab; calls back when
                                           the user hits Save
    .is_active()                        -- any window open?
    .wants_keyboard() / .wants_mouse()  -- pygame event gating
    .process_event(event)               -- feed pygame event;
                                           returns True if imgui
                                           consumed it
    .render(width, height)              -- per-frame draw + the
                                           Ctrl+S handler
    .shutdown()                         -- on app close
"""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
from typing import Callable, Optional


def _log_exc(tag: str, exc: BaseException) -> None:
    """Print a tagged traceback to stderr.  Used at every catch
    site so a crash on a user's machine shows up in their
    console / launcher log instead of being silently swallowed.
    """
    print(f"[xml_editor] {tag}: "
          f"{type(exc).__name__}: {exc}",
          file=sys.stderr)
    traceback.print_exc()

# Module-level flag so we only try the auto-install once per process.
# Set True after the install attempt regardless of outcome -- if it
# failed we don't want every click to re-run pip.
_AUTO_INSTALL_TRIED = False

# Lazy-loaded bindings.  Populated by `_lazy_import()` on first
# use; everything below checks `_imgui is not None` before
# touching the symbols so the module is import-safe even when
# imgui-bundle isn't installed.
_imgui = None
_renderer_cls    = None


def _try_import():
    """Single import attempt.  Returns (imgui_mod, renderer_cls)
    on success or None on ImportError.  Logs the full traceback
    so the failure is visible in the launcher console -- silently
    swallowing the import error makes the "editor doesn't open"
    symptom look like a generic crash.
    """
    try:
        from imgui_bundle import imgui as _i
        from imgui_bundle.python_backends.pygame_backend import (
            PygameRenderer as _r)
    except Exception as exc:
        _log_exc("imgui-bundle import failed", exc)
        return None
    return _i, _r


def ensure_imgui_bundle_installed(verbose: bool = True) -> bool:
    """Best-effort `pip install imgui-bundle` into the current
    interpreter.  Intended to be called at app STARTUP (before
    pygame's GL window grabs the main thread) -- NOT mid-click,
    because pip blocks for ~20 s and Windows will flag the
    pygame window as Not Responding while it waits.  Returns
    True if imgui-bundle is importable after the call (already
    present OR install succeeded).
    """
    global _AUTO_INSTALL_TRIED
    # Already importable?  Nothing to do.
    if _try_import() is not None:
        return True
    if _AUTO_INSTALL_TRIED:
        return False
    _AUTO_INSTALL_TRIED = True
    if verbose:
        print("[xml_editor] imgui-bundle missing -- running "
              "`pip install imgui-bundle` (one-time) ...",
              flush=True)
    cmd_base = [sys.executable, '-m', 'pip', 'install',
                'imgui-bundle']
    try:
        rc = subprocess.call(cmd_base)
    except Exception as exc:
        if verbose:
            print(f"[xml_editor] pip launch failed: "
                  f"{type(exc).__name__}: {exc}", flush=True)
        return False
    if rc != 0:
        if verbose:
            print(f"[xml_editor] system install failed (rc={rc}); "
                  "retrying with --user", flush=True)
        try:
            rc = subprocess.call(cmd_base + ['--user'])
        except Exception as exc:
            if verbose:
                print(f"[xml_editor] --user pip launch failed: "
                      f"{type(exc).__name__}: {exc}", flush=True)
            return False
    if rc != 0:
        if verbose:
            print(f"[xml_editor] pip install --user failed "
                  f"(rc={rc})", flush=True)
        return False
    if verbose:
        print("[xml_editor] imgui-bundle installed.", flush=True)
    try:
        import importlib
        importlib.invalidate_caches()
    except Exception:
        pass
    return _try_import() is not None


def _lazy_import() -> bool:
    """Import the imgui-bundle symbols on first use.  Does NOT
    run pip -- runtime pip would block the pygame main thread
    long enough for Windows to flag the app as Not Responding.
    The startup-time install path in `viewer.run` / `go.bat`
    handles the install before the GL window opens.  Returns
    True if the bindings are loaded, False if the dependency
    is unavailable.
    """
    global _imgui, _renderer_cls
    if _imgui is not None:
        return True
    mods = _try_import()
    if mods is None:
        return False
    _imgui, _renderer_cls = mods
    return True


class _TabState:
    """Per-tab editor state -- the live text buffer (a plain
    Python string), the dirty flag (against the initial text
    snapshot), and the save callback supplied by the viewer.
    """

    def __init__(self, tab_idx: int, label: str, path: str,
                 text: str,
                 save_cb: Callable[[str], None]) -> None:
        self.tab_idx      = int(tab_idx)
        self.label        = str(label)
        self.path         = str(path or '')
        self.text         = text or ''
        self.initial_text = text or ''
        self.save_cb      = save_cb
        self.dirty        = False
        self.open         = True
        # First-frame focus latch.  XMLEditor.render() consumes
        # this once after the tab opens (and after an open_tab
        # refocus) to call imgui.set_keyboard_focus_here() so
        # the input grabs focus without the user having to
        # click into it.
        self.want_focus   = True

    def update_dirty(self) -> None:
        self.dirty = (self.text != self.initial_text)


class XMLEditor:
    """Owns the imgui context + pygame GL renderer + one
    TextEditor per open tab.  Single instance per Viewer.
    """

    def __init__(self) -> None:
        self._ctx    = None
        self._impl   = None
        self._tabs   = {}    # tab_idx -> _TabState
        self._failed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _ensure_init(self, width: int, height: int) -> bool:
        if self._ctx is not None:
            return True
        if self._failed:
            return False
        if not _lazy_import():
            self._failed = True
            return False
        try:
            self._ctx = _imgui.create_context()
        except Exception as exc:
            _log_exc("imgui.create_context() failed", exc)
            self._failed = True
            return False
        try:
            io = _imgui.get_io()
            io.display_size = (float(width), float(height))
            # Don't litter the project dir with imgui.ini.  In
            # this imgui-bundle version `ini_filename` isn't a
            # writable attribute -- use the setter method.  Pass
            # an empty string (treated as "no file") since
            # `None` isn't accepted by the nanobind binding.
            try:
                io.set_ini_filename("")
            except Exception:
                pass
            # Per Coffee 2026-05-20 ("make the font bigger.
            # 125% and bold"): replace the default 13 px ProggyClean
            # with a 16 px (= 125%) bold monospace TTF before the
            # renderer builds its font atlas.  Consolas Bold ships
            # on every modern Windows install; Courier New Bold is
            # the universal fallback.  If neither is on disk (e.g.
            # the user trimmed C:\Windows\Fonts) we leave the default
            # and just bump `font_global_scale` so we at least get
            # the size change.
            self._install_bold_font(io)
        except Exception as exc:
            _log_exc("imgui.get_io() / display_size failed", exc)
            self._failed = True
            self._ctx    = None
            return False
        try:
            self._impl = _renderer_cls()
        except Exception as exc:
            _log_exc("PygameRenderer() init failed", exc)
            self._failed = True
            self._ctx    = None
            self._impl   = None
            return False
        # imgui_bundle's PygameRenderer ships with the alphabet
        # + digit keys COMMENTED OUT of its `key_map` (see
        # `imgui_bundle.python_backends.pygame_backend` ~ line
        # 51).  That means imgui never sees Ctrl+C / V / X / Z /
        # Y / A / F as `mod_ctrl` + Key.c -- the built-in
        # editor shortcuts (copy / paste / undo / redo /
        # select-all / find) all use `is_key_pressed(Key.c)`
        # style detection so they never fire.  We can't just
        # extend `key_map` -- the backend gates on
        # `processed_special_key` and skips the
        # `add_input_character` branch when the key matched,
        # which would break PLAIN typing of those letters.
        # Replace `process_event` instead so both signals go in.
        try:
            self._install_event_patch(self._impl)
        except Exception as exc:
            _log_exc("event patch failed", exc)
        return True

    @staticmethod
    def _install_bold_font(io) -> None:
        """Replace the imgui default font with a 16 px (= ~125 %
        of the 13 px default) bold monospace TTF pulled from the
        system Fonts folder.  No-op + falls back to a scale
        bump if no suitable TTF is found.  MUST be called before
        the renderer constructs its font atlas.
        """
        # Priority list: bold monospace first (best for code +
        # editors), then bold proportional as a fallback.
        win_fonts = os.path.join(
            os.environ.get('SystemRoot', r'C:\Windows'), 'Fonts')
        candidates = [
            'consolab.ttf',     # Consolas Bold (monospace)
            'courbd.ttf',       # Courier New Bold (monospace)
            'lucon.ttf',        # Lucida Console (mono, not bold)
            'arialbd.ttf',      # Arial Bold (proportional)
            'segoeuib.ttf',     # Segoe UI Bold (proportional)
        ]
        chosen = None
        for fn in candidates:
            p = os.path.join(win_fonts, fn)
            if os.path.isfile(p):
                chosen = p
                break
        if chosen is None:
            # Nothing on disk -- at least bump the scale so
            # we still get the size change.
            try:
                io.font_global_scale = 1.25
            except Exception:
                pass
            return
        try:
            # add_font_from_file_ttf builds at the requested px
            # size.  16 px is roughly 125 % of imgui's 13 px
            # default ProggyClean.
            io.fonts.add_font_from_file_ttf(chosen, 16.0)
        except Exception as exc:
            _log_exc("add_font_from_file_ttf failed", exc)
            try:
                io.font_global_scale = 1.25
            except Exception:
                pass

    @staticmethod
    def _install_event_patch(impl) -> None:
        """Replace `impl.process_event` with a version that
        sends imgui BOTH the key event AND the text character
        for alphanumeric keys, so editor shortcuts fire without
        breaking ordinary typing.
        """
        import pygame
        # Build the additional key-mapping (alphabet, digits,
        # common punctuation + F-keys).
        extra_map = {}
        for ch in 'abcdefghijklmnopqrstuvwxyz':
            pg_key = getattr(pygame, f'K_{ch}', None)
            im_key = getattr(_imgui.Key, ch, None)
            if pg_key is not None and im_key is not None:
                extra_map[pg_key] = im_key
        for d in range(10):
            pg_key = getattr(pygame, f'K_{d}', None)
            im_key = getattr(_imgui.Key, f'_{d}', None)
            if pg_key is not None and im_key is not None:
                extra_map[pg_key] = im_key
        punct = {
            'K_SPACE':        'space',
            'K_PERIOD':       'period',
            'K_COMMA':        'comma',
            'K_SLASH':        'slash',
            'K_BACKSLASH':    'backslash',
            'K_SEMICOLON':    'semicolon',
            'K_QUOTE':        'apostrophe',
            'K_LEFTBRACKET':  'left_bracket',
            'K_RIGHTBRACKET': 'right_bracket',
            'K_MINUS':        'minus',
            'K_EQUALS':       'equal',
        }
        for pg_name, im_name in punct.items():
            pg_key = getattr(pygame, pg_name, None)
            im_key = getattr(_imgui.Key, im_name, None)
            if pg_key is not None and im_key is not None:
                extra_map[pg_key] = im_key

        original = impl.process_event

        def patched(event, _extra=extra_map, _orig=original,
                    _impl=impl):
            # For KEYDOWN / KEYUP on a key in `extra_map`, send
            # the imgui key event ourselves, then defer to the
            # original handler -- which won't match the key
            # against its (still-empty) alphabet section, so it
            # falls through to `add_input_character`.  Result:
            # imgui sees BOTH the Key.c event AND the 'c' char.
            if (event.type in (pygame.KEYDOWN, pygame.KEYUP)
                    and event.key in _extra):
                io = _imgui.get_io()
                io.add_key_event(
                    _extra[event.key],
                    down=(event.type == pygame.KEYDOWN))
            return _orig(event)

        impl.process_event = patched

    def shutdown(self) -> None:
        if self._impl is not None:
            try:
                self._impl.shutdown()
            except Exception:
                pass
        if self._ctx is not None and _imgui is not None:
            try:
                _imgui.destroy_context(self._ctx)
            except Exception:
                pass
        self._ctx  = None
        self._impl = None
        self._tabs.clear()

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        """True if at least one editor window is currently open."""
        return any(st.open for st in self._tabs.values())

    def has_dirty(self) -> bool:
        return any(st.open and st.dirty
                   for st in self._tabs.values())

    def dirty_tab_indices(self) -> set:
        return {st.tab_idx for st in self._tabs.values()
                if st.open and st.dirty}

    def wants_keyboard(self) -> bool:
        if self._ctx is None or not self.is_active():
            return False
        try:
            return bool(_imgui.get_io().want_capture_keyboard)
        except Exception:
            return False

    def wants_mouse(self) -> bool:
        if self._ctx is None or not self.is_active():
            return False
        try:
            return bool(_imgui.get_io().want_capture_mouse)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Opening / closing tabs
    # ------------------------------------------------------------------

    def open_tab(self, tab_idx: int, label: str, path: str,
                 text: str,
                 save_cb: Callable[[str], None],
                 width: int, height: int) -> bool:
        """Open (or re-focus) the editor window for `tab_idx`.
        `text` is the initial buffer.  `save_cb(new_text)` fires
        whenever the user hits Save in the editor or Ctrl+S.
        Returns True if the editor was successfully popped,
        False if imgui-bundle isn't installed.
        """
        if not self._ensure_init(width, height):
            return False
        try:
            st = self._tabs.get(tab_idx)
            if st is None or not st.open:
                st = _TabState(tab_idx, label, path, text, save_cb)
                self._tabs[tab_idx] = st
            else:
                # Already open -- just refocus, update metadata.
                st.label      = label
                st.path       = path or ''
                st.save_cb    = save_cb
                st.want_focus = True
        except Exception as exc:
            _log_exc("open_tab failed", exc)
            return False
        return True

    def close_tab(self, tab_idx: int) -> None:
        st = self._tabs.get(tab_idx)
        if st is not None:
            st.open = False

    # ------------------------------------------------------------------
    # Event + render plumbing (called from viewer's main loop)
    # ------------------------------------------------------------------

    def process_event(self, event) -> bool:
        """Feed a pygame event to imgui.  Returns True if imgui
        wants to consume it (i.e. the caller should skip its
        normal event handling).  No-op when no window is open.
        """
        if self._ctx is None or not self.is_active():
            return False
        try:
            self._impl.process_event(event)
        except Exception:
            return False
        # Use the post-event capture flags to gate consumption.
        try:
            import pygame
            io = _imgui.get_io()
            if event.type in (pygame.MOUSEBUTTONDOWN,
                              pygame.MOUSEBUTTONUP,
                              pygame.MOUSEMOTION,
                              pygame.MOUSEWHEEL):
                return bool(io.want_capture_mouse)
            if event.type in (pygame.KEYDOWN,
                              pygame.KEYUP,
                              pygame.TEXTINPUT,
                              pygame.TEXTEDITING):
                return bool(io.want_capture_keyboard)
        except Exception:
            pass
        return False

    def render(self, width: int, height: int,
               dt: float = 1.0 / 60.0) -> None:
        """Per-frame imgui draw.  Call AFTER the main 3D + UI
        passes so the editor window sits on top.  No-op when no
        window is open.  Wrapped in a blanket try/except so any
        imgui-side crash takes the editor offline rather than
        the entire viewer.
        """
        if self._ctx is None or not self.is_active():
            return
        try:
            io = _imgui.get_io()
            io.display_size = (float(width), float(height))
            io.delta_time   = max(1e-3, float(dt))
        except Exception as exc:
            _log_exc("imgui io setup failed", exc)
            self._failed = True
            return
        try:
            self._impl.process_inputs()
            _imgui.new_frame()
        except Exception as exc:
            _log_exc("imgui new_frame failed", exc)
            self._failed = True
            return
        save_now: Optional[_TabState] = None
        # Cache for the post-loop save call so we don't mutate
        # `_tabs` mid-iteration if save_cb closes the window.
        try:
            for st in list(self._tabs.values()):
                if not st.open:
                    continue
                ed_w = min(900, max(400, width  - 80))
                ed_h = min(640, max(300, height - 80))
                _imgui.set_next_window_size(
                    _imgui.ImVec2(ed_w, ed_h),
                    int(_imgui.Cond_.first_use_ever))
                _imgui.set_next_window_pos(
                    _imgui.ImVec2((width  - ed_w) // 2,
                                  (height - ed_h) // 2),
                    int(_imgui.Cond_.first_use_ever))
                title = st.label + (' *' if st.dirty else '')
                title += f"###xml_editor_{st.tab_idx}"
                flags = int(_imgui.WindowFlags_.menu_bar)
                # Per Coffee 2026-05-20 ("make the editor
                # window's background color black or very
                # nearly"): push near-black for the window's
                # backdrop, the child window the TextEditor
                # opens internally, and the menu bar strip.
                # Use a hint of blue-grey (0.02) instead of
                # pure 0.0 to avoid full crush on cheap
                # monitors -- still reads as black, keeps
                # palette-relative anti-aliasing happy.
                bg = _imgui.ImVec4(0.02, 0.02, 0.03, 1.0)
                _imgui.push_style_color(
                    int(_imgui.Col_.window_bg),   bg)
                _imgui.push_style_color(
                    int(_imgui.Col_.child_bg),    bg)
                _imgui.push_style_color(
                    int(_imgui.Col_.menu_bar_bg), bg)
                opened, p_open = _imgui.begin(title, True, flags)
                if not p_open:
                    # User clicked the (x).
                    st.open = False
                    _imgui.end()
                    _imgui.pop_style_color(3)
                    continue
                if not opened:
                    _imgui.end()
                    _imgui.pop_style_color(3)
                    continue
                # File menu: Save + Close.
                if _imgui.begin_menu_bar():
                    if _imgui.begin_menu("File", True):
                        activated, _ = _imgui.menu_item(
                            "Save", "Ctrl+S", False, True)
                        if activated:
                            save_now = st
                        activated, _ = _imgui.menu_item(
                            "Close", "", False, True)
                        if activated:
                            st.open = False
                        _imgui.end_menu()
                    if st.path:
                        _imgui.text(
                            f"  {os.path.basename(st.path)}")
                    _imgui.end_menu_bar()
                # ImGui's built-in multi-line text input.  No
                # syntax highlighting, no line numbers, but the
                # scrollbars actually work and the contents
                # stays inside the window's clip rect -- both
                # of which the ColorTextEdit binding got wrong
                # in this imgui-bundle build.  AllowTabInput
                # lets the user enter tab characters in XML
                # (otherwise Tab would steal focus to the next
                # widget).
                if st.want_focus:
                    try:
                        _imgui.set_keyboard_focus_here()
                    except Exception:
                        pass
                    st.want_focus = False
                try:
                    changed, new_text = (
                        _imgui.input_text_multiline(
                            f"##editor_{st.tab_idx}",
                            st.text,
                            _imgui.ImVec2(-1.0, -1.0),
                            int(_imgui.InputTextFlags_
                                .allow_tab_input)))
                    if changed:
                        st.text = new_text
                except Exception as exc:
                    _log_exc(
                        "input_text_multiline() failed", exc)
                st.update_dirty()
                # Ctrl+S inside the focused editor.
                if (io.key_ctrl
                        and _imgui.is_key_pressed(_imgui.Key.s, False)
                        and _imgui.is_window_focused(0)):
                    save_now = st
                _imgui.end()
                _imgui.pop_style_color(3)
        except Exception as exc:
            _log_exc("imgui window build loop failed", exc)
            self._failed = True
            # Still need to call render() / shutdown the frame
            # cleanly so imgui doesn't leak begin/end state.
            try:
                _imgui.end_frame()
            except Exception:
                pass
            return
        try:
            _imgui.render()
            self._impl.render(_imgui.get_draw_data())
        except Exception as exc:
            _log_exc("imgui draw_data render failed", exc)
            self._failed = True
            return
        if save_now is not None:
            new_text = save_now.text
            try:
                save_now.save_cb(new_text)
                save_now.initial_text = new_text
                save_now.dirty = False
            except Exception:
                pass
