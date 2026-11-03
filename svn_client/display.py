"""High-DPI startup and native, available font selection for Tk.

Call ``enable_high_dpi`` before creating any Tk window. Font sizes stay in
points; ``DisplayMetrics.px`` is only for layout values expressed in pixels.
No system display settings or font-smoothing preferences are changed.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from functools import lru_cache
import math
import sys
import tkinter as tk
from tkinter import font as tkfont


def _windows_library(name):
    try:
        return ctypes.WinDLL(name, use_last_error=True)
    except (AttributeError, OSError):
        return None


def _last_error():
    return ctypes.get_last_error()


@lru_cache(maxsize=1)
def enable_high_dpi():
    """Avoid Windows bitmap scaling, preserving an already chosen DPI mode.

    Recent Windows uses per-monitor v2. Older Windows falls back through
    per-monitor v1 and system awareness. Unavailable APIs are harmless on
    other platforms. The returned value is diagnostic, not a success claim
    about a host process whose DPI awareness was already set.
    """
    if sys.platform != "win32":
        return "native"
    user32 = _windows_library("user32.dll")
    context = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if context is not None:
        context.argtypes = [ctypes.c_void_p]
        context.restype = ctypes.c_int
        for value, mode in ((-4, "per-monitor-v2"), (-3, "per-monitor")):
            if context(ctypes.c_void_p(value)):
                return mode
            # An embedding host or executable manifest owns the existing mode.
            # Do not attempt to replace it with thread-level overrides.
            if _last_error() == 5:  # ERROR_ACCESS_DENIED
                return "existing"
    shcore = _windows_library("shcore.dll")
    awareness = getattr(shcore, "SetProcessDpiAwareness", None)
    if awareness is not None:
        awareness.argtypes = [ctypes.c_int]
        awareness.restype = ctypes.c_long
        result = awareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        if result == 0:
            return "per-monitor"
        if result & 0xFFFFFFFF == 0x80070005:  # E_ACCESSDENIED
            return "existing"
    legacy = getattr(user32, "SetProcessDPIAware", None)
    if legacy is not None:
        legacy.argtypes = []
        legacy.restype = ctypes.c_int
        if legacy():
            return "system"
    return "unavailable"


def _window_dpi(root):
    """Use the new window's real DPI where Windows exposes it."""
    user32 = _windows_library("user32.dll")
    get_dpi = getattr(user32, "GetDpiForWindow", None)
    if get_dpi is None:
        return None
    get_dpi.argtypes = [ctypes.c_void_p]
    get_dpi.restype = ctypes.c_uint
    dpi = get_dpi(ctypes.c_void_p(root.winfo_id()))
    return float(dpi) if dpi else None


def choose_font_families(available, default_family, fixed_family, platform=None):
    """Prefer an installed CJK UI font, without depending on Windows fonts."""
    platform = platform or sys.platform
    installed = {name.casefold(): name for name in available}
    if platform == "win32":
        ui = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI")
        mono = ("Cascadia Mono", "Consolas", "Courier New")
    elif platform == "darwin":
        ui = ("PingFang SC", "Hiragino Sans GB", "Helvetica Neue")
        mono = ("SF Mono", "Menlo", "Monaco")
    else:
        ui = ("Noto Sans CJK SC", "WenQuanYi Micro Hei", "Noto Sans", "DejaVu Sans")
        mono = ("Noto Sans Mono CJK SC", "DejaVu Sans Mono", "Liberation Mono")
    pick = lambda candidates, fallback: next(
        (installed[name.casefold()] for name in candidates if name.casefold() in installed),
        fallback,
    )
    return pick(ui, default_family), pick(mono, fixed_family)


@dataclass(frozen=True)
class DisplayMetrics:
    family: str
    mono_family: str
    scale: float
    dpi: float

    def px(self, value):
        """Convert a 96-DPI layout distance to physical pixels once."""
        return round(value * self.scale)

    def font(self, size=10, weight=None):
        return (self.family, size, weight) if weight else (self.family, size)

    def mono_font(self, size=10):
        return (self.mono_family, size)


def configure_display(root):
    """Configure fonts/scaling before building widgets, then return metrics.

    Tk uses pixels per point, *not* the Windows percentage: 150% is 144 DPI
    and therefore Tk scaling 2.0. macOS and X11 keep their native Tk scaling.
    Tk 8.6 cannot reliably relayout existing widgets on a monitor-DPI change;
    restart on the destination monitor if sizes are wrong after moving it.
    """
    scaling = float(root.tk.call("tk", "scaling"))
    if not math.isfinite(scaling) or scaling <= 0:
        scaling = 96 / 72
    dpi = scaling * 72
    if sys.platform == "win32":
        window_dpi = _window_dpi(root)
        if window_dpi:
            dpi = window_dpi
            root.tk.call("tk", "scaling", dpi / 72)
    # Aqua uses logical pixels and backing-store scaling; multiplying again
    # would shrink/double-scale native Retina widgets.
    scale = 1.0 if sys.platform == "darwin" else dpi / 96
    default = tkfont.nametofont("TkDefaultFont", root=root)
    fixed = tkfont.nametofont("TkFixedFont", root=root)
    family, mono_family = choose_font_families(
        tkfont.families(root), default.actual("family"), fixed.actual("family")
    )
    # Named fonts also cover menus and stock dialogs that don't use ttk styles.
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
                 "TkCaptionFont", "TkIconFont", "TkTooltipFont"):
        try:
            tkfont.nametofont(name, root=root).configure(family=family, size=10)
        except tk.TclError:
            pass  # A minimal/embedded Tk may omit an optional named font.
    fixed.configure(family=mono_family, size=10)
    root.option_add("*Font", "TkDefaultFont")
    return DisplayMetrics(family, mono_family, scale, dpi)
