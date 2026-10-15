'use strict';

/*
 * Everything that differs between operating systems, behind one interface.
 *
 * The rest of the shell is written once and asks this module for the parts
 * that cannot be: what the server binary is called, how to make sure a
 * process tree is gone, which keys mute the microphone, what to tidy up from
 * an earlier from-source install. A macOS target means writing darwin.js to
 * the same shape - nothing outside this folder should need to change.
 */

const PLATFORMS = {
  win32: () => require('./win32'),
  darwin: () => require('./darwin'),
};

/** The implementation for this machine, falling back to the generic one. */
function current() {
  const load = PLATFORMS[process.platform];
  return load ? load() : require('./generic');
}

module.exports = current();
