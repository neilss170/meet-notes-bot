'use strict';

/*
 * Window <-> bubble.
 *
 * Minimising Scribe does not send it to the taskbar: the window flies into the
 * floating bubble (morph.js) and the bubble stays at the edge of the screen,
 * saying Scribe is running and offering the few things worth doing mid-call.
 * Closing the window does the same, once with a hint that it is still
 * running. Ctrl+Alt+S goes either way.
 *
 * Smooth means nothing on screen ever jumps, blinks or waits:
 *
 * - The picture is ready before it is needed. Whenever the window loses
 *   focus, and the instant the mouse goes *down* on minimise or close, it is
 *   photographed and decoded in the overlay. Windows acts on the button's
 *   release, so by then the picture is already sitting exactly over the
 *   window - staged - and the flight starts on the very next frame. Windows'
 *   own minimise animation is off for this window
 *   (platform.disableWindowTransitions), so nothing else moves.
 *
 * - Minimising some other way (Win+Down, the taskbar) skips the press, so it
 *   stages the last picture taken instead: a frame or two later, not a
 *   blank screen while one is encoded.
 *
 * - Restoring shows the real window under the picture and waits until it has
 *   actually painted before the picture fades off it.
 */

/** How long a press on minimise or close stays staged without a release. */
const STAGE_HOLD_MS = 900;
const CAPTURE_TIMEOUT_MS = 250;
const PAINT_TIMEOUT_MS = 400;

const SETTING = {
  minimise: 'bubbleOnMinimise',      // "Minimise to floating bubble", on by default
  always: 'bubbleAlways',            // "Always show bubble"
  hideInShare: 'bubbleHideInShare',  // "Hide bubble during screen share", on by default
  hinted: 'bubbleCloseHinted',
};

class BubbleFlow {
  constructor({ mainWindow, bubble, morph, settings, platform, quitting, log }) {
    Object.assign(this, { mainWindow, bubble, morph, settings, platform, quitting, log });
    this.windowRect = null;
    this.busy = false;
    this.holdTimer = null;
    this.shot = null;
  }

  // -- settings -----------------------------------------------------------------

  get enabled() {
    return Boolean(this.settings.get(SETTING.minimise, true));
  }

  get alwaysShow() {
    return Boolean(this.settings.get(SETTING.always, false));
  }

  get hideInShare() {
    return Boolean(this.settings.get(SETTING.hideInShare, true));
  }

  set(key, value) {
    this.settings.set(SETTING[key], value);
    if (key === 'minimise') this.platform.disableWindowTransitions(this.mainWindow, value);
    if (key === 'hideInShare') this.bubble.applySettings();
    if (key === 'always') this.syncBubble();
  }

  // -- wiring -------------------------------------------------------------------

  attach() {
    const win = this.mainWindow;
    if (this.enabled) {
      const off = this.platform.disableWindowTransitions(win, true);
      this.log(`Windows' own minimise animation ${off ? 'off - the bubble animates instead' : 'could not be turned off'}`);
    }
    this.platform.watchCaptionButtons(win, () => this.pressed());
    win.on('blur', () => this.refreshPicture());
    const remember = () => {
      if (win.isVisible() && !win.isMinimized()) this.windowRect = win.getContentBounds();
    };
    win.on('move', remember);
    win.on('resize', remember);
    win.on('show', remember);
    // Whatever brought the window back - the tray, a second launch - the
    // bubble steps aside unless it is meant to stay.
    win.on('show', () => this.syncBubble());
    win.on('minimize', () => {
      if (this.enabled && !this.quitting()) this.toBubble('minimize');
    });
    // Warmed now so the first minimise has nothing to set up. Whether the
    // bubble shows at start-up is main.js's call: only when the window does not.
    this.morph.warm();
    this.bubble.create();
  }

  /** The bubble shows while the window is away - or always, if asked. */
  syncBubble() {
    const away = !this.mainWindow.isVisible() || this.mainWindow.isMinimized();
    if (this.alwaysShow || (away && this.enabled && !this.bubble.userHidden)) {
      this.bubble.show();
    } else if (!away) {
      this.bubble.hide();
    }
  }

  // -- the picture --------------------------------------------------------------

