'use strict';

/*
 * Freeze the server: dist/scribe-server/ at the repository root, which
 * electron-builder then ships as resources/server. See
 * packaging/scribe-server.spec for what is in it and why.
 *
 * Always from a virtualenv of its own, build/server-venv, holding only
 * packaging/requirements-server.txt - see the note at the top of that file
 * for why a developer's global Python cannot be used. The venv is made on
 * the first build and topped up on later ones, which is quick.
 */

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');
const { ROOT, python } = require('./run-python');

const VENV = path.join(ROOT, 'build', 'server-venv');
const venvPython = process.platform === 'win32'
  ? path.join(VENV, 'Scripts', 'python.exe')
  : path.join(VENV, 'bin', 'python');

function run(file, args) {
  console.log(`> ${path.basename(file)} ${args.join(' ')}`);
  const result = spawnSync(file, args, { cwd: ROOT, stdio: 'inherit' });
  if (result.error || result.status !== 0) {
    console.error(result.error ? result.error.message : `exited ${result.status}`);
    process.exit(result.status || 1);
  }
}

if (!fs.existsSync(venvPython)) {
  run(python(), ['-m', 'venv', VENV]);
}
run(venvPython, ['-m', 'pip', 'install', '--quiet', '--upgrade', 'pip']);
run(venvPython, ['-m', 'pip', 'install', '--quiet', '-r', path.join('packaging', 'requirements-server.txt')]);
run(venvPython, [
  '-m', 'PyInstaller',
  path.join('packaging', 'scribe-server.spec'),
  '--noconfirm',
  '--distpath', 'dist',
  '--workpath', path.join('build', 'pyinstaller'),
]);

const exe = path.join(ROOT, 'dist', 'scribe-server', process.platform === 'win32' ? 'scribe-server.exe' : 'scribe-server');
if (!fs.existsSync(exe)) {
  console.error(`PyInstaller finished but ${exe} is not there.`);
  process.exit(1);
}
console.log(`Built ${exe}`);
