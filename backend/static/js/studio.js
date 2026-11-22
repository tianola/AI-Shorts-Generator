/**
 * studio.js - the clip editor modal: a fixed preview, and three working areas.
 *
 *   Style  - caption preset, framing, switches, thumbnail, title. Apply re-renders the clip.
 *   Cut    - the clip's words. Pick a first and last word; the cut lands on word edges.
 *   AI     - ask the director for a change in plain words.
 *
 * Publishing to YouTube is a separate, deliberate step: it always asks for confirmation.
 *
 * Times the cut uses are render time (0 = the first frame of the clip's own render), which is
 * what /api/clips/{id}/words and /api/clips/{id}/trim both work in.
 */

import { loadJob } from './jobs.js';
import { state } from './state.js';
import { setStatus } from './status.js';
import { API_PREFIX, asMediaUrl, escapeHtml, fetchJson, isYouTubeVideoId } from './util.js';

export let currentStudioClip = null;

const $ = (id) => document.getElementById(id);
const PRESET_SELECTOR = '.preset-card';
const LAYOUT_SELECTOR = '.layout-card';
const TEMPLATE_SELECTOR = '.template-card';

let cut = { words: [], duration: 0, trim: null, inIdx: null, outIdx: null };
let stopAt = null;

function setNote(el, text, isError = false) {
  if (!el) return;
  el.textContent = text || '';
  el.classList.toggle('error', !!isError);
}

/** The clip's own full render, the file the cut is measured against. */
function basePath(clip) {
  return `jobs/${clip.job_id}/clip_${clip.index}/final_video.mp4`;
}

function showPreview(path, { play = false } = {}) {
  const video = $('studioVideoPlayer');
  if (!video || !path) return;
  video.src = `${asMediaUrl(path)}?t=${Date.now()}`;
  video.load();
  if (play) video.play().catch(() => {});
}

function scoreOf(clip) {
  const raw = clip.score || 0.85;
  return raw > 1.0 ? Math.round(raw) : Math.round(raw * 100);
}

function selectOnly(selector, attr, value) {
  document.querySelectorAll(selector).forEach((btn) => {
    const on = btn.dataset[attr] === String(value);
    btn.classList.toggle('active', on);
  });
}

function activeValue(selector, attr) {
  const btn = document.querySelector(`${selector}.active`);
  return btn ? btn.dataset[attr] : null;
}

/** Fill the modal from a clip record. Used on open and after every change the server made. */
function renderClip(clip) {
  currentStudioClip = clip;
  $('studioModalTitle').textContent = clip.title || `Clip #${clip.id}`;
  $('studioTimeBadge').textContent = `${Number(clip.start).toFixed(1)}s → ${Number(clip.end).toFixed(1)}s`;
  $('studioScoreBadge').textContent = `${scoreOf(clip)}/100 virality`;
  $('studioViralityExplanation').textContent = clip.virality_explanation
    || 'High-retention hook with active speaker pacing and a clear payoff.';

  selectOnly(PRESET_SELECTOR, 'style', clip.caption_style || 'mrbeast');
  selectOnly(LAYOUT_SELECTOR, 'layout', clip.layout_mode || 'single_speaker');
  const options = clip.render_options || {};
  const template = options.thumbnail_template ?? (clip.index ? clip.index % 3 : 0);
  selectOnly(TEMPLATE_SELECTOR, 'template', template);
  $('studioKaraokeToggle').checked = options.karaoke !== false;
  $('studioMotionTitleToggle').checked = options.motion_title !== false;
  $('studioStyleTitle').value = clip.title || '';

  $('studioInfoJob').textContent = `#${clip.job_id}`;
  $('studioInfoClip').textContent = `#${clip.id}`;
  $('studioInfoLength').textContent = `${(clip.end - clip.start).toFixed(1)} s`;
  $('studioInfoStyle').textContent = `${clip.caption_style || 'mrbeast'} · ${clip.layout_mode || 'single_speaker'}`;
  $('studioOpenRawBtn').href = clip.file_path ? asMediaUrl(clip.file_path) : '#';
}

