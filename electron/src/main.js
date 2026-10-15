'use strict';

/*
 * Scribe, the desktop app.
 *
 * A shell around the existing Scribe: it starts the server as a hidden child
 * process on a free localhost port, shows the server's own page in a window,
 * and puts the icon by the clock. It decides nothing about meetings. Every
 * action - start, stop, mute, answer a call alert - is one of the server's
 * ordinary HTTP routes, made as the machine's owner with a token only this
 * process holds.
 *
 * Where things live:
 *   %APPDATA%\Scribe\            the server's data - settings, accounts, recordings, logs
 *   %APPDATA%\Scribe\shell\      this process's own (window position, Chromium's cache)
 *   <install dir>\resources\server\scribe-server.exe    the frozen server
 * Nothing user-specific goes in the install directory, which is replaced on
 * every upgrade.
 */

const fs = require('fs');
const path = require('path');
const { app, globalShortcut, Notification } = require('electron');

const platform = require('./platform');
const { Backend } = require('./backend');
const { Settings } = require('./settings');
const { CallAlerts } = require('./calls');
const { ScribeTray } = require('./tray');
const { startUpdater } = require('./updater');
const { createErrorWindow, createMainWindow, createSplash, lockSession } = require('./windows');
const { BubbleWindow } = require('./bubble-window');
const { Morph } = require('./morph');
const { BubbleFlow } = require('./minimise');

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const ASSETS = path.join(__dirname, '..', 'assets');
const WINDOW_ICON = path.join(ASSETS, 'logo.png');

// -- where things live: decided before anything else touches the disk --------

const DATA_DIR = process.env.SCRIBE_DATA_DIR || path.join(app.getPath('appData'), 'Scribe');
// The shell's own Chromium state sits inside the data directory, so removing
// "my data" removes all of it - and so the single-instance lock, which is
// keyed on this path, is per data directory.
app.setPath('userData', path.join(DATA_DIR, 'shell'));

const LOG_DIR = path.join(DATA_DIR, 'logs');
const SERVER_LOG = path.join(LOG_DIR, 'scribe-server.log');
const SHELL_LOG = path.join(LOG_DIR, 'scribe-shell.log');

function log(line) {
  const stamped = `${new Date().toISOString()} ${line}\n`;
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true });
    fs.appendFileSync(SHELL_LOG, stamped);
  } catch (_) {
    /* nowhere to write; carry on */
  }
  if (!app.isPackaged) process.stdout.write(stamped);
}

/** How to run the server: frozen when installed, Python from a checkout. */
function serverCommand() {
  if (process.env.SCRIBE_SERVER_EXE) {
    return { file: process.env.SCRIBE_SERVER_EXE, args: [] };
  }
  if (app.isPackaged) {
    return { file: path.join(process.resourcesPath, 'server', platform.serverExecutable), args: [] };
  }
  return {
    file: process.env.SCRIBE_PYTHON || platform.python,
    args: ['-m', 'meetbot.server_main'],
    cwd: REPO_ROOT,
  };
}

// -- one Scribe at a time -----------------------------------------------------

if (!app.requestSingleInstanceLock()) {
  // The first one is told (second-instance below) and comes to the front.
  app.quit();
} else {
  run();
}

