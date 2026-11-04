# SVN Desk native Windows preview host

A real Windows Forms window hosting Microsoft Edge WebView2, not a browser launcher. The modern UI is served directly by the packaged Python bridge at its readiness-confirmed ephemeral loopback origin. The existing repository Tauri prototype is unchanged; this independent host avoids a Rust installation.

## Existing prerequisites

- Windows x64 with .NET Framework 4.8 and its existing C# compiler (`%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe`).
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
- Startup failures and timeouts also signal shutdown/EOF and retain ownership until the child exits. No blind force-kill, even before readiness.
- Stderr and remaining stdout are drained without logging or retaining workspace paths, tokens or raw SVN output.
- Each run gets an isolated WebView profile under `%LOCALAPPDATA%/SVNDesk/WebView2/<random-id>`. Profile cache is left in place because WebView2 may retain locks after disposal. No cleanup of unrelated files is attempted.
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