function publishDefaults(clip) {
  const tags = Array.isArray(clip.hashtags) && clip.hashtags.length
    ? clip.hashtags
    : (clip.platform_metadata?.youtube?.hashtags || ['#Shorts', '#Viral']);
  const clean = [...new Set(['#Shorts', ...tags.map((t) => (t.startsWith('#') ? t : `#${t}`))])];
  return {
    title: (clip.title || `Viral Short #${clip.id}`).replace(/#shorts/gi, '').trim().slice(0, 100),
    description: clip.description || clip.platform_metadata?.youtube?.description || '',
    hashtags: clean.join(' '),
  };
}

function fillPublish(clip) {
  const defaults = publishDefaults(clip);
  $('ytUploadTitle').value = defaults.title;
  $('ytUploadDesc').value = defaults.description;
  $('ytUploadHashtags').value = defaults.hashtags;
  const status = $('ytUploadStatus');
  if (isYouTubeVideoId(clip.youtube_video_id)) {
    const id = encodeURIComponent(clip.youtube_video_id);
    status.innerHTML = `Live on YouTube: <a href="https://youtube.com/shorts/${id}" target="_blank" rel="noreferrer">youtube.com/shorts/${escapeHtml(clip.youtube_video_id)}</a>`;
  } else if (clip.upload_error) {
    setNote(status, `Last upload error: ${clip.upload_error}`, true);
  } else {
    setNote(status, '');
  }
}

async function refreshClip(id) {
  const res = await fetchJson(`${API_PREFIX}/clips/${id}`);
  return res.clip || res;
}

/* ------------------------------- Style ------------------------------- */

async function applyStyle() {
  if (!currentStudioClip) return;
  const button = $('studioApplyStyleBtn');
  const status = $('studioRerenderStatus');
  const payload = {
    caption_style: activeValue(PRESET_SELECTOR, 'style') || 'mrbeast',
    layout_mode: activeValue(LAYOUT_SELECTOR, 'layout') || 'single_speaker',
    thumbnail_template: parseInt(activeValue(TEMPLATE_SELECTOR, 'template') ?? '0', 10),
    title: $('studioStyleTitle').value.trim() || null,
    karaoke: $('studioKaraokeToggle').checked,
    motion_title: $('studioMotionTitleToggle').checked,
  };
  button.disabled = true;
  setNote(status, 'Rendering the clip with this style. This takes a while.');
  try {
    const res = await fetchJson(`${API_PREFIX}/clips/${currentStudioClip.id}/rerender`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    // The server answers with the whole record: that is what the title and style fields
    // show from now on, so nothing here can be left holding an older title.
    renderClip(res.clip);
    showPreview(res.clip.file_path, { play: true });
    await loadCutWords();
    setNote(status, res.message || 'Done. The clip has the new style.');
    await loadJob(state.activeJobId);
  } catch (err) {
    setNote(status, `Re-render failed: ${err.message}`, true);
  } finally {
    button.disabled = false;
  }
}

/* -------------------------------- Cut -------------------------------- */

async function loadCutWords() {
  if (!currentStudioClip) return;
  try {
    const res = await fetchJson(`${API_PREFIX}/clips/${currentStudioClip.id}/words`);
    cut = { words: res.words || [], duration: res.duration || 0, trim: res.trim || null, inIdx: null, outIdx: null };
    renderWords();
  } catch (err) {
    $('studioCutWords').innerHTML = '';
    setNote($('studioCutStatus'), `Could not load the words: ${err.message}`, true);
  }
}

function renderWords() {
  const box = $('studioCutWords');
  box.textContent = '';
  if (!cut.words.length) {
    box.innerHTML = '<em>No words for this clip (its transcript is not available).</em>';
    updateCutVisuals();
    return;
  }
  const inTrim = (w) => cut.trim && w.start >= cut.trim.start - 0.01 && w.end <= cut.trim.end + 0.01;
  cut.words.forEach((w, i) => {
    const span = document.createElement('button');
    span.type = 'button';
    span.className = 'studio-word';
    span.dataset.i = String(i);
    span.textContent = w.word;
    if (inTrim(w)) span.classList.add('kept');
    span.addEventListener('click', () => onWordClick(i));
    box.appendChild(span);
  });
  updateCutVisuals();
}

function onWordClick(i) {
  if (cut.inIdx === null || cut.outIdx !== null) {
    cut.inIdx = i;
    cut.outIdx = null;
  } else if (i < cut.inIdx) {
    cut.outIdx = cut.inIdx;
    cut.inIdx = i;
  } else {
    cut.outIdx = i;
  }
  updateCutVisuals();
}

function selectionTimes() {
  if (cut.inIdx === null || cut.outIdx === null) return null;
  return { start: cut.words[cut.inIdx].start, end: cut.words[cut.outIdx].end };
}

function updateCutVisuals() {
  const box = $('studioCutWords');
  const from = cut.inIdx;
  const to = cut.outIdx;
  box.querySelectorAll('.studio-word').forEach((el) => {
    const i = Number(el.dataset.i);
    el.classList.toggle('in', i === from);
    el.classList.toggle('out', i === to);
    el.classList.toggle('sel', from !== null && to !== null && i > from && i < to);
  });
  const sel = selectionTimes();
  const summary = $('studioCutSummary');
  if (sel) {
    const count = to - from + 1;
    summary.textContent = `Keeps ${sel.start.toFixed(2)}s to ${sel.end.toFixed(2)}s · ${(sel.end - sel.start).toFixed(1)}s · ${count} words`;
  } else if (from !== null) {
    summary.textContent = `Starts at ${cut.words[from].start.toFixed(2)}s. Click the last word to keep.`;
  } else {
    summary.textContent = cut.trim
      ? `Currently cut to ${cut.trim.start.toFixed(2)}s to ${cut.trim.end.toFixed(2)}s.`
      : 'The full clip is used. Click a word to start a cut.';
  }
  $('studioApplyCutBtn').disabled = !sel;
  $('studioPlaySelBtn').disabled = !sel;
}

async function applyCut() {
  const sel = selectionTimes();
  if (!sel || !currentStudioClip) return;
  const status = $('studioCutStatus');
  setNote(status, 'Cutting the clip...');
  $('studioApplyCutBtn').disabled = true;
  try {
    await fetchJson(`${API_PREFIX}/clips/${currentStudioClip.id}/trim`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start: sel.start, end: sel.end }),
    });
    const fresh = await refreshClip(currentStudioClip.id);
    renderClip(fresh);
    showPreview(fresh.file_path, { play: true });
    await loadCutWords();
    setNote(status, 'Cut applied. Captions stay in sync with the words.');
    await loadJob(state.activeJobId);
  } catch (err) {
    setNote(status, `Cut failed: ${err.message}`, true);
    updateCutVisuals();
  }
}

