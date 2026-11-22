/**
 * timeline_view.js - "How this clip was edited": the edit record of one clip, drawn into the
 * clip editor's disclosure panel (#studioEditPanel).
 *
 * Everything the model wrote (reasons, findings, titles) is set with textContent, never innerHTML.
 * renderEditPanel() does not throw: a failed request shows a short error inside the panel.
 */

import { API_PREFIX, asMediaUrl, fetchJson } from './util.js';

const TRACK_COLOURS = new Map([
  ['zoom_punch', '#4de2ff'],
  ['shake', '#f9c74f'],
  ['flash', '#f5f5f5'],
  ['tint_red', '#ff4d8d'],
]);
const FALLBACK_COLOUR = '#9aa9bf';
const SEVERITY_HIGH = new Set(['error', 'critical', 'high', 'failed']);
const SEVERITY_MID = new Set(['warning', 'warn', 'medium', 'review']);
const QUALITY_STATUSES = new Set(['passed', 'review', 'failed']);

/* ------------------------------ helpers ------------------------------ */

function el(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined && content !== null) node.textContent = String(content);
  return node;
}

/** A finite number, or null. */
function num(value) {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function arr(value) {
  return Array.isArray(value) ? value : [];
}

function fixed(value, digits) {
  const n = num(value);
  return n === null ? '-' : n.toFixed(digits);
}

/** A 0..1 share (or an already-percent number) as a whole percentage. */
function percentText(value) {
  const n = num(value);
  if (n === null) return '-';
  return `${Math.round(n <= 1 ? n * 100 : n)}%`;
}

function text(value) {
  if (value === null || value === undefined) return '-';
  if (typeof value === 'object') {
    try { return JSON.stringify(value); } catch (_) { return '-'; }
  }
  return String(value);
}

/** The server's {"detail": "..."} sentence when there is one, else the raw text, kept short. */
function errorText(err) {
  const raw = String((err && err.message) || err || 'Unknown error');
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed.detail === 'string') return parsed.detail;
  } catch (_) { /* plain text */ }
  return raw.length > 200 ? `${raw.slice(0, 200)}...` : raw;
}

function setStatus(p, message, isError = false) {
  p.textContent = message || '';
  p.classList.toggle('error', !!isError);
}

function note(message, isError = false) {
  const p = el('p', 'studio-note edit-note');
  setStatus(p, message, isError);
  return p;
}

function section(title, ...children) {
  const box = el('div', 'edit-section');
  box.appendChild(el('h4', 'edit-subhead', title));
  children.forEach((child) => box.appendChild(child));
  return box;
}

function severityClass(severity) {
  const key = String(severity || '').toLowerCase();
  if (SEVERITY_HIGH.has(key)) return 'high';
  if (SEVERITY_MID.has(key)) return 'mid';
  return 'low';
}

function stopVideos(container) {
  container.querySelectorAll('video').forEach((video) => video.pause());
}

/* ------------------------------ header ------------------------------- */

function buildHeader(data, speed) {
  const row = el('div', 'edit-header');
  row.appendChild(el('span', 'edit-header-item', `Profile ${text(data.profile)}`));
  row.appendChild(el('span', 'edit-header-item', `Tone ${text(data.tone)}`));
  row.appendChild(el('span', 'edit-header-item', `Speed ${fixed(speed, 2)}x`));

  const status = data.quality && QUALITY_STATUSES.has(data.quality.status) ? data.quality.status : null;
  row.appendChild(status
    ? el('span', `edit-chip edit-chip-${status}`, `Quality ${status}`)
    : el('span', 'edit-chip edit-chip-none', 'Quality not checked'));

  if (data.fingerprint) {
    const full = String(data.fingerprint);
    const fp = el('span', 'edit-fingerprint', full.slice(0, 12));
    fp.title = full;
    row.appendChild(fp);
  }
  return row;
}

/* ----------------------------- timeline ------------------------------ */

