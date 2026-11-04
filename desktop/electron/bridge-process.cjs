'use strict';
const {spawn} = require('node:child_process');
const {EventEmitter} = require('node:events');
const path = require('node:path');
const {parseReadiness, childEnvironment} = require('./policy.cjs');
class BridgeProcess extends EventEmitter {
  constructor(executable, args, options = {}) {
    super(); this.executable = executable; this.args = [...args]; this.options = options;
    this.closing = false; this.started = false; this.finished = false; this.terminated = false;
    this.exited = new Promise(resolve => { this.resolveExit = resolve; });
  }
  start() {
    if (this.started) throw Error('Bridge already started.'); this.started = true;
    return new Promise((resolve, reject) => {
      let pending = Buffer.alloc(0); let settled = false; let timer;
      const fail = () => { if (settled) return; settled = true; clearTimeout(timer); reject(Error('The bundled bridge did not become ready.')); this.shutdown(); };
      try {
        this.child = (this.options.spawn || spawn)(this.executable, this.args, {
          cwd: path.dirname(this.executable), shell: false, windowsHide: true,
          env: childEnvironment(process.env), stdio: ['pipe', 'pipe', 'pipe']
        });
      } catch { this.finished = true; this.resolveExit({code: null, signal: null}); fail(); return; }
      const child = this.child;
      child.stdin.on('error', () => {}); // EPIPE is expected if the child has already exited.
      child.stderr.on('data', () => {}); // Drain, never retain paths/auth output.
      child.stdout.on('data', chunk => {
        if (settled) return; // The listener remains attached and drains all tail output.
        const newline = chunk.indexOf(10);
        const head = newline < 0 ? chunk : chunk.subarray(0, newline + 1);
        if (pending.length + head.length > 4096) { pending = Buffer.alloc(0); fail(); return; }
        pending = Buffer.concat([pending, head]);
        if (newline >= 0) {
          try { const origin = parseReadiness(pending, child.pid); settled = true; clearTimeout(timer); pending = Buffer.alloc(0); resolve(origin); }
          catch { pending = Buffer.alloc(0); fail(); }
        }
      });
      child.stdout.on('end', () => { if (!settled) fail(); });
      child.on('error', fail);
      const exited = () => {
        if (this.terminated) return;
        this.terminated = true;
        if (!settled) fail();
        else if (!this.closing) this.emit('unexpected-exit');
      };
      child.once('exit', exited);
      // close, rather than exit, waits for stdio completion; no competing readers.
      child.once('close', (code, signal) => {
        this.finished = true; clearTimeout(timer);
        exited();
        this.resolveExit({code, signal});
      });
      timer = setTimeout(fail, this.options.timeoutMs || 30000);
    });
  }
  shutdown() {
    if (this.closing) return this.exited;
    this.closing = true;
    if (this.child?.stdin && !this.child.stdin.destroyed) this.child.stdin.end('shutdown\n');
    return this.exited; // No timeout/kill: an accepted SVN write must finish draining.
  }
}
module.exports = {BridgeProcess};
