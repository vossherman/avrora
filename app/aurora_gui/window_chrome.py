"""A shared application/title bar while retaining Windows' native resize frame."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os


class WindowChrome:
    """Only customize our own HWND; Tk still owns its window procedure and lifecycle."""

    def __init__(self, window):
        self.window = window
        self.hwnd = None
        self.user32 = None
        self.original_style = None

    def install(self) -> bool:
        if os.name != "nt":
            return False
        api = ctypes.WinDLL("user32", use_last_error=True)
        api.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        api.GetAncestor.restype = wintypes.HWND
        self.get_style = getattr(api, "GetWindowLongPtrW", api.GetWindowLongW)
        self.set_style = getattr(api, "SetWindowLongPtrW", api.SetWindowLongW)
        self.get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        self.get_style.restype = ctypes.c_ssize_t
        self.set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self.set_style.restype = ctypes.c_ssize_t
        api.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        api.SetWindowPos.restype = wintypes.BOOL
        api.IsZoomed.argtypes = [wintypes.HWND]
        api.IsZoomed.restype = wintypes.BOOL
        api.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        api.ShowWindow.restype = wintypes.BOOL
        api.ReleaseCapture.argtypes = []
        api.ReleaseCapture.restype = wintypes.BOOL
        api.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                     ctypes.c_size_t, ctypes.c_ssize_t]
        api.SendMessageW.restype = ctypes.c_ssize_t
        self.hwnd = api.GetAncestor(self.window.winfo_id(), 2)  # GA_ROOT (Tk wrapper HWND)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.user32 = api
        self.original_style = self.get_style(self.hwnd, -16)  # GWL_STYLE
        try:
            # Remove the caption only. Keep the native sizing border, taskbar,
            # system menu and minimize/maximize capabilities.
            self._apply_style(self.original_style & ~0x00C00000)  # WS_CAPTION
        except OSError:
            self._apply_style(self.original_style)
            raise
        return True

    def _apply_style(self, style):
        ctypes.set_last_error(0)
        previous = self.set_style(self.hwnd, -16, style)
        if not previous and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        # NOSIZE | NOMOVE | NOZORDER | NOACTIVATE | FRAMECHANGED
        if not self.user32.SetWindowPos(self.hwnd, None, 0, 0, 0, 0, 0x0037):
            raise ctypes.WinError(ctypes.get_last_error())

    def is_maximized(self):
        return bool(self.user32.IsZoomed(self.hwnd))

    def minimize(self):
        self.user32.ShowWindow(self.hwnd, 6)  # SW_MINIMIZE

    def toggle_maximize(self):
        self.user32.ShowWindow(self.hwnd, 9 if self.is_maximized() else 3)

    def start_move(self, event):
        self.user32.ReleaseCapture()
        position = (int(event.x_root) & 0xffff) | ((int(event.y_root) & 0xffff) << 16)
        self.user32.SendMessageW(self.hwnd, 0x00A1, 2, position)  # WM_NCLBUTTONDOWN, HTCAPTION
