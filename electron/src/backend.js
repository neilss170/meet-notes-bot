'use strict';

/*
 * The Scribe server as a child process: start it, know when it is up, and
 * make sure it is gone when the app is.
 *
 * "Gone" has three layers, because an orphaned recorder holding somebody's
 * microphone with no window and no icon is the worst state this app could
 * leave behind:
 *
 *   1. Asked: POST /api/control/shutdown, which stops a live recording,
 *      writes it up and exits - the same shutdown Ctrl+C gives a terminal.
 *   2. Waited for: the shell watches the process, not a clock, up to a
 *      generous limit, because the write-up is the thing being protected.
 *   3. Forced: past the limit, the whole process tree is killed.
 *
 * And from the other side, the server is given this process's pid and shuts
 * itself down if the shell dies without asking.
 */

const crypto = require('crypto');
const net = require('net');
const { spawn } = require('child_process');
const { EventEmitter } = require('events');
const { ServerApi, probeHealth } = require('./api');
const platform = require('./platform');

/** How long the server gets to answer /health after launch. A cold first
    start unpacks and imports a lot; later starts take a couple of seconds. */
const START_TIMEOUT_MS = 90 * 1000;

/** How long quitting waits for the server to finish on its own: the server's
    120 s grace for notes, plus time to stop the recording and write the
    transcript. */
const SHUTDOWN_TIMEOUT_MS = 180 * 1000;

/** Lines of the server's own output kept for an error window. */
const TAIL_LINES = 40;

/** A free port on the loopback interface, chosen by the OS. */
function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

class Backend extends EventEmitter {
  /**
   * @param {object} options
   * @param {{file: string, args: string[], cwd?: string}} options.command
   *   How to run the server: the frozen executable, or Python from source.
   * @param {string} options.dataDir  Passed as --data-dir.
   * @param {string} [options.micShortcut]  Shown by the page, if registered.
   * @param {(line: string) => void} [options.log]
   */
  constructor({ command, dataDir, micShortcut = '', log = () => {} }) {
    super();
    this.command = command;
    this.dataDir = dataDir;
    this.micShortcut = micShortcut;
    this.log = log;
    this.child = null;
    this.api = null;
    this.port = 0;
    this.tail = [];
    this.stopping = false;
    this.exited = null; // Promise that resolves with the exit code
  }

  async start() {
    this.port = await freePort();
    const token = crypto.randomBytes(32).toString('hex');
    const args = [
      ...this.command.args,
      '--port', String(this.port),
      '--data-dir', this.dataDir,
      '--parent-pid', String(process.pid),
      '--shell-notifications',
    ];
    if (this.micShortcut) args.push('--mic-shortcut', this.micShortcut);

    const env = { ...process.env, SCRIBE_CONTROL_TOKEN: token, PYTHONUNBUFFERED: '1' };
    // VS Code's terminals set this. It means nothing to the server, and is
    // not passed on to anything the server starts in turn.
    delete env.ELECTRON_RUN_AS_NODE;

    this.log(`Starting the server: ${this.command.file} on port ${this.port}`);
    this.child = spawn(this.command.file, args, {
      cwd: this.command.cwd,
      env,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    // Always drained: a full pipe would block the server mid-write.
    const keep = (chunk) => {
      for (const line of chunk.toString('utf8').split(/\r?\n/)) {
        if (!line.trim()) continue;
        this.tail.push(line);
        if (this.tail.length > TAIL_LINES) this.tail.shift();
      }
    };
    this.child.stdout.on('data', keep);
    this.child.stderr.on('data', keep);

    this.exited = new Promise((resolve) => {
      this.child.once('error', (error) => {
        this.tail.push(String(error && error.message));
        resolve(-1);
      });
      this.child.once('exit', (code, signal) => resolve(code === null ? signal || -1 : code));
    });
    this.exited.then((code) => {
      this.log(`The server exited (${code}).`);
      if (!this.stopping) this.emit('crashed', code);
    });

    this.api = new ServerApi({ port: this.port, token });
    await this.waitUntilHealthy();
    return this.api;
  }

  /** Resolves when /health answers; rejects if the process dies first. */
  async waitUntilHealthy(timeoutMs = START_TIMEOUT_MS) {
    const deadline = Date.now() + timeoutMs;
    let dead = null;
    this.exited.then((code) => {
      dead = code;
    });
    while (Date.now() < deadline) {
      if (dead !== null) {
        throw new Error(`The server stopped while starting (exit code ${dead}).`);
      }
      // eslint-disable-next-line no-await-in-loop
      if (await probeHealth(this.port)) return;
      // eslint-disable-next-line no-await-in-loop
      await new Promise((r) => setTimeout(r, 300));
    }
    throw new Error(`The server did not answer within ${Math.round(timeoutMs / 1000)} seconds.`);
  }

  get running() {
    return Boolean(this.child) && this.child.exitCode === null && !this.child.killed;
  }

  /**
   * Ask, wait, and only then force. Resolves once the process is gone.
   * @returns {Promise<'graceful'|'forced'|'not-running'>}
   */
  async shutdown(timeoutMs = SHUTDOWN_TIMEOUT_MS) {
    this.stopping = true;
    if (!this.running) return 'not-running';
    try {
      await this.api.post('/api/control/shutdown', {}, { timeoutMs: 10000 });
    } catch (error) {
      this.log(`Shutdown request failed (${error.message}); waiting anyway.`);
    }
    const finished = await Promise.race([
      this.exited.then(() => true),
      new Promise((r) => setTimeout(() => r(false), timeoutMs)),
    ]);
    if (finished) return 'graceful';
    this.log('The server did not finish in time; ending it.');
    platform.killTree(this.child);
    await Promise.race([this.exited, new Promise((r) => setTimeout(r, 5000))]);
    return 'forced';
  }

  /** For the error window: the last things the server said. */
  lastOutput() {
    return this.tail.join('\n');
  }
}

module.exports = { Backend, freePort, START_TIMEOUT_MS, SHUTDOWN_TIMEOUT_MS };