/** Tooltip text: "kind 1.20-2.40 s, extra: reason". */
function tooltip(kind, start, end, reason, extra) {
  const range = (end === null || end === start)
    ? `${fixed(start, 2)} s`
    : `${fixed(start, 2)}-${fixed(end, 2)} s`;
  const parts = [`${text(kind)} ${range}`];
  if (extra) parts.push(extra);
  return reason ? `${parts.join(', ')}: ${text(reason)}` : parts.join(', ');
}

function buildTimeline(timeline) {
  const events = arr(timeline.events);
  const visuals = arr(timeline.visual_tracks);
  const audios = arr(timeline.audio_tracks);
  const eventsById = new Map(events.map((ev) => [ev.id, ev]));
  const reasonOf = (eventId) => (eventsById.get(eventId) || {}).reason || '';

  let duration = num(timeline.clip_duration);
  if (!duration || duration <= 0) {
    duration = Math.max(0, ...[...visuals, ...events].map((item) => num(item.end) || 0));
  }
  if (!duration || duration <= 0) return note('The timeline has no length to draw.');

  const pct = (t) => Math.min(100, Math.max(0, ((num(t) || 0) / duration) * 100));
  const strip = el('div', 'edit-strip');

  visuals.forEach((track) => {
    const start = num(track.start) || 0;
    const end = num(track.end) ?? start;
    const left = pct(start);
    const width = Math.max(0.6, Math.min(100 - left, pct(end) - left));
    const block = el('div', 'edit-block');
    block.style.left = `${left}%`;
    block.style.width = `${width}%`;
    block.style.background = TRACK_COLOURS.get(track.kind) || FALLBACK_COLOUR;
    const intensity = num(track.intensity);
    if (intensity !== null) block.style.opacity = String(Math.min(1, Math.max(0.35, intensity)));
    block.title = tooltip(track.kind, start, end, reasonOf(track.event_id));
    strip.appendChild(block);
  });

  events.forEach((ev) => {
    const start = num(ev.start) || 0;
    const marker = el('div', 'edit-marker');
    marker.style.left = `${pct(start)}%`;
    marker.title = tooltip(ev.kind, start, num(ev.end), ev.reason);
    strip.appendChild(marker);
  });

  audios.forEach((track) => {
    const start = num(track.start) || 0;
    const ev = eventsById.get(track.event_id) || {};
    const gain = num(track.gain_db);
    const tick = el('div', 'edit-tick');
    tick.style.left = `${pct(start)}%`;
    tick.title = tooltip(track.kind, start, num(ev.end), ev.reason, gain === null ? '' : `${gain.toFixed(1)} dB`);
    strip.appendChild(tick);
  });

  const legend = el('div', 'edit-legend');
  [...new Set(visuals.map((track) => track.kind))].forEach((kind) => {
    const item = el('span', 'edit-legend-item');
    const swatch = el('span', 'edit-swatch');
    swatch.style.background = TRACK_COLOURS.get(kind) || FALLBACK_COLOUR;
    item.append(swatch, document.createTextNode(text(kind)));
    legend.appendChild(item);
  });
  legend.appendChild(el('span', 'edit-legend-item', 'Lines: events. Ticks: audio.'));

  const axis = el('div', 'edit-axis');
  [0, 0.25, 0.5, 0.75, 1].forEach((f) => {
    axis.appendChild(el('span', null, `${Number((duration * f).toFixed(1))}s`));
  });

  const wrap = el('div', 'edit-timeline');
  wrap.append(legend, strip, axis);
  return wrap;
}

/* ------------------------- why / not applied ------------------------- */

function buildWhy(explanations) {
  const reasons = arr(explanations);
  if (!reasons.length) return section('Why', note('No reasons were recorded.'));
  const list = el('ul', 'edit-list');
  reasons.forEach((reason) => list.appendChild(el('li', null, text(reason))));
  return section('Why', list);
}

