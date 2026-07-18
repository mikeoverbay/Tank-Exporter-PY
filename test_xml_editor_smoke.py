"""Headless smoke-test for the imgui XML editor wrapper.

Boots a minimal pygame OpenGL window, opens the XMLEditor on a
synthetic XML buffer, runs a few render frames, then closes.
Used to verify the editor's GL init + render path don't crash
on this machine without requiring the full TEPY viewer.

Run:
    python test_xml_editor_smoke.py
"""

from __future__ import annotations

import sys
import time

import pygame
from pygame.locals import DOUBLEBUF, OPENGL


def main() -> int:
    pygame.init()
    pygame.display.set_mode((1024, 720), DOUBLEBUF | OPENGL)
    pygame.display.set_caption("xml_editor smoke test")

    from tankExporterPy.xml_editor import (
        XMLEditor, ensure_imgui_bundle_installed)

    print("[smoke] ensure imgui-bundle installed...")
    ok = ensure_imgui_bundle_installed()
    print(f"[smoke] imgui-bundle importable: {ok}")
    if not ok:
        print("[smoke] FAIL: dep missing")
        return 1

    ed = XMLEditor()
    sample = (
        "<?xml version=\"1.0\"?>\n"
        "<root>\n"
        "  <child id=\"a\">value</child>\n"
        "  <child id=\"b\">42</child>\n"
        "</root>\n")

    saved_buf = {}

    def save_cb(text):
        saved_buf['t'] = text
        print(f"[smoke] save_cb called ({len(text)} chars)")

    opened = ed.open_tab(
        tab_idx=0, label="test.visual",
        path="/tmp/test.xml", text=sample,
        save_cb=save_cb, width=1024, height=720)
    print(f"[smoke] open_tab returned: {opened}")
    if not opened:
        print("[smoke] FAIL: open_tab returned False")
        return 1
    print(f"[smoke] is_active: {ed.is_active()}")

    from OpenGL.GL import glClear, glClearColor
    from OpenGL.GL import GL_COLOR_BUFFER_BIT, GL_DEPTH_BUFFER_BIT

    frames = 0
    t0 = time.time()
    while frames < 60:    # ~1 s at 60 fps
        for event in pygame.event.get():
            ed.process_event(event)
            if event.type == pygame.QUIT:
                frames = 999
                break
        glClearColor(0.1, 0.1, 0.15, 1.0)
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        ed.render(1024, 720, dt=1.0 / 60.0)
        pygame.display.flip()
        frames += 1
        time.sleep(1.0 / 60.0)
    elapsed = time.time() - t0
    print(f"[smoke] {frames} frames in {elapsed:.2f}s")
    print(f"[smoke] editor._failed flag: {ed._failed}")
    ed.shutdown()
    pygame.quit()
    print("[smoke] OK -- editor opened, rendered, shut down cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
