#!/usr/bin/env python3
"""Assemble a portable Windows Electron preview from pinned official dependencies.
No system installation, signing, publishing or user workspace access.
"""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, stat, subprocess, tempfile, zipfile
from pathlib import Path, PurePosixPath
ELECTRON = '45.2.2'
# Retrieved independently from the official v45.2.2 SHASUMS256.txt.
ELECTRON_ZIP_SHA256 = 'a234388419ccbcb18dd2feec856e4ae1832499e200837f23f8e653bd51ff3be5'
BASELINE_SHA256 = 'f0986cd13dfec94605739e5f844e1807355a7dfb25c1b679559d8190f96dd266'
HOST_FILES = ('main.cjs','policy.cjs','bridge-process.cjs','profile.cjs')
WEB_OVERRIDES = {
 'app.js': ('7f403795905428ed1ba0a1777ca59ec521b548336e69cfc1eea4e9c35dc25b69','0eb7266a75a1f14fad7edd5683dfde7f29f864632c8ba0e195356c2f028436b2'),
 'model.js': ('6331f7b3cc8981a7ca813669f0242f81fc8870b62e2bf6f3b7b96d06b997aa8b','d7b70e41ed115071a74b027f8772d8da78c003a0a1276f0320e7038247050dd8')
}

def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''): result.update(block)
    return result.hexdigest()

def plain_file(path):
    if path.is_symlink() or not path.is_file(): raise ValueError('Input must be a regular file.')
    return path

def unzip_checked(source, target, prefix=''):
    with zipfile.ZipFile(source) as archive:
        selected = []
        for item in archive.infolist():
            if not (item.filename.startswith(prefix) or item.orig_filename.startswith(prefix)):
                continue
            # ZipInfo normalizes backslashes on Windows and truncates NULs.
            # Validate the original decoded archive name before trusting either
            # the normalized path, directory classification or selected prefix.
            original = item.orig_filename
            if original != item.filename or '\\' in original or '\x00' in original:
                raise ValueError('Unsafe archive member.')
            if not item.is_dir():
                selected.append(item)
        if sum(item.file_size for item in selected) > 2*1024**3: raise ValueError('Unexpectedly large archive.')
        seen = set(); planned = []
        for item in selected:
            name = item.filename[len(prefix):]
            posix = PurePosixPath(name)
            if not name or posix.is_absolute() or '..' in posix.parts or '\\' in name or ':' in name or str(posix) != name or stat.S_ISLNK(item.external_attr >> 16): raise ValueError('Unsafe archive member.')
            if name.casefold() in seen: raise ValueError('Duplicate archive destination.')
            seen.add(name.casefold()); planned.append((item, target.joinpath(*posix.parts)))
        # Reject every selected unsafe name/collision before writing any member.
        for item, destination in planned:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as src, destination.open('xb') as dst: shutil.copyfileobj(src,dst)
        return seen

def inventory(root):
    return {p.relative_to(root).as_posix():digest(plain_file(p)) for p in sorted(root.rglob('*')) if p.is_file()}

def run_node(root, *args):
    result = subprocess.run(['node',str(root/'package-runtime.cjs'),*map(str,args)],cwd=root,capture_output=True,text=True,check=True)
    return json.loads(result.stdout)