function buildSuppressed(suppressed) {
  const items = arr(suppressed);
  const box = el('details', 'edit-details');
  box.appendChild(el('summary', null, `Not applied (${items.length})`));
  if (!items.length) {
    box.appendChild(note('Nothing was held back.'));
    return box;
  }
  const list = el('ul', 'edit-list');
  items.forEach((item) => list.appendChild(el('li', null, `${text(item.action)}: ${text(item.reason)}`)));
  box.appendChild(list);
  return box;
}

/* ------------------------------ quality ------------------------------ */

function findingItem(f) {
  const li = el('li', 'edit-finding');
  const head = el('div', 'edit-finding-head');
  head.appendChild(el('span', `edit-sev edit-sev-${severityClass(f.severity)}`, text(f.severity)));
  head.appendChild(el('strong', null, text(f.issue)));
  li.appendChild(head);
  if (f.explanation) li.appendChild(el('div', 'edit-finding-text', text(f.explanation)));
  const meta = [];
  if (num(f.timestamp_final) !== null) meta.push(`at ${fixed(f.timestamp_final, 1)} s`);
  if (f.evidence && f.evidence.value !== undefined && f.evidence.value !== null) {
    meta.push(`evidence ${text(f.evidence.value)}`);
  }
  if (meta.length) li.appendChild(el('div', 'edit-finding-meta', meta.join(' · ')));
  return li;
}

function editorialItem(f, runs) {
  const li = el('li', 'edit-finding');
  const head = el('div', 'edit-finding-head');
  head.appendChild(el('strong', null, text(f.issue)));
  if (f.stable) head.appendChild(el('span', 'edit-badge-stable', 'stable'));
  li.appendChild(head);
  if (f.category) li.appendChild(el('div', 'edit-finding-meta', text(f.category)));
  if (f.explanation) li.appendChild(el('div', 'edit-finding-text', text(f.explanation)));
  const meta = [];
  if (num(f.confidence) !== null) meta.push(`confidence ${percentText(f.confidence)}`);
  const seen = num(f.runs_seen);
  if (seen !== null && runs !== null) meta.push(`seen in ${seen} of ${runs} runs`);
  if (num(f.timestamp_final) !== null) meta.push(`at ${fixed(f.timestamp_final, 1)} s`);
  if (f.evidence && f.evidence.value !== undefined && f.evidence.value !== null) {
    meta.push(`evidence ${text(f.evidence.value)}`);
  }
  if (meta.length) li.appendChild(el('div', 'edit-finding-meta', meta.join(' · ')));
  return li;
}

function buildEditorial(ed) {
  const parts = [];
  const runs = num(ed.runs_completed);
  const summary = [];
  if (num(ed.agreement_rate) !== null) summary.push(`agreement ${percentText(ed.agreement_rate)}`);
  if (runs !== null) summary.push(`${runs} runs`);
  if (summary.length) parts.push(el('div', 'edit-finding-meta', summary.join(' · ')));

  const findings = arr(ed.findings);
  if (!findings.length) {
    parts.push(note('The editorial review raised nothing.'));
  } else {
    const list = el('ul', 'edit-list');
    findings.forEach((f) => list.appendChild(editorialItem(f, runs)));
    parts.push(list);
  }
  return section('Editorial review (advisory)', ...parts);
}

function buildQuality(quality) {
  if (!quality) return section('Quality', note('The quality check did not run for this clip.'));
  const parts = [];

  const tc = quality.title_check;
  if (tc && tc.replaced) {
    const reasons = arr(tc.reasons).map(text).join('; ');
    parts.push(el('div', 'edit-notice',
      `Title replaced: ${text(tc.original_title)} -> ${text(tc.final_title)}${reasons ? ` (${reasons})` : ''}`));
  }

  const findings = arr(quality.findings).filter((f) => f && f.severity !== 'info');
  if (findings.length) {
    const list = el('ul', 'edit-list');
    findings.forEach((f) => list.appendChild(findingItem(f)));
    parts.push(list);
  } else {
    parts.push(note('No findings above info level.'));
  }

  if (quality.editorial) parts.push(buildEditorial(quality.editorial));
  return section('Quality', ...parts);
}

