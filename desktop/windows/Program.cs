using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

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
        private readonly Panel status = new Panel();
        private readonly FlowLayoutPanel choices = new FlowLayoutPanel();
        private Process bridge;
        private Task stdoutDrain;
        private WebView2 view;
        private Uri origin;
        private bool closing, closeAfterDrain, exited, starting, started;
        private string profileDirectory;

        internal DeskWindow(string[] args) {
            launchArgs = args;
            Text = "SVN Desk · Windows Preview"; ClientSize = new Size(1440, 940); MinimumSize = new Size(800, 640);
            StartPosition = FormStartPosition.CenterScreen; BackColor = Color.FromArgb(245,247,251);
            status.Dock = DockStyle.Fill; status.Padding = new Padding(64,100,64,40);
            heading.Text = "SVN Desk"; heading.Font = new Font("Segoe UI",28,FontStyle.Bold); heading.ForeColor = Color.FromArgb(23,36,58); heading.Dock = DockStyle.Top; heading.Height = 84;
            detail.Font = new Font("Segoe UI",12); detail.ForeColor = Color.FromArgb(82,97,122); detail.Dock = DockStyle.Top; detail.Height = 160;
            choices.Dock = DockStyle.Top; choices.Height = 70;
            status.Controls.Add(choices); status.Controls.Add(detail); status.Controls.Add(heading); Controls.Add(status);
            FormClosing += Closing; Shown += Open;
        }
        private void ShowStatus(string title, string text) {
            // Disposing the WebView removes stale controls and in-flight page scripting.
            if (view != null) { Controls.Remove(view); view.Dispose(); view = null; }
            heading.Text = title; detail.Text = text; status.Visible = true; status.BringToFront();
            choices.Visible = false;
        }
        private async void Open(object sender, EventArgs e) {
            if (launchArgs.Length > 0) { await Start(launchArgs); return; }
            heading.Text = "A clearer view of your working copy";
            detail.Text = "Choose an existing SVN working-copy folder to open it read-only, or explore the demo.\nWrite features remain off unless explicitly enabled with launch options.";
            choices.Visible = true;
            var choose = new Button { Text = "Open workspace…", AutoSize = true, Height = 42, Padding = new Padding(12,8,12,8) };
            var demo = new Button { Text = "Explore demo", AutoSize = true, Height = 42, Padding = new Padding(12,8,12,8) };
            choose.Click += async delegate {
                using (var picker = new FolderBrowserDialog { Description = "Select an existing SVN working-copy folder (read-only preview)", ShowNewFolderButton = false }) {
                    if (picker.ShowDialog(this) == DialogResult.OK) await Start(new[] { "--workspace", picker.SelectedPath });
                }
            };
            demo.Click += async delegate { await Start(new string[0]); };
            choices.Controls.Add(choose); choices.Controls.Add(demo);
        }
        private async Task Start(string[] args) {
            if (starting || started || closing) return;
            starting = true; string[] validated;
            try { validated = Protocol.Arguments(args); }
            catch { starting = false; ShowStatus("Check the launch options", "Only documented workspace paths, an SVN executable path, and feature flags are accepted. No local service was started."); return; }
            ShowStatus("Opening your workspace", "Starting the packaged local service. Your working copy stays on this computer.");
            try {
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
                profileDirectory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "SVNDesk", "WebView2", Guid.NewGuid().ToString("N"));
                view = new WebView2 { Dock = DockStyle.Fill, Visible = false, AllowExternalDrop = false };
                Controls.Add(view);
                // Process-local only: never inherit a real browser profile or debug switches.
                foreach (string variable in new[] { "WEBVIEW2_USER_DATA_FOLDER", "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "WEBVIEW2_RELEASE_CHANNEL_PREFERENCE", "WEBVIEW2_CHANNEL_SEARCH_KIND", "WEBVIEW2_RELEASE_CHANNELS", "WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER" }) Environment.SetEnvironmentVariable(variable, null, EnvironmentVariableTarget.Process);
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
                core.ProcessFailed += delegate { ShowStatus("The workspace view stopped", "The local service is being drained. An operation may already have completed. Check history and working-copy status before retrying."); RequestShutdown(false); };
                status.Visible = false; view.Visible = true; view.BringToFront(); core.Navigate(origin.AbsoluteUri);
            } catch {
                if (closing) return;
                ShowStatus("SVN Desk could not start", "The packaged service or WebView2 runtime could not start, or readiness failed. No workspace controls are available. Any local service is being drained before exit. Check the preview package, then restart after this window can close.");
                if (started && !exited) RequestShutdown(false);
            } finally { starting = false; }
        }
        private static async void Drain(Stream stream) {
            try { byte[] buffer = new byte[4096]; while (await stream.ReadAsync(buffer,0,buffer.Length).ConfigureAwait(false) != 0) {} }
            catch (IOException) {} catch (ObjectDisposedException) {}
        }
        private void Closing(object sender, FormClosingEventArgs e) {
            if (!started || exited || (bridge != null && bridge.HasExited)) return;
            e.Cancel = true;
            if (!closing) {
                ShowStatus("Finishing local operations", "SVN Desk is no longer accepting work. Waiting for operations already started to finish.\n\nKeep this window open. Closing does not undo a commit or guarantee cancellation. There is no automatic force-kill timeout.");
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
            ShowStatus("The local service stopped", "Workspace controls have been disconnected. Restart SVN Desk to reconnect.\n\nAn interrupted operation may have completed. Check repository history and working-copy status before retrying.");
        }
        protected override void Dispose(bool disposing) {
            if (disposing) {
                if (view != null) view.Dispose();
                if (bridge != null) { try { bridge.StandardInput.Close(); } catch (IOException) {} catch (InvalidOperationException) {} bridge.Dispose(); }
                // WebView2 may retain files briefly. Leave its cache rather than broad cleanup/retries.
            }
            base.Dispose(disposing);
        }
    }
}
