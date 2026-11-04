'use strict';
const {app, BrowserWindow, dialog, session, screen} = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const {randomUUID} = require('node:crypto');
const {parseOptions, allowedResource} = require('./policy.cjs');
const {createProfile, prepareProfilePaths} = require('./profile.cjs');
const {BridgeProcess} = require('./bridge-process.cjs');

app.enableSandbox();
app.setName('SVN Desk Electron Preview');
let window = null, statusWindow = null, bridge = null, quitting = false, shutdownStarted = false;
let options;
try {
  if (process.platform !== 'win32' || !app.isPackaged) throw Error('Run the packaged Windows preview.');
  const forbiddenSwitches = ['remote-debugging-port', 'remote-debugging-pipe', 'no-sandbox', 'disable-web-security', 'ignore-certificate-errors', 'disable-features', 'enable-features', 'js-flags', 'load-extension', 'user-data-dir', 'proxy-server', 'proxy-pac-url'];
  if (forbiddenSwitches.some(name => app.commandLine.hasSwitch(name))) throw Error('Unsupported Chromium override.');
  options = parseOptions(process.argv.slice(1), path.win32);
  const profile = createProfile(options.profileRoot, app.getPath('temp'));
  // Electron setPath requires each destination to exist before it is called.
  for (const [name, directory] of Object.entries(prepareProfilePaths(profile))) app.setPath(name, directory);
} catch {
  dialog.showErrorBox('SVN Desk could not start', 'Use the packaged Windows preview with valid startup options and a new local profile directory.');
  app.exit(1);
}

function protectSession(ses, allow) {
  ses.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  ses.setPermissionCheckHandler(() => false);
  ses.setDevicePermissionHandler(() => false);
  ses.on('will-download', event => event.preventDefault());
  ses.webRequest.onBeforeRequest((details, callback) => callback({cancel: !allow(details)}));
}
function protectWindow(win, origin) {
  const wc = win.webContents;
  wc.setWindowOpenHandler(() => ({action: 'deny'}));
  wc.on('will-attach-webview', event => event.preventDefault());
  wc.on('will-navigate', (event, url) => { if (url !== origin + '/') event.preventDefault(); });
  wc.on('will-frame-navigate', (event, details) => {
    // Modern Electron supplies details on the event; older signatures are rejected.
    const info = details || event;
    if (!info.isMainFrame || info.url !== origin + '/') event.preventDefault();
  });
  wc.on('will-redirect', event => event.preventDefault());
}
function securePreferences(ses) {
  return {session: ses, nodeIntegration: false, nodeIntegrationInWorker: false,
    nodeIntegrationInSubFrames: false, contextIsolation: true, sandbox: true,
    webSecurity: true, allowRunningInsecureContent: false, webviewTag: false,
    devTools: false, navigateOnDragDrop: false, spellcheck: false};
}
function closeWindows() {
  if (window && !window.isDestroyed()) window.destroy(); window = null;
  if (statusWindow && !statusWindow.isDestroyed()) statusWindow.destroy(); statusWindow = null;
}
function showStatus(title, message) {
  if (statusWindow && !statusWindow.isDestroyed()) statusWindow.destroy();
  const ses = session.fromPartition('status-' + randomUUID(), {cache: false});
  const escape = value => value.replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const html = `<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'"><title>${escape(title)}</title><style>body{background:#101521;color:#e6edf7;font:16px system-ui;padding:34px;line-height:1.65}h1{font-size:22px}</style><h1>${escape(title)}</h1><p>${escape(message)}</p>`;
  const url = 'data:text/html;charset=utf-8,' + encodeURIComponent(html);
  protectSession(ses, details => details.url === url && details.resourceType === 'mainFrame');
  statusWindow = new BrowserWindow({width:600, height:270, show:true, title, autoHideMenuBar:true, webPreferences:securePreferences(ses)});
  statusWindow.removeMenu(); protectWindow(statusWindow, '');
  statusWindow.on('close', event => { if (!quitting && bridge && !bridge.finished) { event.preventDefault(); void shutdown(); } });
  const current = statusWindow;
  void current.loadURL(url).catch(() => { if (!current.isDestroyed() && !quitting) dialog.showErrorBox(title, message); });
}
async function shutdown(message = 'The workspace view is closed. Waiting for operations already accepted by SVN to finish. Closing does not cancel or roll back an operation.') {
  if (shutdownStarted) return;
  shutdownStarted = true; closeWindows();
  if (bridge && !bridge.finished) {
    showStatus('Closing SVN Desk safely', message);
    await bridge.shutdown();
  }
  quitting = true; closeWindows(); app.quit();
}
app.on('before-quit', event => {
  if (!quitting && bridge && !bridge.finished) { event.preventDefault(); void shutdown(); }
});
// Every managed window has its own close path. Destruction during transitions
// must not race the explicit graceful shutdown sequence.
app.on('window-all-closed', () => {});
app.on('certificate-error', (event, _wc, _url, _error, _certificate, callback) => { event.preventDefault(); callback(false); });

