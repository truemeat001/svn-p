# SVN Desk native Windows preview host

A real Windows Forms window hosting Microsoft Edge WebView2, not a browser launcher. The modern UI is served directly by the packaged Python bridge at its readiness-confirmed ephemeral loopback origin. The existing repository Tauri prototype is unchanged; this independent host avoids a Rust installation.

## Existing prerequisites

- Windows 10 Creators Update (1703) or later / Windows 11 x64 with .NET Framework 4.8 and its existing C# compiler (`%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe`).
- Installed Microsoft Edge WebView2 Runtime.
- Extracted official `Microsoft.Web.WebView2` NuGet 1.0.3537.50 (https://www.nuget.org/packages/Microsoft.Web.WebView2/1.0.3537.50). Build consumes `lib/net462` Core/WinForms assemblies and the x64 native loader. Package restore is a project-local dependency, not a system installation.
- SVN executable installed separately for real working copies. Demo does not require a working copy.

Build with `powershell -File desktop/windows/build.ps1 -WebView2PackagePath <extracted-package> -OutDir <package-directory>`. This compiles and executes pure protocol tests, then compiles the host. Do not claim native compilation or runtime validation from static review alone.

Package layout:

```
SVNDesk.exe
SVNDesk.exe.config
Microsoft.Web.WebView2.Core.dll
Microsoft.Web.WebView2.WinForms.dll
WebView2Loader.dll
Bridge/SVNDeskBridge.exe
Bridge/_internal/...
```

Launch without arguments for a native folder chooser or demo choice. Picker cancellation leaves the chooser open and starts no service. The picker selects one read-only workspace before process startup. There is no live root switching; close and relaunch to select another root. CLI supports repeatable `--workspace PATH`, repeatable `--scan-parent PATH`, optional `--svn PATH`, and explicit `--enable-changelists`, `--enable-shelves`, `--enable-commits`, `--enable-locks`, `--enable-adds`, `--enable-file-ops`. All write features remain off by default. Paths are made absolute before the child starts. No arbitrary backend path or command passthrough is supported.

## Lifecycle/security contract

- Fixed child at `<host-directory>/Bridge/SVNDeskBridge.exe`; no PATH lookup, shell or browser launch.
- Child stdout first line must be <=4096 bytes including newline, exact protocol `svn-desk-desktop/1`, actual child PID, canonical `http://127.0.0.1:<nonzero-port>`. Startup timeout is 30 seconds. No UI HTTP requests before readiness.
- Requires PyInstaller **onedir**, not onefile, so PID ownership is unambiguous.
- No host objects, generic WebView messages, arbitrary native IPC, downloads, external drop, remote navigation, iframes, new windows, or permission grants. Resource requests are restricted to the exact bridge origin. Bridge CSP and origin/session checks remain required.
- Closing disposes the WebView immediately, sends `shutdown\n` on the persistent stdin pipe and closes that pipe. Bridge must stop accepting work, drain already-started HTTP/SVN operations, then exit. No post-readiness force-kill timeout. The native waiting screen explicitly warns that close is not cancellation/rollback. Child crash removes the usable web view and warns that operation outcome may be uncertain.
- The bridge bounds the whole pre-dispatch request-line/header phase to five seconds, independently of its body deadline. A client trickling header bytes cannot restart this budget or hold graceful close for hours. Accepted SVN work still drains normally. This source repair needs a rebuilt bridge and fresh Windows validation; see [HTTP header deadline](../../prototypes/svn-desk-web/docs/HTTP_HEADER_DEADLINE.md).
- Startup failures and timeouts also signal shutdown/EOF and retain ownership until the child exits. No blind force-kill, even before readiness.
- Stderr and remaining stdout are drained without logging or retaining workspace paths, tokens or raw SVN output.
- Without `--profile-root`, each run uses a random WebView profile under the OS-known LocalApplicationData folder's `SVNDesk/WebView2/` directory; process-level `LOCALAPPDATA` overrides do not reliably redirect that API. Profile cache is left in place because WebView2 may retain locks after disposal. No cleanup of unrelated files is attempted.
- Forced OS process termination cannot guarantee transaction cancellation; bridge EOF drain is the recovery path. External Task Manager/OS termination and power loss remain outside orderly shutdown guarantees.

## Required Windows acceptance checks

1. Compile and run protocol checks using `build.ps1`; launch actual native host with demo and verify the modern UI.
2. Choose a disposable SVN working copy via folder chooser, cancel picker once, retry, verify read-only startup and Unicode/space paths.
3. Wrong PID/origin, oversized readiness, missing child, child early exit and 30-second startup timeout fail without opening workspace controls.
4. Attempt external navigation, popup, download, permission prompt and frame load; verify blocked. Verify same-origin bridge session requests work.
5. With a slow operation in a disposable repository, close repeatedly: page vanishes, shutdown is sent once, process remains until operation drains, no false cancellation message or automatic kill.
6. Kill the test bridge: stale UI must disappear. Verify crash warning and no automatic retry/duplicate writes.
7. Verify host and bridge process both exit on normal close, and Windows restart/forced-close caveats are documented.

These checks are not yet complete. Source-level Linux validation does not verify native compilation or Windows UI behavior. Record Windows build and acceptance results separately before distributing a trial package.

Build safety: both output and sibling `.diagnostics` directories must be new. No overwrite mode is provided. NuGet ID/version are checked against the pinned package metadata (not a substitute for trusted retrieval/signature verification). Compiler logs, protocol-test executable/results, and SHA-256 build manifest are written only to the sibling diagnostics directory, not the shipped application. Manifest records GUI validation as not run.

Before creating WebView2, the host clears known `WEBVIEW2_*` user-data-directory, executable-directory, additional-arguments, release-channel and script-debugger environment overrides **in this process only**. It does not change machine/user settings or reuse a configured real browser profile. Enterprise/registry runtime policies still require validation on the target machine.

## Assemble the complete portable package

Do not ship the host output alone. Use distinct, new directories for the bridge build and final package. With the reviewed Python/PyInstaller environment already available, from the repository root:

```powershell
# All three paths below must be controlled build paths, not existing user folders.
$bridgeBuild = 'C:\SVNDeskBuild\bridge-NEW'
$package = 'C:\SVNDeskBuild\SVNDesk-NEW'
$webview2 = 'C:\SVNDeskBuild\deps\Microsoft.Web.WebView2.1.0.3537.50'
python desktop/windows/build_bridge.py --source . --output $bridgeBuild
if ($LASTEXITCODE -ne 0) { throw 'Bridge build/self-check failed' }
powershell -NoProfile -File desktop/windows/build.ps1 -WebView2PackagePath $webview2 -OutDir $package
if ($LASTEXITCODE -ne 0) { throw 'Host build/protocol checks failed' }
$bridgeSource = Join-Path $bridgeBuild 'dist\SVNDeskBridge'
$bridgeDestination = Join-Path $package 'Bridge'
if (Test-Path $bridgeDestination) { throw 'Bridge destination must not already exist' }
# Copy the WHOLE onedir product, including _internal. Never copy only the EXE.
Copy-Item -LiteralPath $bridgeSource -Destination $bridgeDestination -Recurse
$manifest = Get-Content -Raw (Join-Path $bridgeBuild 'bridge-build-manifest.json') | ConvertFrom-Json
$expected = @($manifest.artifactFiles.PSObject.Properties)
$actual = @(Get-ChildItem -LiteralPath $bridgeDestination -File -Recurse)
if ($actual.Count -ne $expected.Count) { throw 'Copied bridge file count differs' }
foreach ($entry in $expected) {
    # Manifest is generated by the reviewed build_bridge.py, not external input.
    $file = Join-Path $bridgeDestination $entry.Name
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash.ToLowerInvariant() -ne $entry.Value) { throw 'Copied bridge hash mismatch' }
}
$selfCheckJson = & (Join-Path $bridgeDestination 'SVNDeskBridge.exe') --self-check
if ($LASTEXITCODE -ne 0) { throw 'Packaged bridge self-check failed' }
$selfCheck = $selfCheckJson | ConvertFrom-Json
if ($selfCheck.protocol -ne 'svn-desk-desktop/1' -or $selfCheck.status -ne 'ok' -or $selfCheck.backendVerified -ne $true) { throw 'Unexpected packaged bridge result' }
```

The bridge output is `<new-bridge-build>/dist/SVNDeskBridge`; final placement is `<new-package>/Bridge`, including every `_internal` resource. Keep bridge and native build manifests in the evidence/diagnostics folder beside the package. Before archiving, record a final recursive SHA-256 inventory of the assembled package and retain it with build evidence; verify the extracted archive against that inventory. Then run the native GUI/lifecycle acceptance checks above. A passing build/self-check is not a GUI pass.

The host builder copies the official package's license and separate notices into `Licenses/`, using only explicit top-level candidates: `LICENSE.txt`, `LICENSE`, `LICENSE.md`, `THIRD-PARTY-NOTICES.txt`, `ThirdPartyNotices.txt`, `NOTICE.txt`, and `NOTICE`. A missing license stops the build. Missing separate notices emit a warning and set `separateWebView2NoticeAbsent` in the manifest; verify the supplied official NuGet contents before redistribution. The actual package has not been inspected on this Linux source-authoring machine, so notice presence is not asserted here. Do not substitute an invented license or recursively copy unrelated dependency files. The bridge freezer's own Python/runtime third-party licensing obligations must also be reviewed for redistribution.

Stdout fault-injection acceptance: use a disposable test child that emits invalid or over-4096-byte readiness, then more garbage than the pipe capacity (e.g. 256 KiB), and only then reads stdin for shutdown. Verify the host reports failure, continuously discards the stdout tail, sends shutdown/EOF, and observes child exit without deadlock. Also delay the first newline until after the 30-second startup timeout, then emit a large tail: the already-attached drain must take ownership after the pending handshake completes. The host attaches one drain chain immediately to handshake completion on success **or failure**, never starts concurrent reads, and observes readiness faults. Pure protocol tests cover invalid/oversized readiness followed by a 256 KiB tail; Windows scheduling/pipe behavior still requires this native fault-injection check.

## Explicit isolated native profile

For acceptance tests, do not depend on overriding `LOCALAPPDATA`: .NET's special-folder API can still select the real user profile. Launch the native host with its native-only option instead:

```powershell
# Parent exists already, outside any Git/SVN working copy. This child must NOT exist.
& 'C:\SVNDeskBuild\SVNDesk-NEW\SVNDesk.exe' --profile-root 'C:\SVNDeskBuild\gui-profile-NEW'
```

This opens the usual workspace/demo chooser. Choose **Explore demo** for isolated UI validation; do not select a real project. Combining `--profile-root` with explicit workspace flags is also supported, but the profile option is removed before bridge argument validation/launch and is never exposed over HTTP/JavaScript. Choosing a workspace from the startup dialog preserves the native profile option. Every new test run must use a fresh profile-root name. The option's existing local parent is mandatory; no parent directories are silently created.

The host rejects duplicate/missing profile options, relative/UNC/device paths, substituted drive aliases, dot/dotdot segments, trailing dots/spaces, alternate data streams and reserved devices. It rejects existing destinations, files, reparse/symlink ancestors, and ancestors containing `.git` (file or directory) or `.svn`. Fresh root and random child creation use `CreateDirectoryW` and fail if already present. Ancestors/root/child are held without delete sharing while the host runs and rechecked before WebView2 creation. Partial fresh directories can remain after a startup failure; never reuse them. Explicit profile directories are not deleted automatically. This is not a security boundary against another process with write/reparse rights to the same directories. Enterprise WebView2 registry policy overrides must also be checked before an isolated acceptance run.

Without `--profile-root`, ordinary user launches retain the standard isolated random child under the user's normal LocalApplicationData. Known WebView2 environment overrides are cleared only for this host process in both modes.

`build.ps1` now compiles/runs separate Windows `ProfilePolicyTests` in the isolated sibling diagnostics directory, including existing-file/directory rejection, missing-parent rejection, working-copy markers, held-directory rename protection, rechecks, and a symlink-parent fixture. The symlink fixture can skip **only** Win32 error 1314 (missing privilege), reporting that skip in its log; any other error fails. Build diagnostics must themselves be outside a Git/SVN working copy. Pure protocol tests cover native-option separation and unsafe path syntax. These new checks have not been run on the source-authoring Linux machine. The previously reported Windows 33-protocol/8-frozen-bridge passes apply to the prior checkpoint, not this profile patch.


Source-process PID fixture: Windows venv `python.exe` is a redirector and can have a different PID from the actual Python process. For these standard-library-only source subprocess fixtures, Windows venv runs now select the existing absolute `sys._base_executable` directly. The exact readiness PID equality check is retained, not skipped or relaxed; a missing base interpreter fails the test. Other tests still run in their selected interpreter. Actual packaged onedir PID checks remain separate. See [Python venv documentation](https://docs.python.org/3/library/venv.html).


## Native welcome-page DPI repair (source only)

The user report and screenshot show blurred text on the native welcome page before WebView2 starts. The 5298602 source configured `PerMonitorV2` in `SVNDesk.exe.config`, but `build.ps1` did not embed a Windows 10 compatibility manifest. That omits a documented prerequisite for .NET Framework high-DPI support. The buttons also inherited the legacy WinForms default font. These are confirmed source defects; the screenshot alone does not establish the target computer's effective DPI, display scaling, compatibility overrides or every cause of blur.

This patch embeds `SVNDesk.manifest` with Windows 10 compatibility and non-elevated `asInvoker` execution. DPI awareness stays in the existing app.config, rather than competing manifest DPI tags. The manually compiled assembly now declares .NET Framework 4.8 explicitly. The native form uses a 96-DPI layout baseline with `AutoScaleMode.Dpi`, and controls inherit Segoe UI 10pt. Native welcome/error/status text is Chinese. Auto-sized labels and a wrapping button row replace the fixed-height gaps, within a centered width capped at 720 logical pixels and the current available window width. The preferred workbench size is retained, with its initial bounds and minimum dimensions capped to the current monitor working area so high-DPI scaling cannot force it below the taskbar. Buttons are created before the first scaling pass, not in `Shown`. Default read-only mode, feature opt-ins, bridge arguments, profile isolation and shutdown behavior are unchanged.

`build.ps1` explicitly compiles UTF-8 source and now runs `DpiTests.exe` from the sibling diagnostics directory. The test executable uses the same host source, application config and embedded manifest. It checks the actual packaged EXE's `RT_MANIFEST`, its packaged app.config, effective thread/window PerMonitorV2 context, native/WinForms DPI agreement, inherited UI/button font, initial button presence and window bounds within the monitor working area. It creates a hidden native window only; it never shows the launcher, opens a working copy, starts the bridge or creates a browser profile. A failing native check blocks a successful build. `nativeGuiTest` remains `not_run` regardless of those checks.

Cross-platform source guard: `python -m unittest discover -s desktop/windows/tests -p 'test_*.py'`. This can validate XML, source/build wiring and build inputs on Linux. It cannot establish Windows font sharpness or native compilation.

The first Windows build of d283bc7 exposed a test initialization-order defect: .NET Framework applied the configured DPI mode during the first `DeskWindow` construction, after `EnableVisualStyles()`. The test had checked thread awareness before that lazy initialization. The unchanged thread assertion now runs immediately after constructing the hidden host window, before requesting its handle; the existing native window/context, DPI, font and bounds assertions remain mandatory. A source guard prevents the assertion from moving back before construction. An isolated Windows experiment with this ordering passed all 21 native checks at 144 DPI, with PerMonitorV2, Segoe UI 10pt and no visible window. That experiment is evidence for the fix, not a rebuilt-package or visual acceptance claim. The repaired source passes 18 cross-platform desktop tests; rerun the complete Windows build on the committed repair before packaging.

Required acceptance on Windows before delivering another portable package:

1. Build both host and bridge from this exact source; require protocol, profile-policy, DPI and bridge tests to pass. Record the compiled EXE/config/source hashes and `DpiTests.log`.
2. Launch from a fresh isolated profile at 100%, 125%, 150% and 200% display scaling. Verify the Chinese welcome heading, body and both buttons are sharp, fully visible and comfortably spaced. Resize narrow/wide and maximize/restore; text and buttons must not overlap or be clipped. Capture native screenshots with each display scale recorded.
3. Move the same window between monitors with different DPI, then back. Check welcome/status controls and WebView2 content after entering demo. No repeated enlargement, blur or stale viewport sizes is allowed.
4. Cancel the folder picker, reopen it, return to the chooser, then open demo. Confirm the real project remains untouched and all write features stay off. Repeat startup-error and orderly-shutdown status screens to verify wrapped Chinese warnings remain readable.
5. Inspect application-specific Windows compatibility DPI overrides if effective runtime DPI does not match the expected mode. Do not silently change user/OS display, compatibility or font settings.

The web CSS contains no page-level zoom/transform workaround for this defect, and this patch does not add one. Browser screenshots or Linux Chromium tests are not evidence that Windows native text is repaired.

References: [Microsoft .NET Framework high-DPI setup](https://learn.microsoft.com/en-us/dotnet/desktop/winforms/high-dpi-support-in-windows-forms), [automatic scaling](https://learn.microsoft.com/en-us/dotnet/desktop/winforms/forms/autoscale), [DPI context comparison](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-aredpiawarenesscontextsequal).

## Checkout source checkpoint

Native argument forwarding and the fixed bridge build allowlist now include the paired `--checkout-url URL --checkout-destination ABSOLUTE_PATH` flow. Startup is local-only; the Web dialog must separately preflight and confirm its fixed URL and new/empty target. Success adds a candidate without selecting it. Both `checkout_ops.py` and `checkout_paths.py` must be in a freshly rebuilt bridge. See [scope, recovery, directory guards and pending validation](../../prototypes/svn-desk-web/docs/CHECKOUT_WORKFLOW.md).

No new Windows package has been built for this change. The Windows directory-handle guard and updated C# protocol tests still require actual Windows execution; Linux source tests and input-manifest validation do not establish native compatibility. Existing delivered trial binaries do not include checkout.