async function useFullClip() {
  if (!currentStudioClip) return;
  const status = $('studioCutStatus');
  try {
    await fetchJson(`${API_PREFIX}/clips/${currentStudioClip.id}/trim/reset`, { method: 'POST' });
    const fresh = await refreshClip(currentStudioClip.id);
    renderClip(fresh);
    showPreview(fresh.file_path);
    await loadCutWords();
    setNote(status, 'Back to the full clip.');
    await loadJob(state.activeJobId);
  } catch (err) {
    setNote(status, `Could not reset: ${err.message}`, true);
  }
}

function playSelection() {
  const sel = selectionTimes();
  if (!sel || !currentStudioClip) return;
  const video = $('studioVideoPlayer');
  const wanted = basePath(currentStudioClip);
  const apply = () => { video.currentTime = sel.start; stopAt = sel.end; video.play().catch(() => {}); };
  if (!video.src.includes(wanted)) {
    video.addEventListener('loadedmetadata', apply, { once: true });
    showPreview(wanted);
  } else {
    apply();
  }
}

/* -------------------------------- AI --------------------------------- */

export async function sendStudioAiPrompt() {
  if (!currentStudioClip) return;
  const input = $('studioAiInput');
  const chatBox = $('studioAiChatBox');
  const promptText = input.value.trim();
  if (!promptText) return;

  const userMsg = document.createElement('div');
  userMsg.className = 'ai-chat-msg user';
  userMsg.textContent = promptText;
  chatBox.appendChild(userMsg);
  input.value = '';

  const thinking = document.createElement('div');
  thinking.className = 'ai-chat-msg system';
  thinking.textContent = 'The AI director is working on it...';
  chatBox.appendChild(thinking);
  chatBox.scrollTop = chatBox.scrollHeight;

  try {
    const res = await fetchJson(`${API_PREFIX}/clips/${currentStudioClip.id}/ai-edit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: promptText }),
    });
    // The AI can change the title and description; the fields must show what it wrote.
    const fresh = await refreshClip(currentStudioClip.id);
    renderClip(fresh);
    fillPublish(fresh);
    thinking.textContent = res.message || 'Done. The clip was updated.';
    setStatus('AI edit applied.', 'success');
    await loadCutWords();
    await loadJob(state.activeJobId);
  } catch (err) {
    thinking.textContent = `The AI edit failed: ${err.message}`;
  }
  chatBox.scrollTop = chatBox.scrollHeight;
}

/* ------------------------------ Publish ------------------------------ */

function readPublishFields() {
  return {
    title: $('ytUploadTitle').value.trim(),
    description: $('ytUploadDesc').value.trim(),
    hashtags: $('ytUploadHashtags').value.trim().split(/[\s,]+/).filter(Boolean)
      .map((t) => (t.startsWith('#') ? t : `#${t}`)),
    privacy: $('ytUploadPrivacy').value || 'unlisted',
  };
}

/**
 * Upload one clip to YouTube, after the person confirms. Used by the modal and by the clip list.
 * Returns the response, or null when the person cancelled.
 */
export async function uploadClip(clip, fields) {
  const privacyLabel = { public: 'public', unlisted: 'unlisted', private: 'private' }[fields.privacy] || fields.privacy;
  const ok = window.confirm(
    `Upload "${fields.title}" to YouTube as ${privacyLabel}?\n\n`
    + 'This publishes to your channel. Delete it on YouTube if you change your mind.'
  );
  if (!ok) return null;

  const isOpen = currentStudioClip && currentStudioClip.id === clip.id;
  const status = $('ytUploadStatus');
  const button = $('ytDoUploadBtn');
  if (isOpen) { setNote(status, 'Uploading to YouTube...'); button.disabled = true; }
  try {
    const res = await fetchJson(`${API_PREFIX}/clips/${clip.id}/upload-youtube`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: fields.title,
        description: fields.description,
        hashtags: fields.hashtags,
        privacy_status: fields.privacy,
      }),
    });
    if (isOpen) {
      const fresh = await refreshClip(clip.id);
      renderClip(fresh);
      fillPublish(fresh);
      setNote(status, 'Uploaded.');
    }
    setStatus(`Clip #${clip.id} uploaded to YouTube.`, 'success');
    await loadJob(state.activeJobId);
    return res;
  } catch (err) {
    if (isOpen) setNote(status, `Upload failed: ${err.message}`, true);
    setStatus(`Upload failed: ${err.message}`, 'error');
    return null;
  } finally {
    if (isOpen) button.disabled = false;
  }
}

