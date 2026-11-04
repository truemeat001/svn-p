'use strict';
const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const os=require('node:os');const path=require('node:path');const {createProfile,prepareProfilePaths}=require('../profile.cjs');
const realCreateProfile = createProfile;
function isolatedCreateProfile(requested, temp) { const base = requested ? path.dirname(requested) : temp; const filesystem = {...fs, existsSync: p => p.startsWith(base + path.sep) ? fs.existsSync(p) : false}; return realCreateProfile(requested, temp, filesystem); }
function fixture(t){const root=fs.mkdtempSync(path.join(os.tmpdir(),'svn-electron-test-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));return root;}
test('fresh explicit profile, never reuse existing directory',t=>{const root=fixture(t);const dest=path.join(root,'new');assert.equal(isolatedCreateProfile(dest,root),dest);assert.throws(()=>isolatedCreateProfile(dest,root));});
test('default is a unique profile outside workspaces',t=>{const root=fixture(t);const a=isolatedCreateProfile(null,root),b=isolatedCreateProfile(null,root);assert.notEqual(a,b);assert(fs.statSync(a).isDirectory());});
test('repository parent rejected before creating profile',t=>{const root=fixture(t);fs.mkdirSync(path.join(root,'.svn'));const dest=path.join(root,'new');assert.throws(()=>isolatedCreateProfile(dest,root));assert(!fs.existsSync(dest));});
test('symlink parent rejected and missing parent not created',t=>{const root=fixture(t);fs.mkdirSync(path.join(root,'real'));fs.symlinkSync(path.join(root,'real'),path.join(root,'link'),'dir');assert.throws(()=>isolatedCreateProfile(path.join(root,'link','new'),root));assert.throws(()=>isolatedCreateProfile(path.join(root,'missing','new'),root));});

test('all Electron path override directories exist before setPath',t=>{const root=fixture(t);const profile=isolatedCreateProfile(path.join(root,'profile'),root);const paths=prepareProfilePaths(profile);assert.deepEqual(Object.keys(paths),['userData','sessionData','crashDumps']);for(const directory of Object.values(paths)) assert(fs.statSync(directory).isDirectory());assert.throws(()=>prepareProfilePaths(profile));});
