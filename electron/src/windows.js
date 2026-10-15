'use strict';

/*
 * The three windows the shell owns: the splash while the server starts, the
 * app itself, and the error that replaces both when the server will not.
 *
 * The app window is a browser pointed at one origin and nothing else. It is
 * sandboxed, has no Node, isolates its context, and is not allowed to
 * navigate, load a sub-resource from, or open a window onto anywhere but the
 * server's own http://127.0.0.1:<port>. Links elsewhere - "open call" on a
 * Meet meeting - go to the default browser instead. The page is the same
 * file a browser tab gets; nothing in it knows it is inside Electron.
 */

const fs = require('fs');
const path = require('path');
const { BrowserWindow, nativeTheme, screen, shell, session } = require('electron');

/** The page's own background in each theme, so a window never flashes white
    in dark mode before the page has painted (ui.html, --base). */
const BACKGROUND = { light: '#eceef4', dark: '#07080c' };

const DEFAULT_BOUNDS = { width: 1280, height: 820 };

/** Permissions the page actually uses. Everything else is refused. */
const ALLOWED_PERMISSIONS = new Set(['clipboard-sanitized-write']);

const background = () => (nativeTheme.shouldUseDarkColors ? BACKGROUND.dark : BACKGROUND.light);

function sameOrigin(url, origin) {
  try {
    return new URL(url).origin === origin;
  } catch (_) {
    return false;
  }
}

/** Only web links leave the app, and only to the default browser. */
function openOutside(url) {
  try {
    const { protocol } = new URL(url);
    if (protocol === 'https:' || protocol === 'http:' || protocol === 'mailto:') {
      shell.openExternal(url);
    }
  } catch (_) {
    /* not a URL; nothing to open */
  }
}

/** Saved bounds, if they still land on a screen that exists. */
function restoredBounds(saved) {
  if (!saved || !saved.width || !saved.height) return { ...DEFAULT_BOUNDS };
  const visible = screen.getAllDisplays().some(({ workArea: a }) => (
    saved.x < a.x + a.width - 80 && saved.x + saved.width > a.x + 80
    && saved.y >= a.y - 10 && saved.y < a.y + a.height - 60
  ));
  return visible
    ? { x: saved.x, y: saved.y, width: saved.width, height: saved.height }
    : { width: saved.width, height: saved.height };
}

/**
 * Lock the app's session to one origin. Applied once, to the default
 * session, before the window loads anything.
 */
function lockSession(origin) {
  const ses = session.defaultSession;
  ses.setPermissionRequestHandler((_wc, permission, callback) => {
    callback(ALLOWED_PERMISSIONS.has(permission));
  });
  ses.setPermissionCheckHandler((_wc, permission) => ALLOWED_PERMISSIONS.has(permission));
  // Sub-resources included, not only navigation: the page is self-contained,
  // so anything reaching off-machine is not something it asked for.
  ses.webRequest.onBeforeRequest({ urls: ['http://*/*', 'https://*/*', 'ws://*/*', 'wss://*/*'] },
    (details, callback) => {
      callback({ cancel: !sameOrigin(details.url, origin) && !details.url.startsWith('devtools:') });
    });
}

/**
 * The app window. Hidden until its page is ready; closing it hides it.
 * @param {object} options
 * @param {string} options.origin  http://127.0.0.1:<port>
 * @param {import('./settings').Settings} options.settings
 * @param {string} options.icon
 * @param {() => boolean} options.quitting  True once the app is really quitting.
 * @param {() => void} [options.onClose]  What closing does instead of
 *   quitting - into the bubble, or simply hidden.
 */
function createMainWindow({ origin, settings, icon, quitting, onClose }) {
  const bounds = restoredBounds(settings.get('bounds'));
  const win = new BrowserWindow({
    ...bounds,
    minWidth: 880,
    minHeight: 560,
    show: false,
    title: 'Scribe',
    icon,
    backgroundColor: background(),
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      spellcheck: true,
    },
  });
  win.setMenuBarVisibility(false);
  if (settings.get('maximized')) win.maximize();

  const wc = win.webContents;
  wc.setWindowOpenHandler(({ url }) => {
    if (!sameOrigin(url, origin)) openOutside(url);
    return { action: 'deny' };
  });
  const guard = (event, url) => {
    if (sameOrigin(url, origin)) return;
    event.preventDefault();
    openOutside(url);
  };
  wc.on('will-navigate', guard);
  wc.on('will-redirect', guard);
  wc.on('will-attach-webview', (event) => event.preventDefault());

  // The title bar follows the system theme on its own; the area behind the
  // page has to be told.
  const retint = () => {
    if (!win.isDestroyed()) win.setBackgroundColor(background());
  };
  nativeTheme.on('updated', retint);

  let saveTimer = null;
  const remember = () => {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      if (win.isDestroyed()) return;
      settings.set('maximized', win.isMaximized());
      if (!win.isMaximized() && !win.isMinimized()) settings.set('bounds', win.getBounds());
    }, 400);
  };
  win.on('resize', remember);
  win.on('move', remember);
  win.on('maximize', remember);
  win.on('unmaximize', remember);

  // Closing the window is not quitting: a recording carries on, and the
  // icon by the clock is how to get the window back.
  win.on('close', (event) => {
    if (quitting()) return;
    event.preventDefault();
    if (!win.isMaximized() && !win.isMinimized()) settings.set('bounds', win.getBounds());
    if (onClose) onClose();
    else win.hide();
  });
  win.on('closed', () => nativeTheme.removeListener('updated', retint));
  return win;
}