/* ------------------------------ Modal ------------------------------- */

function closeModal() {
  $('clipStudioModal').style.display = 'none';
  $('studioVideoPlayer').pause();
  stopAt = null;
}

function showTab(name) {
  document.querySelectorAll('.studio-seg .tab-nav-btn').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  $('tabContentStyle').hidden = name !== 'style';
  $('tabContentCut').hidden = name !== 'cut';
  $('tabContentAi').hidden = name !== 'ai';
  if (name === 'cut' && currentStudioClip) showPreview(basePath(currentStudioClip));
}

export function setupStudioModal() {
  const modal = $('clipStudioModal');
  if (!modal) return;

  $('closeStudioBtn')?.addEventListener('click', closeModal);
  modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });

  document.querySelectorAll('.studio-seg .tab-nav-btn').forEach((btn) => {
    btn.addEventListener('click', () => showTab(btn.dataset.tab));
  });

  document.querySelectorAll(PRESET_SELECTOR).forEach((btn) => btn.addEventListener('click', () => selectOnly(PRESET_SELECTOR, 'style', btn.dataset.style)));
  document.querySelectorAll(LAYOUT_SELECTOR).forEach((btn) => btn.addEventListener('click', () => selectOnly(LAYOUT_SELECTOR, 'layout', btn.dataset.layout)));
  document.querySelectorAll(TEMPLATE_SELECTOR).forEach((btn) => btn.addEventListener('click', () => selectOnly(TEMPLATE_SELECTOR, 'template', btn.dataset.template)));

  $('studioApplyStyleBtn')?.addEventListener('click', applyStyle);
  $('studioApplyCutBtn')?.addEventListener('click', applyCut);
  $('studioResetCutBtn')?.addEventListener('click', useFullClip);
  $('studioClearCutBtn')?.addEventListener('click', () => { cut.inIdx = null; cut.outIdx = null; updateCutVisuals(); });
  $('studioPlaySelBtn')?.addEventListener('click', playSelection);

  document.querySelectorAll('.quick-prompt-btn').forEach((btn) => {
    btn.addEventListener('click', () => { $('studioAiInput').value = btn.dataset.prompt; sendStudioAiPrompt(); });
  });
  $('studioAiSendBtn')?.addEventListener('click', sendStudioAiPrompt);
  $('studioAiInput')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') sendStudioAiPrompt(); });

  $('ytDoUploadBtn')?.addEventListener('click', () => {
    if (!currentStudioClip) return;
    uploadClip(currentStudioClip, readPublishFields());
  });

  $('studioOpenFolderBtn')?.addEventListener('click', async () => {
    if (!currentStudioClip) return;
    try {
      await fetchJson(`${API_PREFIX}/jobs/${currentStudioClip.job_id}/open-folder`, { method: 'POST' });
      setStatus('Folder opened on this computer.', 'success');
    } catch (err) {
      setStatus(`Could not open the folder: ${err.message}`, 'error');
    }
  });

  const video = $('studioVideoPlayer');
  video.addEventListener('timeupdate', () => {
    if (stopAt !== null && video.currentTime >= stopAt) { video.pause(); stopAt = null; }
  });
}

export function openClipStudio(clip) {
  if (!$('clipStudioModal')) return;
  renderClip(clip);
  fillPublish(clip);
  cut = { words: [], duration: 0, trim: null, inIdx: null, outIdx: null };
  setNote($('studioRerenderStatus'), '');
  setNote($('studioCutStatus'), '');
  setNote($('studioRenderState'), '');
  showTab('style');
  showPreview(clip.file_path, { play: true });
  $('clipStudioModal').style.display = 'flex';
  loadCutWords();
}
