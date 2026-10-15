'use strict';

/*
 * `npm run dev`: the shell, running the server from source.
 *
 * Electron starts unpackaged, so main.js runs `python -m meetbot.server_main`
 * from the repository root instead of the frozen executable - edit Python,
 * quit from the tray, run this again. Uses the same %APPDATA%\Scribe data as
 * the installed app unless SCRIBE_DATA_DIR says otherwise.
 */

const { spawn } = require('child_process');
const path = require('path');

const electron = require('electron'); // the path to the binary, from Node

const env = { ...process.env };
// Set in VS Code's terminals. With it, electron.exe behaves as plain Node and
// "starts" by printing a version number.
delete env.ELECTRON_RUN_AS_NODE;

const child = spawn(electron, [path.resolve(__dirname, '..')], { stdio: 'inherit', env });
child.on('exit', (code) => process.exit(code === null ? 1 : code));