/* ---------------------------- before / after -------------------------- */

function renderResult(box, ab) {
  box.textContent = '';
  if (!ab) {
    box.appendChild(note('Effects have not been checked yet.'));
    return;
  }
  const control = ab.control;
  box.appendChild(el('p', 'edit-finding-meta', control
    ? `Control: SSIM ${fixed(control.ssim, 3)}, PSNR ${fixed(control.psnr, 1)} dB`
    : 'Control: not measured.'));

  const items = arr(ab.items);
  if (!items.length) {
    box.appendChild(note('No effects to check in this clip.'));
    return;
  }
  const table = el('table', 'edit-table');
  const head = el('tr');
  ['Effect', 'At (s)', 'SSIM', 'PSNR', 'Visible'].forEach((label) => head.appendChild(el('th', null, label)));
  const thead = el('thead');
  thead.appendChild(head);
  const tbody = el('tbody');
  items.forEach((item) => {
    const row = el('tr');
    row.append(
      el('td', null, text(item.kind)),
      el('td', null, fixed(item.time_final, 1)),
      el('td', null, fixed(item.ssim, 3)),
      el('td', null, fixed(item.psnr, 1)),
      el('td', null, item.visible ? 'visible' : 'not visible'),
    );
    tbody.appendChild(row);
  });
  table.append(thead, tbody);
  const wrap = el('div', 'edit-table-wrap');
  wrap.appendChild(table);
  box.appendChild(wrap);
}

function figure(label, media) {
  const fig = el('figure', 'edit-ab-fig');
  fig.append(el('figcaption', null, label), media);
  return fig;
}

function makeVideo(path, muted) {
  const video = document.createElement('video');
  video.preload = 'metadata';
  video.playsInline = true;
  video.muted = muted;
  video.src = asMediaUrl(path);
  return video;
}

/**
 * One set of transport controls for both videos. The edited file is the master clock: its time
 * t maps to plain time t * speed, and the plain video runs at playbackRate = speed so the two
 * stay aligned while playing.
 */
function buildTransport(plain, edited, speed, duration) {
  const bar = el('div', 'edit-ab-controls');
  const playBtn = el('button', 'ghost-btn', 'Play');
  playBtn.type = 'button';
  const slider = document.createElement('input');
  slider.type = 'range';
  slider.min = '0';
  slider.step = '0.05';
  slider.value = '0';
  slider.max = String(duration > 0 ? duration : 1);
  slider.setAttribute('aria-label', 'Seek both videos');
  const time = el('span', 'edit-ab-time');

  const total = () => (Number.isFinite(edited.duration) && edited.duration > 0 ? edited.duration : (duration || 0));
  const show = (t) => { time.textContent = `${t.toFixed(1)} / ${total().toFixed(1)} s`; };
  const seek = (t) => {
    edited.currentTime = t;
    plain.currentTime = t * speed;
    slider.value = String(t);
    show(t);
  };

  plain.playbackRate = speed;
  plain.addEventListener('loadedmetadata', () => { plain.playbackRate = speed; });
  edited.addEventListener('loadedmetadata', () => {
    slider.max = String(total() || 1);
    show(edited.currentTime);
  });
  edited.addEventListener('timeupdate', () => {
    const t = edited.currentTime;
    slider.value = String(t);
    show(t);
    const want = t * speed;
    if (Math.abs(plain.currentTime - want) > 0.25 && !plain.seeking) plain.currentTime = want;
  });
  edited.addEventListener('play', () => {
    plain.currentTime = edited.currentTime * speed;
    plain.play().catch(() => {});
    playBtn.textContent = 'Pause';
  });
  const stopped = () => { plain.pause(); playBtn.textContent = 'Play'; };
  edited.addEventListener('pause', stopped);
  edited.addEventListener('ended', stopped);

  playBtn.addEventListener('click', () => {
    if (edited.paused) edited.play().catch(() => {});
    else edited.pause();
  });
  slider.addEventListener('input', () => seek(Number(slider.value)));

  show(0);
  bar.append(playBtn, slider, time);
  return bar;
}

