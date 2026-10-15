'use strict';

/*
 * macOS. A placeholder that is correct as far as it goes: the server binary,
 * the shortcut spelled the Mac way, and a template tray image. Capturing the
 * speakers on a Mac needs a loopback driver (BlackHole or similar) that the
 * server does not support yet, so this target builds and runs but records
 * only what the microphone hears until that lands.
 */

const generic = require('./generic');

module.exports = {
  ...generic,
  micShortcut: { accelerator: 'Command+Option+M', label: '⌘⌥M' },
  bubbleShortcut: { accelerator: 'Command+Option+S', label: '⌘⌥S' },
  // TODO(mac): an idle "Template" image, which macOS redraws for light and
  // dark menu bars. The recording states keep their colour either way.
  setup(app) {
    // A menu-bar app: no Dock icon while the window is hidden.
    if (app.dock) app.dock.hide();
  },
};
