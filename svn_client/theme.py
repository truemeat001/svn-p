"""A compact dark theme for the real native SVN Desk widgets.

Semantic colors are shared by ttk, classic Tk text widgets and row tags.
There are no image-based controls, web views or third-party theme packages.
"""
from types import MappingProxyType
from tkinter import ttk


COLORS = MappingProxyType({
    "bg": "#10151D",
    "nav": "#0C1118",
    "panel": "#161E2A",
    "raised": "#1B2635",
    "input": "#101820",
    "line": "#2A3A4F",
    "control_border": "#647D9B",
    "text": "#E5EDF7",
    "muted": "#9EADC2",
    "disabled": "#8291A5",
    "accent": "#6BA7FF",
    "primary": "#2869C7",
    "primary_hover": "#2D72D2",
    "primary_pressed": "#2159A9",
    "on_primary": "#FFFFFF",
    "focus": "#8BB9FF",
    "hover": "#26374D",
    "pressed": "#203047",
    "selection": "#243D60",
    "selection_inactive": "#2C3544",
    "group_bg": "#1B2B40",
    "group_fg": "#A5C9FF",
    "success": "#78D4A2",
    "danger": "#FF9298",
    "warning": "#EBC473",
    "diff_add_bg": "#172D26",
    "diff_add_fg": "#98E1B5",
    "diff_remove_bg": "#321E25",
    "diff_remove_fg": "#FFABB1",
    "diff_header_bg": "#1A2B44",
    "diff_header_fg": "#A5C9FF",
    "scrollbar": "#63738A",
})


def text_options(kind="normal"):
    """Options shared by previews, editors, commit messages and merge views."""
    backgrounds = {"normal": "panel", "console": "nav", "input": "input"}
    return {
        "background": COLORS[backgrounds[kind]],
        "foreground": COLORS["text"],
        "insertbackground": COLORS["focus"],
        "selectbackground": COLORS["selection"],
        "selectforeground": COLORS["text"],
        "inactiveselectbackground": COLORS["selection_inactive"],
        "highlightbackground": COLORS["line"],
        "highlightcolor": COLORS["focus"],
    }