function run() {
  const settings = new Settings(app.getPath('userData'));
  let backend = null;
  let mainWindow = null;
  let splash = null;
  let tray = null;
  let calls = null;
  let bubble = null;
  let morph = null;
  let flow = null;
  let quitting = false;
  let shutdownDone = false;

  const notify = (body) => {
    if (Notification.isSupported()) new Notification({ title: 'Scribe', body, icon: WINDOW_ICON }).show();
  };

  function showWindow() {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    // Away in the bubble: come back the way it went.
    if (flow && (!mainWindow.isVisible() || mainWindow.isMinimized()) && bubble && bubble.visible) {
      flow.toWindow();
      return;
    }
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  }

  function showMeeting(jobId) {
    if (!jobId || !mainWindow) return;
    showWindow();
    // The page's own navigation, so a note half-typed elsewhere is saved the
    // way switching meetings always saves it - not lost to a reload.
    mainWindow.webContents
      .executeJavaScript(`typeof select === "function" && select(${JSON.stringify(jobId)})`)
      .catch(() => {});
  }

  // -- start at sign-in ------------------------------------------------------

  const LOGIN_ARGS = ['--hidden'];
  const loginItem = () => app.getLoginItemSettings({ args: LOGIN_ARGS }).openAtLogin;
  function setLoginItem(on) {
    if (!app.isPackaged) {
      // A dev build would register electron.exe; say so instead of doing it.
      log(`(dev) start at sign-in would be ${on ? 'on' : 'off'}`);
      settings.set('devLoginItem', on);
      return;
    }
    app.setLoginItemSettings({ openAtLogin: on, args: LOGIN_ARGS });
    log(`Start at sign-in: ${on ? 'on' : 'off'}`);
  }
  const loginItemState = () => (app.isPackaged ? loginItem() : Boolean(settings.get('devLoginItem', false)));

  // -- failure ---------------------------------------------------------------

  function showFailure(message) {
    log(`Failure: ${message}`);
    if (splash && !splash.isDestroyed()) splash.destroy();
    if (tray) tray.stop();
    if (calls) calls.stop();
    if (bubble) bubble.destroy();
    if (morph) morph.destroy();
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.destroy();
    quitting = true;
    const errorWindow = createErrorWindow({
      icon: WINDOW_ICON,
      message,
      logFile: SERVER_LOG,
      output: backend ? backend.lastOutput() : '',
      onAction: (action) => {
        if (action === 'open-log') {
          platform.openText(fs.existsSync(SERVER_LOG) ? SERVER_LOG : SHELL_LOG);
          return;
        }
        if (backend && backend.child) platform.killTree(backend.child);
        shutdownDone = true;
        if (action === 'restart') app.relaunch();
        app.exit(0);
      },
    });
    // Closing it is quitting: with no server there is no tray to come back to.
    errorWindow.on('closed', () => {
      if (backend && backend.child) platform.killTree(backend.child);
      shutdownDone = true;
      app.exit(0);
    });
  }

  // -- quitting --------------------------------------------------------------

  /**
   * Quit from the tray, or anything else that asks the app to quit: stop a
   * live recording, let it be written up, and only then go. The server is
   * killed only if it never finishes - see Backend.shutdown.
   */
  async function quitGracefully() {
    if (quitting) return;
    quitting = true;
    globalShortcut.unregisterAll();
    if (tray && tray.status.recording) notify('Finishing the recording and its notes, then closing.');
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.hide();
    if (bubble) bubble.destroy();
    if (morph) morph.destroy();
    if (calls) calls.stop();
    if (tray && tray.tray) tray.tray.setToolTip('Scribe — finishing up…');
    const how = backend ? await backend.shutdown() : 'not-running';
    log(`Server shut down: ${how}`);
    shutdownDone = true;
    if (tray) tray.stop();
    app.quit();
  }

  app.on('before-quit', (event) => {
    if (shutdownDone) return;
    event.preventDefault();
    quitGracefully();
  });
  app.on('will-quit', () => globalShortcut.unregisterAll());
  // Hidden to the tray, not closed: having no windows is normal here.
  app.on('window-all-closed', () => {});
  app.on('second-instance', () => showWindow());

  // -- start -----------------------------------------------------------------

  app.whenReady().then(async () => {
    platform.setup(app);
    log(`Scribe ${app.getVersion()} starting (data: ${DATA_DIR})`);

    // A scripted check of a build (see src/smoke.js) must not change how
    // this machine starts up; a real first run does.
    if (app.isPackaged && !process.env.SCRIBE_SMOKE) {
      platform.retireSourceInstall(log, DATA_DIR);
      if (!settings.get('loginItemDefaulted', false)) {
        // On by default, once: a notetaker that is not running when the
        // call starts is no use. The tray menu turns it off.
        setLoginItem(true);
        settings.set('loginItemDefaulted', true);
      }
    }

    const hidden = process.argv.includes('--hidden')
      || (app.isPackaged && app.getLoginItemSettings().wasOpenedAtLogin);
    if (!hidden) splash = createSplash({ assets: ASSETS, icon: WINDOW_ICON });

    // Registered before the server starts, so the page is only told about a
    // shortcut that is really ours. Another program may hold it already.
    const shortcut = platform.micShortcut;
    const haveShortcut = globalShortcut.register(shortcut.accelerator, () => {
      // Nothing recording: do nothing, quietly - the same keys mean
      // something in other programs.
      if (tray) tray.toggleMic().catch((error) => notify(error.message));
    });
    if (!haveShortcut) log(`${shortcut.label} is taken by another program; mute from the page or tray.`);

    backend = new Backend({
      command: serverCommand(),
      dataDir: DATA_DIR,
      micShortcut: haveShortcut ? shortcut.label : '',
      log,
    });
    let api;
    try {
      api = await backend.start();
    } catch (error) {
      if (backend.child) platform.killTree(backend.child);
      showFailure(error.message);
      return;
    }
    backend.on('crashed', (code) => {
      if (!quitting) showFailure(`Scribe's server stopped unexpectedly (exit code ${code}).`);
    });
    log(`Server is up at ${api.origin}`);

    lockSession(api.origin);
    // The window is the owner's, as the icon is: sign it in rather than ask
    // for a password the first run only ever wrote to a log.
    try {
      const grant = await api.post('/api/control/session');
      const { session } = require('electron');
      await session.defaultSession.cookies.set({
        url: api.origin,
        name: grant.cookie,
        value: grant.value,
        httpOnly: true,
        sameSite: 'lax',
        expirationDate: Math.floor(Date.now() / 1000) + grant.max_age,
      });
    } catch (error) {
      log(`Could not sign the window in (${error.message}); the login page will ask.`);
    }

    mainWindow = createMainWindow({
      origin: api.origin,
      settings,
      icon: WINDOW_ICON,
      quitting: () => quitting,
      // Closing is going away, not quitting: into the bubble if that is on.
      onClose: () => {
        if (flow && flow.enabled) flow.toBubble('close');
        else mainWindow.hide();
      },
    });
    // Windows logging off ends both processes; ask the server to finish the
    // recording in whatever time is left rather than being cut off.
    mainWindow.on('session-end', () => {
      api.post('/api/control/shutdown', {}, { timeoutMs: 2000 }).catch(() => {});
    });
    mainWindow.once('ready-to-show', () => {
      if (splash && !splash.isDestroyed()) splash.destroy();
      splash = null;
      if (!hidden) showWindow();
      // Started at sign-in with no window: the bubble is how you know.
      else if (flow) flow.syncBubble();
    });
    mainWindow.loadURL(`${api.origin}/`);

    tray = new ScribeTray({
      api,
      assets: ASSETS,
      paths: {
        recordings: path.join(DATA_DIR, 'recordings'),
        log: SERVER_LOG,
        settings: path.join(DATA_DIR, '.env'),
      },
      showWindow,
      showMeeting,
      loginItem: loginItemState,
      setLoginItem,
      quit: () => quitGracefully(),
    });
    // The floating bubble, and the animation into it.
    bubble = new BubbleWindow({
      settings,
      log,
      hideInShare: () => (flow ? flow.hideInShare : true),
      onOpen: () => showWindow(),
      onQuit: () => quitGracefully(),
      onAction: (action) => {
        const run = {
          record: () => tray.toggleRecording(),
          mute: () => tray.toggleMic(),
          mark: () => tray.mark(),
          keep: () => tray.keepRecording(),
          'call-yes': () => calls && calls.answer(true),
          'call-no': () => calls && calls.answer(false),
        }[action];
        if (run) Promise.resolve(run()).catch((error) => notify(error.message));
      },
    });
    morph = new Morph({ hideInShare: () => (flow ? flow.hideInShare : true), log });
    flow = new BubbleFlow({
      mainWindow, bubble, morph, settings, platform, log, quitting: () => quitting,
    });
    flow.attach();
    tray.onStatus = (status) => bubble.update(status);
    tray.options = [
      { label: 'Minimise to floating bubble', get: () => flow.enabled, set: (on) => flow.set('minimise', on) },
      { label: 'Always show bubble', get: () => flow.alwaysShow, set: (on) => flow.set('always', on) },
      { label: 'Hide bubble during screen share', get: () => flow.hideInShare, set: (on) => flow.set('hideInShare', on) },
    ];
    tray.render(true);

    const bubbleKeys = platform.bubbleShortcut;
    if (!globalShortcut.register(bubbleKeys.accelerator, () => flow.toggle())) {
      log(`${bubbleKeys.label} is taken by another program; use the tray or the bubble instead.`);
    }

    calls = new CallAlerts({ api, icon: WINDOW_ICON, showWindow, log, bubble });
    calls.start();

    startUpdater(log);

    if (process.env.SCRIBE_SMOKE) {
      require('./smoke').attach({ app, mainWindow, tray, api, log, quit: quitGracefully, bubble, flow, morph });
    }
  });
}
