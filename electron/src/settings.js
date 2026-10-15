'use strict';

/*
 * The shell's own few preferences - window position, whether start-at-sign-in
 * has been switched on for the first time - in one small JSON file under the
 * shell's userData folder. Everything that matters more lives with the server.
 */

const fs = require('fs');
const path = require('path');

class Settings {
  constructor(directory) {
    this.file = path.join(directory, 'settings.json');
    this.values = {};
    try {
      this.values = JSON.parse(fs.readFileSync(this.file, 'utf8')) || {};
    } catch (_) {
      this.values = {};
    }
  }

  get(key, fallback) {
    return Object.prototype.hasOwnProperty.call(this.values, key) ? this.values[key] : fallback;
  }

  set(key, value) {
    this.values[key] = value;
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true });
      // Written whole and renamed into place, so a crash mid-write cannot
      // leave a half file that forgets every setting.
      const temp = `${this.file}.tmp`;
      fs.writeFileSync(temp, JSON.stringify(this.values, null, 2));
      fs.renameSync(temp, this.file);
    } catch (_) {
      /* a preference not remembered is not worth failing over */
    }
  }
}

module.exports = { Settings };
