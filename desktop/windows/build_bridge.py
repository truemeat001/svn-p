#!/usr/bin/env python3
"""Build only the fixed reviewed Windows bridge; never install dependencies."""
from __future__ import annotations
import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

PYINSTALLER_VERSION = '6.22.3'
BACKEND_MODULES = ('backend', 'status_stream', 'discovery', 'workspaces', 'history',
                   'shelves', 'locks', 'fileops', 'file_identity')
BRIDGE_MODULES = ('desktop_bridge', 'server', 'workspace_bridge', 'file_reads',
                  'property_reads', 'changelist_ops', 'shelf_ops', 'commit_ops',
                  'commit_diagnostics', 'lock_ops', 'add_ops', 'file_ops', 'checkout_ops', 'checkout_paths')
ASSETS = ('index.html', 'app.js', 'model.js', 'demo-data.js', 'styles.css', 'favicon.svg')


def regular_source(root, relative):
    root = root.resolve(strict=True)
    path = root / relative
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError('Build inputs must not be symbolic links.')
        current = current.parent
    info = path.stat()
    if not path.is_file() or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('Build input must be a plain regular file.')
    if info.st_size > 4 * 1024 * 1024:
        raise ValueError('Unexpectedly large source input.')
    return path


def inputs(root):
    web = Path('prototypes/svn-desk-web')
    paths = [*(web / (name + '.py') for name in BRIDGE_MODULES),
             *(web / 'web' / name for name in ASSETS),
             *(Path('svn_client') / (name + '.py') for name in BACKEND_MODULES)]
    return {path.as_posix(): regular_source(root, path) for path in paths}


def stdlib_imports(paths):
    # Backend files are shipped as source resources, so freezer analysis will not
    # discover their imports by itself. Include their standard-library imports.
    names = set()
    for path in paths:
        if path.suffix != '.py':
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
            if isinstance(node, ast.Import):
                names.update(item.name for item in node.names if item.name.split('.')[0] in sys.stdlib_module_names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module and node.module.split('.')[0] in sys.stdlib_module_names:
                names.add(node.module)
    return sorted(names)


def manifest(files):
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sorted(files.items())}


def runtime_notices():
    candidates = (Path(sys.base_prefix) / 'LICENSE.txt', Path(sys.base_prefix) / 'LICENSE')
    python_notice = next((path for path in candidates if path.is_file()), None)
    if python_notice is None:
        raise RuntimeError('Python runtime license is missing from the installed build interpreter.')
    distribution = importlib.metadata.distribution('pyinstaller')
    candidates = [distribution.locate_file(path) for path in (distribution.files or ())
                  if Path(path).name.lower() == 'copying.txt']
    pyinstaller_notice = next((Path(path) for path in candidates if Path(path).is_file()), None)
    if pyinstaller_notice is None:
        raise RuntimeError('PyInstaller distribution notice is missing from the installed build dependency.')
    return {'Python-LICENSE.txt': python_notice, 'PyInstaller-COPYING.txt': pyinstaller_notice}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--output', type=Path, help='New isolated build directory, must not already exist')
    parser.add_argument('--check-inputs', action='store_true')
    args = parser.parse_args(argv)
    root = args.source.resolve(strict=True)
    source = inputs(root)
    source_manifest = manifest(source)
    if args.check_inputs:
        print(json.dumps({'format': 1, 'sourceFiles': source_manifest, 'hiddenStdlibImports': stdlib_imports(source.values())}, indent=2))
        return 0
    if os.name != 'nt':
        parser.error('Windows is required. Input validation is available with --check-inputs.')
    if not args.output:
        parser.error('--output is required for a build.')
    if importlib.metadata.version('pyinstaller') != PYINSTALLER_VERSION:
        parser.error('Use the already-installed, reviewed PyInstaller version ' + PYINSTALLER_VERSION + '.')
    notices = runtime_notices()
    output = Path(os.path.abspath(args.output))
    if output.exists():
        parser.error('Output must be a new isolated directory; existing files will not be overwritten.')
    output.parent.resolve(strict=True)
    output.mkdir()
    staged = output / 'source'
    # Stage only the fixed application inputs. Never copy settings, workspaces,
    # SVN metadata, caches, user documents or the surrounding source checkout.
    for name, path in source.items():
        target = staged / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    staged_files = inputs(staged)
    if manifest(staged_files) != source_manifest:
        raise RuntimeError('Source changed during staging. Rebuild from a stable checkpoint.')
    web = staged / 'prototypes/svn-desk-web'
    command = [sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm', '--onedir', '--console', '--noupx',
               '--name', 'SVNDeskBridge', '--distpath', str(output / 'dist'), '--workpath', str(output / 'work'),
               '--specpath', str(output), '--paths', str(web),
               '--add-data', str(web / 'web') + ';web',
               '--add-data', str(staged / 'svn_client') + ';backend-source/svn_client']
    for name in stdlib_imports(staged_files.values()):
        command.extend(['--hidden-import', name])
    command.append(str(web / 'desktop_bridge.py'))
    with (output / 'build.log').open('wb') as log:
        subprocess.run(command, cwd=staged, stdout=log, stderr=subprocess.STDOUT, check=True)
    built = output / 'dist/SVNDeskBridge/SVNDeskBridge.exe'
    check = subprocess.run([str(built), '--self-check'], cwd=built.parent, capture_output=True, text=True, timeout=30, check=True)
    if json.loads(check.stdout) != {'protocol': 'svn-desk-desktop/1', 'status': 'ok', 'backendVerified': True}:
        raise RuntimeError('Frozen bridge did not validate the paired source and assets.')
    license_directory = built.parent / 'Licenses'
    license_directory.mkdir()
    for name, path in notices.items():
        shutil.copyfile(path, license_directory / name)
    report = {'format': 1, 'python': sys.version.split()[0], 'pyinstaller': PYINSTALLER_VERSION,
              'sourceFiles': source_manifest, 'frozenSelfCheck': 'passed',
              'artifactFiles': manifest({p.relative_to(built.parent).as_posix(): p for p in built.parent.rglob('*') if p.is_file()})}
    (output / 'bridge-build-manifest.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print('Built bridge directory: ' + str(built.parent))
    print('Packaged backend self-check passed. Native GUI and real-SVN acceptance are separate gates.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
