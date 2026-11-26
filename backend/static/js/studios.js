/**
 * studios.js - the Narrate Studio and Live Studio views, and the tabs that switch to them.
 *
 * Everything lives on the same page as the clip dashboard: the tab buttons only show or
 * hide the three <main> blocks, so nothing here reloads or leaves the page.
 */

import { asMediaUrl, escapeHtml, fetchJson } from './util.js';

const $ = (id) => document.getElementById(id);
const post = (url, body) => fetchJson(url, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
});

let livePoll = null;
let liveError = '';

function showView(viewId) {
  document.querySelectorAll('.view-tab').forEach((b) => b.classList.toggle('active', b.dataset.view === viewId));
  ['viewClips', 'viewNarrate', 'viewLive'].forEach((id) => { $(id).hidden = id !== viewId; });
  if (viewId === 'viewNarrate') loadNarrateLists();
  clearInterval(livePoll);
  if (viewId === 'viewLive') { refreshLive(); livePoll = setInterval(refreshLive, 3000); }
}

function setNote(el, text, isError) {
  // The API reports errors as {"detail": "..."}; show just the sentence.
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed.detail === 'string') text = parsed.detail;
  } catch (_) { /* plain text */ }
  el.textContent = text || '';
  el.classList.toggle('error', !!isError);
}

/* ------------------------------ Narrate ------------------------------ */

async function loadNarrateLists() {
  try {
    const [voices, lib] = await Promise.all([fetchJson('/api/narrate/voices'), fetchJson('/api/narrate/library')]);
    const voiceSel = $('nrVoice');
    const keep = voiceSel.value;
    voiceSel.innerHTML = '';
    (voices.voices || []).forEach((v) => {
      const o = document.createElement('option');
      o.value = v.id;
      o.textContent = `${v.name} (${v.engine}${v.language ? ', ' + v.language : ''})`;
      voiceSel.appendChild(o);
    });
    if (!voiceSel.options.length) voiceSel.innerHTML = '<option value="">No voice found</option>';
    if (keep) voiceSel.value = keep;
    const bg = $('nrBackground');
    const keepBg = bg.value;
    bg.innerHTML = '<option value="">Automatic (stock footage that fits the story)</option>';
    (lib.videos || []).forEach((v) => {
      const o = document.createElement('option');
      o.value = v.name;
      o.textContent = v.name;
      bg.appendChild(o);
    });
    bg.value = keepBg;
    setNote($('nrNote'), (lib.videos || []).length ? '' : 'Background footage is found automatically; add your own video only if you prefer it.');
  } catch (err) {
    setNote($('nrNote'), err.message, true);
  }
}

async function writeScript() {
  const btn = $('nrWriteBtn');
  btn.disabled = true;
  setNote($('nrNote'), 'Writing the script...');
  try {
    const res = await post('/api/narrate/script', {
      idea: $('nrIdea').value, style: $('nrStyle').value,
      seconds: parseInt($('nrSeconds').value, 10), language: $('nrLang').value.trim(),
    });
    $('nrScript').value = res.script;
    setNote($('nrNote'), 'Script ready. Edit it if you like, then make the video.');
  } catch (err) {
    setNote($('nrNote'), err.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function uploadBackground(event) {
  const file = event.target.files[0];
  if (!file) return;
  setNote($('nrNote'), `Uploading ${file.name}...`);
  const form = new FormData();
  form.append('file', file);
  try {
    // Not fetchJson: it forces a JSON Content-Type, and an upload needs the multipart boundary.
    const res = await fetch('/api/narrate/library', { method: 'POST', body: form });
    if (!res.ok) throw new Error(await res.text());
    await loadNarrateLists();
    $('nrBackground').value = file.name;
    setNote($('nrNote'), `${file.name} added.`);
  } catch (err) {
    setNote($('nrNote'), err.message, true);
  }
  event.target.value = '';
}

async function renderNarration() {
  const btn = $('nrRenderBtn');
  btn.disabled = true;
  $('nrVideo').hidden = true;
  $('nrDownload').hidden = true;
  $('nrProgress').hidden = false;
  $('nrBar').style.width = '2%';
  $('nrMsg').textContent = 'Starting';
  setNote($('nrNote'), '');
  try {
    const { job_id: id } = await post('/api/narrate/render', {
      script: $('nrScript').value, voice: $('nrVoice').value,
      background: $('nrBackground').value || null, caption_style: $('nrCaption').value,
    });
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500));
      const st = await fetchJson(`/api/narrate/status/${encodeURIComponent(id)}`);
      $('nrBar').style.width = `${st.percent || 0}%`;
      $('nrMsg').textContent = st.message || '';
      if (st.state === 'done') {
        const url = asMediaUrl(st.path);
        $('nrVideo').src = url;
        $('nrVideo').hidden = false;
        $('nrDownload').href = url;
        $('nrDownload').hidden = false;
        break;
      }
      if (st.state === 'failed') throw new Error(st.message || 'The video could not be made');
    }
  } catch (err) {
    setNote($('nrNote'), err.message, true);
  } finally {
    btn.disabled = false;
  }
}

