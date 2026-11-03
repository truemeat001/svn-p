"""DPI API and font fallback tests; Windows calls are mocked on other OSes."""
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from svn_client import display


class DpiAwarenessTests(unittest.TestCase):
    def setUp(self):
        display.enable_high_dpi.cache_clear()

    def tearDown(self):
        display.enable_high_dpi.cache_clear()

    def test_other_platforms_do_not_load_windows_dlls(self):
        for platform in ("linux", "darwin"):
            display.enable_high_dpi.cache_clear()
            with patch.object(display.sys, "platform", platform), patch.object(display, "_windows_library") as load:
                self.assertEqual(display.enable_high_dpi(), "native")
                load.assert_not_called()

    @patch.object(display.sys, "platform", "win32")
    def test_per_monitor_v2_is_preferred_and_startup_is_idempotent(self):
        context = Mock(return_value=1)
        with patch.object(display, "_windows_library", return_value=SimpleNamespace(SetProcessDpiAwarenessContext=context)) as load:
            self.assertEqual(display.enable_high_dpi(), "per-monitor-v2")
            self.assertEqual(display.enable_high_dpi(), "per-monitor-v2")
        self.assertEqual(context.call_count, 1)
        self.assertEqual(context.call_args.args[0].value, display.ctypes.c_void_p(-4).value)
        self.assertEqual(load.call_count, 1)

    @patch.object(display.sys, "platform", "win32")
    def test_unsupported_v2_falls_back_to_v1_context(self):
        context = Mock(side_effect=[0, 1])
        with patch.object(display, "_last_error", return_value=87), patch.object(display, "_windows_library", return_value=SimpleNamespace(SetProcessDpiAwarenessContext=context)):
            self.assertEqual(display.enable_high_dpi(), "per-monitor")
        self.assertEqual(context.call_args_list[1].args[0].value, display.ctypes.c_void_p(-3).value)

    @patch.object(display.sys, "platform", "win32")
    def test_existing_manifest_awareness_is_respected(self):
        context = Mock(return_value=0)
        legacy = Mock(return_value=1)
        with patch.object(display, "_last_error", return_value=5), patch.object(display, "_windows_library", return_value=SimpleNamespace(SetProcessDpiAwarenessContext=context, SetProcessDPIAware=legacy)) as load:
            self.assertEqual(display.enable_high_dpi(), "existing")
        self.assertEqual(load.call_count, 1)
        self.assertEqual(context.call_count, 1)
        legacy.assert_not_called()

    @patch.object(display.sys, "platform", "win32")
    def test_windows_81_uses_shcore(self):
        awareness = Mock(return_value=0)
        with patch.object(display, "_windows_library", side_effect=[SimpleNamespace(), SimpleNamespace(SetProcessDpiAwareness=awareness)]):
            self.assertEqual(display.enable_high_dpi(), "per-monitor")
        awareness.assert_called_once_with(2)

    @patch.object(display.sys, "platform", "win32")
    def test_shcore_existing_awareness_is_not_downgraded(self):
        for result in (0x80070005, -2147024891):
            display.enable_high_dpi.cache_clear()
            legacy = Mock(return_value=1)
            with patch.object(display, "_windows_library", side_effect=[SimpleNamespace(SetProcessDPIAware=legacy), SimpleNamespace(SetProcessDpiAwareness=Mock(return_value=result))]):
                self.assertEqual(display.enable_high_dpi(), "existing")
            legacy.assert_not_called()

    @patch.object(display.sys, "platform", "win32")
    def test_older_windows_uses_system_awareness(self):
        legacy = Mock(return_value=1)
        with patch.object(display, "_windows_library", side_effect=[SimpleNamespace(SetProcessDPIAware=legacy), None]):
            self.assertEqual(display.enable_high_dpi(), "system")
        legacy.assert_called_once_with()

    @patch.object(display.sys, "platform", "win32")
    def test_no_apis_does_not_block_startup(self):
        with patch.object(display, "_windows_library", return_value=None):
            self.assertEqual(display.enable_high_dpi(), "unavailable")

    def test_missing_windows_loader_is_harmless(self):
        with patch.object(display.ctypes, "WinDLL", side_effect=OSError("missing"), create=True):
            self.assertIsNone(display._windows_library("user32.dll"))

    def test_launcher_sets_awareness_before_ui_main(self):
        events = []
        fake_display = SimpleNamespace(enable_high_dpi=lambda: events.append("dpi"))
        fake_ui = SimpleNamespace(main=lambda: events.append("ui"))
        with patch.dict("sys.modules", {"svn_client.display": fake_display, "svn_client.ui": fake_ui}):
            runpy.run_path(str(Path(__file__).resolve().parents[1] / "main.py"), run_name="__main__")
        self.assertEqual(events, ["dpi", "ui"])


