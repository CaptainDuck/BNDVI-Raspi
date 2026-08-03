/* Capture detail: edit the label/notes, delete the capture. */
(function () {
  'use strict';

  const form = document.getElementById('meta-form');
  if (form) {
    form.addEventListener('submit', async function (e) {
      e.preventDefault();
      const btn = form.querySelector('button[type="submit"]');
      const original = btn.textContent;
      btn.disabled = true;
      btn.textContent = 'Saving…';
      try {
        await HC.api('/api/captures/' + form.dataset.id, {
          method: 'PATCH',
          body: {
            label: form.elements.label.value || null,
            notes: form.elements.notes.value || null
          }
        });
        btn.textContent = 'Saved ✓';
      } catch (err) {
        HC.toast('Could not save: ' + err.message, true);
        btn.textContent = original;
      } finally {
        btn.disabled = false;
        setTimeout(function () { btn.textContent = original; }, 1200);
      }
    });
  }

  const del = document.getElementById('delete-capture');
  if (del) {
    del.addEventListener('click', async function () {
      const ok = await HC.confirmDialog({
        title: 'Delete this capture?',
        body: 'The photos and the measurements both go. If this capture belongs ' +
          'to a flight, that flight\'s numbers will no longer include it. This ' +
          'cannot be undone.',
        action: 'Delete it'
      });
      if (!ok) return;
      try {
        await HC.api('/api/captures/' + del.dataset.id, { method: 'DELETE' });
        window.location.href = '/';
      } catch (err) {
        HC.toast('Delete failed: ' + err.message, true);
      }
    });
  }
}());
