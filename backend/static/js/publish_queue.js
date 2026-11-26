/**
 * publish_queue.js - the "Next releases" list: every clip waiting for its YouTube release, in order.
 *
 * Only the first few rows show; "Show all" opens the rest. It re-reads the list every minute. The
 * rows are only rebuilt when the order, a time or the expanded state changes, so the buttons stay
 * where they are under the pointer (the same reason the clip list is keyed).
 */

import { escapeHtml, fetchJson } from './util.js';

const $ = (id) => document.getElementById(id);
const REFRESH_MS = 60000;
const PREVIEW_ROWS = 3;

let expanded = false;
let lastItems = [];
let lastGap = null;

// This computer's clock and time zone, e.g. "Thu 20:15".
const LOCAL = new Intl.DateTimeFormat(undefined, {
  weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
});

function local(iso) {
  if (!iso) return '-';
  return LOCAL.format(new Date(iso));
}

function describeGap(seconds) {
  if (seconds && seconds % 3600 === 0) return `${seconds / 3600} h`;
  return `${Math.round((seconds || 0) / 60)} min`;
}

function rowHtml(it, n) {
  return `
    <div class="pq-row">
      <span class="pq-pos">${n + 1}</span>
      <div class="pq-info"><strong>${escapeHtml(it.title)}</strong><small>Job ${it.job_id} · clip ${it.index}</small></div>
      <div class="pq-times"><span>Goes up ~${local(it.expected_upload_at || it.upload_at)}</span><small>Public ${local(it.expected_release_at || it.release_at)}</small></div>
    </div>`;
}

function render(items, gap) {
  const box = $('pqList');
  lastItems = items;
  lastGap = gap;
  $('pqGap').textContent = `One upload every ${describeGap(gap)}, in the order below. `
    + 'Times are your local time and can run up to 10 minutes later.';
  const shown = expanded ? items : items.slice(0, PREVIEW_ROWS);
  const sig = JSON.stringify([expanded, items.map((i) => [i.id, i.expected_release_at, i.expected_upload_at])]);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  if (!items.length) { box.innerHTML = '<em>Nothing waiting</em>'; return; }
  let html = shown.map((it, n) => rowHtml(it, n)).join('');
  if (items.length > PREVIEW_ROWS) {
    const label = expanded ? 'Show fewer' : `Show all (${items.length})`;
    html += `<button type="button" class="ghost-btn pq-toggle" data-pq-toggle>${label}</button>`;
  }
  box.innerHTML = html;
}

async function refresh() {
  try {
    const data = await fetchJson('/api/publish/queue');
    $('pqErr').textContent = '';
    render(data.items || [], data.upload_gap_seconds || data.gap_seconds);
  } catch (err) {
    $('pqErr').textContent = err.message;
  }
}

export function setupPublishQueue() {
  $('pqList').addEventListener('click', (e) => {
    if (!e.target.closest('[data-pq-toggle]')) return;
    expanded = !expanded;
    render(lastItems, lastGap);
  });
  refresh();
  setInterval(refresh, REFRESH_MS);
  $('pqRefresh').addEventListener('click', refresh);
}
