/**
 * publish_queue.js - the "Next releases" list: every clip waiting for its YouTube release, in order.
 *
 * It re-reads the list every minute. The rows are only rebuilt when the order or a time changes,
 * so the buttons stay where they are under the pointer (the same reason the clip list is keyed).
 */

import { escapeHtml, fetchJson } from './util.js';

const $ = (id) => document.getElementById(id);
const REFRESH_MS = 60000;

function utc(iso) {
  if (!iso) return '-';
  return new Date(iso).toISOString().replace('T', ' ').slice(0, 16) + ' UTC';
}

function describeGap(seconds) {
  if (seconds && seconds % 3600 === 0) return `${seconds / 3600} h`;
  return `${Math.round((seconds || 0) / 60)} min`;
}

function render(items, gap) {
  const box = $('pqList');
  $('pqGap').textContent = `One release every ${describeGap(gap)}, in the order below.`;
  const sig = JSON.stringify(items.map((i) => [i.id, i.release_at, i.upload_at]));
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  if (!items.length) { box.innerHTML = '<em>Nothing waiting</em>'; return; }
  box.innerHTML = items.map((it, n) => `
    <div class="pq-row">
      <span class="pq-pos">${n + 1}</span>
      <div class="pq-info"><strong>${escapeHtml(it.title)}</strong><small>Job ${it.job_id} · clip ${it.index}</small></div>
      <div class="pq-times"><span>Release ${utc(it.release_at)}</span><small>Upload ${utc(it.upload_at)}</small></div>
    </div>`).join('');
}

async function refresh() {
  try {
    const data = await fetchJson('/api/publish/queue');
    $('pqErr').textContent = '';
    render(data.items || [], data.gap_seconds);
  } catch (err) {
    $('pqErr').textContent = err.message;
  }
}

export function setupPublishQueue() {
  refresh();
  setInterval(refresh, REFRESH_MS);
  $('pqRefresh').addEventListener('click', refresh);
}
