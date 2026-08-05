/* Mission planning calculator.
 *
 * Shared by /plan (working the numbers out at home, nowhere near the farm) and
 * /new-flight (checking them minutes before take-off). One file because the maths
 * and the Mission Planner instructions are identical -- the pages differ only in
 * framing.
 *
 * The arithmetic itself lives in flights.mission_plan() on the server, not here.
 * Duplicating trigonometry into the browser is how the two ended up disagreeing
 * about footprints the first time round.
 */
(function () {
  'use strict';

  function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
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
    const plotW = document.getElementById('mp-plot-w');
    const plotH = document.getElementById('mp-plot-h');
    const blockPick = document.getElementById('mp-block');

    function paintPlan(p) {
      setText('mp-trigger', p.trigger_distance_m + ' m');
      setText('mp-spacing', p.line_spacing_m + ' m');
      setText('mp-footprint', p.footprint_w_m + ' × ' + p.footprint_h_m + ' m');
      setText('mp-gsd', p.gsd_cm ? p.gsd_cm + ' cm per pixel' : '—');
      setText('mp-photos', p.photos + ' over ' + p.plot_area_ha + ' ha (' +
        p.plot_w_m + ' × ' + p.plot_h_m + ' m)');
      setText('mp-lines', p.lines + ' lines, ' + p.photos_per_line + ' photos each');
      // Which way round matters on a rectangle: the long axis means fewer turns.
      setText('mp-direction', p.line_direction +
        ', along the longer side');
      setText('mp-direction-2', p.line_direction);
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
        plot_w: plotW ? plotW.value : '',
        plot_h: plotH ? plotH.value : ''
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
    if (plotW) plotW.addEventListener('input', refreshPlan);
    if (plotH) plotH.addEventListener('input', refreshPlan);

    /* What are we flying over? Three kinds of answer, all legitimate:
       a drawn block (real ground), a practice area (a size you can rehearse over
       at school), or a size you type. Choosing one fills in the dimensions; they
       stay editable, and editing them switches the picker to "Type the size
       myself" rather than silently contradicting the named thing above. */
    const areas = [];
    try {
      JSON.parse(planner.dataset.areas || '[]').forEach(function (b) {
        areas.push({ id: b.id, w: Math.round(b.width_m), h: Math.round(b.height_m),
                     note: '' });
      });
      JSON.parse(planner.dataset.tests || '[]').forEach(function (a) {
        areas.push({ id: a.id, w: a.w, h: a.h, note: a.note || '' });
      });
    } catch (e) { /* a mangled attribute must not take the sliders down */ }

    function areaById(id) {
      return areas.find(function (a) { return a.id === id; });
    }

    function paintAreaNote() {
      const a = blockPick && areaById(blockPick.value);
      setText('mp-block-note', a ? a.note : (blockPick && blockPick.value === ''
        ? 'Not tied to any ground — just a size.' : ''));
    }

    if (blockPick) {
      blockPick.addEventListener('change', function () {
        const a = areaById(blockPick.value);
        paintAreaNote();
        if (!a) return;
        if (plotW) plotW.value = a.w;
        if (plotH) plotH.value = a.h;
        refreshPlan();
        remember();
      });
      [plotW, plotH].forEach(function (el) {
        if (!el) return;
        el.addEventListener('input', function () {
          const a = areaById(blockPick.value);
          if (a && (a.w !== parseInt(plotW.value, 10) ||
                    a.h !== parseInt(plotH.value, 10))) {
            blockPick.value = '';
            paintAreaNote();
          }
        });
      });
    }

    /* Remember the last plan. Someone working out numbers at home will come back
       to this page repeatedly, and re-entering four values each time is the sort
       of friction that makes people stop checking. UI-only state, so localStorage
       rather than the server -- it is not a setting the Pi acts on. */
    const KEY = 'hc.plan';

    function remember() {
      try {
        localStorage.setItem(KEY, JSON.stringify({
          alt: alt.value, fwd: fwd.value, side: side.value,
          w: plotW && plotW.value, h: plotH && plotH.value,
          area: blockPick && blockPick.value
        }));
      } catch (e) { /* private browsing, or a full quota. Not worth a message. */ }
    }

    function restore() {
      let saved = null;
      try { saved = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (e) {}
      if (!saved) return false;
      if (saved.alt) alt.value = saved.alt;
      if (saved.fwd) fwd.value = saved.fwd;
      if (saved.side) side.value = saved.side;
      if (saved.w && plotW) plotW.value = saved.w;
      if (saved.h && plotH) plotH.value = saved.h;
      // Only restore the area if it still exists — a deleted block must not come
      // back as a selection pointing at nothing.
      if (blockPick && (saved.area === '' || areaById(saved.area))) {
        blockPick.value = saved.area;
      }
      return true;
    }

    [alt, fwd, side, plotW, plotH].forEach(function (el) {
      if (el) el.addEventListener('input', remember);
    });

    /* Paint the plan the server already worked out so the page is useful before
       any JS runs, then re-plan if we restored a different set of values. */
    try { paintPlan(JSON.parse(planner.dataset.plan)); } catch (e) { refreshPlan(); }
    paintAreaNote();
    if (restore()) {
      // repaint the slider labels for the restored values
      ['mp-alt', 'mp-fwd', 'mp-side'].forEach(function (id) {
        const el = document.getElementById(id);
        if (el) el.dispatchEvent(new Event('input'));
      });
      refreshPlan();
    }
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

}());
