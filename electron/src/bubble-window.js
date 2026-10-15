'use strict';

/*
 * The floating bubble: Scribe minimised to a slim pill that stays on top of
 * everything against the edge of the screen. It wears the same bars as the
 * recording pill in the page - moving and green while recording, grey and
 * still while held, an amber badge while your mic is muted - and a click
 * goes back to the note.
 *
 * What it must never do is get in the way of the call it is recording. So it
 * never takes focus (focusable: false, shown inactive), never has a taskbar
 * button, is hidden from screen shares by default (content protection), and
 * costs nothing while idle - its page runs no timers, and this side only
 * moves the window while it is actually being dragged or snapped.
 *
 * It is never hidden in the window-manager sense. It stays on screen,
 * invisible and click-through, and appearing is one opacity change: a hidden
 * window shown afresh can paint an empty first frame, which in the middle of
 * the minimise animation is a visible blink.
 *
 * Geometry is all in DIPs, the coordinates Electron's screen API speaks on
 * every display whatever its scaling. The window is the pill plus a margin
 * for the shadow the page draws (the OS shadow is off: it would be square).
 */

const path = require('path');
const { BrowserWindow, Menu, ipcMain, screen } = require('electron');

const W = 36;            // the pill, collapsed: slim...
const H = 76;            // ...and tall, so it reads as a handle on the edge
const M = 12;            // room around it for its shadow; bubble.html --m
const EDGE = 6;          // how far inside the work area the pill sits
const SNAP_MS = 320;
const SETTINGS_KEY = 'bubblePositions';

/** easeOutBack: a little past the edge and back - the "spring". */
function springEase(t) {
  const c1 = 1.25;
  const c3 = c1 + 1;
  return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2;
}

class BubbleWindow {
  /**
   * @param {object} options
   * @param {import('./settings').Settings} options.settings
   * @param {(action: string) => void} options.onAction  record | mute | mark | open | call-yes | call-no
   * @param {() => void} options.onOpen     Left click on the circle.
   * @param {() => void} options.onQuit
   * @param {() => boolean} options.hideInShare
   * @param {(line: string) => void} options.log
   */
  constructor(options) {
    Object.assign(this, options);
    this.win = null;
    this.side = 'right';
    this.circle = null;          // { x, y } of the pill's top-left, DIP
    this.width = 0;              // expanded pill width, 0 when collapsed
    this.drag = null;
    this.snapTimer = null;
    this.ready = null;
    this.userHidden = false;
    this.lastState = null;
    this.shown = false;
  }

  // -- the window ---------------------------------------------------------------

  create() {
    if (this.win) return this.ready;
    this.win = new BrowserWindow({
      width: W + 2 * M,
      height: H + 2 * M,
      show: false,
      frame: false,
      transparent: true,
      backgroundColor: '#00000000',
      hasShadow: false,
      resizable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      focusable: false,
      alwaysOnTop: true,
      title: 'Scribe',
      webPreferences: {
        preload: path.join(__dirname, 'bubble', 'preload.js'),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        backgroundThrottling: false,
      },
    });
    this.win.setAlwaysOnTop(true, 'floating');
    this.applySettings();
    this.win.webContents.on('will-navigate', (event) => event.preventDefault());
    this.win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    this.win.on('blur', () => this.win && this.win.setFocusable(false));
    this.wire();
    this.restorePosition();
    this.ready = this.win.loadFile(path.join(__dirname, 'bubble', 'bubble.html')).then(() => {
      if (this.lastState) this.send('state', this.lastState);
      this.send('layout', { side: this.side, expanded: false });
      // On screen from here on, invisible and click-through until shown.
      this.win.setOpacity(0);
      this.win.setIgnoreMouseEvents(true);
      this.win.showInactive();
    });
    screen.on('display-removed', () => this.reposition());
    screen.on('display-metrics-changed', () => this.reposition());
    return this.ready;
  }

