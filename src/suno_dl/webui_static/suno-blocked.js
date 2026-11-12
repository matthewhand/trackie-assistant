const list = document.getElementById('skip-entries');
const count = document.getElementById('skip-count');
const status = document.getElementById('skip-status');
const editor = document.getElementById('skip-editor');
const edit = document.getElementById('skip-edit');
const cancel = document.getElementById('skip-cancel');
const rows = document.getElementById('skip-rows');
const template = document.getElementById('skip-row-template');
const add = document.getElementById('skip-add');
const songBase = 'https://suno.com/song/';
const failedPanel = document.getElementById('failed-downloads');
const failedList = document.getElementById('failed-track-entries');
let recentFailures = {failures: [], blocked: []};
let failureKey = '';
let current;
const lookups = new WeakMap();

function songLink(value) {
  const url = new URL(value);
  const song = url.pathname.match(/^\/song\/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\/?$/i);
  const share = /^\/s\/[A-Za-z0-9_-]{8,64}\/?$/.test(url.pathname);
  if (url.origin !== 'https://suno.com' || url.username || url.password || !(song || share)) {
    throw new Error('Paste a Suno song or Share link.');
  }
  return song ? song[1].toLowerCase() : null;
}

function lookupRow(row) {
  const link = row.querySelector('[name="url"]');
  const title = row.querySelector('[name="title"]');
  const message = row.querySelector('[data-lookup-status]');
  const value = link.value;
  const previous = lookups.get(row);
  if (previous?.value === value) return previous.promise;
  const stillCurrent = () => row.isConnected && !editor.hidden && link.value === value;
  message.textContent = 'Looking up title…';
  const promise = (async () => {
    try {
      const response = await fetch('api/blocked', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({lookup: value})
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Title lookup failed.');
      if (!stillCurrent()) return null;
      link.value = data.url;
      if (!title.value.trim() && data.title) {
        title.value = data.title;
        row.dataset.suggestedTitle = title.value;
      }
      message.textContent = data.title ? '' : 'Title unavailable. You can enter it manually.';
      return data.id;
    } catch (error) {
      if (stillCurrent()) message.textContent = error.message;
      if (lookups.get(row)?.value === value) lookups.delete(row);
      return null;
    }
  })();
  lookups.set(row, {value, promise});
  return promise;
}

function show(data) {
  current = data;
  count.textContent = '(' + data.entries.length + ')';
  list.replaceChildren(...data.entries.map(entry => {
    const item = document.createElement('li');
    const title = document.createElement('a');
    title.href = songBase + encodeURIComponent(entry.id);
    title.target = '_blank';
    title.rel = 'noopener noreferrer';
    title.textContent = entry.title;
    item.append(title, ' — ' + entry.reason);
    return item;
  }));
  status.textContent = data.entries.length ? '' : 'No tracks are on the skip list.';
  edit.disabled = false;
  showFailures();
}

async function load() {
  try {
    const response = await fetch('api/blocked', {cache: 'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not load the skip list.');
    show(data);
  } catch (error) {
    status.textContent = error.message;
  }
}

function addRow(entry) {
  const row = template.content.firstElementChild.cloneNode(true);
  const link = row.querySelector('[name="url"]');
  if (entry) {
    link.value = songBase + entry.id;
    row.querySelector('[name="title"]').value = entry.title === entry.id ? '' : entry.title;
    row.querySelector('[name="reason"]').value = entry.reason;
  }
  const title = row.querySelector('[name="title"]');
  let timer;
  link.addEventListener('input', () => {
    clearTimeout(timer);
    lookups.delete(row);
    row.dataset.lookupChanged = 'true';
    if (title.value === row.dataset.suggestedTitle) title.value = '';
    delete row.dataset.suggestedTitle;
    row.querySelector('[data-lookup-status]').textContent = '';
    try { songLink(link.value); } catch (_) { return; }
    timer = setTimeout(() => {
      if (row.isConnected && !editor.hidden && !link.disabled) lookupRow(row);
    }, 350);
  });
  row.querySelector('button').addEventListener('click', () => {
    row.remove();
    add.focus();
  });
  rows.append(row);
  return link;
}

add.addEventListener('click', () => addRow().focus());

function openEditor() {
  rows.replaceChildren();
  current.entries.forEach(addRow);
  editor.hidden = false;
  edit.hidden = true;
  list.hidden = true;
  (rows.querySelector('input') || add).focus();
}

edit.addEventListener('click', openEditor);

function draftFailure(failure) {
  if (!current || editor.querySelector('[type="submit"]').disabled) return;
  document.getElementById('skip-list').open = true;
  if (editor.hidden) openEditor();
  const existing = [...rows.querySelectorAll('[name="url"]')]
    .find(input => input.value === songBase + failure.id);
  if (existing) {
    existing.focus();
    status.textContent = 'This track is already in the draft skip list.';
    return;
  }
  const link = addRow(failure);
  const row = link.closest('fieldset');
  row.querySelector('[name="reason"]').focus();
  status.textContent = 'Review this track, then select Save skip list.';
  if (!failure.title) lookupRow(row);
}

function showFailures() {
  const {failures, blocked} = recentFailures;
  const key = JSON.stringify([failures, blocked, !!current]);
  if (key === failureKey) return;
  failureKey = key;
  failedPanel.hidden = failures.length === 0;
  const retryAll = document.getElementById('retry-all');
  const eligible = failures.filter(track => !blocked.includes(track.id)).length;
  retryAll.hidden = eligible === 0;
  retryAll.dataset.count = String(eligible);
  retryAll.textContent = `Retry all (${eligible} failed ${eligible === 1 ? 'track' : 'tracks'})`;
  failedList.replaceChildren(...failures.map(failure => {
    const item = document.createElement('li');
    const title = document.createElement('a');
    title.href = songBase + encodeURIComponent(failure.id);
    title.target = '_blank';
    title.rel = 'noopener noreferrer';
    title.textContent = failure.title || 'Suno track';
    item.append(title, ' — ' + failure.reason + ' ');
    if (blocked.includes(failure.id)) {
      item.append('On skip list');
    } else {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'act secondary';
      button.textContent = 'Skip track';
      button.disabled = !current;
      button.addEventListener('click', () => draftFailure(failure));
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'act secondary';
      retry.textContent = 'Retry track';
      retry.dataset.retryTrack = failure.id;
      retry.disabled = failedList.dataset.retryLocked !== 'false';
      item.append(button, ' ', retry);
    }
    return item;
  }));
}

document.addEventListener('trackie-sync-failures', event => {
  recentFailures = event.detail;
  showFailures();
});

async function entriesFromForm() {
  const entries = new Map();
  for (const row of rows.children) {
    const link = row.querySelector('[name="url"]');
    const title = row.querySelector('[name="title"]');
    let id = songLink(link.value);
    if (!id || (row.dataset.lookupChanged && !title.value.trim())) {
      const resolved = await lookupRow(row);
      id = resolved || id;
    }
    if (!id) throw new Error('Resolve the Share link before saving.');
    if (entries.has(id)) {
      link.focus();
      throw new Error('This song is already in the skip list.');
    }
    entries.set(id, {
      title: row.querySelector('[name="title"]').value.trim(),
      reason: row.querySelector('[name="reason"]').value.trim(),
    });
  }
  return entries;
}

function updatedText(entries) {
  const original = new Map(current.entries.map(entry => [entry.id, entry]));
  const lines = current.text.split(/\r?\n/);
  const emitted = new Set();
  const format = (id, fields) => id + ' # trackie: ' + JSON.stringify(fields)
    .replace(/[\u0085\u2028\u2029]/g, char => '\\u' + char.charCodeAt(0).toString(16).padStart(4, '0'));
  for (let i = 0; i < lines.length; i++) {
    const id = lines[i].split('#', 1)[0].trim().toLowerCase();
    const before = original.get(id);
    if (!before) continue;
    const after = entries.get(id);
    if (after && (after.title || id) === before.title
        && (after.reason || 'Manually skipped') === before.reason) continue;
    const inlineNote = lines[i].slice(lines[i].indexOf('#') + 1).trim();
    const usesPreviousNote = !lines[i].includes('#') || !inlineNote;
    if (i && usesPreviousNote && /^\s*#.* \| /.test(lines[i - 1] || '')) lines[i - 1] = null;
    lines[i] = after && !emitted.has(id) ? format(id, after) : null;
    emitted.add(id);
  }
  const result = lines.filter(line => line !== null).join('\n');
  const added = [...entries].filter(([id]) => !original.has(id));
  if (!added.length) return result;
  const separator = result && !result.endsWith('\n') ? '\n' : '';
  return result + separator + added.map(([id, fields]) => format(id, fields)).join('\n') + '\n';
}

function close() {
  editor.hidden = true;
  edit.hidden = false;
  list.hidden = false;
  edit.focus();
}

cancel.addEventListener('click', async () => {
  close();
  await load();
});

editor.addEventListener('submit', async event => {
  event.preventDefault();
  const controls = editor.querySelectorAll('input, button');
  controls.forEach(control => { control.disabled = true; });
  try {
    const body = updatedText(await entriesFromForm());
    const response = await fetch('api/blocked', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text: body, revision: current.revision})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not save the skip list.');
    show(data);
    close();
    status.textContent = 'Skip list saved. Changes apply to the next sync.';
  } catch (error) {
    status.textContent = error.message;
  } finally {
    controls.forEach(control => { control.disabled = false; });
  }
});

load();
