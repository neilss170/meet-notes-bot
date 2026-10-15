'use strict';

/*
 * Windows.
 *
 * Two things only Windows needs:
 *
 * The AppUserModelID. Windows files notifications, taskbar grouping and the
 * Start-menu shortcut under it, and a notification raised under an id with no
 * shortcut behind it either does not appear or loses its buttons. It must be
 * the appId electron-builder writes into the shortcut it installs.
 *
 * Retiring the from-source start-up. `meetbot shortcut --startup` put a
 * Scribe.lnk in the Startup folder that runs pythonw. Left there, signing in
 * would start the old Scribe *and* the app, two recorders fighting over one
 * microphone. Only a link that runs Python is touched - the installer's own
 * shortcuts run Scribe.exe - and it is moved, not deleted, to
 * <data>
etired\Scribe-startup.lnk. Moving it back into the Startup folder
 * undoes it. It keeps its .lnk name on purpose: the server reads its working
 * directory to find the checkout whose settings and recordings to copy in,
 * and this runs before the server does.
 */

const fs = require('fs');
const path = require('path');
const { execFile } = require('child_process');
const { shell } = require('electron');
const generic = require('./generic');

const APP_ID = 'com.neilsharma.scribe';

// Window messages and hit-test codes, from WinUser.h.
const WM_NCLBUTTONDOWN = 0x00a1;
const HTMINBUTTON = 8;
const HTCLOSE = 20;
// dwmapi.h: stop DWM animating this window's minimise and restore.
const DWMWA_TRANSITIONS_FORCEDISABLED = 3;

let dwmSetWindowAttribute = null;
function dwm() {
  // Loaded on first use, through koffi (FFI, prebuilt - no compiler at
  // install time). One function from one system DLL.
  if (dwmSetWindowAttribute === null) {
    const koffi = require('koffi');
    dwmSetWindowAttribute = koffi.load('dwmapi.dll').func(
      'long __stdcall DwmSetWindowAttribute(void *hwnd, uint32_t attr, _In_ int *value, uint32_t size)',
    );
  }
  return dwmSetWindowAttribute;
}

function hwndOf(win) {
  const handle = win.getNativeWindowHandle();
  return handle.length >= 8 ? handle.readBigUInt64LE(0) : BigInt(handle.readUInt32LE(0));
}

module.exports = {
  ...generic,
  appId: APP_ID,
  serverExecutable: 'scribe-server.exe',
  python: 'python',

  setup(app) {
    app.setAppUserModelId(APP_ID);
  },

  openText(file) {
    // .env has no program associated with it, so openPath would do nothing.
    execFile('notepad.exe', [file], { windowsHide: false }, () => {});
    return Promise.resolve('');
  },

  /*
   * Windows animates a minimise toward the taskbar button, and nothing in
   * Electron can cancel the minimise itself. Left alone, the window would
   * shrink to the taskbar *and* the shell's snapshot would fly to the bubble
   * - two ghosts going different ways. With DWM's transitions off for this
   * one window, the minimise is instant and the bubble animation is the only
   * movement on screen.
   */
  disableWindowTransitions(win, off = true) {
    try {
      const result = dwm()(hwndOf(win), DWMWA_TRANSITIONS_FORCEDISABLED, [off ? 1 : 0], 4);
      return result === 0;
    } catch (_) {
      return false;
    }
  },

  watchCaptionButtons(win, onPress) {
    win.hookWindowMessage(WM_NCLBUTTONDOWN, (wParam) => {
      const hit = wParam.readUInt32LE(0);
      if (hit === HTMINBUTTON) onPress('minimize');
      else if (hit === HTCLOSE) onPress('close');
    });
  },

  killTree(child) {
    if (!child || child.exitCode !== null || !child.pid) return;
    // child.kill() on Windows ends only that process. The server can have
    // started PowerShell for voice commands, or a browser for the bot.
    execFile('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true }, () => {});
  },

  retireSourceInstall(log, dataDir) {
    const startup = path.join(
      process.env.APPDATA || '', 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup',
    );
    const link = path.join(startup, 'Scribe.lnk');
    if (!fs.existsSync(link)) return [];
    let target = '';
    try {
      target = shell.readShortcutLink(link).target || '';
    } catch (_) {
      return [];
    }
    if (!/python(w)?\.exe$/i.test(target)) return [];
    const parked = path.join(dataDir, 'retired', 'Scribe-startup.lnk');
    try {
      fs.mkdirSync(path.dirname(parked), { recursive: true });
      fs.copyFileSync(link, parked);
      fs.unlinkSync(link);
      log(`Retired the from-source start-up shortcut: ${link} -> ${parked}`);
      return [parked];
    } catch (error) {
      log(`Could not retire ${link}: ${error.message}`);
      return [];
    }
  },
};