function htmlUrl(html) {
  return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
}

function logoDataUrl(assets) {
  try {
    return `data:image/png;base64,${fs.readFileSync(path.join(assets, 'logo.png')).toString('base64')}`;
  } catch (_) {
    return '';
  }
}

/** Shared by the splash and the error window: the page's palette and type. */
const BASE_CSS = `
  :root { color-scheme: light dark; --ink:#101115; --muted:#585b6b; --base:#eceef4;
          --card: rgba(255,255,255,.7); --edge: rgba(255,255,255,.8); --spark:#e2703a; --bad:#c43b2d; }
  @media (prefers-color-scheme: dark) {
    :root { --ink:#f1f2f6; --muted:#a4a7b6; --base:#07080c; --card: rgba(30,31,39,.82);
            --edge: rgba(255,255,255,.085); --spark:#f08a4b; --bad:#ef7a64; } }
  * { box-sizing: border-box; }
  html, body { margin:0; height:100%; }
  body { font: 13.5px/1.5 "Segoe UI Variable Text","Segoe UI",-apple-system,sans-serif;
         color: var(--ink); background: var(--base);
         background-image: radial-gradient(600px 400px at 0% 0%, rgba(255,159,107,.35), transparent 60%),
                           radial-gradient(600px 400px at 100% 0%, rgba(146,132,255,.28), transparent 60%);
         -webkit-user-select: none; user-select: none; }
  h1 { font: 400 28px/1.15 "Instrument Serif", Georgia, "Times New Roman", serif;
       letter-spacing: -.02em; margin: 0; }
  .muted { color: var(--muted); }
`;

/** A small branded window while the server starts. */
function createSplash({ assets, icon }) {
  const win = new BrowserWindow({
    width: 360,
    height: 220,
    frame: false,
    resizable: false,
    movable: true,
    center: true,
    show: false,
    skipTaskbar: false,
    title: 'Scribe',
    icon,
    backgroundColor: background(),
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  const logo = logoDataUrl(assets);
  win.loadURL(htmlUrl(`<!doctype html><meta charset="utf-8"><title>Scribe</title>
    <style>${BASE_CSS}
      body { display:flex; flex-direction:column; align-items:center; justify-content:center; gap:14px; }
      img { width:56px; height:56px; animation: turn 2.4s linear infinite; }
      @keyframes turn { to { transform: rotate(360deg); } }
      @media (prefers-reduced-motion: reduce) { img { animation: none; } }
    </style>
    ${logo ? `<img src="${logo}" alt="">` : ''}
    <h1>Scribe</h1><div class="muted">Starting…</div>`));
  win.once('ready-to-show', () => win.show());
  win.webContents.on('will-navigate', (event) => event.preventDefault());
  return win;
}

/**
 * Said instead of a window when the server would not start.
 * Its buttons are links to scribe-action:, caught here, so the page needs
 * no preload and no IPC.
 */
function createErrorWindow({ icon, message, logFile, output, onAction }) {
  const win = new BrowserWindow({
    width: 620,
    height: 460,
    title: 'Scribe could not start',
    icon,
    show: false,
    backgroundColor: background(),
    autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  win.setMenuBarVisibility(false);
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  win.loadURL(htmlUrl(`<!doctype html><meta charset="utf-8"><title>Scribe could not start</title>
    <style>${BASE_CSS}
      body { padding: 28px 32px; }
      .card { background: var(--card); border: 1px solid var(--edge); border-radius: 18px; padding: 18px 20px; margin-top: 16px; }
      code, pre { font-family: "Cascadia Mono", Consolas, monospace; font-size: 12px; }
      pre { white-space: pre-wrap; max-height: 130px; overflow: auto; margin: 10px 0 0; color: var(--muted);
            -webkit-user-select: text; user-select: text; }
      .bad { color: var(--bad); font-weight: 600; }
      .row { display:flex; gap:8px; margin-top: 18px; }
      a.btn { text-decoration:none; color: var(--ink); background: var(--card); border:1px solid var(--edge);
              border-radius: 999px; padding: 7px 15px; font-weight: 550; }
      a.btn.primary { background: var(--spark); color: #fff; border-color: transparent; }
    </style>
    <h1>Scribe could not start</h1>
    <div class="card">
      <div class="bad">${esc(message)}</div>
      <div style="margin-top:8px">The details are in the log:<br><code style="-webkit-user-select:text;user-select:text">${esc(logFile)}</code></div>
      ${output ? `<pre>${esc(output)}</pre>` : ''}
    </div>
    <div class="row">
      <a class="btn primary" href="scribe-action:restart">Try again</a>
      <a class="btn" href="scribe-action:open-log">Open the log</a>
      <a class="btn" href="scribe-action:quit">Quit</a>
    </div>`));
  win.webContents.on('will-navigate', (event, url) => {
    event.preventDefault();
    if (url.startsWith('scribe-action:')) onAction(url.slice('scribe-action:'.length));
  });
  win.once('ready-to-show', () => win.show());
  return win;
}

module.exports = { createMainWindow, createSplash, createErrorWindow, lockSession, sameOrigin };
