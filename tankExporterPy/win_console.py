"""Win32 helpers for the console window TEPY launches from.

Used at startup to minimize / hide the cmd window so the user sees
the GL viewport rather than a stack of black batch-file output.
Stdout still goes to the console -- diagnostic prints are preserved
for anyone who pops the console open mid-session via the taskbar.

No-ops on non-Windows platforms (`os.name != 'nt'`).  Also a no-op
when the launcher script wasn't started from a console at all
(double-click on a .py / pythonw.exe / IDE run -- there's no console
window to minimise).

Public API:
    minimize_console()  -- minimise the launching cmd window
    hide_console()      -- fully hide it (still receives prints,
                           reachable via task manager only)
    restore_console()   -- bring it back to normal-window state
"""

import os


# Win32 ShowWindow command codes -- values from winuser.h.
_SW_HIDE      = 0
_SW_NORMAL    = 1   # SW_SHOWNORMAL
_SW_MINIMIZE  = 6


def _console_hwnd():
    """Return the Win32 HWND of the console window TEPY was launched
    from, or None when there's nothing to act on (non-Windows,
    pythonw.exe, no console attached, ctypes import failure).
    """
    if os.name != 'nt':
        return None
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # GetConsoleWindow returns NULL when no console is attached
        # (common for pythonw.exe / GUI shortcut launches).
        hwnd = kernel32.GetConsoleWindow()
        return hwnd if hwnd else None
    except Exception:
        return None


def _show_console(state):
    """ShowWindow on the console handle with `state`.  Silent fail
    when ctypes / user32 / the console handle aren't available."""
    hwnd = _console_hwnd()
    if not hwnd:
        return False
    try:
        import ctypes
        ctypes.windll.user32.ShowWindow(hwnd, state)
        return True
    except Exception:
        return False


def minimize_console():
    """Minimise the launching cmd window.  No-op on non-Windows.

    Called from the launcher entry point before pygame init so the
    user only ever briefly sees the cmd flash up before the GL
    window takes over.  The console keeps receiving stdout so log
    lines are recoverable -- pop the taskbar entry to see them.
    """
    return _show_console(_SW_MINIMIZE)


def hide_console():
    """Hide the launching cmd window completely.  No-op on
    non-Windows.  Stricter than `minimize_console` -- the window
    disappears from the taskbar; only the task manager (or
    `restore_console()` from in-process) brings it back.
    """
    return _show_console(_SW_HIDE)


def restore_console():
    """Bring the launching cmd window back to a normal visible
    state after a prior minimize / hide.  No-op on non-Windows.
    """
    return _show_console(_SW_NORMAL)