  /** Photograph the window and have the overlay decode it, ready to fly. */
  async refreshPicture() {
    const win = this.mainWindow;
    if (!win.isVisible() || win.isMinimized()) return false;
    try {
      const image = await Promise.race([
        win.webContents.capturePage(),
        new Promise((r) => setTimeout(() => r(null), CAPTURE_TIMEOUT_MS)),
      ]);
      if (!image || image.isEmpty()) return false;
      this.shot = image;
      this.windowRect = win.getContentBounds();
      await this.morph.preload(image, this.windowRect);
      return true;
    } catch (_) {
      return false;
    }
  }

  /**
   * The mouse went down on minimise or close. Photograph, and stage the
   * picture over the window, so the release finds the flight ready to go.
   * A press that is dragged off the button never releases on it: the stage
   * is taken down again after a moment.
   */
  async pressed() {
    if (!this.enabled || this.busy || this.quitting()) return;
    clearTimeout(this.holdTimer);
    if (!(await this.refreshPicture())) return;
    await this.bubble.create();
    if (!this.mainWindow.isVisible() || this.mainWindow.isMinimized()) return;
    await this.morph.stage(this.windowRect, this.bubble.circleRect(), false);
    this.holdTimer = setTimeout(() => {
      if (!this.busy && this.morph.isStaged) this.morph.finish();
    }, STAGE_HOLD_MS);
  }

  // -- the two directions -------------------------------------------------------

  /**
   * The window into the bubble.
   * @param {'minimize'|'close'|'hotkey'} reason
   */
  async toBubble(reason) {
    if (this.busy) return;
    this.busy = true;
    clearTimeout(this.holdTimer);
    const win = this.mainWindow;
    const t0 = Date.now();
    try {
      await this.bubble.create();
      this.bubble.userHidden = false;
      let staged = this.morph.isStaged;
      if (!staged) {
        // No press to have prepared it (the shortcut, Win+Down): a fresh
        // picture if the window is still up, the last one if it has gone.
        if (win.isVisible() && !win.isMinimized()) await this.refreshPicture();
        staged = await this.morph.stage(
          this.windowRect || win.getContentBounds(), this.bubble.circleRect(), false,
        );
      }
      this.log(`toBubble: flight ready ${Date.now() - t0} ms after the ${reason}${staged ? '' : ' (no picture; straight to the bubble)'}`);
      win.hide();
      if (staged) {
        // Hiding a window makes Windows recompose the desktop. The picture
        // already covers it, so let that settle for a frame rather than have
        // the flight's first frames compete with it - which is where the
        // last dropped frames were.
        await new Promise((r) => setTimeout(r, 24));
        await this.morph.fly(() => this.bubble.show({ pop: true }));
      } else {
        this.bubble.show({ pop: true });
      }
      if (reason === 'close' && !this.settings.get(SETTING.hinted, false)) {
        this.settings.set(SETTING.hinted, true);
        this.bubble.hint('Scribe is still running. Right-click to quit.');
      }
    } finally {
      this.morph.finish();
      this.busy = false;
    }
  }

  /** The bubble back into the window, where it was. */
  async toWindow() {
    const win = this.mainWindow;
    if (win.isVisible() && !win.isMinimized()) {
      win.focus();
      return;
    }
    if (this.busy) return;
    this.busy = true;
    try {
      const staged = this.bubble.visible && this.morph.hasPicture
        && await this.morph.stage(this.windowRect || win.getContentBounds(), this.bubble.circleRect(), true);
      if (staged) {
        if (!this.alwaysShow) this.bubble.hide();
        await this.morph.fly();
      }
      // The window comes up under the picture, which only leaves once the
      // page has really painted - never a frame of blank window.
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
      if (staged) {
        await Promise.race([
          win.webContents.executeJavaScript(
            'new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))',
          ).catch(() => false),
          new Promise((r) => setTimeout(r, PAINT_TIMEOUT_MS)),
        ]);
        await this.morph.release();
      }
      if (!this.alwaysShow) this.bubble.hide();
      this.log(`Bubble -> window (${staged ? 'animated' : 'direct'})`);
    } finally {
      this.morph.finish();
      this.busy = false;
    }
  }

  /** Ctrl+Alt+S. Into the bubble with the keys on it, or back out. */
  async toggle() {
    const win = this.mainWindow;
    if (win.isVisible() && !win.isMinimized()) {
      await this.toBubble('hotkey');
      this.bubble.focusForKeyboard();
    } else {
      await this.toWindow();
    }
  }
}

module.exports = { BubbleFlow, SETTING };
