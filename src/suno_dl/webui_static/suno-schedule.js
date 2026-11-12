// The backend owns scheduling; pausing never cancels an in-flight download.
const panel = document.getElementById('automatic-sync');
const enabled = document.getElementById('schedule-enabled');
const interval = document.getElementById('schedule-interval');
const sources = document.getElementById('schedule-sources');
const status = document.getElementById('schedule-status');
const save = document.getElementById('schedule-save');
let dirty = false;
for (const field of [enabled, interval, sources]) {
  field.addEventListener('input', () => {
    dirty = true; status.textContent = 'Unsaved changes. Select Save schedule to apply.';
  });
}
function when(value) {
  return value ? new Date(value * 1000).toLocaleString() : 'not scheduled';
}
async function refresh() {
  try {
    const response = await fetch('api/schedule');
    const data = await response.json();
    if (!response.ok || !data.available) return;
    panel.hidden = false;
    if (!dirty && document.activeElement !== interval && document.activeElement !== sources) {
      enabled.checked = data.enabled;
      interval.value = data.interval_minutes / 60;
      sources.value = data.sources.join('\n');
    }
    const run = data.last_run;
    const active = run && run.returncode === null;
    if (dirty) return;
    status.textContent = data.notice || (
      (data.enabled ? active
        ? 'Next check: ' + (data.interval_minutes / 60) + ' hours after this sync finishes.'
        : 'Next check: ' + when(data.next_run) + '.' : 'Automatic syncing is paused.') +
      (active ? ' A sync is running; it will finish normally.' :
        run ? ' Last sync: ' + run.state + ', ' + when(run.finished || run.started) + '.' : '')
    );
  } catch (_) {
    if (!panel.hidden) status.textContent = 'Cannot reach the Trackie backend. Settings were not changed.';
  }
}
save.addEventListener('click', async () => {
  save.disabled = true;
  status.textContent = 'Saving schedule…';
  try {
    const response = await fetch('api/schedule', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        enabled: enabled.checked,
        interval_minutes: Number(interval.value) * 60,
        sources: sources.value.split(/\r?\n/).map(x => x.trim()).filter(Boolean)
      })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Schedule could not be saved');
    dirty = false;
    await refresh();
    status.textContent += ' Saved.';
  } catch (error) {
    status.textContent = 'Could not confirm the save: ' + error.message + '. Refresh settings before trying again.';
  } finally { save.disabled = false; }
});
refresh();
setInterval(refresh, 5000);
