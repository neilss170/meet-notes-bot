'use strict';

/* The overlay's channel to main.js: a few named messages and nothing else. */

const { contextBridge, ipcRenderer } = require('electron');

const SENDS = new Set(['staged', 'ready', 'arrive', 'done', 'failed', 'scrubbed', 'released']);
const LISTENS = new Set(['preload', 'stage', 'shown', 'go', 'scrub', 'release', 'clear']);

contextBridge.exposeInMainWorld('overlay', {
  send(channel) {
    if (SENDS.has(channel)) ipcRenderer.send(`overlay:${channel}`);
  },
  on(channel, listener) {
    if (!LISTENS.has(channel)) return;
    ipcRenderer.on(`overlay:${channel}`, (_event, payload) => listener(payload || {}));
  },
});
