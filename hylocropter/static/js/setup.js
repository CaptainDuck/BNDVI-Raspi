/* Guided camera setup.
 *
 * One step visible at a time, with a live verdict per step measured off the feed
 * rather than left to the operator's judgement. State is remembered server-side
 * so closing the tab doesn't lose your place.
 *
 * The live-feed engine is shared with the Debug view (feed.js).
 */
(function () {
  'use strict';

  const STEPS = [];
  let index = 0;
  let feed = null;
  let picker = null;
  const verdicts = {};      // step -> 'ok' | 'warn' | 'bad' | 'skip'
  let cfg = null;

  function init() {
    const root = document.getElementById('setup-root');
    if (!root) return;
    cfg = JSON.parse(root.dataset.settings);

    HC.$$('.setup-step').forEach(function (el) {
      STEPS.push({ key: el.dataset.step, title: el.dataset.title, el: el });
    });

    // The feed has to exist before wireSteps(), because some control callbacks
    // reach into it.
    feed = new Feed({
      fps: Math.min(8, cfg.preview_fps),   // the wizard needs less than Debug
      correctNir: cfg.correct_nir_leakage,
      k: cfg.nir_leak_coef,
      tHealthy: cfg.threshold_healthy,
      tModerate: cfg.threshold_moderate,
      onFrame: paintFeeds
    });

    buildRail();
    wireNav();
    wireSteps();
    feed.start();

    // resume where they left off
    HC.api('/api/setup/state').then(function (st) {
      (st.done_steps || []).forEach(function (k) { verdicts[k] = 'ok'; });
      const at = Math.max(0, Math.min(STEPS.length - 1, st.step || 0));
      show(at);
      checkCamera();
      checkLocking();
    }).catch(function () { show(0); });
  }

  /* ── navigation ─────────────────────────────────────────────────────────── */

  function buildRail() {
    const rail = document.getElementById('setup-rail');
    rail.innerHTML = STEPS.map(function (s, i) {
      return '<li class="setup-rail-item" data-goto="' + i + '" tabindex="0" ' +
        'role="button"><span class="setup-rail-dot"></span>' +
        '<span class="setup-rail-label">' + s.title + '</span></li>';
    }).join('');
    rail.addEventListener('click', onRail);
    rail.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') onRail(e);
    });
  }

  function onRail(e) {
    const item = e.target.closest('[data-goto]');
    if (item) show(parseInt(item.dataset.goto, 10));
  }

  function show(i) {
    index = Math.max(0, Math.min(STEPS.length - 1, i));
    STEPS.forEach(function (s, n) { s.el.hidden = n !== index; });

    HC.$$('.setup-rail-item').forEach(function (el, n) {
      el.classList.toggle('is-current', n === index);
      el.classList.remove('is-ok', 'is-warn', 'is-bad', 'is-skip');
      const v = verdicts[STEPS[n].key];
      if (v) el.classList.add('is-' + v);
    });

    const last = index === STEPS.length - 1;
    document.getElementById('setup-prev').disabled = index === 0;
    document.getElementById('setup-next').hidden = last;
    document.getElementById('setup-progress').textContent =
      last ? 'Finished' : 'Step ' + (index + 1) + ' of ' + (STEPS.length - 1);

    if (STEPS[index].key === 'done') buildSummary();
    if (STEPS[index].key === 'farm') drawFarmMap();
    window.scrollTo({ top: 0, behavior: 'smooth' });
    saveState();
  }

  function wireNav() {
    document.getElementById('setup-prev')
      .addEventListener('click', function () { show(index - 1); });
    document.getElementById('setup-next')
      .addEventListener('click', function () { show(index + 1); });
  }

  const saveState = HC.debounce(function () {
    HC.api('/api/setup/state', {
      method: 'POST',
      body: {
        step: index,
        done_steps: Object.keys(verdicts).filter(function (k) {
          return verdicts[k] === 'ok';
        })
      }
    }).catch(function () {});
  }, 400);

  /* ── verdicts ───────────────────────────────────────────────────────────── */

  const MARKS = { ok: '✓', warn: '!', bad: '✕', skip: '–' };

  function setVerdict(step, level, message) {
    verdicts[step] = level;
    const el = document.getElementById('v-' + step);
    if (el) {
      el.className = 'setup-verdict is-' + level;
      HC.$('.setup-mark', el).textContent = MARKS[level] || '';
      HC.$('.setup-verdict-text', el).textContent = message;
    }
    const railItem = HC.$$('.setup-rail-item')[STEPS.findIndex(
      function (s) { return s.key === step; })];
    if (railItem) {
      railItem.classList.remove('is-ok', 'is-warn', 'is-bad', 'is-skip');
      railItem.classList.add('is-' + level);
    }
    saveState();
  }

  /* ── painting ───────────────────────────────────────────────────────────── */

  function paintFeeds() {
    if (!feed || !feed.nir) return;
    const key = STEPS[index] ? STEPS[index].key : null;

    if (key === 'filter') {
      feed.paintRaw(document.getElementById('setup-raw'));
      feed.paintChannel(document.getElementById('setup-ch-r'), 'nir', [1, .22, .22]);
      feed.paintChannel(document.getElementById('setup-ch-g'), 'green', [.28, 1, .36]);
      feed.paintChannel(document.getElementById('setup-ch-b'), 'blue', [.3, .42, 1]);
      const ch = feed.channelMeans();
      if (ch) {
        setText('setup-channel-readout',
          'NIR ' + ch.nir.toFixed(0) +
          '  ·  green ' + (ch.green === null ? '?' : ch.green.toFixed(0)) +
          '  ·  blue ' + ch.blue.toFixed(0) +
          (ch.green === null ? '' :
            '   —   green is ' + (ch.green / Math.max(1, ch.blue) * 100).toFixed(0) +
            '% of blue'));
      }
      const label = document.getElementById('setup-feed-label');
      if (label) {
        label.textContent = feed.source === 'synthetic' ? 'SYNTHETIC' : 'LIVE';
      }
    } else if (key === 'exposure' || key === 'leak') {
      feed.paintRaw(document.getElementById('setup-exp-raw'));
      if (picker) picker.redraw();
    } else if (key === 'thresholds') {
      feed.paintFalse(document.getElementById('setup-fc'));
      const s = feed.stats();
      if (s) {
        setText('setup-band-readout',
          'Right now: ' + s.h.toFixed(0) + '% doing well · ' +
          s.m.toFixed(0) + '% keep an eye · ' + s.s.toFixed(0) + '% needs a look');
      }
    } else if (key === 'plant') {
      feed.paintHeat(document.getElementById('setup-plant-heat'));
      const s = feed.stats();
      if (s) {
        setText('setup-plant-mean', HC.fmt(s.mean));
        setText('setup-plant-healthy', s.h.toFixed(0) + '%');
      }
    }
  }

  function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  /* ── per-step logic ─────────────────────────────────────────────────────── */

  function wireSteps() {
    setVerdict('intro', 'ok', 'Nothing to do here — on to the camera.');

    // ── camera
    document.getElementById('setup-retry-camera')
      .addEventListener('click', function () {
        HC.api('/api/camera/restart', { method: 'POST' })
          .then(paintCamera)
          .catch(function (e) { setVerdict('camera', 'bad', e.message); });
      });
    document.getElementById('setup-use-synthetic')
      .addEventListener('click', function () {
        HC.api('/api/camera/synthetic', { method: 'POST', body: { on: true } })
          .then(function (probe) {
            paintCamera(probe);
            show(index + 1);
          })
          .catch(function (e) { HC.toast(e.message, true); });
      });

    // ── filter
    document.getElementById('setup-check-filter')
      .addEventListener('click', function () {
        HC.api('/api/diagnose/filter', { method: 'POST', body: {} })
          .then(function (res) {
            setVerdict('filter',
              res.verdict === 'fitted' ? 'ok'
                : (res.verdict === 'missing' ? 'bad' : 'warn'),
              res.message);
          })
          .catch(function (e) { setVerdict('filter', 'bad', e.message); });
      });

    // ── exposure + region picker (shared with the k step)
    const layer = document.getElementById('setup-roi-layer');
    if (layer) {
      picker = new RegionPicker(layer, document.getElementById('setup-roi-box'),
        function (box) {
          document.getElementById('setup-check-exposure').disabled = !box;
          document.getElementById('setup-solve-k').disabled = !box;
        });
    }
    HC.slider(document.getElementById('setup-exposure'),
      document.getElementById('setup-exposure-label'),
      function (v) { return Math.round(v) + ' µs'; },
      function (v) { HC.saveSetting({ exposure_us: Math.round(v) }); });
    HC.slider(document.getElementById('setup-gain'),
      document.getElementById('setup-gain-label'),
      function (v) { return v.toFixed(1) + '×'; },
      function (v) { HC.saveSetting({ gain: v }); });

    document.getElementById('setup-check-exposure')
      .addEventListener('click', function () {
        if (!picker || !picker.box) return;
        HC.api('/api/diagnose/white-reference',
          { method: 'POST', body: picker.box })
          .then(function (res) {
            setVerdict('exposure',
              res.verdict === 'good' ? 'ok'
                : (res.verdict === 'dim' ? 'warn' : 'bad'),
              res.message);
            const peak = Math.max(res.region.nir_max, res.region.blue_max);
            const fill = document.getElementById('setup-meter-fill');
            if (fill) fill.style.width = (peak / 255 * 100).toFixed(1) + '%';
          })
          .catch(function (e) { setVerdict('exposure', 'bad', e.message); });
      });

    // ── k
    document.getElementById('setup-solve-k')
      .addEventListener('click', function () {
        if (!picker || !picker.box) {
          setVerdict('leak', 'warn',
            'Go back a step and drag a box over the white paper first.');
          return;
        }
        HC.api('/api/calibrate/solve-k',
          { method: 'POST', body: Object.assign({ apply: true }, picker.box) })
          .then(function (res) {
            if (res.k === null) {
              setVerdict('leak', 'bad', res.message);
              setText('setup-k-readout', '');
              return;
            }
            feed.k = res.k;
            feed.correctNir = true;
            setVerdict('leak', 'ok', res.message +
              ' Saved, and the correction is now on.');
            setText('setup-k-readout',
              'NIR ' + res.region.nir.toFixed(0) + ' · blue ' +
              res.region.blue.toFixed(0) + '  →  k = ' + res.k.toFixed(3) +
              (res.k < 0.6 ? '   (below 0.8, as expected for this gel)' : ''));
          })
          .catch(function (e) { setVerdict('leak', 'bad', e.message); });
      });
    document.getElementById('setup-skip-k')
      .addEventListener('click', function () {
        HC.saveSetting({ correct_nir_leakage: false });
        feed.correctNir = false;
        setVerdict('leak', 'skip',
          'Skipped. The index will use the blue channel as-is, which reads a ' +
          'little pessimistic. You can measure k later in Debug.');
      });

    // ── thresholds
    HC.slider(document.getElementById('setup-t-healthy'),
      document.getElementById('setup-th-label'),
      function (v) { return v.toFixed(2); },
      function (v) {
        feed.tHealthy = v;
        feed.compute();
        paintFeeds();
        HC.saveSetting({ threshold_healthy: v });
        setVerdict('thresholds', 'ok', 'Bands set to your values.');
      });
    HC.slider(document.getElementById('setup-t-moderate'),
      document.getElementById('setup-tm-label'),
      function (v) { return v.toFixed(2); },
      function (v) {
        feed.tModerate = v;
        feed.compute();
        paintFeeds();
        HC.saveSetting({ threshold_moderate: v });
        setVerdict('thresholds', 'ok', 'Bands set to your values.');
      });
    setVerdict('thresholds', 'ok',
      'Using ' + cfg.threshold_healthy.toFixed(2) + ' and ' +
      cfg.threshold_moderate.toFixed(2) + ' — fine to leave for now.');

    // ── plant
    document.getElementById('setup-check-plant')
      .addEventListener('click', function () {
        const s = feed.stats();
        if (!s) {
          setVerdict('plant', 'bad', 'No live frame to read.');
          return;
        }
        if (feed.source === 'synthetic') {
          setVerdict('plant', 'skip',
            'These are synthetic frames, so this proves the pipeline works but ' +
            'not your camera. Mean reads ' + HC.fmt(s.mean) + '. Re-run this ' +
            'step at the rig.');
        } else if (s.mean >= 0.3 && s.mean <= 0.75) {
          setVerdict('plant', 'ok',
            'Mean ' + HC.fmt(s.mean) + ' — right in the expected range for ' +
            'healthy vegetation. The whole chain is working.');
        } else if (s.mean > 0.75) {
          setVerdict('plant', 'warn',
            'Mean ' + HC.fmt(s.mean) + ' is higher than real vegetation ' +
            'usually reads. The leakage correction may be over-correcting — try ' +
            'a lower k, or re-measure it on the white paper.');
        } else if (s.mean > 0.1) {
          setVerdict('plant', 'warn',
            'Mean ' + HC.fmt(s.mean) + ' is low for a healthy plant. Check the ' +
            'gel is fitted (step 3), that you are in daylight and not behind ' +
            'glass, and that the leakage correction is on (step 6).');
        } else if (s.mean > -0.1) {
          setVerdict('plant', 'bad',
            'Mean ' + HC.fmt(s.mean) + ' is about zero, which is what bare soil ' +
            'or a non-plant reads. Either this is not vegetation, or the gel is ' +
            'missing.');
        } else {
          setVerdict('plant', 'bad',
            'Mean ' + HC.fmt(s.mean) + ' is strongly negative. That means the ' +
            'channels are the wrong way round — a bug rather than a setting. ' +
            'Worth reporting.');
        }
        setText('setup-plant-readout',
          'Healthy ' + s.h.toFixed(0) + '% · keep an eye ' + s.m.toFixed(0) +
          '% · needs a look ' + s.s.toFixed(0) + '%');
      });

    document.getElementById('setup-save-frame')
      .addEventListener('click', function (e) {
        const btn = e.currentTarget;
        btn.disabled = true;
        HC.api('/api/captures', {
          method: 'POST',
          body: { from_preview: true, label: 'Setup check' }
        }).then(function (rec) {
          HC.toast('Saved as capture ' + rec.id +
            ' — mean ' + HC.fmt(rec.stats.mean));
        }).catch(function (err) {
          HC.toast('Could not save: ' + err.message, true);
        }).finally(function () { btn.disabled = false; });
      });

    // ── farm
    setVerdict('farm', 'skip', 'Optional — you can fly later.');

    document.getElementById('setup-finish')
      .addEventListener('click', function () {
        HC.api('/api/setup/state',
          { method: 'POST', body: { completed: true, step: index } })
          .then(function () { window.location.href = '/debug'; })
          .catch(function (e) { HC.toast(e.message, true); });
      });
  }

  function checkCamera() {
    HC.api('/api/camera/status?force=true').then(paintCamera)
      .catch(function (e) { setVerdict('camera', 'bad', e.message); });
  }

  function paintCamera(probe) {
    if (probe.available) {
      setVerdict('camera', 'ok', 'Camera found — ' + (probe.detail || 'ready') + '.');
    } else if (probe.synthetic) {
      setVerdict('camera', 'skip',
        'No camera attached, so the dashboard is generating test frames. ' +
        'Everything below still works, but the readings are not from your rig.');
    } else {
      setVerdict('camera', 'bad',
        'Not detected — ' + (probe.detail || 'unknown') +
        '. Power down, reseat the ribbon cable at both ends, and check again.');
    }
  }

  function checkLocking() {
    HC.api('/api/camera/status').then(function (probe) {
      if (probe.synthetic) {
        setVerdict('locking', 'skip',
          'No camera attached, so there is nothing to lock yet. This will check ' +
          'itself once the camera is connected.');
        setText('setup-lock-readout',
          'Exposure, gain, white balance, sharpening, denoise and the colour ' +
          'matrix are all pinned in software — the check runs against the ' +
          'camera’s own metadata on every frame.');
        return;
      }
      if (feed && feed.mismatch) {
        setVerdict('locking', 'bad',
          'The camera is not honouring the locked settings: ' + feed.mismatch);
        return;
      }
      setVerdict('locking', 'ok',
        'The camera is reporting back the settings we asked for on every frame.');
      setText('setup-lock-readout',
        'Exposure and gain pinned, white balance pinned, sharpening and denoise ' +
        'off, colour matrix neutralised.');
    }).catch(function (e) { setVerdict('locking', 'warn', e.message); });
  }

  function drawFarmMap() {
    const host = document.getElementById('setup-map');
    if (!host || !window.L || host._done) return;
    host._done = true;
    const box = [[cfg.plot_lat - 0.003, cfg.plot_lon - 0.003],
                 [cfg.plot_lat + 0.003, cfg.plot_lon + 0.003]];
    const map = L.map(host, {
      zoomControl: false, attributionControl: false, scrollWheelZoom: false,
      dragging: false
    });
    L.tileLayer('/tiles/{z}/{x}/{y}.jpg',
      { minZoom: 12, maxZoom: 21, minNativeZoom: 16, maxNativeZoom: 19 }).addTo(map);
    map.fitBounds(box);
    setTimeout(function () { map.invalidateSize(); }, 60);
  }

  /* ── summary ────────────────────────────────────────────────────────────── */

  function buildSummary() {
    const rows = STEPS.filter(function (s) {
      return s.key !== 'done' && s.key !== 'intro';
    }).map(function (s) {
      const v = verdicts[s.key] || 'warn';
      const word = { ok: 'done', warn: 'needs a look', bad: 'not right yet',
                     skip: 'skipped' }[v];
      return '<div class="kv-row"><span>' + s.title + '</span>' +
        '<span class="setup-summary-' + v + '">' + word + '</span></div>';
    }).join('');
    const el = document.getElementById('setup-summary');
    if (el) el.innerHTML = rows;

    const bad = STEPS.filter(function (s) { return verdicts[s.key] === 'bad'; });
    const note = document.getElementById('setup-summary-note');
    if (!note) return;
    if (bad.length) {
      note.textContent = 'Still to fix: ' +
        bad.map(function (s) { return s.title.toLowerCase(); }).join(', ') +
        '. Until those pass, treat the numbers as provisional.';
    } else if (feed && feed.source === 'synthetic') {
      note.textContent = 'You went through this on synthetic frames, which proves ' +
        'the pipeline works but not your camera. Run it again at the rig — it ' +
        'takes about ten minutes.';
    } else {
      note.textContent = 'Everything checked out. The one thing left that this ' +
        'page cannot do for you is deriving thresholds for dragon fruit ' +
        'specifically — that needs plants you have judged by eye. See ' +
        'RESEARCH-GAPS.md §5.';
    }
  }

  document.addEventListener('DOMContentLoaded', init);
}());
