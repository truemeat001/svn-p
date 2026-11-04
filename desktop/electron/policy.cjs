'use strict';
const path = require('node:path');
const PROTOCOL = 'svn-desk-desktop/1';
const WRITE_FLAGS = new Set(['--enable-changelists', '--enable-shelves', '--enable-commits', '--enable-locks', '--enable-adds', '--enable-file-ops']);
function parseOptions(argv, paths = path) {
  const bridgeArgs = []; let profileRoot = null; let demo = false; let hasWorkspace = false;
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--demo') { if (demo) throw Error('Duplicate demo option.'); demo = true; }
    else if (arg === '--profile-root' || ['--workspace', '--scan-parent', '--svn'].includes(arg)) {
      const value = argv[++i];
      if (!value || value.startsWith('--') || !paths.isAbsolute(value) || /[\x00-\x1f]/.test(value)) throw Error('An absolute local path is required.');
      if (paths === path.win32 && (!/^[A-Za-z]:\\/.test(value) || /(?:^|\\)\.\.?($|\\)/.test(value) || /[. ](?:\\|$)/.test(value) || value.slice(2).includes(':'))) throw Error('A canonical local drive path is required.');
      if (arg === '--profile-root') { if (profileRoot) throw Error('Duplicate profile option.'); profileRoot = paths.resolve(value); }
      else { bridgeArgs.push(arg, paths.resolve(value)); hasWorkspace ||= arg !== '--svn'; }
    } else if (WRITE_FLAGS.has(arg)) {
      if (bridgeArgs.includes(arg)) throw Error('Duplicate capability option.');
      bridgeArgs.push(arg);
    } else throw Error('Unknown startup option.');
  }
  if ((demo && bridgeArgs.length) || (!hasWorkspace && bridgeArgs.length)) throw Error('Workspace capabilities require an explicitly selected workspace.');
  return {bridgeArgs, profileRoot, demo, hasWorkspace};
}
function parseReadiness(bytes, pid) {
  if (bytes.length > 4096 || bytes.length < 2 || bytes.at(-1) !== 10) throw Error('Invalid readiness size.');
  // Round-trip UTF-8 validation prevents replacement characters hiding malformed input.
  const text = bytes.toString('utf8');
  if (!Buffer.from(text, 'utf8').equals(bytes)) throw Error('Invalid readiness encoding.');
  let message; try { message = JSON.parse(text); } catch { throw Error('Invalid readiness JSON.'); }
  if (!message || Array.isArray(message) || Object.keys(message).sort().join(',') !== 'origin,pid,protocol' || message.protocol !== PROTOCOL || !Number.isSafeInteger(pid) || pid < 1 || message.pid !== pid) throw Error('Invalid readiness identity.');
  if (typeof message.origin !== 'string' || !/^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/.test(message.origin)) throw Error('Invalid readiness origin.');
  const url = new URL(message.origin);
  if (url.origin !== message.origin || Number(url.port) > 65535 || Number(url.port) < 1) throw Error('Invalid readiness port.');
  return message.origin;
}
function allowedResource(raw, origin, type) {
  try { const url = new URL(raw); return url.origin === origin && url.protocol === 'http:' && !url.username && !url.password && type !== 'subFrame' && (type !== 'mainFrame' || raw === origin + '/'); }
  catch { return false; }
}
function childEnvironment(environment) {
  return Object.fromEntries(Object.entries(environment).filter(([name]) => !/^(PYTHON|NODE_|ELECTRON_|CHROME_|WEBVIEW2_)/i.test(name)));
}
module.exports = {PROTOCOL, parseOptions, parseReadiness, allowedResource, childEnvironment};
