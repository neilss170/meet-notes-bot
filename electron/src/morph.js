'use strict';

/*
 * Plays the window <-> bubble animation on a transparent, click-through
 * overlay. See morph/overlay.js for what is drawn and why it is drawn that
 * way.
 *
 * Three steps, so the slow parts happen before anyone is waiting:
 *
 *   preload(image)  encode and decode a picture of the window - ahead of time,
 *                   whenever the window loses focus or a caption button is
 *                   pressed
 *   stage(...)      put that picture on screen at the start of the flight -
 *                   exactly over the window when minimising, so nothing
 *                   changes for the eye
 *   fly(...)        the flight itself, which then starts on the next frame
 *
 * The overlay is made once and kept: a new window takes longer to appear
 * than the whole animation lasts.
 *
 * Every step has a deadline. If the picture will not decode, the overlay will
 * not paint, or the flight overruns, the overlay is hidden and the caller
 * jumps to the end state. An animation can fail; an overlay stuck over
 * somebody's screen cannot be allowed to.
 */

const path = require('path');
const { BrowserWindow, ipcMain } = require('electron');

const STEP_TIMEOUT_MS = 300;
const FLIGHT_TIMEOUT_MS = 700;   // the flight is 420 ms
const PAD = 140;                 // room around the path for the curve

function union(a, b) {
  const x = Math.min(a.x, b.x) - PAD;
  const y = Math.min(a.y, b.y) - PAD;
  return {
    x: Math.round(x),
    y: Math.round(y),
    width: Math.round(Math.max(a.x + a.width, b.x + b.width) + PAD - x),
    height: Math.round(Math.max(a.y + a.height, b.y + b.height) + PAD - y),
  };
}

class Morph {
  /**
   * @param {object} options
   * @param {() => boolean} options.hideInShare
   * @param {(line: string) => void} options.log
   */
  constructor({ hideInShare, log }) {
    this.hideInShare = hideInShare;
    this.log = log;
    this.win = null;
    this.ready = null;
    this.src = '';          // the picture the overlay has decoded
    this.staged = null;     // { reverse } while a flight is waiting to go
  }

  warm() {
    if (this.win) return this.ready;
    this.win = new BrowserWindow({
      show: false,
      frame: false,
      transparent: true,
      backgroundColor: '#00000000',
      hasShadow: false,
      resizable: false,
      skipTaskbar: true,
      focusable: false,
      enableLargerThanScreen: true,
      alwaysOnTop: true,
      webPreferences: {
        preload: path.join(__dirname, 'morph', 'preload.js'),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        backgroundThrottling: false,
      },
    });
    this.win.setAlwaysOnTop(true, 'screen-saver');
    this.win.setIgnoreMouseEvents(true);
    this.win.webContents.on('will-navigate', (event) => event.preventDefault());
    this.win.webContents.on('console-message', (event) => {
      const message = event.message || '';
      if (message.startsWith('overlay-stats')) this.log(message);
    });
    this.ready = this.win.loadFile(path.join(__dirname, 'morph', 'overlay.html'));
    return this.ready;
  }

  /** Resolve true when the overlay says `name`, false on failure or timeout. */
  next(name, ms) {
    return new Promise((resolve) => {
      const channel = `overlay:${name}`;
      const failed = 'overlay:failed';
      const done = (value) => {
        clearTimeout(timer);
        ipcMain.removeListener(channel, ok);
        ipcMain.removeListener(failed, bad);
        resolve(value);
      };
      const ok = (event) => this.win && event.sender === this.win.webContents && done(true);
      const bad = (event) => this.win && event.sender === this.win.webContents && done(false);
      const timer = setTimeout(() => done(false), ms);
      ipcMain.on(channel, ok);
      ipcMain.on(failed, bad);
    });
  }

  send(channel, payload) {
    if (this.win && !this.win.isDestroyed()) this.win.webContents.send(`overlay:${channel}`, payload);
  }

