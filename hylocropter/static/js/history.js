/* History filtering: client-side, because the whole list is already on the page
 * and a round-trip per keystroke would be slower and pointless. */
(function () {
  'use strict';

  const search = document.getElementById('history-search');
  const list = document.getElementById('history-list');
  if (!list) return;

  const rows = HC.$$('.history-row', list);
  const total = rows.length;
  const countEl = document.getElementById('history-count');
  const emptyEl = document.getElementById('history-empty');
  let block = 'All';

  function apply() {
    const q = (search ? search.value : '').trim().toLowerCase();
    let shown = 0;
    rows.forEach(function (row) {
      const matchesBlock = block === 'All' || row.dataset.name === block;
      const matchesQuery = !q || row.dataset.search.indexOf(q) !== -1;
      const visible = matchesBlock && matchesQuery;
      row.hidden = !visible;
      if (visible) shown++;
    });

    // Hide a month heading whose flights are all filtered out.
    HC.$$('.history-group', list).forEach(function (group) {
      const any = HC.$$('.history-row', group).some(function (r) { return !r.hidden; });
      group.hidden = !any;
    });

    if (countEl) {
      countEl.textContent = shown === total
        ? total + ' flight' + (total === 1 ? '' : 's') + ' on record, newest first'
        : shown + ' of ' + total + ' flights';
    }
    if (emptyEl) emptyEl.hidden = shown !== 0;
    if (list) list.hidden = shown === 0;
  }

  if (search) search.addEventListener('input', apply);

  HC.$$('#block-filters [data-block]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      block = btn.dataset.block;
      HC.$$('#block-filters [data-block]').forEach(function (b) {
        b.classList.toggle('is-active', b === btn);
      });
      apply();
    });
  });
}());
