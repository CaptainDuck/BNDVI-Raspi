/* Debug view.
 *
 * The live-feed engine (fetch, BNDVI, canvas painting) is shared with the setup
 * wizard and lives in feed.js. This file is the controls and the readouts.
 */
(function () {
  'use strict';

  let feed = null;
  let picker = null;
  const canvases = {};

  function init() {
    const root = HC.$('#debug-root');
    if (!root) return;

    const cfg = JSON.parse(root.dataset.settings);
    ['dbg-raw', 'dbg-heat', 'dbg-fc', 'dbg-hist', 'ch-r', 'ch-g', 'ch-b']
      .forEach(function (id) { canvases[id] = HC.$('#' + id); });

    feed = new Feed({
      fps: cfg.preview_fps,
      correctNir: cfg.correct_nir_leakage,
      k: cfg.nir_leak_coef,
      tHealthy: cfg.threshold_healthy,
      tModerate: cfg.threshold_moderate,
      onFrame: render,
      onError: function (err) {
        HC.toast('Live feed stopped: ' + err.message, true);
      }
    });

    wireControls();
    wireRegionPicker();
    feed.start();
    pollLogs();
  }

  /* ── painting ───────────────────────────────────────────────────────────── */

  function render() {
    if (!feed || !feed.nir) return;
    feed.paintRaw(canvases['dbg-raw']);
    feed.paintHeat(canvases['dbg-heat']);
    feed.paintFalse(canvases['dbg-fc']);
    feed.paintHistogram(canvases['dbg-hist']);
    feed.paintChannel(canvases['ch-r'], 'nir', [1, 0.22, 0.22]);
    feed.paintChannel(canvases['ch-g'], 'green', [0.28, 1, 0.36]);
    feed.paintChannel(canvases['ch-b'], 'blue', [0.3, 0.42, 1]);
    paintStats();
    setFeedLabel();
    if (picker) picker.redraw();
  }

  /** Repaint from the frame already in hand — for slider moves, where there is
   *  no new data and no reason to touch the network. */
  function repaint() {
    if (!feed || !feed.nir) return;
    feed.compute();
    render();
  }

  function paintStats() {
    const s = feed.stats();
    if (!s) return;
    setText('live-mean', HC.fmt(s.mean));
    setText('live-healthy', s.h.toFixed(1) + '%');
    setText('live-moderate', s.m.toFixed(1) + '%');
    setText('live-stressed', s.s.toFixed(1) + '%');
    setText('live-std', s.std.toFixed(3));
    setText('live-range', HC.fmt(s.min) + ' … ' + HC.fmt(s.max));
    setText('sanity-note', sanityNote(s, feed.channelMeans()));
  }

  /** The most valuable thing on this page: what the numbers mean, in words. */
  function sanityNote(s, ch) {
    if (feed.mismatch) {
      // Nothing else matters if the camera is ignoring the locked settings.
      return 'The camera is not honouring the locked settings: ' + feed.mismatch +
        '. Until that is fixed every BNDVI number here is meaningless — see ' +
        'RESEARCH-GAPS.md §4.';
    }
    if (feed.source === 'synthetic') {
      return 'Synthetic frames — no camera attached. The white square top-left ' +
        'is a simulated reference card, so you can try the calibration below.';
    }
    if (!ch) return 'Waiting for the first frame…';
    if (ch.nir > 245 || ch.blue > 245) {
      return 'Channels are clipping at 255. A clipped channel makes BNDVI read ' +
        'falsely flat — lower the exposure or the gain.';
    }
    if (ch.nir < 25 && ch.blue < 25) {
      return 'The frame is nearly black. Raise the exposure or gain, and shoot ' +
        'in daylight — indoor LEDs emit almost no NIR.';
    }
    if (ch.green !== null && ch.green >= ch.blue * 0.9) {
      return 'Green is nearly as bright as blue, which is what an unfiltered ' +
        'camera looks like. Check the blue gel has not fallen out of the lens cap.';
    }
    if (s.mean > 0.25) {
      return 'Raw frame reads pink over the plants — that is the NIR landing in ' +
        'the red channel, so the filter and white balance are behaving.';
    }
    return 'Low mean. If the raw frame looks natural-coloured rather than pink, ' +
      'auto white balance is still on, or the gel has fallen out.';
  }

  function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  function setFeedLabel() {
    const el = HC.$('#feed-label');
    if (!el) return;
    el.textContent = !feed.live ? 'FROZEN'
      : (feed.source === 'synthetic' ? 'SYNTHETIC' : 'LIVE');
  }

  /* ── white-reference region ─────────────────────────────────────────────── */

  function wireRegionPicker() {
    const layer = HC.$('#roi-layer');
    if (!layer) return;
    picker = new RegionPicker(layer, HC.$('#roi-box'), function (box) {
      const btn = HC.$('#solve-k');
      if (btn) btn.disabled = !box;
    });
    const btn = HC.$('#solve-k');
    if (btn) btn.disabled = true;
  }

  /* ── controls ───────────────────────────────────────────────────────────── */

  function wireControls() {
    HC.$$('[data-nir]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        feed.correctNir = btn.dataset.nir === 'on';
        HC.$$('[data-nir]').forEach(function (b) {
          b.classList.toggle('is-on',
            (b.dataset.nir === 'on') === feed.correctNir);
        });
        setText('nir-label', feed.correctNir ? 'On' : 'Off');
        HC.saveSetting({ correct_nir_leakage: feed.correctNir });
        repaint();
      });
    });

    HC.slider(HC.$('#k'), HC.$('#k-label'),
      function (v) { return v.toFixed(2); },
      function (v) { feed.k = v; repaint(); HC.saveSetting({ nir_leak_coef: v }); });

    HC.slider(HC.$('#t-healthy'), HC.$('#t-healthy-label'),
      function (v) { return v.toFixed(2); },
      function (v) {
        feed.tHealthy = v; repaint(); HC.saveSetting({ threshold_healthy: v });
      });

    HC.slider(HC.$('#t-moderate'), HC.$('#t-moderate-label'),
      function (v) { return v.toFixed(2); },
      function (v) {
        feed.tModerate = v; repaint(); HC.saveSetting({ threshold_moderate: v });
      });

    // Exposure and gain go to the camera, so these need a round-trip. The
    // preview picks the change up on its next frame.
    HC.slider(HC.$('#exposure'), HC.$('#exposure-label'),
      function (v) { return Math.round(v) + ' µs'; },
      function (v) { HC.saveSetting({ exposure_us: Math.round(v) }); });

    HC.slider(HC.$('#gain'), HC.$('#gain-label'),
      function (v) { return v.toFixed(1) + '×'; },
      function (v) { HC.saveSetting({ gain: v }); });

    HC.slider(HC.$('#fps'), HC.$('#fps-label'),
      function (v) { return Math.round(v) + ' fps'; },
      function (v) {
        HC.saveSetting({ preview_fps: Math.round(v) });
        feed.setFps(Math.round(v));
      });

    HC.$$('[name="resolution"]').forEach(function (radio) {
      radio.addEventListener('change', function () {
        HC.saveSetting({ resolution: radio.value });
      });
    });

    HC.$$('[data-scene]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        HC.$$('[data-scene]').forEach(function (b) { b.classList.remove('is-on'); });
        btn.classList.add('is-on');
        HC.api('/api/preview/scene', {
          method: 'POST', body: { scene: btn.dataset.scene }
        }).catch(function (err) { HC.toast(err.message, true); });
      });
    });

    const liveBtn = HC.$('#toggle-live');
    if (liveBtn) {
      liveBtn.addEventListener('click', function () {
        feed.live = !feed.live;
        liveBtn.textContent = feed.live ? 'Freeze frame' : 'Resume live';
        setFeedLabel();
      });
    }

    const saveBtn = HC.$('#save-frame');
    if (saveBtn) {
      saveBtn.addEventListener('click', async function () {
        saveBtn.disabled = true;
        const original = saveBtn.textContent;
        saveBtn.textContent = 'Saving…';
        try {
          const rec = await HC.api('/api/captures', {
            method: 'POST',
            body: {
              from_preview: true,
              label: 'Debug frame',
              correct_nir_leakage: feed.correctNir,
              nir_leak_coef: feed.k
            }
          });
          saveBtn.textContent = 'Saved ✓';
          HC.toast('Saved as capture ' + rec.id + ' (mean ' +
            HC.fmt(rec.stats.mean) + ')');
        } catch (err) {
          HC.toast('Could not save: ' + err.message, true);
        } finally {
          saveBtn.disabled = false;
          setTimeout(function () { saveBtn.textContent = original; }, 2200);
        }
      });
    }

    const solveBtn = HC.$('#solve-k');
    if (solveBtn) {
      solveBtn.addEventListener('click', async function () {
        if (!picker || !picker.box) return;
        solveBtn.disabled = true;
        try {
          const res = await HC.api('/api/calibrate/solve-k', {
            method: 'POST',
            body: Object.assign({ apply: true }, picker.box)
          });
          setText('solve-result', res.message);
          if (res.k !== null) {
            feed.k = res.k;
            feed.correctNir = true;
            const input = HC.$('#k');
            if (input) {
              input.value = res.k;
              input.dispatchEvent(new Event('input'));
            }
            HC.$$('[data-nir]').forEach(function (b) {
              b.classList.toggle('is-on', b.dataset.nir === 'on');
            });
            setText('nir-label', 'On');
            repaint();
          }
        } catch (err) {
          setText('solve-result', err.message);
        } finally {
          solveBtn.disabled = false;
        }
      });
    }

    const retryBtn = HC.$('#retry-camera');
    if (retryBtn) {
      retryBtn.addEventListener('click', function () {
        retryBtn.disabled = true;
        HC.api('/api/camera/restart', { method: 'POST' })
          .then(function (probe) {
            HC.toast(probe.available ? 'Camera detected.'
              : 'Still not detected — ' + probe.detail, !probe.available);
            if (probe.available) location.reload();
          })
          .catch(function (err) { HC.toast(err.message, true); })
          .finally(function () { retryBtn.disabled = false; });
      });
    }

    const synthBtn = HC.$('#use-synthetic');
    if (synthBtn) {
      synthBtn.addEventListener('click', function () {
        HC.api('/api/camera/synthetic', { method: 'POST', body: { on: true } })
          .then(function () { location.reload(); })
          .catch(function (err) { HC.toast(err.message, true); });
      });
    }

    const resetBtn = HC.$('#reset-debug');
    if (resetBtn) {
      resetBtn.addEventListener('click', async function () {
        const ok = await HC.confirmDialog({
          title: 'Reset the camera settings?',
          body: 'Exposure, gain, the leakage coefficient and the thresholds all ' +
            'go back to their defaults. Saved captures keep the values they ' +
            'were taken with.',
          action: 'Reset'
        });
        if (!ok) return;
        await HC.api('/api/settings', {
          method: 'PATCH',
          body: {
            exposure_us: 5000, gain: 2.0, nir_leak_coef: 0.8,
            correct_nir_leakage: false, threshold_healthy: 0.3,
            threshold_moderate: 0.1, preview_fps: 12
          }
        });
        location.reload();
      });
    }
  }

  /* ── log viewer ─────────────────────────────────────────────────────────── */

  function pollLogs() {
    const view = HC.$('#log-view');
    if (!view) return;
    HC.poll(function () {
      return HC.api('/api/logs?limit=120').then(function (lines) {
        const atBottom =
          view.scrollTop + view.clientHeight >= view.scrollHeight - 24;
        view.innerHTML = lines.map(function (l) {
          return '<div class="log-line ' + l.level + '">' +
            '<span class="log-time">' + esc(l.time) + '</span>' +
            '<span class="log-level">' + esc(l.level) + '</span>' +
            '<span class="log-text">' + esc(l.text) + '</span></div>';
        }).join('');
        if (atBottom) view.scrollTop = view.scrollHeight;
      });
    }, 4000);
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  document.addEventListener('DOMContentLoaded', init);
}());
