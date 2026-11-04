'use strict';
// Build dependency execution only. The app itself has zero npm runtime dependencies.
const fs = require('node:fs'); const path = require('node:path');
const {downloadArtifact} = require('@electron/get');
const asar = require('@electron/asar');
const {flipFuses, getCurrentFuseWire, FuseVersion, FuseV1Options, FuseState} = require('@electron/fuses');
(async () => {
  const [command, ...args] = process.argv.slice(2);
  if (command === 'download' && args.length === 1) {
    const artifact = await downloadArtifact({version:'45.2.2', artifactName:'electron',platform:'win32',arch:'x64',cacheRoot:path.resolve(args[0]),mirrorOptions:{resolveAssetURL:async details => {
      if (details.version !== 'v45.2.2') throw Error('Unexpected Electron version.');
      const filename = details.isGeneric && details.artifactName === 'SHASUMS256.txt' ? 'SHASUMS256.txt' : (!details.isGeneric && details.artifactName === 'electron' && details.platform === 'win32' && details.arch === 'x64' ? 'electron-v45.2.2-win32-x64.zip' : null);
      if (!filename) throw Error('Unexpected Electron artifact.');
      return 'https://github.com/electron/electron/releases/download/v45.2.2/' + filename;
    }}});
    console.log(JSON.stringify({version:'45.2.2', artifact})); return;
  }
  if (command === 'seal' && args.length === 2) {
    const [stage, output] = args.map(p => path.resolve(p));
    await asar.createPackage(stage, path.join(output,'resources','app.asar'));
    const executable = path.join(output,'SVNDesk-Electron.exe');
    const fuses = {version:FuseVersion.V1,
      [FuseV1Options.RunAsNode]:false,
      [FuseV1Options.EnableCookieEncryption]:true,
      [FuseV1Options.EnableNodeOptionsEnvironmentVariable]:false,
      [FuseV1Options.EnableNodeCliInspectArguments]:false,
      [FuseV1Options.EnableEmbeddedAsarIntegrityValidation]:false,
      [FuseV1Options.OnlyLoadAppFromAsar]:true,
      [FuseV1Options.LoadBrowserProcessSpecificV8Snapshot]:false,
      [FuseV1Options.GrantFileProtocolExtraPrivileges]:false,
      [FuseV1Options.WasmTrapHandlers]:true,
      strictlyRequireAllFuses:true};
    await flipFuses(executable,fuses);
    const actual = await getCurrentFuseWire(executable);
    for (const [key,value] of Object.entries(fuses)) {
      if (/^\d+$/.test(key) && actual[key] !== (value ? FuseState.ENABLE : FuseState.DISABLE)) throw Error('Packaged fuse mismatch.');
    }
    console.log(JSON.stringify({electron:'45.2.2',fuses:actual,asarFiles:asar.listPackage(path.join(output,'resources','app.asar'))})); return;
  }
  throw Error('Expected download CACHE or seal STAGE OUTPUT.');
})().catch(error=>{console.error(error.message); process.exitCode=1;});
