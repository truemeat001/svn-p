"""Guard readable semantic colors and native-widget theme integration."""
import ast
from contextlib import ExitStack
from pathlib import Path
import re
import unittest
from unittest.mock import Mock, patch

from svn_client.display import DisplayMetrics
from svn_client.theme import COLORS, apply_theme, text_options
from svn_client import conflict_dialogs, fileops_dialogs, locks_dialogs, ui


def contrast_ratio(first, second):
    def luminance(color):
        channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        channels = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in channels]
        return sum(weight * value for weight, value in zip((.2126, .7152, .0722), channels))
    dark, light = sorted((luminance(first), luminance(second)))
    return (light + .05) / (dark + .05)


class ColorTests(unittest.TestCase):
    def test_semantic_colors_are_immutable_hex_values(self):
        self.assertTrue(all(re.fullmatch(r"#[0-9A-F]{6}", color) for color in COLORS.values()))
        with self.assertRaises(TypeError):
            COLORS["text"] = "white"

    def test_body_and_secondary_text_exceed_minimum_contrast(self):
        for foreground in ("text", "muted", "accent", "success", "danger", "warning"):
            for background in ("bg", "nav", "panel", "raised"):
                with self.subTest(foreground=foreground, background=background):
                    self.assertGreaterEqual(contrast_ratio(COLORS[foreground], COLORS[background]), 4.5)

    def test_primary_button_text_in_all_enabled_states(self):
        for background in ("primary", "primary_hover", "primary_pressed"):
            self.assertGreaterEqual(contrast_ratio(COLORS["on_primary"], COLORS[background]), 4.5)

    def test_selection_and_diff_text_remain_readable(self):
        for foreground, background in (("text", "selection"), ("text", "selection_inactive"),
                                      ("group_fg", "group_bg"), ("diff_add_fg", "diff_add_bg"),
                                      ("diff_remove_fg", "diff_remove_bg"), ("diff_header_fg", "diff_header_bg")):
            self.assertGreaterEqual(contrast_ratio(COLORS[foreground], COLORS[background]), 4.5)

    def test_input_outlines_focus_and_scrollbar_are_visible(self):
        for foreground, background in (("control_border", "input"), ("control_border", "bg"),
                                      ("focus", "raised"), ("scrollbar", "nav")):
            self.assertGreaterEqual(contrast_ratio(COLORS[foreground], COLORS[background]), 3)

    def test_text_widgets_use_the_same_selection_and_caret_colors(self):
        for kind in ("normal", "console", "input"):
            options = text_options(kind)
            self.assertEqual(options["foreground"], COLORS["text"])
            self.assertEqual(options["selectbackground"], COLORS["selection"])
            self.assertEqual(options["insertbackground"], COLORS["focus"])
        self.assertEqual(text_options("console")["background"], COLORS["nav"])


class ThemeTests(unittest.TestCase):
    @patch("svn_client.theme.ttk.Style")
    def test_theme_scales_layout_once_and_keeps_font_points(self, factory):
        root = Mock()
        style = factory.return_value
        metrics = DisplayMetrics("Native UI", "Native Mono", 1.5, 144)
        self.assertIs(apply_theme(root, metrics), style)
        style.theme_use.assert_called_once_with("clam")
        configurations = {call.args[0]: call.kwargs for call in style.configure.call_args_list}
        self.assertEqual(configurations["Treeview"]["rowheight"], 42)
        self.assertEqual(configurations["Treeview"]["font"], ("Native UI", 10))
        self.assertEqual(configurations["Heading.TLabel"]["font"], ("Native UI", 15, "bold"))
        self.assertEqual(configurations["White.TFrame"]["background"], COLORS["panel"])
        root.option_add.assert_any_call("*TCombobox*Listbox.selectForeground", COLORS["text"])
        root.option_add.assert_any_call("*Menu.disabledForeground", COLORS["disabled"])

    @patch("svn_client.theme.ttk.Style")
    def test_disabled_buttons_take_priority_over_hover_and_pressed(self, factory):
        apply_theme(Mock(), DisplayMetrics("UI", "Mono", 1, 96))
        maps = {call.args[0]: call.kwargs for call in factory.return_value.map.call_args_list}
        for name in ("TButton", "Primary.TButton"):
            self.assertEqual(maps[name]["background"][0][0], "disabled")
            self.assertEqual(maps[name]["foreground"][0], ("disabled", COLORS["disabled"]))
        self.assertEqual(maps["TCombobox"]["foreground"][1], ("readonly", COLORS["text"]))


