'use strict';

/*
 * "A call started - record it?" as a native notification.
 *
 * The server still does the noticing: meetbot/capture/calls.py reads the
 * microphone state Windows keeps for its privacy indicator and turns a new
 * call into a prompt. Started by the shell, the server stops raising its own
 * PowerShell toast for it (--shell-notifications), and this module asks
 * instead - with real buttons wired to the existing routes:
 *
 *   Record  -> POST /api/recordings { title, call_id }
 *   Ignore  -> POST /api/calls/{id}/dismiss
 *
 * A prompt that disappears from the server - the call ended, or it was
 * answered in the page - takes its notification with it.
 *
 * When the floating bubble is on screen it asks instead - "Record?" with Yes
 * and Ignore - because it is already where the person is looking, and a
 * notification on top of it would be the same question twice.
 */

const { Notification } = require('electron');

const POLL_MS = 2000;

class CallAlerts {
  /**
   * @param {object} options
   * @param {import('./api').ServerApi} options.api
   * @param {string} options.icon
   * @param {() => void} options.showWindow
   * @param {(line: string) => void} options.log
   * @param {import('./bubble-window').BubbleWindow} [options.bubble]  Asks
   *   instead of a notification while it is on screen.
   */
  constructor({ api, icon, showWindow, log, bubble = null }) {
    this.api = api;
    this.bubble = bubble;
    this.icon = icon;
    this.showWindow = showWindow;
    this.log = log;
    this.shown = new Map(); // prompt id -> Notification
    this.titles = new Map(); // prompt id -> what the recording would be called
    this.inBubble = null;    // the prompt the bubble is asking about
    this.timer = null;
  }

  start() {
    if (!Notification.isSupported()) {
      this.log('Notifications are not supported here; call alerts stay in the page.');
      return;
    }
    this.timer = setInterval(() => this.poll(), POLL_MS);
  }

  stop() {
    clearInterval(this.timer);
    for (const note of this.shown.values()) note.close();
    this.shown.clear();
  }

  async poll() {
    let data;
    try {
      data = await this.api.get('/api/calls');
    } catch (_) {
      return;
    }
    const prompts = data && data.enabled ? data.prompts || [] : [];
    const waiting = new Set(prompts.map((p) => p.id));
    for (const [id, note] of this.shown) {
      if (!waiting.has(id)) {
        note.close();
        this.shown.delete(id);
      }
    }
    for (const prompt of prompts) {
      this.titles.set(prompt.id, prompt.title);
      if (!this.shown.has(prompt.id)) this.ask(prompt);
    }
  }

  ask(prompt) {
    if (this.bubble && this.bubble.visible) {
      this.bubble.offerCall(prompt);
      this.inBubble = prompt.id;
      // Shaped like a notification, so the poll can take it back the same way.
      this.shown.set(prompt.id, { close: () => this.bubble.clearCall() });
      this.log(`Asked about a ${prompt.app} call in the bubble (${prompt.id}).`);
      return;
    }
    const note = new Notification({
      title: `${prompt.service || prompt.app} call detected`,
      body: `${prompt.app} is using your microphone. Take notes with Scribe? Nothing joins the call.`,
      icon: this.icon,
      timeoutType: 'never',
      actions: [
        { type: 'button', text: 'Record' },
        { type: 'button', text: 'Ignore' },
      ],
    });
    note.on('action', (event, legacyIndex) => {
      const index = event && typeof event.actionIndex === 'number' ? event.actionIndex : legacyIndex;
      if (index === 0) this.record(prompt);
      else this.dismiss(prompt);
    });
    // Clicking the body rather than a button: show the page, where the same
    // offer is waiting as a card.
    note.on('click', () => this.showWindow());
    note.show();
    this.shown.set(prompt.id, note);
    this.log(`Asked about a ${prompt.app} call (${prompt.id}).`);
  }

  /** The bubble's Yes or Ignore, for whichever call it is asking about. */
  answer(yes) {
    const id = this.inBubble;
    this.inBubble = null;
    if (!id) return;
    const prompt = { id, title: this.titles.get(id) || 'Call' };
    if (yes) this.record(prompt);
    else this.dismiss(prompt);
  }

  async record(prompt) {
    try {
      await this.api.post('/api/recordings', { title: prompt.title, call_id: prompt.id });
    } catch (error) {
      new Notification({ title: 'Scribe could not record', body: error.message, icon: this.icon }).show();
    }
  }

  async dismiss(prompt) {
    try {
      await this.api.post(`/api/calls/${encodeURIComponent(prompt.id)}/dismiss`);
    } catch (_) {
      /* already ended - nothing to dismiss */
    }
  }
}

module.exports = { CallAlerts };
