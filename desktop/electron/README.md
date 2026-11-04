# SVN Desk Electron proof of concept

Independent Windows Electron host alongside `desktop/windows`. It keeps the existing HTML/CSS UI, reviewed Python bridge and installed SVN CLI workflow. It does not assert complete P4V parity.

## Build

Prerequisites: existing Node.js >=22.12, npm, Python 3.12+, and the verified `SVNDesk-Windows-x64-5298602-Trial.zip` (SHA-256 `f0986cd13dfec94605739e5f844e1807355a7dfb25c1b679559d8190f96dd266`). No system install, global npm modification, publishing or signing is performed. Use a new isolated build location.

```
cd desktop/electron
npm ci --ignore-scripts --no-audit --no-fund --cache <project-local-cache>
npm test
python3 -m unittest discover -s tests -p 'test_*.py'
python3 build.py --bridge-package <verified-baseline.zip> --output <new-directory> --cache <project-local-electron-cache>
```

The pinned official `electron` 45.2.2 package provides `@electron/get`, which verifies its Windows x64 runtime against upstream checksums. The resolver forces the official release URL, and the builder independently pins the official ZIP SHA-256 `a234388419ccbcb18dd2feec856e4ae1832499e200837f23f8e653bd51ff3be5`; environment-provided mirrors or custom versions cannot substitute another artifact. The npm dependency graph and integrity hashes are locked in `package-lock.json`. Build tooling is `@electron/asar` 4.3.1 and `@electron/fuses` 2.1.3; the shipped app has no npm runtime dependencies. All official Electron/Chromium and original bridge licenses are retained.

`build.py` copies only four explicitly named host modules into `app.asar`, verifies the entire original bridge against its package manifest, and applies the two explicitly hash-pinned external JavaScript resources from the reviewed interaction fix. Frozen Python code and executable remain byte-identical to 5298602. Other backend revisions are refused rather than silently mixed. Input archive paths and symlinks are checked. Output must be new; existing packages are not overwritten. A recursive SHA-256 manifest and archive round-trip checks validate the result.

The build result is a portable Windows x64 directory and ZIP. It is **unsigned**. Windows security warnings are not bypassed. ASAR-only loading is enabled, but embedded ASAR integrity enforcement and code signing are not implemented; the package is not tamper-proof against a local account allowed to modify it. Packaging is repeatable from pinned inputs, not a bit-for-bit reproducible binary claim because archive metadata/timestamps may vary.

## Runtime boundary

- `SVNDesk-Electron.exe` launches only `resources/Bridge/SVNDeskBridge.exe`, with `shell:false` and fixed argument schema. It accepts no generic process/path override or page-to-native IPC.
- Opening without arguments gives a native folder/demo chooser. Cancelling a folder selection returns to that chooser without starting the bridge. Demo does not inspect a working copy. `--workspace`, `--scan-parent`, `--svn` and the existing explicit `--enable-*` flags are supported. Workspace write features remain disabled by default.
- Readiness is at most 4096 bytes, has strict UTF-8/JSON/schema, exact owned PID, and canonical `http://127.0.0.1:<nonzero-port>`. A 30-second startup timer fails closed. Stdout/stderr tails are always drained without retaining sensitive output.
- Sandboxed, context-isolated renderer, no Node/preload/webview/devtools/IPC. Exact bridge-origin requests only, no subframes, remote navigation, popups, downloads, permissions, external shell links or system proxy routing. The server supplies CSP and origin/session verification.
- Per-launch fresh temporary user-data directory and non-persistent sessions. Explicit `--profile-root` must be a fresh local path below an existing plain directory outside Git/SVN roots. The profile is retained, never recursively deleted. Same-user filesystem races, hostile administrator policies and OS termination are outside the guarantee.
- Closing removes the workspace view immediately, sends shutdown/EOF exactly once and waits for the bridge to drain accepted operations. It never force-kills or claims closing means rollback. All Electron path overrides are created before `app.setPath`; a regression test covers the startup prerequisite. Bridge `exit` immediately invalidates the view; `close` independently tracks completion of stdio drainage.
- Fuses disable RunAsNode, Node-options environment injection, CLI Node inspection and extra file-protocol privileges. ASAR-only loading is enabled. Known Chromium override switches are rejected. No updater, background service or login integration is added.

## Verification and limits

41 Node host tests and 6 Python package tests passed. Merged web suite: 176 Node tests passed; 536 Python tests ran, 534 passed and two existing optional real-working-copy tests skipped. Existing bridge build-input tests: 9 passed. Source bridge self-check and real source-process demo readiness/HTTP/CSP/graceful close passed without a workspace.

The unconfigured web test command initially failed because `SVN_DESK_BACKEND_SOURCE` was unset. It passed when rerun using the documented source setting, without changing tests:

```
cd prototypes/svn-desk-web
SVN_DESK_BACKEND_SOURCE=<absolute-repository-root> npm test
```

Windows executable assembly and binary/fuse/hash verification passed on Linux. **A real Windows Electron window has not been launched or accepted.** No scrolling, startup, memory or responsiveness comparison with WebView2 has been measured. The web data-model benchmark is not an Electron/WebView2 comparison.

Before broader use, test Windows demo, folder cancel/retry, Unicode paths, 100/125/150/200% DPI, keyboard/context menus, external navigation and permissions blocking, child crash, startup failure/timeout, and repeated close during a disposable slow SVN operation. Verify both host and bridge exit on normal close. Test only disposable workspaces, never real credentials/projects for acceptance.

## Official references

- https://www.electronjs.org/docs/latest/tutorial/security
- https://www.electronjs.org/docs/latest/tutorial/sandbox
- https://www.electronjs.org/docs/latest/tutorial/application-distribution
- https://www.electronjs.org/docs/latest/tutorial/fuses
- https://releases.electronjs.org/release/v45.2.2