  wire() {
    const mine = (event) => this.win && event.sender === this.win.webContents;
    ipcMain.on('bubble:expand', (event, { width } = {}) => {
      if (!mine(event)) return;
      this.width = Math.max(W, Math.min(Number(width) || 240, 380));
      this.place();
      this.send('layout', { side: this.side, expanded: true });
    });
    ipcMain.on('bubble:collapse', (event) => {
      if (!mine(event)) return;
      this.width = 0;
      this.place();
      this.send('layout', { side: this.side, expanded: false });
    });
    ipcMain.on('bubble:act', (event, action) => {
      if (!mine(event)) return;
      if (action === 'open') this.onOpen();
      else this.onAction(String(action));
    });
    ipcMain.on('bubble:drag-start', (event) => mine(event) && this.dragStart());
    ipcMain.on('bubble:drag-move', (event) => mine(event) && this.dragMove());
    ipcMain.on('bubble:drag-end', (event) => mine(event) && this.dragEnd());
    ipcMain.on('bubble:blur', (event) => mine(event) && this.win.setFocusable(false));
    ipcMain.on('bubble:menu', (event) => {
      if (!mine(event)) return;
      Menu.buildFromTemplate([
        { label: 'Open Scribe', click: () => this.onOpen() },
        { label: 'Hide bubble', click: () => { this.userHidden = true; this.hide(); } },
        { type: 'separator' },
        { label: 'Quit Scribe', click: () => this.onQuit() },
      ]).popup({ window: this.win });
    });
  }

  send(channel, payload) {
    if (this.win && !this.win.isDestroyed()) this.win.webContents.send(`bubble:${channel}`, payload);
  }

  applySettings() {
    if (this.win) this.win.setContentProtection(Boolean(this.hideInShare()));
  }

  // -- where it sits ------------------------------------------------------------

  /** The window's bounds for the circle's position, side and pill width. */
  bounds() {
    const width = (this.width || W) + 2 * M;
    const x = this.side === 'right' ? this.circle.x + W + M - width : this.circle.x - M;
    return { x: Math.round(x), y: Math.round(this.circle.y - M), width, height: H + 2 * M };
  }

  place() {
    if (!this.win || !this.circle) return;
    // Width and height every time: moving a window between displays of
    // different scaling with setPosition alone lets Windows resize it.
    this.win.setBounds(this.bounds());
  }

  /** Where the pill is on screen, for the animation to aim at. */
  circleRect() {
    return { x: this.circle.x, y: this.circle.y, width: W, height: H };
  }

  /** The stable resting place for this side of this display. */
  restingPlace(display, side, yRatio) {
    const area = display.workArea;
    const x = side === 'left' ? area.x + EDGE : area.x + area.width - EDGE - W;
    const top = area.y + EDGE;
    const bottom = area.y + area.height - EDGE - H;
    const y = top + (bottom - top) * Math.min(1, Math.max(0, yRatio));
    return { x, y };
  }

  restorePosition() {
    const saved = this.settings.get(SETTINGS_KEY, {}) || {};
    const displays = screen.getAllDisplays();
    const last = saved.last && displays.find((d) => String(d.id) === String(saved.last));
    const display = last || screen.getPrimaryDisplay();
    const spot = last ? saved[String(display.id)] : null;
    // Default, and fallback when that display is gone: the right edge of the
    // primary display, a little below the middle, clear of the taskbar.
    this.side = spot ? spot.side : 'right';
    this.circle = this.restingPlace(display, this.side, spot ? spot.y : 0.62);
    this.place();
  }

  reposition() {
    if (!this.win) return;
    this.restorePosition();
    this.send('layout', { side: this.side, expanded: Boolean(this.width) });
  }

  savePosition(display) {
    const saved = { ...(this.settings.get(SETTINGS_KEY, {}) || {}) };
    const area = display.workArea;
    const top = area.y + EDGE;
    const span = Math.max(1, area.height - 2 * EDGE - H);
    saved[String(display.id)] = { side: this.side, y: (this.circle.y - top) / span };
    saved.last = String(display.id);
    this.settings.set(SETTINGS_KEY, saved);
  }