  /**
   * Encode a picture of the window and have the overlay decode it now, so
   * staging it later is instant. Sent at the size it is drawn, as JPEG: a
   * full-resolution PNG takes longer to encode than the flight lasts.
   */
  async preload(image, rect) {
    if (!image || image.isEmpty() || !rect) return;
    await this.warm();
    const sized = image.resize({ width: Math.max(1, Math.round(rect.width)), quality: 'good' });
    this.src = `data:image/jpeg;base64,${sized.toJPEG(90).toString('base64')}`;
    this.send('preload', { src: this.src });
  }

  get hasPicture() {
    return Boolean(this.src);
  }

  /**
   * Show the picture at the start of a flight. Resolves true once it is
   * really on screen - the moment it is safe to hide what it stands in for.
   *
   * @param {Electron.Rectangle} windowRect  The window's page, on screen.
   * @param {Electron.Rectangle} bubbleRect  The bubble's circle, on screen.
   * @param {boolean} reverse  Starting from the bubble rather than the window.
   */
  async stage(windowRect, bubbleRect, reverse = false) {
    if (!this.src || !windowRect || !bubbleRect) return false;
    await this.warm();
    this.win.setContentProtection(Boolean(this.hideInShare()));
    const area = union(windowRect, bubbleRect);
    const rel = (r) => ({ x: r.x - area.x, y: r.y - area.y, width: r.width, height: r.height });
    // Bounds every time, not position: the overlay may span displays.
    this.win.setBounds(area);
    const staged = this.next('staged', STEP_TIMEOUT_MS);
    this.send('stage', { src: this.src, from: rel(windowRect), to: rel(bubbleRect), reverse });
    if (!(await staged)) return this.fail('the picture did not load in time');
    const ready = this.next('ready', STEP_TIMEOUT_MS);
    if (!this.win.isVisible()) this.win.showInactive();
    this.win.setAlwaysOnTop(true, 'screen-saver');
    this.send('shown');
    if (!(await ready)) return this.fail('the overlay did not paint in time');
    this.staged = { reverse };
    return true;
  }

  get isStaged() {
    return Boolean(this.staged);
  }

  /**
   * Fly the staged picture. Resolves when it has landed, or at the deadline.
   * @param {() => void} [onArrive]  Nearly in the bubble: show it now.
   */
  async fly(onArrive) {
    if (!this.staged) return false;
    const { reverse } = this.staged;
    this.staged = null;
    const arrived = reverse ? null : this.next('arrive', FLIGHT_TIMEOUT_MS);
    const done = this.next('done', FLIGHT_TIMEOUT_MS);
    this.send('go');
    let called = false;
    const arrive = () => {
      if (called || !onArrive) return;
      called = true;
      onArrive();
    };
    if (arrived) arrived.then(arrive);
    const landed = await done;
    arrive();
    if (!landed) this.log('The bubble animation overran its deadline; finishing it.');
    return landed;
  }

  /** Restoring: the real window is painted underneath, so fade the picture. */
  async release() {
    const released = this.next('released', STEP_TIMEOUT_MS);
    this.send('release');
    await released;
    this.finish();
  }

  /** Hide the overlay and forget the flight. Always safe to call. */
  finish() {
    this.staged = null;
    this.send('clear');
    if (this.win && !this.win.isDestroyed() && this.win.isVisible()) this.win.hide();
  }

  fail(reason) {
    this.log(`Bubble animation skipped: ${reason}`);
    this.finish();
    return false;
  }

  /**
   * For screenshots of the animation (smoke.js): stage it, then hold each
   * moment in `times` (ms into the flight) long enough for `capture`.
   */
  async film(windowRect, bubbleRect, reverse, times, capture) {
    if (!(await this.stage(windowRect, bubbleRect, reverse))) return;
    this.staged = null;
    for (const [i, t] of times.entries()) {
      const held = this.next('scrubbed', 1000);
      this.send('scrub', { t });
      // eslint-disable-next-line no-await-in-loop
      await held;
      // eslint-disable-next-line no-await-in-loop
      await capture(i);
    }
    this.finish();
  }

  destroy() {
    if (this.win && !this.win.isDestroyed()) this.win.destroy();
    this.win = null;
  }
}

module.exports = { Morph };
