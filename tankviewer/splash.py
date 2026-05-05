"""
Startup splash screen.

Shown immediately after the GL context is created and held visible
while the rest of the viewer initialises (shaders compile, IBL bake,
tier-tree build, etc.).  Self-contained -- compiles its own shader
pair, owns its own VAO/VBO, doesn't depend on UIManager / UIShader
(those don't exist yet at the point we paint the splash).

Usage:
    splash = Splash('resources/splash.png', width, height)
    splash.render()
    pygame.display.flip()
    # ... slow init ...
    splash.cleanup()
"""

import os
import ctypes
import numpy as np

try:
    from PIL import Image
except ImportError:
    Image = None

from OpenGL.GL import *


# NDC-space quad shader supporting both textured (splash, text) and
# solid-color (banner background) draws.  u_use_tex flips the path.
_VS = """#version 330 core
layout(location=0) in vec2 a_pos;
layout(location=1) in vec2 a_uv;
out vec2 v_uv;
void main() {
    gl_Position = vec4(a_pos, 0.0, 1.0);
    v_uv = a_uv;
}
"""

_FS = """#version 330 core
in vec2 v_uv;
out vec4 FragColor;
uniform sampler2D u_tex;
uniform vec4      u_color;
uniform int       u_use_tex;
void main() {
    if (u_use_tex == 1) {
        vec4 t = texture(u_tex, v_uv);
        // Multiply by u_color so we can tint OR alpha-modulate the
        // text texture (alpha 0 outside glyphs).
        FragColor = t * u_color;
    } else {
        FragColor = u_color;
    }
}
"""