function buildCompare(data, clipId, speed, duration) {
  const box = el('div', 'edit-ab');
  if (!data.final_url) {
    box.appendChild(note('The edited file is not available, so the two cannot be compared here.'));
  } else {
    const plain = makeVideo(data.plain_url, true);
    const edited = makeVideo(data.final_url, false);
    const grid = el('div', 'edit-ab-grid');
    grid.append(figure('Plain', plain), figure('Edited', edited));
    box.append(grid, buildTransport(plain, edited, speed, duration));
  }

  const result = el('div', 'edit-ab-result');
  const status = note('');
  const check = el('button', 'ghost-btn', 'Check effects');
  check.type = 'button';
  check.addEventListener('click', async () => {
    check.disabled = true;
    setStatus(status, 'Checking the effects against the plain render. This can take a while.');
    try {
      const res = await fetchJson(`${API_PREFIX}/clips/${encodeURIComponent(clipId)}/ab-check`, { method: 'POST' });
      renderResult(result, res);
      setStatus(status, '');
    } catch (err) {
      setStatus(status, `Check failed: ${errorText(err)}`, true);
    } finally {
      check.disabled = false;
    }
  });

  renderResult(result, data.ab || null);
  box.append(check, status, result);
  return section('Before and after', box);
}

/* ------------------------------ entry point ------------------------------ */

function drawPanel(container, data, clipId) {
  const timeline = data.timeline || null;
  const speedValue = num(data.speed);
  const speed = speedValue && speedValue > 0 ? speedValue : 1;
  const duration = num(timeline && timeline.clip_duration) || 0;

  container.appendChild(buildHeader(data, speed));
  if (timeline) {
    container.appendChild(section('Timeline', buildTimeline(timeline)));
  } else {
    container.appendChild(note('No edit timeline is recorded for this clip.'));
  }
  container.appendChild(buildWhy(data.explanations));
  if (timeline) container.appendChild(buildSuppressed(timeline.suppressed));
  container.appendChild(buildQuality(data.quality));
  if (data.plain_url) container.appendChild(buildCompare(data, clipId, speed, duration));
}

/** Empty the panel and stop any video in it. Forgets which clip it was loaded for. */
export function clearEditPanel(container) {
  if (!container) return;
  stopVideos(container);
  delete container.dataset.editFor;
  container.textContent = '';
}

/**
 * Fetch GET /api/clips/{clipId}/edit and draw it into container. Never throws: a failed request
 * or a bad record shows a short message in the panel instead.
 */
export async function renderEditPanel(clipId, container) {
  if (!container) return;
  const token = String(clipId);
  clearEditPanel(container);
  container.dataset.editFor = token;
  container.appendChild(note('Loading how this clip was edited...'));

  let data;
  try {
    data = await fetchJson(`${API_PREFIX}/clips/${encodeURIComponent(token)}/edit`);
  } catch (err) {
    // Ignore the answer if the panel was reset or given to another clip meanwhile.
    if (container.dataset.editFor === token) {
      container.textContent = '';
      container.appendChild(note(`Could not load the edit record: ${errorText(err)}`, true));
    }
    return;
  }
  if (container.dataset.editFor !== token) return;

  container.textContent = '';
  try {
    drawPanel(container, data, clipId);
  } catch (err) {
    container.textContent = '';
    container.appendChild(note(`Could not show the edit record: ${errorText(err)}`, true));
  }
}