class FontTests(unittest.TestCase):
    def test_windows_prefers_cjk_ui_family(self):
        self.assertEqual(display.choose_font_families(["Segoe UI", "Microsoft YaHei UI", "Consolas"], "Arial", "Courier", "win32"), ("Microsoft YaHei UI", "Consolas"))

    def test_other_platforms_choose_installed_fonts(self):
        self.assertEqual(display.choose_font_families(["PingFang SC", "Menlo"], "system", "mono", "darwin"), ("PingFang SC", "Menlo"))
        self.assertEqual(display.choose_font_families(["Noto Sans CJK SC", "DejaVu Sans Mono"], "system", "mono", "linux"), ("Noto Sans CJK SC", "DejaVu Sans Mono"))

    def test_missing_fonts_use_actual_tk_defaults(self):
        self.assertEqual(display.choose_font_families([], "Native UI", "Native Mono", "win32"), ("Native UI", "Native Mono"))

    def test_font_name_matching_is_case_insensitive(self):
        self.assertEqual(display.choose_font_families(["microsoft yahei ui"], "default", "mono", "win32"), ("microsoft yahei ui", "mono"))

    def test_font_points_are_not_multiplied_by_screen_scale(self):
        metrics = display.DisplayMetrics("UI", "Mono", 2, 192)
        self.assertEqual(metrics.font(10), ("UI", 10))
        self.assertEqual(metrics.font(15, "bold"), ("UI", 15, "bold"))
        self.assertEqual(metrics.mono_font(10), ("Mono", 10))
        self.assertEqual(metrics.px(30), 60)
        self.assertEqual(metrics.px(0), 0)


class ConfigureDisplayTests(unittest.TestCase):
    def setUp(self):
        self.root = Mock()
        self.root.tk.call.return_value = 96 / 72
        self.fonts = {}

        def named_font(name, root):
            if name not in self.fonts:
                self.fonts[name] = Mock()
                self.fonts[name].actual.return_value = "Default Mono" if name == "TkFixedFont" else "Default UI"
            return self.fonts[name]

        self.font_patch = patch.object(display.tkfont, "nametofont", side_effect=named_font)
        self.families_patch = patch.object(display.tkfont, "families", return_value=[])
        self.font_patch.start()
        self.families_patch.start()
        self.addCleanup(self.font_patch.stop)
        self.addCleanup(self.families_patch.stop)

    @patch.object(display.sys, "platform", "win32")
    def test_windows_150_percent_sets_two_pixels_per_point(self):
        with patch.object(display, "_window_dpi", return_value=144):
            metrics = display.configure_display(self.root)
        self.root.tk.call.assert_any_call("tk", "scaling", 2.0)
        self.assertEqual(metrics.scale, 1.5)
        self.assertEqual(metrics.px(30), 45)
        self.assertEqual(metrics.dpi, 144)
        self.root.option_add.assert_called_once_with("*Font", "TkDefaultFont")
        self.fonts["TkMenuFont"].configure.assert_called_once_with(family="Default UI", size=10)
        self.fonts["TkFixedFont"].configure.assert_called_once_with(family="Default Mono", size=10)

    @patch.object(display.sys, "platform", "win32")
    def test_missing_dpi_api_preserves_tk_scaling(self):
        self.root.tk.call.return_value = 2
        with patch.object(display, "_window_dpi", return_value=None):
            metrics = display.configure_display(self.root)
        self.root.tk.call.assert_called_once_with("tk", "scaling")
        self.assertEqual(metrics.scale, 1.5)

    @patch.object(display.sys, "platform", "darwin")
    def test_mac_retina_is_not_double_scaled(self):
        self.root.tk.call.return_value = 2
        with patch.object(display, "_window_dpi") as dpi:
            metrics = display.configure_display(self.root)
        dpi.assert_not_called()
        self.root.tk.call.assert_called_once_with("tk", "scaling")
        self.assertEqual(metrics.scale, 1)

    @patch.object(display.sys, "platform", "linux")
    def test_linux_respects_native_tk_scaling(self):
        self.root.tk.call.return_value = 2
        with patch.object(display, "_window_dpi") as dpi:
            metrics = display.configure_display(self.root)
        dpi.assert_not_called()
        self.root.tk.call.assert_called_once_with("tk", "scaling")
        self.assertEqual(metrics.scale, 1.5)

    @patch.object(display.sys, "platform", "linux")
    def test_invalid_scaling_has_safe_layout_default(self):
        for value in (0, -1, float("nan"), float("inf")):
            self.root.tk.call.return_value = value
            self.assertEqual(display.configure_display(self.root).scale, 1)

    def test_get_window_dpi_uses_full_pointer_sized_handle(self):
        get_dpi = Mock(return_value=192)
        self.root.winfo_id.return_value = 0x1234567890
        with patch.object(display, "_windows_library", return_value=SimpleNamespace(GetDpiForWindow=get_dpi)):
            self.assertEqual(display._window_dpi(self.root), 192)
        self.assertEqual(get_dpi.call_args.args[0].value, 0x1234567890)

    def test_invalid_window_handle_does_not_set_zero_scaling(self):
        self.root.winfo_id.return_value = 0
        with patch.object(display, "_windows_library", return_value=SimpleNamespace(GetDpiForWindow=Mock(return_value=0))):
            self.assertIsNone(display._window_dpi(self.root))


if __name__ == "__main__":
    unittest.main()
