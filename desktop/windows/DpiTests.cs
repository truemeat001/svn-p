using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using System.Xml;

namespace SVNDeskDesktop {
    // Built from the same Program.cs, manifest and config as the shipped host.
    // No Show/Run, bridge process, browser profile, SVN or working copy is used.
    internal static class DpiTests {
        private static int checks;
        private static readonly IntPtr PerMonitorV2 = new IntPtr(-4);
        [DllImport("user32.dll")] private static extern IntPtr GetThreadDpiAwarenessContext();
        [DllImport("user32.dll")] private static extern IntPtr GetWindowDpiAwarenessContext(IntPtr hwnd);
        [DllImport("user32.dll")] private static extern uint GetDpiForWindow(IntPtr hwnd);
        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool AreDpiAwarenessContextsEqual(IntPtr left, IntPtr right);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, ExactSpelling=true, SetLastError=true)]
        private static extern IntPtr LoadLibraryExW(string path, IntPtr file, uint flags);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, ExactSpelling=true, SetLastError=true)]
        private static extern IntPtr FindResourceW(IntPtr module, IntPtr name, IntPtr type);
        [DllImport("kernel32.dll", SetLastError=true)] private static extern IntPtr LoadResource(IntPtr module, IntPtr resource);
        [DllImport("kernel32.dll", SetLastError=true)] private static extern uint SizeofResource(IntPtr module, IntPtr resource);
        [DllImport("kernel32.dll")] private static extern IntPtr LockResource(IntPtr resource);
        [DllImport("kernel32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)] private static extern bool FreeLibrary(IntPtr module);

        private static void Check(bool condition, string message) {
            ++checks;
            if (!condition) throw new InvalidOperationException(message);
        }
        private static XmlDocument ReadManifest(string executable) {
            // Resource-only mapping, never execute the target or its dependencies.
            IntPtr module = LoadLibraryExW(Path.GetFullPath(executable), IntPtr.Zero, 0x02 | 0x20);
            if (module == IntPtr.Zero) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            try {
                IntPtr resource = FindResourceW(module, new IntPtr(1), new IntPtr(24));
                Check(resource != IntPtr.Zero, "Host is missing embedded RT_MANIFEST #1.");
                uint length = SizeofResource(module, resource);
                Check(length > 0 && length <= 65536, "Embedded manifest must be bounded and nonempty.");
                IntPtr data = LockResource(LoadResource(module, resource));
                Check(data != IntPtr.Zero, "Cannot read embedded manifest.");
                byte[] bytes = new byte[(int)length]; Marshal.Copy(data, bytes, 0, bytes.Length);
                var xml = new XmlDocument { XmlResolver = null };
                using (var stream = new MemoryStream(bytes)) xml.Load(stream);
                return xml;
            } finally { FreeLibrary(module); }
        }
        [STAThread] internal static int Main(string[] args) {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            try {
                Check(args.Length == 1, "Pass the built SVNDesk.exe path.");
                Check(Environment.OSVersion.Version >= new Version(10, 0, 15063), "DPI checks require Windows 10 Creators Update or later.");
                var manifest = ReadManifest(args[0]);
                var namespaces = new XmlNamespaceManager(manifest.NameTable);
                namespaces.AddNamespace("compat", "urn:schemas-microsoft-com:compatibility.v1");
                Check(manifest.SelectSingleNode("//compat:application/compat:supportedOS[@Id='{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}']", namespaces) != null, "Built host has no Windows 10 compatibility declaration.");
                Check(manifest.SelectSingleNode("//*[local-name()='dpiAware' or local-name()='dpiAwareness']") == null, "Manifest must not override app.config DPI handling.");
                var config = new XmlDocument { XmlResolver = null }; config.Load(args[0] + ".config");
                Check(config.SelectSingleNode("/configuration/System.Windows.Forms.ApplicationConfigurationSection/add[@key='DpiAwareness' and @value='PerMonitorV2']") != null, "Packaged host config does not opt in to PerMonitorV2.");
                Check(AppDomain.CurrentDomain.SetupInformation.TargetFrameworkName == ".NETFramework,Version=v4.8", "DPI test must target .NET Framework 4.8.");
                Check(AreDpiAwarenessContextsEqual(GetThreadDpiAwarenessContext(), PerMonitorV2), "WinForms startup did not activate PerMonitorV2. Check config, manifest and compatibility overrides.");
                using (var window = new DeskWindow(new string[0])) {
                    Check(window.AutoScaleMode == AutoScaleMode.Dpi, "Host must use DPI-based control scaling.");
                    Check(window.Font.Name == "Segoe UI" && Math.Abs(window.Font.SizeInPoints - 10F) < 0.01F, "Native controls must inherit Segoe UI 10pt.");
                    IntPtr handle = window.Handle;
                    Check(AreDpiAwarenessContextsEqual(GetWindowDpiAwarenessContext(handle), PerMonitorV2), "Native window is not PerMonitorV2.");
                    Check(GetDpiForWindow(handle) == (uint)window.DeviceDpi, "WinForms and native window DPI disagree.");
                    foreach (string name in new[] { "open-workspace", "explore-demo" }) {
                        Control[] matches = window.Controls.Find(name, true);
                        Check(matches.Length == 1 && matches[0] is Button, "Both launcher buttons must exist before Shown/autoscaling.");
                        Check(matches[0].Font.Name == "Segoe UI", "Launcher button did not inherit the UI font.");
                    }
                    window.FitWindowToScreen();
                    System.Drawing.Rectangle area = Screen.FromControl(window).WorkingArea;
                    Check(window.MinimumSize.Width <= area.Width && window.MinimumSize.Height <= area.Height, "Minimum size exceeds the monitor work area.");
                    Check(area.Contains(window.Bounds), "Normal window bounds exceed the monitor work area.");
                    Check(!window.Visible, "DPI checks must not show or start the host.");
                    Console.WriteLine("Native DPI: " + window.DeviceDpi + "; font: " + window.Font.Name + "; mode: PerMonitorV2");
                }
                Console.WriteLine("PASS: " + checks + " native DPI/build checks. Visual sharpness and monitor transitions still require GUI acceptance.");
                return 0;
            } catch (Exception error) { Console.Error.WriteLine(error); return 1; }
        }
    }
}
