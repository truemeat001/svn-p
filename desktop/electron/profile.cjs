'use strict';
const fs = require('node:fs');
const path = require('node:path');
function plainAncestors(directory, filesystem = fs) {
  let current = directory;
  while (true) {
    const info = filesystem.lstatSync(current);
    if (!info.isDirectory() || info.isSymbolicLink() || filesystem.realpathSync(current).toLowerCase() !== current.toLowerCase()) throw Error('Profile parent must be a plain local directory.');
    if (filesystem.existsSync(path.join(current, '.git')) || filesystem.existsSync(path.join(current, '.svn'))) throw Error('Profile must be outside a working copy.');
    const parent = path.dirname(current); if (parent === current) return; current = parent;
  }
}
function createProfile(requested, temp, filesystem = fs) {
  if (requested) {
    plainAncestors(path.dirname(requested), filesystem);
    filesystem.mkdirSync(requested, {recursive: false}); // Existing destinations are never reused.
    return requested;
  }
  const parent = filesystem.realpathSync(temp); plainAncestors(parent, filesystem);
  return filesystem.mkdtempSync(path.join(parent, 'SVNDesk-Electron-'));
}
function prepareProfilePaths(root, filesystem = fs) {
  const paths = {userData: root, sessionData: path.join(root, 'session'), crashDumps: path.join(root, 'crash-dumps')};
  filesystem.mkdirSync(paths.sessionData, {recursive: false});
  filesystem.mkdirSync(paths.crashDumps, {recursive: false});
  return paths;
}
module.exports = {createProfile, prepareProfilePaths};
