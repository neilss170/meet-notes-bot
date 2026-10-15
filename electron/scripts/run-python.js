'use strict';

/*
 * Run a Python script or module from the repository root, with whichever
 * interpreter the developer uses: SCRIBE_PYTHON if set, otherwise `python` on
 * Windows and `python3` elsewhere. `npm run icons` and `npm run build:server`
 * go through here so they work the same from any shell.
 *
 *   node scripts/run-python.js electron/scripts/make_icons.py
 *   node scripts/run-python.js -m PyInstaller packaging/scribe-server.spec
 */

const path = require('path');
const { spawnSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..', '..');

function python() {
  return process.env.SCRIBE_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
}

function runPython(args) {
  const result = spawnSync(python(), args, { cwd: ROOT, stdio: 'inherit' });
  if (result.error) {
    console.error(`Could not run ${python()}: ${result.error.message}. Set SCRIBE_PYTHON to your interpreter.`);
    return 1;
  }
  return result.status === null ? 1 : result.status;
}

if (require.main === module) {
  process.exit(runPython(process.argv.slice(2)));
}

module.exports = { ROOT, python, runPython };
