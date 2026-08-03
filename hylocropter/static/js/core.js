/* Shared plumbing for every page: fetch, polling, dialogs, toasts, controls.
 * No framework and no build step — this is loaded directly by a <script> tag.
 */
(function (global) {
  'use strict';

  /* ── fetch ──────────────────────────────────────────────────────────────
     The API always returns JSON, including on errors (the old app returned
     Flask's HTML 404 page on /api/ paths, so res.json() threw and the real
     message was lost). This surfaces `error` as the thrown message. */
  async function api(path, options) {
    options = options || {};
    const init = { method: options.method || 'GET', headers: {} };
    if (options.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(options.body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (err) {
      // Offline device, Flask restarted, cable pulled — say something useful.
      throw new Error('Could not reach the device. Is the dashboard still running?');
    }
    if (res.status === 204) return null;
    const type = res.headers.get('content-type') || '';
    let data = null;
    if (type.indexOf('application/json') !== -1) {
      data = await res.json().catch(function () { return null; });
    }
    if (!res.ok) {
      const message = (data && data.error) || (data && data.message) ||
        ('HTTP ' + res.status);
      const err = new Error(message);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  /* ── polling ────────────────────────────────────────────────────────────
     Backs off when the device stops answering rather than hammering it, and
     pauses entirely while the tab is hidden — the Pi has better things to do
     than serve telemetry to a background tab. */
  function poll(fn, intervalMs, options) {
    options = options || {};
    let timer = null;
    let failures = 0;
    let stopped = false;

    async function tick() {
      if (stopped) return;
      if (document.hidden && !options.runHidden) {
        timer = setTimeout(tick, intervalMs);
        return;
      }
      try {
        const keepGoing = await fn();
        failures = 0;
        if (keepGoing === false) { stopped = true; return; }
      } catch (err) {
        failures++;
        if (options.onError) options.onError(err, failures);
      }
      const delay = failures ? Math.min(intervalMs * Math.pow(2, failures), 30000)
        : intervalMs;
      timer = setTimeout(tick, delay);
    }

    tick();
    return {
      stop: function () { stopped = true; if (timer) clearTimeout(timer); },
      now: function () { if (timer) clearTimeout(timer); tick(); }
    };
  }

  /* ── dialog ─────────────────────────────────────────────────────────────
     Built rather than using window.confirm() so destructive actions can carry
     the explanatory copy the mockup writes for them. */
  function confirmDialog(spec) {
    return new Promise(function (resolve) {
      const backdrop = document.createElement('div');
      backdrop.className = 'dialog-backdrop hc-noprint';
      backdrop.innerHTML =
        '<div class="dialog" role="dialog" aria-modal="true">' +
        '<div class="dialog-title"></div>' +
        '<div class="dialog-body"></div>' +
        '<div class="dialog-actions">' +
        '<button class="btn btn-secondary" data-act="cancel">Never mind</button>' +
        '<button class="btn btn-primary" data-act="ok"></button>' +
        '</div></div>';
      backdrop.querySelector('.dialog-title').textContent = spec.title || 'Are you sure?';
      backdrop.querySelector('.dialog-body').textContent = spec.body || '';
      const okBtn = backdrop.querySelector('[data-act="ok"]');
      okBtn.textContent = spec.action || 'Confirm';

      function close(result) {
        document.removeEventListener('keydown', onKey);
        backdrop.remove();
        resolve(result);
      }
      function onKey(e) { if (e.key === 'Escape') close(false); }

      backdrop.addEventListener('click', function (e) {
        const act = e.target.getAttribute && e.target.getAttribute('data-act');
        if (act === 'ok') close(true);
        else if (act === 'cancel' || e.target === backdrop) close(false);
      });
      document.addEventListener('keydown', onKey);
      document.body.appendChild(backdrop);
      okBtn.focus();
    });
  }

  /* ── toast ──────────────────────────────────────────────────────────────── */
  function toast(message, isError) {
    let el = document.getElementById('toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'toast';
      el.className = 'toast';
      el.style.cssText = 'position:fixed;left:50%;bottom:24px;transform:translateX(-50%);' +
        'z-index:950;max-width:min(520px,92vw);box-shadow:var(--shadow-lg)';
      document.body.appendChild(el);
    }
    el.className = 'toast' + (isError ? ' is-error' : '');
    el.textContent = message;
    el.hidden = false;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(function () { el.hidden = true; }, 5000);
  }

  /* ── small helpers ──────────────────────────────────────────────────────── */

  function fmt(v, digits) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    return (v >= 0 ? '+' : '') + Number(v).toFixed(digits === undefined ? 3 : digits);
  }
  function pct(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    return Number(v).toFixed(1) + '%';
  }
  function bytes(n) {
    if (!n) return '0 B';
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(0) + ' KB';
    if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
    return (n / 1073741824).toFixed(2) + ' GB';
  }
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }

  /** Debounce, for slider inputs that write back to the server. */
  function debounce(fn, ms) {
    let t = null;
    return function () {
      const args = arguments, self = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(self, args); }, ms || 250);
    };
  }

  /** Wire a slider to a live label and a callback.
   *
   * `onChange` fires only when the user actually moves it. The initial call
   * paints the label and nothing else — firing onChange during wiring meant
   * every page load immediately PATCHed the same settings straight back to the
   * server, and any callback touching state built *after* the slider was wired
   * would throw.
   */
  function slider(input, label, format, onChange) {
    if (!input) return function () {};
    function paint() {
      const v = parseFloat(input.value);
      if (label) label.textContent = format ? format(v) : String(v);
      return v;
    }
    input.addEventListener('input', function () {
      const v = paint();
      if (onChange) onChange(v);
    });
    paint();
    return paint;
  }

  /** PATCH a settings key, debounced, with a toast on failure. */
  const saveSetting = debounce(function (patch, done) {
    api('/api/settings', { method: 'PATCH', body: patch })
      .then(function (res) {
        (res.warnings || []).forEach(function (w) { toast(w, true); });
        if (done) done(res);
      })
      .catch(function (err) { toast('Could not save: ' + err.message, true); });
  }, 350);

  global.HC = {
    api: api, poll: poll, confirmDialog: confirmDialog, toast: toast,
    fmt: fmt, pct: pct, bytes: bytes, $: $, $$: $$,
    debounce: debounce, slider: slider, saveSetting: saveSetting
  };
}(window));