class ThemeIntegrationTests(unittest.TestCase):
    """Inspect real wiring without claiming live Tk or Windows rendering."""
    modules = (ui, conflict_dialogs, fileops_dialogs, locks_dialogs)

    @staticmethod
    def tree(module):
        return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))

    @patch("svn_client.ui.apply_theme")
    def test_main_style_delegates_to_shared_theme(self, apply):
        app = Mock(display=DisplayMetrics("UI", "Mono", 1.5, 144))
        ui.SvnDesk._make_style(app)
        apply.assert_called_once_with(app, app.display)

    def test_theme_and_dpi_are_initialized_before_widgets(self):
        app_class = next(node for node in self.tree(ui).body if isinstance(node, ast.ClassDef) and node.name == "SvnDesk")
        constructor = next(node for node in app_class.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
        calls = sorted((node.lineno, ast.unparse(node.func)) for node in ast.walk(constructor) if isinstance(node, ast.Call))
        positions = {name: line for line, name in calls}
        self.assertLess(positions["enable_high_dpi"], positions["super().__init__"])
        self.assertLess(positions["configure_display"], positions["self._make_style"])
        self.assertLess(positions["self._make_style"], positions["self._make_menu"])
        self.assertLess(positions["self._make_style"], positions["self._build_ui"])

    def test_every_classic_text_constructor_uses_shared_options_without_overrides(self):
        color_keywords = set(text_options()) | {"bg", "fg"}
        for module in self.modules:
            calls = [node for node in ast.walk(self.tree(module)) if isinstance(node, ast.Call)
                     and ast.unparse(node.func) == "tk.Text"]
            self.assertTrue(calls, module.__name__)
            for node in calls:
                with self.subTest(module=module.__name__, line=node.lineno):
                    shared = [keyword for keyword in node.keywords if keyword.arg is None
                              and isinstance(keyword.value, ast.Call) and ast.unparse(keyword.value.func) == "text_options"]
                    self.assertEqual(len(shared), 1)
                    self.assertFalse(color_keywords.intersection(keyword.arg for keyword in node.keywords if keyword.arg))

    def test_preview_and_all_dialog_helpers_pass_actual_theme_options(self):
        metrics = DisplayMetrics("UI", "Mono", 1.5, 144)
        cases = (
            (ui, "console", lambda parent: ui.SvnDesk._text_area(Mock(display=metrics), parent, kind="console"), metrics.mono_font()),
            (conflict_dialogs, "normal", lambda parent: conflict_dialogs._text(parent, metrics), metrics.font()),
            (fileops_dialogs, "normal", lambda parent: fileops_dialogs._readonly_text(parent, metrics, "需要核对的路径"), metrics.font()),
            (locks_dialogs, "input", lambda parent: locks_dialogs._text(parent, metrics, kind="input"), metrics.font()),
        )
        for module, kind, create, font in cases:
            with self.subTest(module=module.__name__), ExitStack() as stack:
                stack.enter_context(patch.object(module.ttk, "Frame"))
                scrollbars = stack.enter_context(patch.object(module.ttk, "Scrollbar"))
                text = stack.enter_context(patch.object(module.tk, "Text"))
                self.assertIs(create(Mock()), text.return_value)
                kwargs = text.call_args.kwargs
                for key, value in text_options(kind).items():
                    self.assertEqual(kwargs[key], value)
                self.assertEqual(kwargs["font"], font)
                self.assertEqual({call.kwargs["orient"] for call in scrollbars.call_args_list}, {"vertical", "horizontal"})
                if module is fileops_dialogs:
                    text.return_value.configure.assert_any_call(state="disabled")

    def test_status_and_diff_tags_use_semantic_palette(self):
        tags = {}
        for node in ast.walk(self.tree(ui)):
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(".tag_configure"):
                tags[(ast.unparse(node.func.value), ast.literal_eval(node.args[0]))] = {
                    keyword.arg: ast.unparse(keyword.value) for keyword in node.keywords}
        for tag, token in (("conflict", "danger"), ("added", "success"), ("missing", "warning")):
            self.assertEqual(tags[("self.changes", tag)]["foreground"], f"COLORS['{token}']")
        for tag, prefix in (("add", "diff_add"), ("remove", "diff_remove"), ("header", "diff_header")):
            self.assertEqual(tags[("self.preview", tag)]["foreground"], f"COLORS['{prefix}_fg']")
            self.assertEqual(tags[("self.preview", tag)]["background"], f"COLORS['{prefix}_bg']")

    def test_main_toolbar_emphasizes_commit_not_group_creation(self):
        actions = {}
        for node in ast.walk(self.tree(ui)):
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "self._button":
                keywords = {item.arg: item.value for item in node.keywords}
                if "action" in keywords:
                    actions[ast.literal_eval(keywords["action"])] = ast.literal_eval(keywords.get("primary", ast.Constant(False)))
        self.assertTrue(actions["commit_files"])
        self.assertFalse(actions["new_list"])

    def test_sidebar_uses_dedicated_native_dark_styles(self):
        styles = {keyword.value.value for node in ast.walk(self.tree(ui)) if isinstance(node, ast.Call)
                  for keyword in node.keywords if keyword.arg == "style" and isinstance(keyword.value, ast.Constant)}
        self.assertTrue({"Sidebar.TFrame", "Sidebar.TLabel", "SidebarMuted.TLabel", "Navigation.Treeview"}.issubset(styles))


if __name__ == "__main__":
    unittest.main()
