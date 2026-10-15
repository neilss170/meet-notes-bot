'use strict';

/*
 * Updates from GitHub Releases - wired, and switched off.
 *
 * Off until there is a signed build and a release process to go with it: an
 * updater pulling unsigned installers from a private repository needs a
 * token baked into every copy, and that is a decision, not a default. To turn
 * it on, set "scribe": { "autoUpdate": true } in package.json (or
 * SCRIBE_AUTO_UPDATE=1 for one run) and publish with `npm run dist -- -p always`.
 * The feed is the `publish` block in electron-builder.yml.
 */

const { app } = require('electron');
const pkg = require('../package.json');

function enabled() {
  if (process.env.SCRIBE_AUTO_UPDATE === '1') return true;
  return Boolean(pkg.scribe && pkg.scribe.autoUpdate);
}

function startUpdater(log) {
  if (!app.isPackaged) return false;
  if (!enabled()) {
    log('Automatic updates are off (package.json scribe.autoUpdate).');
    return false;
  }
  // Required lazily, so a disabled updater costs nothing at start-up.
  const { autoUpdater } = require('electron-updater');
  autoUpdater.logger = { info: log, warn: log, error: log, debug: () => {} };
  // Installed on quit, never mid-meeting.
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.checkForUpdatesAndNotify().catch((error) => log(`Update check failed: ${error.message}`));
  return true;
}

module.exports = { startUpdater };