/* ── header status chips, on every page ─────────────────────────────────── */
document.addEventListener('DOMContentLoaded', function () {
  const link = HC.$('#chip-drone');
  const camChip = HC.$('#chip-camera');
  const batteryChip = HC.$('#chip-battery');
  if (!link) return;

  function paint(snap) {
    const connected = snap.connected;
    link.className = 'tag ' + (connected ? 'tag-healthy' : 'tag-stressed');
    HC.$('.chip-text', link).textContent = connected
      ? 'Connected to drone'
      : (snap.status === 'stale' ? 'Drone link lost' : 'Drone not connected');
    link.title = snap.detail || '';

    const cam = snap.camera || {};
    const camOk = cam.available;
    camChip.className = 'tag ' + (camOk ? 'tag-healthy'
      : (cam.synthetic ? 'tag-neutral' : 'tag-stressed'));
    HC.$('.chip-text', camChip).textContent = camOk ? 'Camera ready'
      : (cam.synthetic ? 'Synthetic frames' : 'No camera');
    camChip.title = cam.detail || '';

    if (batteryChip) {
      const pct = snap.battery_pct;
      batteryChip.hidden = pct === null || pct === undefined;
      if (!batteryChip.hidden) batteryChip.textContent = 'Battery ' + pct + '%';
    }
  }

  HC.poll(function () {
    return HC.api('/api/telemetry').then(paint);
  }, 3000);
});