def main():
    parser=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    parser.add_argument('--bridge-package',type=Path,required=True,help='Verified SVNDesk-Windows-x64-5298602-Trial.zip')
    parser.add_argument('--output',type=Path,required=True,help='New build directory; must not exist')
    parser.add_argument('--cache',type=Path,required=True,help='Project-local official Electron download cache')
    args=parser.parse_args();root=Path(__file__).resolve().parent
    source=plain_file(args.bridge_package.resolve(strict=True))
    if digest(source)!=BASELINE_SHA256: raise ValueError('Expected the verified 5298602 baseline package. Rebuild integration requires updating the explicit input contract.')
    output=args.output.absolute(); output.parent.resolve(strict=True)
    if output.exists(): raise ValueError('Output must be new; never overwrite another package.')
    # Dependency graph is exact in package-lock.json; npm ci --ignore-scripts is explicit.
    for package,version in [('electron',ELECTRON),('@electron/asar','4.3.1'),('@electron/fuses','2.1.3')]:
        if json.loads((root/'node_modules'/package/'package.json').read_text())['version']!=version: raise ValueError('Dependency version mismatch.')
    for name in HOST_FILES: plain_file(root/name)
    subprocess.run(['node','--test',*map(str,sorted((root/'tests').glob('*.test.cjs')))],cwd=root,check=True)
    subprocess.run([os.sys.executable,'-m','unittest','discover','-s',str(root/'tests'),'-p','test_*.py'],cwd=root,check=True)
    official=run_node(root,'download',args.cache.absolute())
    if digest(plain_file(Path(official['artifact']))) != ELECTRON_ZIP_SHA256: raise ValueError('Official Electron archive hash differs from the pinned release.')
    output.mkdir(); app=output/'SVNDesk-Electron-Windows-x64';app.mkdir()
    unzip_checked(Path(official['artifact']),app)
    (app/'electron.exe').rename(app/'SVNDesk-Electron.exe')
    if (app/'electron_wer.dll').exists(): (app/'electron_wer.dll').rename(app/'SVNDesk-Electron_wer.dll')
    # The default sample archive is official runtime content and never part of our app.
    if (app/'resources/default_app.asar').exists(): (app/'resources/default_app.asar').unlink()
    bridge=app/'resources/Bridge';bridge.mkdir()
    prefix='SVNDesk-5298602-Trial/'
    with zipfile.ZipFile(source) as original:
        expected=json.loads(original.read(prefix+'SHA256-MANIFEST.json'))
        expected={name[len('Bridge/'):]:value for name,value in expected.items() if name.startswith('Bridge/')}
    unzip_checked(source,bridge,prefix+'Bridge/')
    if inventory(bridge)!=expected: raise ValueError('Bundled bridge differs from verified original manifest.')
    # These are external static resources, not modifications to the frozen executable.
    # Apply only the two reviewed UI changes after verifying the original bridge.
    overlays = {}
    for name, (before, after) in WEB_OVERRIDES.items():
        current = bridge/'_internal/web'/name
        replacement = plain_file(root.parents[1]/'prototypes/svn-desk-web/web'/name)
        if digest(current) != before or digest(replacement) != after: raise ValueError('Reviewed web asset identity mismatch.')
        shutil.copyfile(replacement, current)
        if digest(current) != after: raise ValueError('Web asset copy mismatch.')
        overlays[name] = {'before': before, 'after': after}
    stage=output/'app-source';stage.mkdir()
    for name in HOST_FILES: shutil.copyfile(root/name,stage/name)
    (stage/'package.json').write_text(json.dumps({'name':'svn-desk-electron-preview','version':'0.1.0','main':'main.cjs','private':True},indent=2)+'\n')
    sealed=run_node(root,'seal',stage,app)
    (app/'Start-Demo.cmd').write_text('@echo off\r\nstart "" "%~dp0SVNDesk-Electron.exe" --demo\r\n',encoding='ascii',newline='')
    shutil.copyfile(root/'TRIAL-README.txt',app/'README-试用说明.txt')
    # Preserve all official Electron and bridge dependency notices in place.
    for required in ['LICENSE','LICENSES.chromium.html','resources/Bridge/Licenses/Python-LICENSE.txt']:
        if not (app/required).is_file(): raise ValueError('Required redistribution notice missing: '+required)
    report={'format':1,'electron':ELECTRON,'bridgeBase':'5298602','reviewedStaticAssetOverrides':overlays,'bridgePackageSHA256':digest(source),'officialElectronZipSHA256':digest(Path(official['artifact'])),'packagedHostSHA256':inventory(stage),'fuseVerification':sealed,'tests':'passed','guiValidation':'not-run','performanceComparison':'not-measured','signed':False,'asarIntegrityEnforcement':False}
    (output/'BUILD-EVIDENCE.json').write_text(json.dumps(report,indent=2)+'\n')
    manifest=inventory(app);(app/'SHA256-MANIFEST.json').write_text(json.dumps(manifest,indent=2)+'\n')
    archive=output/'SVNDesk-Electron-Windows-x64-PoC.zip'
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for file in sorted(app.rglob('*')):
            if file.is_file(): z.write(file,app.name+'/'+file.relative_to(app).as_posix())
    with zipfile.ZipFile(archive) as z:
        for name,value in manifest.items():
            if hashlib.sha256(z.read(app.name+'/'+name)).hexdigest()!=value: raise ValueError('Archive verification failed.')
    print(json.dumps({'package':str(archive),'sha256':digest(archive),'bytes':archive.stat().st_size,'files':len(manifest),'guiValidation':'not-run'}))
if __name__=='__main__': main()
