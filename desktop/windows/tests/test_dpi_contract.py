"""Cross-platform source guards, not a replacement for Windows DPI/GUI tests."""
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET

WINDOWS = Path(__file__).resolve().parents[1]
NS = {"asm": "urn:schemas-microsoft-com:asm.v1",
      "compat": "urn:schemas-microsoft-com:compatibility.v1",
      "trust": "urn:schemas-microsoft-com:asm.v3"}


class DpiContractTests(unittest.TestCase):
    def test_manifest_declares_windows_10_without_dpi_override(self):
        manifest = ET.parse(WINDOWS / "SVNDesk.manifest")
        self.assertIsNotNone(manifest.find("compat:compatibility/compat:application/compat:supportedOS[@Id='{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}']", NS))
        self.assertFalse(any(node.tag.split("}")[-1] in {"dpiAware", "dpiAwareness"} for node in manifest.iter()))

    def test_manifest_does_not_request_elevation(self):
        manifest = ET.parse(WINDOWS / "SVNDesk.manifest")
        level = manifest.find("trust:trustInfo/trust:security/trust:requestedPrivileges/trust:requestedExecutionLevel", NS)
        self.assertEqual(level.attrib, {"level": "asInvoker", "uiAccess": "false"})

    def test_config_retains_per_monitor_v2_and_framework_48(self):
        config = ET.parse(WINDOWS / "SVNDesk.exe.config")
        dpi = config.find("System.Windows.Forms.ApplicationConfigurationSection/add[@key='DpiAwareness']")
        self.assertEqual(dpi.get("value"), "PerMonitorV2")
        self.assertEqual(config.find("startup/supportedRuntime").get("sku"), ".NETFramework,Version=v4.8")

    def test_compiler_embeds_manifest_and_runs_native_probe(self):
        build = (WINDOWS / "build.ps1").read_text(encoding="utf-8")
        host = next(line for line in build.splitlines() if line.startswith("Invoke-Compiler 'SVNDesk'"))
        probe = next(line for line in build.splitlines() if line.startswith("Invoke-Compiler 'DpiTests'"))
        self.assertIn('/win32manifest:$manifest', host)
        self.assertIn('/win32manifest:$manifest', probe)
        self.assertIn('/main:SVNDeskDesktop.DpiTests', probe)
        self.assertIn('/codepage:65001', build)
        self.assertIn('"$PSScriptRoot\\SVNDesk.exe.config" -Destination "$diag\\DpiTests.exe.config"', build)
        self.assertIn('& "$diag\\DpiTests.exe" "$out\\SVNDesk.exe"', build)
        self.assertRegex(build, r"if \(\$dpiExit -ne 0\) \{ throw")
        self.assertIn("nativeGuiTest = 'not_run'", build)

    def test_host_targets_framework_and_sets_dpi_baseline(self):
        source = (WINDOWS / "Program.cs").read_text(encoding="utf-8")
        self.assertIn('[assembly: TargetFramework(".NETFramework,Version=v4.8"', source)
        self.assertIn('AutoScaleDimensions = new SizeF(96F, 96F)', source)
        self.assertIn('AutoScaleMode = AutoScaleMode.Dpi', source)
        self.assertIn('Font = new Font("Segoe UI", 10F', source)
        self.assertLess(source.index('Application.EnableVisualStyles()'), source.index('Application.Run('))

    def test_welcome_copy_and_layout_are_localized_and_content_sized(self):
        source = (WINDOWS / "Program.cs").read_text(encoding="utf-8")
        for text in ('更清晰地管理 SVN 工作副本', '打开工作副本…', '体验演示', '默认关闭所有写入功能'):
            self.assertIn(text, source)
        for name in ('heading', 'detail', 'choices', 'welcome'):
            self.assertIn(name + '.AutoSize = true', source)
            self.assertNotRegex(source, re.escape(name) + r'\.Height\s*=')
        self.assertIn('choices.WrapContents = true', source)
        self.assertIn('status.SizeChanged += delegate { FitWelcomeWidth(); }', source)
        self.assertIn('LogicalToDeviceUnits(720)', source)
        self.assertIn('Math.Min(available,', source)

    def test_buttons_exist_before_initial_layout_and_window_stays_on_screen(self):
        source = (WINDOWS / "Program.cs").read_text(encoding="utf-8")
        constructor = source[source.index('internal DeskWindow('):source.index('protected override void OnShown(')]
        self.assertIn('choices.Controls.Add(chooseWorkspace)', constructor)
        self.assertIn('choices.Controls.Add(exploreDemo)', constructor)
        self.assertLess(constructor.index('choices.Controls.Add('), constructor.index('ResumeLayout(false)'))
        self.assertIn('Math.Min(LogicalToDeviceUnits(640), area.Height)', source)
        self.assertIn('Math.Min(Height, area.Height)', source)

    def test_probe_checks_native_context_not_just_config(self):
        probe = (WINDOWS / "DpiTests.cs").read_text(encoding="utf-8")
        self.assertIn('GetThreadDpiAwarenessContext(), PerMonitorV2', probe)
        self.assertIn('GetWindowDpiAwarenessContext(handle), PerMonitorV2', probe)
        self.assertIn('GetDpiForWindow(handle) == (uint)window.DeviceDpi', probe)
        self.assertIn('ReadManifest(args[0])', probe)
        self.assertNotIn('Application.Run(', probe)
        self.assertNotIn('.Show(', probe)
        self.assertNotIn('Process.Start(', probe)


if __name__ == '__main__':
    unittest.main()