  // -- dragging -----------------------------------------------------------------

  dragStart() {
    clearInterval(this.snapTimer);
    const cursor = screen.getCursorScreenPoint();
    this.width = 0;
    this.drag = { dx: cursor.x - this.circle.x, dy: cursor.y - this.circle.y };
    this.place();
  }

  dragMove() {
    if (!this.drag) return;
    const cursor = screen.getCursorScreenPoint();
    this.circle = { x: cursor.x - this.drag.dx, y: cursor.y - this.drag.dy };
    // While dragging the pill sits centred in its window, side or no side.
    this.win.setBounds({
      x: Math.round(this.circle.x - M), y: Math.round(this.circle.y - M),
      width: W + 2 * M, height: H + 2 * M,
    });
  }

  dragEnd() {
    if (!this.drag) return;
    this.drag = null;
    this.snap();
  }

  /** Spring to the nearest side edge of whichever display it was let go on. */
  snap() {
    const centre = { x: this.circle.x + W / 2, y: this.circle.y + H / 2 };
    const display = screen.getDisplayNearestPoint(centre);
    const area = display.workArea;
    this.side = centre.x < area.x + area.width / 2 ? 'left' : 'right';
    const top = area.y + EDGE;
    const span = Math.max(1, area.height - 2 * EDGE - H);
    const target = this.restingPlace(display, this.side, (this.circle.y - top) / span);
    const from = { ...this.circle };
    const started = Date.now();
    this.send('layout', { side: this.side, expanded: false });
    clearInterval(this.snapTimer);
    // A timer only for the third of a second the spring takes.
    this.snapTimer = setInterval(() => {
      const t = Math.min(1, (Date.now() - started) / SNAP_MS);
      const k = springEase(t);
      this.circle = { x: from.x + (target.x - from.x) * k, y: from.y + (target.y - from.y) * k };
      this.place();
      if (t >= 1) {
        clearInterval(this.snapTimer);
        this.circle = target;
        this.place();
        this.savePosition(display);
      }
    }, 16);
  }

  // -- showing ------------------------------------------------------------------

  get visible() {
    return Boolean(this.win && !this.win.isDestroyed() && this.shown);
  }

  async show({ pop = false } = {}) {
    await this.create();
    this.userHidden = false;
    this.place();
    if (pop) this.send('pop');
    // Never activated: appearing must not pull focus out of the meeting.
    if (!this.win.isVisible()) this.win.showInactive();
    this.win.setAlwaysOnTop(true, 'floating');
    this.win.setIgnoreMouseEvents(false);
    this.win.setOpacity(1);
    this.shown = true;
  }

  hide() {
    if (!this.win || this.win.isDestroyed()) return;
    this.shown = false;
    this.win.setOpacity(0);
    this.win.setIgnoreMouseEvents(true);
    if (this.width) {
      this.width = 0;
      this.place();
      this.send('layout', { side: this.side, expanded: false });
    }
  }

  update(state) {
    this.lastState = state;
    this.send('state', state);
  }

  offerCall(prompt) {
    this.send('call', prompt);
  }

  clearCall() {
    this.send('call', null);
  }

  hint(text) {
    this.send('hint', text);
  }

  /** Ctrl+Alt+S: take focus on purpose, so the pill can be driven by keys. */
  focusForKeyboard() {
    if (!this.visible) return;
    this.win.setFocusable(true);
    this.win.focus();
    this.send('keyboard');
  }

  destroy() {
    clearInterval(this.snapTimer);
    if (this.win && !this.win.isDestroyed()) this.win.destroy();
    this.win = null;
  }
}

module.exports = { BubbleWindow, BUBBLE_SIZE: { width: W, height: H }, BUBBLE_MARGIN: M };