def apply_theme(root, display):
    """Configure native widgets before construction; return the ttk style."""
    c, p = COLORS, display.px
    style = ttk.Style(root)
    # Clam exposes field/border colors consistently across desktop platforms.
    style.theme_use("clam")
    root.configure(background=c["bg"])
    style.configure(".", background=c["bg"], foreground=c["text"],
                    font=display.font(), bordercolor=c["line"],
                    lightcolor=c["line"], darkcolor=c["line"],
                    troughcolor=c["nav"], selectbackground=c["selection"],
                    selectforeground=c["text"], focuscolor=c["focus"])
    for name, background in (("TFrame", "bg"), ("White.TFrame", "panel"),
                             ("Surface.TFrame", "panel"), ("Sidebar.TFrame", "nav"),
                             ("Toolbar.TFrame", "raised")):
        style.configure(name, background=c[background])
    style.configure("TLabel", background=c["bg"], foreground=c["text"])
    style.configure("Heading.TLabel", font=display.font(15, "bold"))
    style.configure("Muted.TLabel", foreground=c["muted"])
    style.configure("Accent.TLabel", foreground=c["accent"])
    style.configure("Sidebar.TLabel", background=c["nav"])
    style.configure("SidebarMuted.TLabel", background=c["nav"], foreground=c["muted"])
    style.configure("Surface.TLabel", background=c["panel"])
    style.configure("SurfaceMuted.TLabel", background=c["panel"], foreground=c["muted"])
    style.configure("Danger.TLabel", foreground=c["danger"])
    style.configure("TButton", padding=(p(10), p(6)), relief="flat", borderwidth=1,
                    background=c["raised"], foreground=c["text"],
                    bordercolor=c["line"], focusthickness=1, focuscolor=c["focus"])
    style.map("TButton", background=[("disabled", c["panel"]), ("pressed", c["pressed"]), ("active", c["hover"])],
              foreground=[("disabled", c["disabled"])],
              bordercolor=[("focus", c["focus"]), ("active", c["control_border"])])
    style.configure("Primary.TButton", background=c["primary"], foreground=c["on_primary"],
                    bordercolor=c["primary"])
    style.map("Primary.TButton", background=[("disabled", c["panel"]), ("pressed", c["primary_pressed"]), ("active", c["primary_hover"])],
              foreground=[("disabled", c["disabled"]), ("!disabled", c["on_primary"])],
              bordercolor=[("focus", c["focus"]), ("disabled", c["line"])])
    style.configure("Danger.TButton", foreground=c["danger"])
    style.map("Danger.TButton", foreground=[("disabled", c["disabled"]), ("!disabled", c["danger"])])
    for name in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(name, fieldbackground=c["input"], background=c["raised"],
                        foreground=c["text"], insertcolor=c["focus"],
                        bordercolor=c["control_border"], arrowcolor=c["muted"],
                        padding=(p(7), p(5)), selectbackground=c["selection"],
                        selectforeground=c["text"])
        style.map(name, fieldbackground=[("disabled", c["panel"]), ("readonly", c["input"])],
                  foreground=[("disabled", c["disabled"]), ("readonly", c["text"])],
                  bordercolor=[("focus", c["focus"]), ("disabled", c["line"])],
                  arrowcolor=[("disabled", c["disabled"]), ("active", c["text"])])
    style.configure("Treeview", background=c["panel"], fieldbackground=c["panel"],
                    foreground=c["text"], rowheight=p(28), borderwidth=0,
                    font=display.font(), indent=p(17))
    style.map("Treeview", background=[("selected", c["selection"])],
              foreground=[("selected", c["text"])])
    style.configure("Treeview.Heading", background=c["raised"], foreground=c["muted"],
                    padding=(p(8), p(7)), relief="flat", font=display.font(10, "bold"))
    style.map("Treeview.Heading", background=[("active", c["hover"])],
              foreground=[("active", c["text"])])
    style.configure("Navigation.Treeview", background=c["nav"], fieldbackground=c["nav"], rowheight=p(27))
    style.configure("TNotebook", background=c["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure("TNotebook.Tab", padding=(p(13), p(7)), background=c["raised"],
                    foreground=c["muted"], borderwidth=0)
    style.map("TNotebook.Tab", background=[("selected", c["panel"]), ("active", c["hover"])],
              foreground=[("selected", c["accent"]), ("active", c["text"])])
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=c["bg"], foreground=c["text"],
                        indicatorbackground=c["input"], indicatorforeground=c["text"],
                        focuscolor=c["focus"], padding=(p(2), p(3)))
        style.map(name, background=[("active", c["bg"])],
                  foreground=[("disabled", c["disabled"])],
                  indicatorbackground=[("selected", c["primary"])])
    style.configure("TSeparator", background=c["line"])
    style.configure("TPanedwindow", background=c["bg"])
    style.configure("Sash", sashthickness=p(6), sashpad=0, background=c["line"])
    for name in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(name, background=c["scrollbar"], troughcolor=c["nav"],
                        bordercolor=c["nav"], arrowcolor=c["text"], width=p(13), relief="flat")
        style.map(name, background=[("active", c["muted"]), ("pressed", c["accent"])])
    style.configure("Horizontal.TProgressbar", troughcolor=c["nav"], background=c["accent"], borderwidth=0)

    # ttk's combobox dropdown and classic menus are separate Tk widgets.
    # Native OS dialogs may retain platform chrome; never fake those dialogs.
    defaults = {
        "*Toplevel.background": c["bg"],
        "*Menu.background": c["raised"], "*Menu.foreground": c["text"],
        "*Menu.activeBackground": c["selection"], "*Menu.activeForeground": c["text"],
        "*Menu.disabledForeground": c["disabled"], "*Menu.borderWidth": 0,
        "*Menu.activeBorderWidth": 0,
        "*TCombobox*Listbox.background": c["raised"],
        "*TCombobox*Listbox.foreground": c["text"],
        "*TCombobox*Listbox.selectBackground": c["selection"],
        "*TCombobox*Listbox.selectForeground": c["text"],
    }
    for option, value in defaults.items():
        root.option_add(option, value)
    return style
