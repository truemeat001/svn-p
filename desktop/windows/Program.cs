using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.Versioning;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

[assembly: TargetFramework(".NETFramework,Version=v4.8", FrameworkDisplayName = ".NET Framework 4.8")]

namespace SVNDeskDesktop {
    internal static class Program {
        [STAThread] private static void Main(string[] args) {
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new DeskWindow(args));
        }
    }
    internal sealed class DeskWindow : Form {
        private readonly string[] launchArgs;
        private readonly Label heading = new Label();
        private readonly Label detail = new Label();
        private readonly TableLayoutPanel status = new TableLayoutPanel();
        private readonly TableLayoutPanel welcome = new TableLayoutPanel();
        private readonly FlowLayoutPanel choices = new FlowLayoutPanel();
        private readonly Button chooseWorkspace = new Button();
        private readonly Button exploreDemo = new Button();
        private Process bridge;
        private Task stdoutDrain;
        private WebView2 view;
        private Uri origin;
        private bool closing, closeAfterDrain, exited, starting, started;
        private string profileDirectory;
        private string explicitProfileRoot;
        private ProfilePolicy profilePolicy;

        internal DeskWindow(string[] args) {
            SuspendLayout();
            // These layout measurements are 96-DPI logical units. Let WinForms
            // scale controls once; do not enlarge a finished bitmap or CSS page.
            AutoScaleDimensions = new SizeF(96F, 96F);
            AutoScaleMode = AutoScaleMode.Dpi;
            Font = new Font("Segoe UI", 10F, FontStyle.Regular, GraphicsUnit.Point);
            launchArgs = args;
            Text = "SVN Desk · Windows 预览版"; ClientSize = new Size(1440, 940); MinimumSize = new Size(800, 640);
            StartPosition = FormStartPosition.CenterScreen; BackColor = Color.FromArgb(245,247,251);
            status.SuspendLayout(); welcome.SuspendLayout();
            status.Dock = DockStyle.Fill; status.Padding = new Padding(40);
            status.ColumnCount = 1; status.RowCount = 3;
            status.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            status.RowStyles.Add(new RowStyle(SizeType.Percent, 50));
            status.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            status.RowStyles.Add(new RowStyle(SizeType.Percent, 50));
            welcome.AutoSize = true; welcome.AutoSizeMode = AutoSizeMode.GrowAndShrink;
            welcome.Anchor = AnchorStyles.None; welcome.ColumnCount = 1; welcome.RowCount = 3;
            welcome.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            for (int row = 0; row < 3; ++row) welcome.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            heading.Text = "SVN Desk"; heading.Font = new Font("Segoe UI",24,FontStyle.Bold);
            heading.ForeColor = Color.FromArgb(23,36,58); heading.AutoSize = true;
            heading.Dock = DockStyle.Fill; heading.Margin = new Padding(0,0,0,18);
            detail.Font = new Font("Segoe UI",11); detail.ForeColor = Color.FromArgb(82,97,122);
            detail.AutoSize = true; detail.Dock = DockStyle.Fill; detail.Margin = new Padding(0,0,0,24);
            choices.AutoSize = true; choices.AutoSizeMode = AutoSizeMode.GrowAndShrink;
            choices.Dock = DockStyle.Fill; choices.WrapContents = true; choices.Margin = Padding.Empty;
            chooseWorkspace.Name = "open-workspace"; chooseWorkspace.Text = "打开工作副本…";
            exploreDemo.Name = "explore-demo"; exploreDemo.Text = "体验演示";
            foreach (Button button in new[] { chooseWorkspace, exploreDemo }) {
                button.AutoSize = true; button.AutoSizeMode = AutoSizeMode.GrowAndShrink;
                button.Padding = new Padding(16,10,16,10); button.Margin = new Padding(0,0,12,8);
            }
            chooseWorkspace.Click += async delegate {
                using (var picker = new FolderBrowserDialog { Description = "选择已有 SVN 工作副本文件夹（只读预览）", ShowNewFolderButton = false }) {
                    if (picker.ShowDialog(this) == DialogResult.OK) await Start(new[] { "--workspace", picker.SelectedPath });
                }
            };
            exploreDemo.Click += async delegate { await Start(new string[0]); };
            choices.Controls.Add(chooseWorkspace); choices.Controls.Add(exploreDemo);
            choices.Visible = false;
            welcome.Controls.Add(heading,0,0); welcome.Controls.Add(detail,0,1); welcome.Controls.Add(choices,0,2);
            status.Controls.Add(welcome,0,1); Controls.Add(status);
            welcome.ResumeLayout(false); status.ResumeLayout(false);
            status.SizeChanged += delegate { FitWelcomeWidth(); };
            DpiChanged += delegate {
                FitWelcomeWidth();
                // Apply after WinForms has completed the monitor's suggested bounds.
                if (IsHandleCreated && !IsDisposed) BeginInvoke(new Action(delegate {
                    if (!IsDisposed) { FitWindowToScreen(); FitWelcomeWidth(); }
                }));
            };
            FormClosing += OnClosingRequested; Shown += Open;
            ResumeLayout(false);
            FitWelcomeWidth();
        }
        protected override void OnShown(EventArgs e) {
            FitWindowToScreen();
            base.OnShown(e);
        }
        internal void FitWindowToScreen() {
            // A logical minimum must not force a high-DPI window below the taskbar.
            Rectangle area = Screen.FromControl(this).WorkingArea;
            MinimumSize = new Size(Math.Min(LogicalToDeviceUnits(800), area.Width), Math.Min(LogicalToDeviceUnits(640), area.Height));
            if (WindowState == FormWindowState.Normal) {
                Size = new Size(Math.Min(Width, area.Width), Math.Min(Height, area.Height));
                Location = new Point(Math.Max(area.Left, Math.Min(Left, area.Right - Width)), Math.Max(area.Top, Math.Min(Top, area.Bottom - Height)));
            }
        }
        private void FitWelcomeWidth() {
            // Keep the welcome/status copy together while allowing text and buttons
            // to wrap on smaller windows. Device units are derived from logical width.
            int available = Math.Max(1, status.ClientSize.Width - status.Padding.Horizontal);
            int width = Math.Min(available, LogicalToDeviceUnits(720));
            welcome.MinimumSize = new Size(width, 0);
            welcome.MaximumSize = new Size(width, 0);
        }
        private void ShowStatus(string title, string text) {
            // Disposing the WebView removes stale controls and in-flight page scripting.
            if (view != null) { Controls.Remove(view); view.Dispose(); view = null; }
            heading.Text = title; detail.Text = text; FitWelcomeWidth(); status.Visible = true; status.BringToFront();
            choices.Visible = false;
        }
        private async void Open(object sender, EventArgs e) {
            string[] bridgeArgs;
            try {
                bridgeArgs = Protocol.NativeArguments(launchArgs, out explicitProfileRoot);
                Protocol.Arguments(bridgeArgs);
                if (explicitProfileRoot != null) ProfilePolicy.ValidateFresh(explicitProfileRoot);
            } catch { ShowStatus("请检查启动参数", "浏览器资料目录必须是工作副本之外的新建本地目录，且其父目录已存在并且安全。尚未启动本地服务或浏览器资料。"); return; }
            if (bridgeArgs.Length > 0) { await Start(bridgeArgs); return; }
            heading.Text = "更清晰地管理 SVN 工作副本";
            detail.Text = "打开已有工作副本，以只读方式查看文件和变更；也可以先体验演示。\n默认关闭所有写入功能，只有明确使用启动参数启用后才可操作。";
            choices.Visible = true;
        }
        private async Task Start(string[] args) {
            if (starting || started || closing) return;
            starting = true; string[] validated;
            try { validated = Protocol.Arguments(args); }
            catch { starting = false; ShowStatus("请检查启动参数", "仅支持文档列出的工作副本路径、SVN 程序路径及功能开关。尚未启动本地服务。"); return; }
            ShowStatus("正在打开工作副本", "正在启动随应用提供的本地服务。工作副本保留在这台电脑上。");
            try {
                if (explicitProfileRoot != null) {
                    profilePolicy = ProfilePolicy.Create(explicitProfileRoot);
                    profileDirectory = profilePolicy.ProfileDirectory;
                }
                string executable = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "Bridge", "SVNDeskBridge.exe");
                if (!File.Exists(executable)) throw new FileNotFoundException();
                var info = new ProcessStartInfo(executable, Protocol.CommandLine(validated)) {
                    UseShellExecute = false, CreateNoWindow = true,
                    RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
                    WorkingDirectory = Path.GetDirectoryName(executable)
                };
                info.EnvironmentVariables.Remove("PYTHONPATH"); info.EnvironmentVariables.Remove("PYTHONHOME");
                bridge = new Process { StartInfo = info, EnableRaisingEvents = true };
                bridge.Exited += delegate { if (!IsDisposed && IsHandleCreated) { try { BeginInvoke(new Action(BridgeExited)); } catch (InvalidOperationException) {} } };
                bridge.Start(); started = true;
                // Consume diagnostic pipes without retaining credentials, paths or SVN output.
                Drain(bridge.StandardError.BaseStream);
                Stream stdout = bridge.StandardOutput.BaseStream;
                Task<byte[]> handshake = Protocol.ReadHandshake(stdout);
                // Exactly one reader at a time, on success, failure and late readiness.
                stdoutDrain = Protocol.DrainAfterHandshake(handshake, stdout);
                if (await Task.WhenAny(handshake, Task.Delay(30000)) != handshake) throw new TimeoutException();
                origin = Protocol.ReadyOrigin(await handshake, bridge.Id);
                if (closing || exited || bridge.HasExited) { if (bridge.HasExited) BridgeExited(); return; }
                if (profilePolicy != null) profilePolicy.Recheck();
                else profileDirectory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "SVNDesk", "WebView2", Guid.NewGuid().ToString("N"));
                view = new WebView2 { Dock = DockStyle.Fill, Visible = false, AllowExternalDrop = false };
                Controls.Add(view);
                // Process-local only: never inherit a real browser profile or debug switches.
                foreach (string variable in new[] { "WEBVIEW2_USER_DATA_FOLDER", "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "WEBVIEW2_RELEASE_CHANNEL_PREFERENCE", "WEBVIEW2_CHANNEL_SEARCH_KIND", "WEBVIEW2_RELEASE_CHANNELS", "WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER" }) Environment.SetEnvironmentVariable(variable, null, EnvironmentVariableTarget.Process);
                if (profilePolicy != null) profilePolicy.Recheck();
                var environment = await CoreWebView2Environment.CreateAsync(null, profileDirectory);
                if (closing || exited || view == null) return;
                await view.EnsureCoreWebView2Async(environment);
                if (closing || exited || view == null) return;
                var core = view.CoreWebView2;
                core.Settings.AreDevToolsEnabled = false;
                core.Settings.AreDefaultContextMenusEnabled = false;
                core.Settings.AreBrowserAcceleratorKeysEnabled = false;
                core.Settings.AreHostObjectsAllowed = false;
                core.Settings.IsWebMessageEnabled = false;
                core.Settings.IsPasswordAutosaveEnabled = false;
                core.Settings.IsGeneralAutofillEnabled = false;
                core.NavigationStarting += delegate(object s, CoreWebView2NavigationStartingEventArgs ev) { ev.Cancel = closing || exited || !Protocol.SameOrigin(ev.Uri, origin); };
                core.FrameNavigationStarting += delegate(object s, CoreWebView2NavigationStartingEventArgs ev) { ev.Cancel = true; };
                core.NewWindowRequested += delegate(object s, CoreWebView2NewWindowRequestedEventArgs ev) { ev.Handled = true; };
                core.DownloadStarting += delegate(object s, CoreWebView2DownloadStartingEventArgs ev) { ev.Cancel = true; ev.Handled = true; };
                core.PermissionRequested += delegate(object s, CoreWebView2PermissionRequestedEventArgs ev) { ev.State = CoreWebView2PermissionState.Deny; ev.Handled = true; };
                core.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All);
                core.WebResourceRequested += delegate(object s, CoreWebView2WebResourceRequestedEventArgs ev) {
                    if (closing || exited || !Protocol.SameOrigin(ev.Request.Uri, origin)) ev.Response = environment.CreateWebResourceResponse(new MemoryStream(new byte[0]), 403, "Blocked", "Content-Type: text/plain");
                };
                core.ProcessFailed += delegate { ShowStatus("工作区视图已停止", "正在等待本地服务完成已开始的操作。操作可能已经完成，请先检查历史记录和工作副本状态，再决定是否重试。"); RequestShutdown(false); };
                status.Visible = false; view.Visible = true; view.BringToFront(); core.Navigate(origin.AbsoluteUri);
            } catch {
                if (closing) return;
                ShowStatus("SVN Desk 未能启动", "本地服务或 WebView2 运行时未能启动，或启动校验失败。工作区操作不可用。正在等待本地服务完成已开始的工作；请检查预览包，并在此窗口能够关闭后重新启动。");
                if (started && !exited) RequestShutdown(false);
            } finally { starting = false; }
        }
        private static async void Drain(Stream stream) {
            try { byte[] buffer = new byte[4096]; while (await stream.ReadAsync(buffer,0,buffer.Length).ConfigureAwait(false) != 0) {} }
            catch (IOException) {} catch (ObjectDisposedException) {}
        }
        private void OnClosingRequested(object sender, FormClosingEventArgs e) {
            if (!started || exited || (bridge != null && bridge.HasExited)) return;
            e.Cancel = true;
            if (!closing) {
                ShowStatus("正在完成本地操作", "SVN Desk 已停止接收新操作，正在等待已开始的操作完成。\n\n请保持此窗口打开。关闭窗口不会撤销提交，也不保证取消操作；程序不会超时强制终止服务。");
                RequestShutdown(true);
            } else { closeAfterDrain = true; }
        }
        private void RequestShutdown(bool closeWindow) {
            closeAfterDrain = closeAfterDrain || closeWindow;
            if (closing) return; closing = true;
            if (bridge == null || exited) { if (closeAfterDrain) Close(); return; }
            try { bridge.StandardInput.WriteLine("shutdown"); bridge.StandardInput.Flush(); }
            catch (IOException) {} catch (InvalidOperationException) {}
            finally { try { bridge.StandardInput.Close(); } catch (IOException) {} catch (InvalidOperationException) {} }
            // EOF is a second graceful shutdown signal. Never force-kill, including startup failures.
        }
        private void BridgeExited() {
            if (exited) return; exited = true;
            if (closing) { if (closeAfterDrain) Close(); return; }
            ShowStatus("本地服务已停止", "工作区操作已断开连接，请重新启动 SVN Desk。\n\n被中断的操作可能已经完成。重试前请先检查仓库历史记录和工作副本状态。");
        }
        protected override void Dispose(bool disposing) {
            if (disposing) {
                if (view != null) view.Dispose();
                if (bridge != null) { try { bridge.StandardInput.Close(); } catch (IOException) {} catch (InvalidOperationException) {} bridge.Dispose(); }
                if (profilePolicy != null) profilePolicy.Dispose();
                // WebView2 may retain files briefly. Leave its cache rather than broad cleanup/retries.
            }
            base.Dispose(disposing);
        }
    }
}
