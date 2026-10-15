'use strict';

/*
 * Talking to the server over HTTP, as the shell.
 *
 * Every request carries the control token the server was started with, which
 * makes it the machine owner's request - see the docstring of
 * meetbot/service/app.py. Plain Node http rather than Electron's net module:
 * these calls must not share the window's cookies, and must work before any
 * window exists.
 */

const http = require('http');

const HEADER = 'x-scribe-control';

class ServerApi {
  /**
   * @param {object} options
   * @param {number} options.port
   * @param {string} options.token
   */
  constructor({ port, token }) {
    this.port = port;
    this.token = token;
    this.origin = `http://127.0.0.1:${port}`;
  }

  /**
   * One request. Resolves with the parsed JSON body; rejects with an Error
   * whose message is the server's own `detail`, which is written to be shown.
   */
  request(method, path, body, { timeoutMs = 15000 } = {}) {
    const payload = body === undefined ? null : Buffer.from(JSON.stringify(body));
    return new Promise((resolve, reject) => {
      const req = http.request(
        {
          host: '127.0.0.1',
          port: this.port,
          method,
          path,
          headers: {
            [HEADER]: this.token,
            Accept: 'application/json',
            ...(payload
              ? { 'Content-Type': 'application/json', 'Content-Length': payload.length }
              : {}),
          },
          timeout: timeoutMs,
        },
        (res) => {
          const chunks = [];
          res.on('data', (chunk) => chunks.push(chunk));
          res.on('end', () => {
            const text = Buffer.concat(chunks).toString('utf8');
            let data = null;
            try {
              data = text ? JSON.parse(text) : null;
            } catch (_) {
              data = null;
            }
            if (res.statusCode >= 200 && res.statusCode < 300) {
              resolve(data);
            } else {
              const detail = data && data.detail ? data.detail : `HTTP ${res.statusCode}`;
              const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
              error.status = res.statusCode;
              reject(error);
            }
          });
        },
      );
      req.on('timeout', () => req.destroy(new Error(`${method} ${path} timed out`)));
      req.on('error', reject);
      if (payload) req.write(payload);
      req.end();
    });
  }

  get(path, options) {
    return this.request('GET', path, undefined, options);
  }

  post(path, body, options) {
    return this.request('POST', path, body === undefined ? {} : body, options);
  }
}

/** Whether Scribe answers /health on this port. Needs no token. */
function probeHealth(port, timeoutMs = 1500) {
  return new Promise((resolve) => {
    const req = http.get(
      { host: '127.0.0.1', port, path: '/health', timeout: timeoutMs },
      (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          try {
            const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
            resolve(res.statusCode === 200 && body.app === 'scribe');
          } catch (_) {
            resolve(false);
          }
        });
      },
    );
    req.on('timeout', () => req.destroy());
    req.on('error', () => resolve(false));
  });
}

module.exports = { ServerApi, probeHealth, HEADER };
