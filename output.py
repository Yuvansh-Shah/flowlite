"""Paste text at the cursor via clipboard + Ctrl+V, preserving the user's clipboard.

Before pasting we sniff the foreground window's class to decide whether
the target can plausibly receive a Ctrl+V. The desktop, taskbar, and
Start menu show up here often when a transcript would otherwise vanish
into thin air — we skip the paste attempt and let the caller queue the
text for retry via the retrieve hotkey.
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from enum import Enum
from typing import Optional

import keyboard
import pyperclip

from config import OutputConfig


# Windows class names that mean "no real text field will accept Ctrl+V here".
# Each one represents a system surface that owns the foreground when the user
# clicked the pill without first focusing a text input.
_UNPASTABLE_CLASSES = frozenset({
    "Progman",                       # the desktop
    "WorkerW",                       # desktop wallpaper layer
    "Shell_TrayWnd",                 # primary taskbar
    "Shell_SecondaryTrayWnd",        # secondary-monitor taskbar
    "DV2ControlHost",                # Windows 10 start menu
    "Windows.UI.Core.CoreWindow",    # Windows 11 start menu / search
    "TrayNotifyWnd",                 # notification area
    "TaskListThumbnailWnd",          # alt-tab preview
    "MultitaskingViewFrame",         # Task View
    "XamlExplorerHostIslandWindow",  # some shell surfaces
})

# Window classes of common text-input controls — when the focused control
# in the foreground thread matches one of these, paste is almost certainly
# going to land somewhere useful.
_TEXT_INPUT_CLASSES = frozenset({
    "Edit",                                    # Win32 single/multi-line edit
    "RichEdit", "RichEdit20A", "RichEdit20W",  # rich edit variants
    "RichEdit50W", "RICHEDIT60W",
    "Scintilla",                               # code editors (Notepad++, SciTE)
    "Chrome_RenderWidgetHostHWND",             # Chromium content area (incl. inputs)
    "MozillaWindowClass",                      # Firefox content area
    "Internet Explorer_Server",                # legacy IE/embedded
    "_WwG",                                    # Microsoft Word text area
    "EXCEL7",                                  # Excel cell-edit
    "OpusApp",                                 # Word top-level
    "PPTFrameClass",                           # PowerPoint
    "OUTEX_LVWS_CLASS",                        # Outlook list-view, etc.
    "rctrl_renwnd32",                          # Outlook compose
    "Notepad",                                 # Notepad top-level
    "ConsoleWindowClass",                      # Windows console (accepts paste)
    "VirtualConsoleClass",                     # Windows Terminal
    "CASCADIA_HOSTING_WINDOW_CLASS",           # Windows Terminal
    "WindowsForms10.EDIT.app.0.*",             # WinForms TextBox prefix (matched via startswith)
    "Qt5QWindowIcon", "Qt6QWindowIcon",        # Qt apps — content windows
    "SunAwtCanvas",                            # JetBrains IDEs / Swing
    "GtkWidget",                               # GTK apps
})


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


# Resolve user32 once and pin argtypes/restypes so 64-bit HWND values
# round-trip correctly. Without this, ctypes defaults to c_int return,
# which silently truncates 8-byte handles to 4 bytes on x64 Python and
# breaks every subsequent call.
if sys.platform.startswith("win"):
    _user32 = ctypes.windll.user32
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetGUIThreadInfo.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(_GUITHREADINFO),
    ]
    _user32.GetGUIThreadInfo.restype = wintypes.BOOL
else:
    _user32 = None


class PasteResult(Enum):
    PASTED = "pasted"
    SKIPPED = "skipped"     # focus check decided not to paste
    FAILED = "failed"       # exception raised during clipboard/keyboard work


def get_foreground_class() -> Optional[str]:
    """Return the window-class name of the currently-focused top-level
    window. Returns "" if there's no foreground window (rare), None only
    if not on Windows."""
    if _user32 is None:
        return None
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    n = _user32.GetClassNameW(hwnd, buf, 256)
    if n <= 0:
        return ""
    return buf.value


def is_unpastable_target() -> bool:
    """True when the foreground window is a known non-text-input surface."""
    cls = get_foreground_class()
    if cls is None:
        return False
    return cls in _UNPASTABLE_CLASSES


def get_focused_control_info() -> tuple[Optional[str], bool]:
    """Inspect the focused control inside the foreground window's thread.
    Returns (control_class_name, has_caret)."""
    if _user32 is None:
        return None, False
    fg_hwnd = _user32.GetForegroundWindow()
    if not fg_hwnd:
        return None, False
    fg_thread_id = _user32.GetWindowThreadProcessId(fg_hwnd, None)
    if not fg_thread_id:
        return None, False
    gti = _GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(_GUITHREADINFO)
    if not _user32.GetGUIThreadInfo(fg_thread_id, ctypes.byref(gti)):
        return None, False
    focused = gti.hwndFocus
    has_caret = bool(gti.hwndCaret)
    if not focused:
        return None, has_caret
    buf = ctypes.create_unicode_buffer(256)
    n = _user32.GetClassNameW(focused, buf, 256)
    if n <= 0:
        return None, has_caret
    return buf.value, has_caret


def paste_target_status() -> tuple[bool, str]:
    """Return (can_paste, reason).

    Hard skips (definitely-bad targets):
      - Foreground class on blacklist (desktop, taskbar, start menu, etc.)
      - On Windows with no foreground window at all (lock screen, transient)

    Otherwise we paste optimistically. The GUITHREADINFO probe earlier
    was rejecting valid Chromium/Electron/Qt apps that don't expose their
    internal text focus as a child HWND with a caret — a Chrome web page
    with a focused <input> reports focus on the renderer HWND, no caret,
    so the strict check was a constant false negative. If an optimistic
    paste silently lands nowhere, Ctrl+Alt+V is the retrieve hatch.
    """
    cls = get_foreground_class()
    if cls is None:
        return True, "non-windows-platform"
    if cls == "":
        return False, "no foreground window"
    if cls in _UNPASTABLE_CLASSES:
        return False, f"class blacklisted: {cls}"
    focused_cls, has_caret = get_focused_control_info()
    return True, f"foreground={cls} focus={focused_cls or '?'} caret={has_caret}"


def paste(text: str, cfg: OutputConfig, force: bool = False) -> PasteResult:
    """Paste text at the cursor. If `force` is False and the foreground window
    can't accept a paste, skip the attempt and return SKIPPED so the caller
    can save the text and warn the user."""
    if not text:
        return PasteResult.FAILED
    if not force:
        can_paste, reason = paste_target_status()
        print(f"[output] paste target: can={can_paste} ({reason})", file=sys.stderr)
        if not can_paste:
            return PasteResult.SKIPPED

    saved: Optional[str] = None
    if cfg.preserve_clipboard:
        try:
            saved = pyperclip.paste()
        except Exception as e:
            print(f"[output] couldn't read clipboard: {e}", file=sys.stderr)
    try:
        pyperclip.copy(text)
        time.sleep(cfg.paste_delay_ms / 1000.0)
        keyboard.send("ctrl+v")
        time.sleep(cfg.paste_delay_ms / 1000.0)
    except Exception as e:
        print(f"[output] paste failed: {e}", file=sys.stderr)
        return PasteResult.FAILED
    if cfg.preserve_clipboard and saved is not None:
        try:
            time.sleep(0.05)
            pyperclip.copy(saved)
        except Exception as e:
            print(f"[output] couldn't restore clipboard: {e}", file=sys.stderr)
    return PasteResult.PASTED