app.whenReady().then(async () => {
  if (!options) return;
  if (!options.demo && !options.hasWorkspace) {
    while (true) {
      const choice = await dialog.showMessageBox({type:'question', title:'SVN Desk Electron Preview', message:'Open SVN Desk', detail:'Choose one working copy in read-only mode, or explore the sample data. No folders are scanned automatically.', buttons:['Choose working copy', 'Explore demo', 'Cancel'], defaultId:0, cancelId:2, noLink:true});
      if (choice.response === 2) { quitting = true; app.quit(); return; }
      if (choice.response === 1) break;
      const chosen = await dialog.showOpenDialog({title:'Choose an SVN working copy', properties:['openDirectory']});
      if (!chosen.canceled && chosen.filePaths.length === 1) { options.bridgeArgs = ['--workspace', chosen.filePaths[0]]; break; }
    }
  }
  const executable = path.join(process.resourcesPath, 'Bridge', 'SVNDeskBridge.exe');
  if (!fs.existsSync(executable) || !fs.statSync(executable).isFile()) throw Error('Missing bundled bridge.');
  bridge = new BridgeProcess(executable, options.bridgeArgs);
  bridge.on('unexpected-exit', () => {
    closeWindows();
    dialog.showErrorBox('SVN Desk bridge stopped', 'The workspace view has been closed. An operation may have an uncertain outcome. Check its result before retrying.');
    void shutdown();
  });
  showStatus('Starting SVN Desk', 'Starting the bundled bridge. No workspace controls open until its local endpoint and process identity have been verified.');
  const origin = await bridge.start();
  if (shutdownStarted || bridge.terminated || bridge.finished) return;
  const ses = session.fromPartition('svn-desk-' + randomUUID(), {cache:false});
  protectSession(ses, details => allowedResource(details.url, origin, details.resourceType));
  await ses.setProxy({mode:'direct'});
  if (shutdownStarted || bridge.terminated || bridge.finished) return;
  const area = screen.getPrimaryDisplay().workAreaSize;
  window = new BrowserWindow({width:Math.min(1440,area.width), height:Math.min(920,area.height), minWidth:Math.min(800,area.width), minHeight:Math.min(500,area.height), backgroundColor:'#101521', show:false, title:'SVN Desk · Electron Preview', autoHideMenuBar:true, webPreferences:securePreferences(ses)});
  window.removeMenu(); protectWindow(window, origin);
  window.on('close', event => { if (!quitting) { event.preventDefault(); void shutdown(); } });
  window.webContents.on('render-process-gone', () => { if (!shutdownStarted) void shutdown('The renderer stopped. Waiting for accepted SVN work to finish. Check operation results before retrying.'); });
  await window.loadURL(origin + '/');
  if (shutdownStarted || bridge.terminated || bridge.finished || !window || window.isDestroyed()) return;
  window.show();
  if (statusWindow && !statusWindow.isDestroyed()) statusWindow.destroy(); statusWindow = null;
}).catch(async () => {
  if (shutdownStarted) return;
  dialog.showErrorBox('SVN Desk could not start', 'The packaged bridge or local view could not start. No automatic retry is performed. An already accepted operation must finish draining.');
  await shutdown();
});
