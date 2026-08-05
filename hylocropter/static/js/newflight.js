/* New flight: arm the camera, then watch the flight controller.
 *
 * This page never commands the aircraft. Mission Planner arms and flies it; all
 * this does is open a flight record so incoming CAMERA_TRIGGER messages get
 * saved, and close it when the drone disarms.
 */
(function () {
  'use strict';

  const root = document.getElementById('newflight-root');
  if (!root) return;
  const recording = root.dataset.recording;

  const TRIGGER_NOTES = {
    distance: 'The Pi watches GPS and takes a photo every few metres along the ' +
      'mission line. Best for even coverage.',
    waypoint: 'One photo at every waypoint in the Mission Planner file. Fewer ' +
      'photos, tied to your plan.',
    interval: 'A steady timer, ignoring position. Simplest, but coverage depends ' +
      'on how fast you fly.'
  };

  function paintTriggerNote() {
    const note = document.getElementById('trigger-note');
    const checked = HC.$('[name="trigger"]:checked');
    if (note && checked) note.textContent = TRIGGER_NOTES[checked.value] || '';
  }
  HC.$$('[name="trigger"]').forEach(function (radio) {
    radio.addEventListener('change', function () {
      paintTriggerNote();
      HC.saveSetting({ trigger_mode: radio.value });
    });
  });
  paintTriggerNote();

  const override = document.getElementById('override-trigger');
  if (override) {
    override.addEventListener('click', function () {
      HC.api('/api/settings', {
        method: 'PATCH', body: { trigger_source: 'dashboard' }
      }).then(function () { location.reload(); })
        .catch(function (err) { HC.toast(err.message, true); });
    });
  }

  const useMission = document.getElementById('use-mission-trigger');
  if (useMission) {
    useMission.addEventListener('click', function () {
      HC.api('/api/settings', {
        method: 'PATCH', body: { trigger_source: 'mission' }
      }).then(function () { location.reload(); })
        .catch(function (err) { HC.toast(err.message, true); });
    });
  }

  const start = document.getElementById('start-session');
  if (start) {
    start.addEventListener('click', async function () {
      start.disabled = true;
      start.textContent = 'Getting ready…';
      try {
        await HC.api('/api/flights', { method: 'POST' });
        location.reload();
      } catch (err) {
        HC.toast(err.message, true);
        start.disabled = false;
        start.textContent = 'Get the camera ready';
      }
    });
  }

  const cancel = document.getElementById('cancel-session');
  if (cancel) {
    cancel.addEventListener('click', async function () {
      const ok = await HC.confirmDialog({
        title: 'Cancel this flight?',
        body: 'Photos already saved are kept and the flight is closed. If nothing ' +
          'has been photographed yet, the empty flight is removed.',
        action: 'Cancel the flight'
      });
      if (!ok) return;
      try {
        await HC.api('/api/flights/' + recording + '/cancel', { method: 'POST' });
        window.location.href = '/';
      } catch (err) { HC.toast(err.message, true); }
    });
  }

  const finish = document.getElementById('finish-session');
  if (finish) {
    finish.addEventListener('click', async function () {
      finish.disabled = true;
      try {
        await HC.api('/api/flights/' + recording + '/process', { method: 'POST' });
        window.location.href = '/processing';
      } catch (err) {
        HC.toast(err.message, true);
        finish.disabled = false;
      }
    });
  }

  /* ── mission planner ─────────────────────────────────────────────────────
     Recomputes the two Mission Planner numbers from the lens geometry as you
     move the sliders. Server-side so the maths lives in one place (flights.py)
     rather than being duplicated here and drifting. */

  const planner = document.getElementById('mission-planner');
  if (planner) {
    const alt = document.getElementById('mp-alt');
    const fwd = document.getElementById('mp-fwd');
    const side = document.getElementById('mp-side');
    const plot = document.getElementById('mp-plot');

    function paintPlan(p) {
      setText('mp-trigger', p.trigger_distance_m + ' m');
      setText('mp-spacing', p.line_spacing_m + ' m');
      setText('mp-footprint', p.footprint_w_m + ' × ' + p.footprint_h_m + ' m');
      setText('mp-gsd', p.gsd_cm ? p.gsd_cm + ' cm per pixel' : '—');
      setText('mp-photos', p.photos + ' over ' + p.plot_area_ha + ' ha');
      setText('mp-lines', p.lines + ' lines, ' + p.photos_per_line + ' photos each');
      setText('mp-time', p.minutes + ' min at ' + p.speed_ms + ' m/s');
      setText('mp-storage', p.storage_mb >= 1024
        ? (p.storage_mb / 1024).toFixed(1) + ' GB'
        : p.storage_mb + ' MB');
      setText('mp-speed', p.speed_ms + ' m/s');

      const box = document.getElementById('mp-warnings');
      if (box) {
        box.innerHTML = (p.warnings || []).map(function (w) {
          return '<div class="mp-warning">' + esc(w) + '</div>';
        }).join('');
      }
    }

    const refreshPlan = HC.debounce(function () {
      const params = new URLSearchParams({
        altitude: alt.value,
        forward: (parseFloat(fwd.value) / 100).toFixed(2),
        side: (parseFloat(side.value) / 100).toFixed(2),
        plot_side: plot.value
      });
      HC.api('/api/mission/plan?' + params).then(paintPlan)
        .catch(function () {});
    }, 200);

    HC.slider(alt, document.getElementById('mp-alt-label'),
      function (v) { return Math.round(v) + ' m'; }, refreshPlan);
    HC.slider(fwd, document.getElementById('mp-fwd-label'),
      function (v) { return Math.round(v) + '%'; }, refreshPlan);
    HC.slider(side, document.getElementById('mp-side-label'),
      function (v) { return Math.round(v) + '%'; }, refreshPlan);
    // Block size is one number in two places — here and the picker on the map —
    // so editing it here persists rather than diverging from what the map draws.
    if (plot) {
      plot.addEventListener('input', function () {
        refreshPlan();
        const v = parseInt(plot.value, 10);
        if (v >= 10) HC.saveSetting({ survey_side_m: v });
      });
    }

    // Render the plan the server already worked out, then let the sliders take over.
    try { paintPlan(JSON.parse(planner.dataset.plan)); } catch (e) { refreshPlan(); }
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  /* ── live telemetry while armed ──────────────────────────────────────────── */

  if (recording) {
    let wasArmed = null;
    HC.poll(function () {
      return HC.api('/api/telemetry').then(function (snap) {
        setText('tel-mode', snap.mode || (snap.connected ? '—' : 'not connected'));
        setText('tel-wp', snap.mission.count
          ? (snap.mission.current + ' of ' + snap.mission.count)
          : '—');
        setText('tel-alt', snap.position
          ? snap.position.rel_alt_m.toFixed(1) + ' m'
          : '—');
        setText('tel-gps', snap.gps.fix_type >= 2
          ? snap.gps.fix_label + ', ' + snap.gps.satellites + ' sats'
          : (snap.connected ? 'no fix' : '—'));

        const title = document.getElementById('session-title');
        const body = document.getElementById('session-body');
        if (snap.armed && title) {
          title.textContent = 'Recording photos';
          if (body) {
            body.textContent = 'Leave this page open — it closes the flight by ' +
              'itself when the drone disarms.';
          }
        }

        // The flight closes server-side on disarm; follow it to the processing
        // page so the operator sees the progress rather than a stale screen.
        if (wasArmed === true && snap.armed === false) {
          window.location.href = '/processing';
          return false;
        }
        wasArmed = snap.armed;

        if (!snap.recording_flight) {
          window.location.href = '/processing';
          return false;
        }
        return true;
      });
    }, 2000);

    HC.poll(function () {
      return HC.api('/api/flights/' + recording).then(function (f) {
        setText('tel-photos', f.capture_count);
      });
    }, 4000);
  }

  function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }
}());