class Splash:
    """Fullscreen splash image rendered as a textured NDC quad,
    optionally with a centred 'Welcome to ...' banner overlay
    (white text on burnt-orange background, matching the Tank
    Exporter colour scheme)."""

    def __init__(self, image_path, window_w, window_h, welcome_text=None):
        if Image is None:
            raise RuntimeError("Pillow (PIL) is required for the splash")
        if not os.path.isfile(image_path):
            raise FileNotFoundError(image_path)

        self.window_w = int(window_w)
        self.window_h = int(window_h)
        self.welcome_text = welcome_text or ''

        # ---- Load + upload the splash texture --------------------------
        img = Image.open(image_path).convert('RGBA')
        # GL texture origin is bottom-left; PNGs are top-left
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
        self.tex_w = img.width
        self.tex_h = img.height
        data = img.tobytes()

        self.tex_id = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, self.tex_id)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8,
                     self.tex_w, self.tex_h, 0,
                     GL_RGBA, GL_UNSIGNED_BYTE, data)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        glBindTexture(GL_TEXTURE_2D, 0)

        # ---- Compile the tiny splash shader ----------------------------
        self.program     = self._compile_program(_VS, _FS)
        self.u_tex       = glGetUniformLocation(self.program, 'u_tex')
        self.u_color     = glGetUniformLocation(self.program, 'u_color')
        self.u_use_tex   = glGetUniformLocation(self.program, 'u_use_tex')

        # ---- Welcome banner texture (rendered via pygame.font) ---------
        # Filled when welcome_text is supplied; else stays at 0/None.
        self.banner_text_tex = 0
        self.banner_text_w   = 0
        self.banner_text_h   = 0
        if self.welcome_text:
            self._build_banner_texture()

        # ---- Status log (top-down, grows with every set_status call) --
        # Shows short progress messages on the splash so users know the
        # app isn't hung during the multi-second startup phase (shaders
        # compile, IBL bake, pkg pre-warm, tier-tree build).  Each call
        # to set_status() APPENDS a new line under the previous one --
        # the log builds top-down as the user watches.  Rendered as
        # white text on per-line translucent dark strips for legibility
        # against whatever splash artwork is showing.
        #
        # Each entry is a dict: {'text': str, 'tex': GLuint, 'w': int, 'h': int}
        # Cleaned up wholesale in cleanup() at viewer-shutdown time.
        self.status_lines = []
        # Max-character cap per line so a long path string can't push
        # past the splash's edge.  Also caps total kept (we don't
        # expect more than ~15 entries during init, so no real cap
        # needed; this is just defensive).
        self._status_max_chars = 80
        self._status_max_lines = 30

        # ---- Fullscreen quad in NDC, with letterbox padding ------------
        # Aspect-fit the splash inside the window so it's not stretched.
        # Excess area on the left/right (or top/bottom) is left black via
        # the clear in render().
        sx, sy = self._aspect_fit_scale()
        verts = np.array([
            #  pos.x, pos.y,    uv.x, uv.y
            (-sx, -sy,           0.0, 0.0),
            ( sx, -sy,           1.0, 0.0),
            ( sx,  sy,           1.0, 1.0),
            (-sx, -sy,           0.0, 0.0),
            ( sx,  sy,           1.0, 1.0),
            (-sx,  sy,           0.0, 1.0),
        ], dtype=np.float32)

        self.vao = glGenVertexArrays(1)
        self.vbo = glGenBuffers(1)
        glBindVertexArray(self.vao)
        glBindBuffer(GL_ARRAY_BUFFER, self.vbo)
        glBufferData(GL_ARRAY_BUFFER, verts.nbytes, verts, GL_STATIC_DRAW)
        # 4 floats per vertex: 2 pos + 2 uv = 16 bytes stride
        stride = 4 * 4
        glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, stride,
                              ctypes.c_void_p(0))
        glEnableVertexAttribArray(0)
        glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, stride,
                              ctypes.c_void_p(8))
        glEnableVertexAttribArray(1)
        glBindVertexArray(0)

        # ---- Dynamic quad VAO for banner overlay (rect + text) ---------
        self.banner_vao = glGenVertexArrays(1)
        self.banner_vbo = glGenBuffers(1)
        glBindVertexArray(self.banner_vao)
        glBindBuffer(GL_ARRAY_BUFFER, self.banner_vbo)
        # 6 verts * 4 floats * 4 bytes = 96 bytes; allocate, fill per-frame
        glBufferData(GL_ARRAY_BUFFER, 96, None, GL_DYNAMIC_DRAW)
        glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, stride,
                              ctypes.c_void_p(0))
        glEnableVertexAttribArray(0)
        glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, stride,
                              ctypes.c_void_p(8))
        glEnableVertexAttribArray(1)
        glBindVertexArray(0)

    # ------------------------------------------------------------------
    # Cursive / script font fallback chain for the welcome banner.
    # pygame.font.SysFont walks the comma-list left-to-right and uses
    # the first installed match.  Gabriola sits at the top -- it's
    # the user-picked face (Vista+ Microsoft, ornate stylistic
    # alternates, reads beautifully on the burnt-orange strip).
    # Everything below it is a graceful-degradation chain for the
    # rare box where Gabriola isn't installed.
    _BANNER_FONT_CHAIN = (
        'Gabriola,'                  # picked face (Vista+ MS, ornate)
        'Monotype Corsiva,'          # classic cursive, ships with Office
        'Lucida Handwriting,'        # handwriting-style fallback
        'Segoe Script,'              # Windows 7+ script face
        'Brush Script MT,'           # legacy office font
        'Comic Sans MS,'             # informal but always available
        'Calibri'                    # safe stop
    )

    def _build_banner_texture(self):
        """Render self.welcome_text to a GL texture using pygame.font.
        Stored on self.banner_text_tex / w / h.  No-op on failure --
        the banner just doesn't appear in that case.

        Uses a cursive font chain so the welcome message reads like a
        signature rather than a flat sans-serif label.  Cursive faces
        are usually display-only (no real bold cut), so we render at
        a larger size instead of forcing bold -- the heavier strokes
        come from the typeface itself.
        """
        try:
            import pygame
            if not pygame.font.get_init():
                pygame.font.init()
            font = pygame.font.SysFont(self._BANNER_FONT_CHAIN, 32)
            surf = font.render(self.welcome_text, True, (255, 255, 255))
            data = pygame.image.tostring(surf, 'RGBA', False)
            self.banner_text_w = surf.get_width()
            self.banner_text_h = surf.get_height()

            self.banner_text_tex = glGenTextures(1)
            glBindTexture(GL_TEXTURE_2D, self.banner_text_tex)
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8,
                         self.banner_text_w, self.banner_text_h, 0,
                         GL_RGBA, GL_UNSIGNED_BYTE, data)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
            glBindTexture(GL_TEXTURE_2D, 0)
        except Exception as exc:
            print(f"[splash] banner build failed: {exc}")
            self.banner_text_tex = 0

    # ------------------------------------------------------------------
    def set_status(self, text):
        """Append one line to the splash status log.

        Each call adds a NEW line under the previous one -- the log
        builds top-down as the user watches startup progress.  Caller
        should follow with `self.render()` and `pygame.display.flip()`
        (Viewer's `_splash_status` helper does both).

        An empty / None text is ignored (no spurious blank lines).
        """
        text = (text or '').strip()
        if not text:
            return
        # Cap each line at _status_max_chars so a long path string
        # never extends past the splash's left edge.
        if len(text) > self._status_max_chars:
            text = text[:self._status_max_chars - 1] + '...'

        try:
            import pygame
            if not pygame.font.get_init():
                pygame.font.init()
            # Calibri 16 bold -- user's pick.  Big enough to read across
            # the room; a 1 px black drop-shadow in render() handles
            # legibility against the splash artwork without a backdrop.
            font = pygame.font.SysFont('Calibri', 16, bold=True)
            surf = font.render(text, True, (255, 255, 255))
            data = pygame.image.tostring(surf, 'RGBA', False)
            tex = glGenTextures(1)
            glBindTexture(GL_TEXTURE_2D, tex)
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8,
                         surf.get_width(), surf.get_height(), 0,
                         GL_RGBA, GL_UNSIGNED_BYTE, data)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
            glBindTexture(GL_TEXTURE_2D, 0)
            self.status_lines.append({
                'text': text,
                'tex':  tex,
                'w':    surf.get_width(),
                'h':    surf.get_height(),
            })
            # Cap total kept (defensive; init shouldn't produce 30+ lines)
            if len(self.status_lines) > self._status_max_lines:
                old = self.status_lines.pop(0)
                if old.get('tex'):
                    try: glDeleteTextures(1, [old['tex']])
                    except Exception: pass
        except Exception as exc:
            # Splash status is a UX nicety; never let it crash startup.
            print(f"[splash] status texture failed: {exc}")

    # ------------------------------------------------------------------
    def _set_banner_quad(self, px, py, pw, ph, flip_v=False):
        """Update the banner VBO with a quad covering pixel rect
        (px, py, pw, ph) in screen coords (top-left origin).  Converts
        to NDC.  flip_v=True is for pygame text textures whose row 0
        is at the TOP of the surface (no PIL FLIP_TOP_BOTTOM applied)."""
        if self.window_w <= 0 or self.window_h <= 0:
            return
        # Pixel rect -> NDC.  Window y=0 is top, NDC y=+1 is top.
        x0 =  (px            / self.window_w) * 2.0 - 1.0
        x1 = ((px + pw)      / self.window_w) * 2.0 - 1.0
        y0 = 1.0 - (py             / self.window_h) * 2.0   # top edge (high NDC y)
        y1 = 1.0 - ((py + ph)      / self.window_h) * 2.0   # bottom edge
        # UV mapping for the four corners.  For a pygame surface the
        # first row of pixel data IS the visual top of the image, so
        # screen-top must sample V=0 to display right-side-up.  For a
        # GL-prepared texture (FLIP_TOP_BOTTOM during upload), screen-
        # top samples V=1 instead.
        if flip_v:
            v_top, v_bot = 0.0, 1.0     # pygame surface upload
        else:
            v_top, v_bot = 1.0, 0.0     # GL bottom-left convention
        verts = np.array([
            (x0, y1,   0.0, v_bot),     # bottom-left
            (x1, y1,   1.0, v_bot),     # bottom-right
            (x1, y0,   1.0, v_top),     # top-right
            (x0, y1,   0.0, v_bot),
            (x1, y0,   1.0, v_top),
            (x0, y0,   0.0, v_top),     # top-left
        ], dtype=np.float32)
        glBindBuffer(GL_ARRAY_BUFFER, self.banner_vbo)
        glBufferSubData(GL_ARRAY_BUFFER, 0, verts.nbytes, verts)

    # ------------------------------------------------------------------
    def _aspect_fit_scale(self):
        """Return (sx, sy) NDC half-extents that letterbox the splash to
        the current window aspect ratio.  Splash texture is centered."""
        if self.window_w <= 0 or self.window_h <= 0:
            return 1.0, 1.0
        win_aspect = self.window_w / self.window_h
        tex_aspect = self.tex_w   / self.tex_h
        if tex_aspect > win_aspect:
            # Splash is wider than window -> letterbox top/bottom
            sx = 1.0
            sy = win_aspect / tex_aspect
        else:
            # Splash is taller than window -> letterbox left/right
            sx = tex_aspect / win_aspect
            sy = 1.0
        return float(sx), float(sy)

    # ------------------------------------------------------------------
    # Burnt-orange banner colour, matched to the rest of the UI
    # (active toggle buttons, active tier tab, selected tree row).
    BANNER_BG = (0.78, 0.33, 0.05, 1.0)

    def render(self):
        """Paint the splash full-window plus the optional welcome
        banner overlay.  Caller is responsible for pygame.display.flip()."""
        glViewport(0, 0, self.window_w, self.window_h)
        glClearColor(0.0, 0.0, 0.0, 1.0)
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)

        glDisable(GL_DEPTH_TEST)
        glDisable(GL_CULL_FACE)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

        glUseProgram(self.program)

        # ---- 1. Splash background (textured, white tint = no tint) -----
        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, self.tex_id)
        glUniform1i(self.u_tex,     0)
        glUniform1i(self.u_use_tex, 1)
        glUniform4f(self.u_color,   1.0, 1.0, 1.0, 1.0)
        glBindVertexArray(self.vao)
        glDrawArrays(GL_TRIANGLES, 0, 6)

        # ---- 2. Welcome banner overlay --------------------------------
        if self.banner_text_tex:
            # Banner anchored to the top-right corner of the splash, 15
            # pixels off both edges.  Text size dictates strip size.
            EDGE = 15
            pad_x = 24
            pad_y = 14
            rect_w = self.banner_text_w + pad_x * 2
            rect_h = self.banner_text_h + pad_y * 2
            rect_x = max(0, self.window_w - rect_w - EDGE)
            rect_y = EDGE
            text_x = rect_x + pad_x
            text_y = rect_y + pad_y

            # 2a. Burnt-orange background
            self._set_banner_quad(rect_x, rect_y, rect_w, rect_h)
            glUniform1i(self.u_use_tex, 0)
            glUniform4f(self.u_color, *self.BANNER_BG)
            glBindVertexArray(self.banner_vao)
            glDrawArrays(GL_TRIANGLES, 0, 6)

            # 2b. White text on top (alpha-blended via the texture's alpha)
            self._set_banner_quad(text_x, text_y,
                                  self.banner_text_w, self.banner_text_h,
                                  flip_v=True)
            glActiveTexture(GL_TEXTURE0)
            glBindTexture(GL_TEXTURE_2D, self.banner_text_tex)
            glUniform1i(self.u_tex,     0)
            glUniform1i(self.u_use_tex, 1)
            glUniform4f(self.u_color,   1.0, 1.0, 1.0, 1.0)
            glDrawArrays(GL_TRIANGLES, 0, 6)

        # ---- 3. Status log (top-down stack, transparent + scrolling) --
        # Each set_status() call appended a line; we render them all
        # stacked top-down so the user sees startup accomplishments
        # accumulate in real time.  When the stack would extend past
        # the bottom of the splash, the WHOLE log shifts up by the
        # excess so the newest line stays visible -- older lines
        # naturally fall off the top of the window (GL viewport clips
        # them automatically).
        # No solid backdrop -- the text is white-on-splash with a 1 px
        # dark drop-shadow drawn one pixel down/right.  That's enough
        # contrast to keep the white text readable against any splash
        # artwork without a translucent strip muddying the background.
        if self.status_lines:
            LEFT_PAD     = 18         # left margin for the column
            TOP_PAD      = 18         # top margin (clear of corner)
            BOTTOM_PAD   = 18         # don't run text into the very bottom edge
            LINE_GAP     = 4          # vertical pixel gap between rows
            ROW_PAD      = 2          # vertical pad per row (legibility)

            # Per-line vertical advance.  Lines might have slightly
            # different heights (e.g. glyphs with descenders), but in
            # practice Calibri 16 bold renders at a consistent height.
            line_advances = [
                line['h'] + ROW_PAD * 2 + LINE_GAP
                for line in self.status_lines
            ]
            total_h = sum(line_advances)
            available_h = self.window_h - TOP_PAD - BOTTOM_PAD

            # Scroll offset: when the stack overflows, shift everything
            # UP so the newest line ends near the bottom of the window.
            # Older lines that would now be ABOVE the splash's top edge
            # render off-screen and clip naturally via the GL viewport.
            scroll_off = max(0, total_h - available_h)

            cur_y = TOP_PAD - scroll_off
            glBindVertexArray(self.banner_vao)
            for line, adv in zip(self.status_lines, line_advances):
                tx = LEFT_PAD
                ty = cur_y + ROW_PAD

                # 3a. Black drop-shadow -- same texture, +1 px offset,
                # darkened via the colour multiply.  Cheap (one extra
                # quad per line) and dramatically improves readability.
                self._set_banner_quad(tx + 1, ty + 1,
                                      line['w'], line['h'],
                                      flip_v=True)
                glActiveTexture(GL_TEXTURE0)
                glBindTexture(GL_TEXTURE_2D, line['tex'])
                glUniform1i(self.u_tex,     0)
                glUniform1i(self.u_use_tex, 1)
                # Fragment = texture * u_color; texture is white +
                # alpha mask, so multiplying by black turns it into a
                # black silhouette.  Alpha stays 1.0 so the shadow
                # sits at full opacity wherever glyph alpha is > 0.
                glUniform4f(self.u_color, 0.0, 0.0, 0.0, 1.0)
                glDrawArrays(GL_TRIANGLES, 0, 6)

                # 3b. White text on top of the shadow
                self._set_banner_quad(tx, ty,
                                      line['w'], line['h'],
                                      flip_v=True)
                glUniform4f(self.u_color, 1.0, 1.0, 1.0, 1.0)
                glDrawArrays(GL_TRIANGLES, 0, 6)

                cur_y += adv

        glBindVertexArray(0)

        # Restore the GL state the rest of the viewer expects
        glDisable(GL_BLEND)
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_CULL_FACE)

    # ------------------------------------------------------------------
    def cleanup(self):
        if self.tex_id:
            glDeleteTextures(1, [self.tex_id])
            self.tex_id = None
        if self.banner_text_tex:
            glDeleteTextures(1, [self.banner_text_tex])
            self.banner_text_tex = 0
        # Free every status-line texture in the top-down log.
        for line in self.status_lines:
            tex = line.get('tex')
            if tex:
                try: glDeleteTextures(1, [tex])
                except Exception: pass
        self.status_lines = []
        if self.vbo:
            glDeleteBuffers(1, [self.vbo])
            self.vbo = None
        if self.vao:
            glDeleteVertexArrays(1, [self.vao])
            self.vao = None
        if self.banner_vbo:
            glDeleteBuffers(1, [self.banner_vbo])
            self.banner_vbo = None
        if self.banner_vao:
            glDeleteVertexArrays(1, [self.banner_vao])
            self.banner_vao = None
        if self.program:
            glDeleteProgram(self.program)
            self.program = None

    # ------------------------------------------------------------------
    @staticmethod
    def _compile_program(vsrc, fsrc):
        """Compile + link a vert/frag pair, return the GL program ID."""
        def compile_one(src, kind):
            sh = glCreateShader(kind)
            glShaderSource(sh, src)
            glCompileShader(sh)
            if not glGetShaderiv(sh, GL_COMPILE_STATUS):
                log = glGetShaderInfoLog(sh).decode('utf-8', errors='replace')
                raise RuntimeError(f"splash shader compile failed:\n{log}")
            return sh
        vs = compile_one(vsrc, GL_VERTEX_SHADER)
        fs = compile_one(fsrc, GL_FRAGMENT_SHADER)
        prog = glCreateProgram()
        glAttachShader(prog, vs)
        glAttachShader(prog, fs)
        glLinkProgram(prog)
        if not glGetProgramiv(prog, GL_LINK_STATUS):
            log = glGetProgramInfoLog(prog).decode('utf-8', errors='replace')
            raise RuntimeError(f"splash program link failed:\n{log}")
        glDeleteShader(vs)
        glDeleteShader(fs)
        return prog