/* -------------------------------- Live -------------------------------- */

function syncLiveSource() {
  const obs = $('lvSource').value === 'obs';
  $('lvUrlWrap').hidden = obs;
  $('lvObs').hidden = !obs;
}

// The clip list is redrawn every few seconds. Rebuilding it wholesale replaced the element under
// the pointer before a click could land, so only clips that appeared or went away are touched.
function renderClips(clips) {
  const grid = $('lvClips');
  if (!clips.length) {
    if (!grid.querySelector('em')) grid.innerHTML = '<em>No clips yet</em>';
    return;
  }
  if (grid.querySelector('em')) grid.innerHTML = '';
  const cards = new Map([...grid.querySelectorAll('.studio-clip')].map((el) => [el.dataset.id, el]));
  const wanted = new Set(clips.map((c) => c.id));
  cards.forEach((el, id) => { if (!wanted.has(id)) el.remove(); });
  clips.forEach((c, i) => {
    let card = cards.get(c.id);
    if (card) {
      card.querySelector('strong').textContent = c.title;
      card.querySelector('small').textContent = c.category || '';
    } else {
      card = document.createElement('div');
      card.className = 'studio-clip';
      card.dataset.id = c.id;
      card.innerHTML = `<video controls preload="metadata" src="${escapeHtml(asMediaUrl(c.path))}"></video>
        <strong></strong><small></small>
        <button class="ghost-btn" type="button">Delete</button>`;
      card.querySelector('strong').textContent = c.title;
      card.querySelector('small').textContent = c.category || '';
      card.querySelector('button').addEventListener('click', async () => {
        const url = c.watch_id
          ? `/api/live/watchlist/${encodeURIComponent(c.watch_id)}/clips/${encodeURIComponent(c.id)}`
          : `/api/live/clips/${encodeURIComponent(c.id)}`;
        try { await fetchJson(url, { method: 'DELETE' }); refreshLive(); }
        catch (err) { setNote($('lvNote'), err.message, true); }
      });
    }
    if (grid.children[i] !== card) grid.insertBefore(card, grid.children[i] || null);
  });
}

