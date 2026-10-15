'use strict';

/*
 * `npm run smoke [-- <app.exe>] [--record <seconds>]`: a scripted run of the
 * app against a throwaway data directory, reporting into electron/smoke-out/.
 *
 * With no executable it runs the shell from source; given the installed
 * Scribe.exe, it checks the real build. Afterwards it confirms no
 * scribe-server process was left behind. See src/smoke.js for the steps.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn, execFileSync } = require('child_process');

const args = process.argv.slice(2);
const recordAt = args.indexOf('--record');
const record = recordAt >= 0 ? args.splice(recordAt, 2)[1] : '0';
const exe = args[0];

const out = path.resolve(__dirname, '..', 'smoke-out');
fs.rmSync(out, { recursive: true, force: true });
const data = process.env.SCRIBE_DATA_DIR || fs.mkdtempSync(path.join(os.tmpdir(), 'scribe-smoke-'));

const env = {
  ...process.env,
  SCRIBE_SMOKE: out,
  SCRIBE_SMOKE_RECORD: record,
  SCRIBE_DATA_DIR: data,
};
delete env.ELECTRON_RUN_AS_NODE;

const command = exe || require('electron');
const commandArgs = exe ? [] : [path.resolve(__dirname, '..')];
console.log(`smoke: ${command} (data in ${data})`);
const started = Date.now();
const child = spawn(command, commandArgs, { stdio: 'inherit', env });
child.on('exit', (code) => {
  console.log(`smoke: app exited ${code} after ${Math.round((Date.now() - started) / 1000)}s`);
  // Only a server belonging to the copy under test counts: an installed
  // Scribe running alongside is somebody's real app, not a leak.
  let leftover = '';
  if (process.platform === 'win32') {
    try {
      leftover = execFileSync('powershell', ['-NoProfile', '-Command',
        "Get-CimInstance Win32_Process -Filter \"Name='scribe-server.exe'\" | ForEach-Object { $_.ExecutablePath }"],
      { encoding: 'utf8' });
    } catch (_) {
      leftover = '';
    }
  }
  const ours = exe ? path.dirname(path.resolve(exe)).toLowerCase() : path.resolve(__dirname, '..', '..').toLowerCase();
  const orphaned = leftover.split(/\r?\n/).some((line) => line.trim() && line.trim().toLowerCase().startsWith(ours));
  console.log(`smoke: scribe-server left running: ${orphaned ? 'YES' : 'no'}`);
  console.log(`smoke: report in ${out}`);
  process.exit(orphaned ? 2 : code || 0);
});
