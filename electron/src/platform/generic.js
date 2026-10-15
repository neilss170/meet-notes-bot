'use strict';

/*
 * What any platform gets unless it says otherwise. darwin.js and win32.js
 * start from this and replace only what differs.
 */

module.exports = {
  /** The frozen server, inside resources/server. */
  serverExecutable: 'scribe-server',

  /** The interpreter to run the server from source with, in `npm run dev`. */
  python: 'python3',

  /** Ctrl+Alt+M as Electron spells it, and as the page should show it. */
  micShortcut: { accelerator: 'Control+Alt+M', label: 'Ctrl+Alt+M' },

  /** Window <-> floating bubble. */
  bubbleShortcut: { accelerator: 'Control+Alt+S', label: 'Ctrl+Alt+S' },

  /** Tray images: the notification area wants small PNGs. */
  trayImage(state) {
    return `tray-${state}.png`;
  },

  /** Open a plain-text file - settings, a log - in something that edits it. */
  openText(file) {
    return require('electron').shell.openPath(file);
  },

  /** Stop the OS animating this window's minimise and restore, so the
      shell's own animation into the bubble is the only one. Returns
      whether it could. */
  disableWindowTransitions(_win, _off = true) {
    return false;
  },

  /** Call `onPress(button)` as soon as the mouse goes down on the window's
      minimise or close button - before the OS acts, which is on release. */
  watchCaptionButtons(_win, _onPress) {},

  /** Called once the app is ready, before any window or notification. */
  setup(_app) {},

  /** End a process and anything it started. The last resort, after asking. */
  killTree(child) {
    try {
      child.kill('SIGKILL');
    } catch (_) {
      /* already gone */
    }
  },

  /** Clean up after a from-source Scribe the installed app replaces. */
  retireSourceInstall(_log, _dataDir) {
    return [];
  },
};