// The watch list: one row per saved link. Rebuilt only when what the row shows changes, so the
// buttons stay put under the pointer (the same reason the clip list is updated in place).
function renderWatch(entries) {
  const box = $('lvWatchList');
  const sig = JSON.stringify(entries.map((e) => [e.id, e.name, e.enabled, e.state, e.clips.length]));
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.classList.add('studio-watch-list');
  if (!entries.length) { box.innerHTML = '<em>No links yet</em>'; return; }
  box.innerHTML = '';
  entries.forEach((e) => {
    const row = document.createElement('div');
    row.className = 'studio-watch-row';
    const info = document.createElement('div');
    info.className = 'studio-watch-info';
    const label = document.createElement('strong');
    label.textContent = e.name || e.url;
    const small = document.createElement('small');
    small.textContent = e.state === 'error' ? e.detail : (e.name ? e.url : '');
    info.append(label, small);
    const badge = document.createElement('span');
    badge.className = `studio-badge ${e.state}`;
    badge.textContent = e.state;
    const actions = document.createElement('div');
    actions.className = 'studio-watch-actions';
    const toggle = document.createElement('button');
    toggle.className = 'ghost-btn';
    toggle.type = 'button';
    toggle.textContent = e.enabled ? 'Pause' : 'Resume';
    toggle.addEventListener('click', () => watchAction(() => fetchJson(`/api/live/watchlist/${encodeURIComponent(e.id)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !e.enabled }),
    })));
    const remove = document.createElement('button');
    remove.className = 'ghost-btn';
    remove.type = 'button';
    remove.textContent = 'Remove';
    remove.addEventListener('click', () => {
      if (window.confirm(`Remove ${e.name || e.url} from the watch list? Its clips stay on disk.`)) {
        watchAction(() => fetchJson(`/api/live/watchlist/${encodeURIComponent(e.id)}`, { method: 'DELETE' }));
      }
    });
    actions.append(toggle, remove);
    row.append(info, badge, actions);
    box.appendChild(row);
  });
}

async function watchAction(fn) {
  try { await fn(); setNote($('lvWatchNote'), '', false); }
  catch (err) { setNote($('lvWatchNote'), err.message, true); }
  refreshLive();
}

async function refreshLive() {
  try {
    const [st, wl] = await Promise.all([fetchJson('/api/live/status'), fetchJson('/api/live/watchlist')]);
    renderWatch(wl.entries || []);
    const watched = (wl.entries || []).flatMap((e) => e.clips || []);
    const allClips = [...(st.clips || []), ...watched].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    const badge = $('lvStatus');
    badge.textContent = `${st.status}${st.elapsed ? ' · ' + Math.floor(st.elapsed / 60) + ' min' : ''}`;
    badge.className = `studio-badge ${st.status}`;
    // A failed button press stays on screen until the next press; otherwise the
    // refresh that follows it would replace the reason with the session's status text.
    if (liveError) setNote($('lvNote'), liveError, true);
    else if (st.detail && st.status !== 'live') setNote($('lvNote'), st.detail, st.status === 'disconnected');
    else setNote($('lvNote'), '');
    const tr = $('lvTranscript');
    tr.innerHTML = (st.transcript || []).length
      ? st.transcript.map((t) => `<div>${escapeHtml(t.text)}</div>`).join('')
      : '<em>Nothing yet</em>';
    tr.scrollTop = tr.scrollHeight;
    renderClips(allClips);
  } catch (err) {
    setNote($('lvNote'), err.message, true);
  }
}

async function liveAction(fn) {
  liveError = '';
  try { await fn(); } catch (err) { liveError = err.message; }
  refreshLive();
}

export function setupStudios() {
  document.querySelectorAll('.view-tab').forEach((b) => b.addEventListener('click', () => showView(b.dataset.view)));
  $('nrWriteBtn').addEventListener('click', writeScript);
  $('nrRenderBtn').addEventListener('click', renderNarration);
  $('nrUpload').addEventListener('change', uploadBackground);

  $('lvSource').addEventListener('change', syncLiveSource);
  $('lvSens').addEventListener('input', () => { $('lvSensVal').textContent = $('lvSens').value; });
  $('lvConnectBtn').addEventListener('click', () => liveAction(() => post('/api/live/connect', {
    source: $('lvSource').value, url: $('lvUrl').value.trim(), sensitivity: parseInt($('lvSens').value, 10),
    buffer_minutes: parseInt($('lvBuffer').value, 10), auto_clip: $('lvAuto').value === '1',
  })));
  $('lvStopBtn').addEventListener('click', () => liveAction(() => post('/api/live/disconnect')));
  $('lvWatchAdd').addEventListener('click', () => {
    const input = $('lvWatchUrl');
    watchAction(async () => { await post('/api/live/watchlist', { url: input.value.trim() }); input.value = ''; });
  });
  $('lvInstantBtn').addEventListener('click', () => liveAction(() => post('/api/live/instant-clip', { seconds: 30 })));
  syncLiveSource();
}
