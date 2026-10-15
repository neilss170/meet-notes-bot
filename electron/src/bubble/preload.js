'use strict';

/*
 * The bubble's only way out: a handful of named messages to the main
 * process, which turns them into the server's ordinary HTTP routes. The page
 * gets no Node, no ipcRenderer and no way to name a channel of its own.
 */

const { contextBridge, ipcRenderer } = require('electron');

const SENDS = new Set([
  'expand', 'collapse', 'act', 'drag-start', 'drag-move', 'drag-end', 'menu', 'blur',
]);
const LISTENS = new Set(['state', 'layout', 'call', 'hint', 'keyboard', 'pop']);

contextBridge.exposeInMainWorld('bubble', {
  send(channel, payload) {
    if (SENDS.has(channel)) ipcRenderer.send(`bubble:${channel}`, payload);
  },
  on(channel, listener) {
    if (!LISTENS.has(channel)) return;
    ipcRenderer.on(`bubble:${channel}`, (_event, payload) => listener(payload));
  },
});
