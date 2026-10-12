/**
 * source.js - part of the job dashboard, split out of app.js.
 *
 * Where the short comes from - a YouTube URL or a local file - and
 * the model choice that depends on how long it is.
 */

import { els, state } from './state.js';
import { setStatus, updateTranscriptionEstimateBadge } from './status.js';
import { API_PREFIX, escapeHtml, fetchJson, formatDuration } from './util.js';


export let selectedUploadFile = null;
export let activeSourceMode = 'url'; // 'url' | 'upload'
state.systemModels = [];
state.currentVideoDuration = 300; // default 5 min reference until video is loaded

export function setSourceMode(mode) {
  activeSourceMode = mode;
  const isUrl = mode === 'url';
  els.sourceTabUrl?.classList.toggle('active', isUrl);
  els.sourceTabUpload?.classList.toggle('active', !isUrl);
  if (els.sourceUrlPanel) els.sourceUrlPanel.style.display = isUrl ? 'block' : 'none';
  if (els.sourceUploadPanel) els.sourceUploadPanel.style.display = isUrl ? 'none' : 'block';
}

export function formatFileSize(bytes) {
  if (!bytes) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function setupSourceTabs() {
  els.sourceTabUrl?.addEventListener('click', () => setSourceMode('url'));
  els.sourceTabUpload?.addEventListener('click', () => setSourceMode('upload'));

  els.uploadDropZone?.addEventListener('click', (e) => {
    if (e.target.closest('.upload-remove-btn')) return;
    els.videoFile?.click();
  });

  els.uploadDropZone?.addEventListener('dragover', (e) => {
    e.preventDefault();
    els.uploadDropZone.classList.add('dragover');
  });
  els.uploadDropZone?.addEventListener('dragleave', () => {
    els.uploadDropZone.classList.remove('dragover');
  });
  els.uploadDropZone?.addEventListener('drop', (e) => {
    e.preventDefault();
    els.uploadDropZone.classList.remove('dragover');
    const files = e.dataTransfer?.files;
    if (files && files.length) handleFileSelected(files[0]);
  });

  els.videoFile?.addEventListener('change', () => {
    const file = els.videoFile.files?.[0];
    if (file) handleFileSelected(file);
  });

  els.uploadRemoveBtn?.addEventListener('click', () => {
    selectedUploadFile = null;
    if (els.videoFile) els.videoFile.value = '';
    if (els.uploadFileInfo) els.uploadFileInfo.style.display = 'none';
    if (els.uploadDropContent) els.uploadDropContent.style.display = 'flex';
  });
}

export function handleFileSelected(file) {
  const allowed = ['.mp4', '.webm', '.mov', '.m4v', '.mkv'];
  const ext = file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
  if (!allowed.includes(ext)) {
    setStatus(`Unsupported file type "${ext}". Please choose an MP4, WebM, MOV, M4V or MKV file.`, 'error');
    return;
  }
  selectedUploadFile = file;
  if (els.uploadFileName) els.uploadFileName.textContent = file.name;
  if (els.uploadFileSize) els.uploadFileSize.textContent = formatFileSize(file.size);
  if (els.uploadFileInfo) els.uploadFileInfo.style.display = 'flex';
  if (els.uploadDropContent) els.uploadDropContent.style.display = 'none';

  // Read video duration from file in browser in <100ms
  try {
    const tempVideo = document.createElement('video');
    tempVideo.preload = 'metadata';
    tempVideo.onloadedmetadata = function() {
      URL.revokeObjectURL(tempVideo.src);
      const dur = tempVideo.duration;
      if (dur && dur > 0) {
        state.currentVideoDuration = dur;
        renderModelOptions(dur);
        if (els.uploadFileSize) {
          els.uploadFileSize.textContent = `${formatFileSize(file.size)} | ⏱️ Duration: ${formatDuration(dur)}`;
        }
      }
    };
    tempVideo.src = URL.createObjectURL(file);
  } catch (e) {
    console.warn('Could not probe video duration:', e);
  }

  setStatus(`Selected: ${file.name} (${formatFileSize(file.size)})`, 'success');
}

export function renderModelOptions(durationSec = 300) {
  if (!els.whisperModel || !state.systemModels || !state.systemModels.length) return;
  const currentVal = els.whisperModel.value;
  els.whisperModel.innerHTML = '';
  state.systemModels.forEach(m => {
    const option = document.createElement('option');
    option.value = m.id;
    let est = '~5-10 sec';
    if (!m.is_cloud) {
      const calcSec = Math.max(5, Math.round(durationSec * (m.rtf || 0.22)));
      est = calcSec < 60 ? `~${calcSec} sec` : `~${(calcSec / 60).toFixed(1).replace('.0', '')} min`;
    }
    const recBadge = m.is_recommended ? ' ⭐ (Recommended)' : '';
    option.textContent = `${m.name} - ${est} for ${formatDuration(durationSec)}${recBadge}`;
    if (currentVal ? option.value === currentVal : m.is_recommended) {
      option.selected = true;
    }
    els.whisperModel.appendChild(option);
  });
  updateTranscriptionEstimateBadge(durationSec);
}

// Live YouTube URL metadata fetching & transcription time calculation
export let urlDebounceTimer = null;
if (els.videoUrl) {
  const fetchUrlMeta = () => {
    clearTimeout(urlDebounceTimer);
    const url = els.videoUrl.value.trim();
    if (!url || !url.includes('http')) {
      if (els.urlMetaBadge) els.urlMetaBadge.style.display = 'none';
      return;
    }
    urlDebounceTimer = setTimeout(async () => {
      try {
        if (els.urlMetaBadge) {
          els.urlMetaBadge.style.display = 'block';
          els.urlMetaBadge.innerHTML = '⏳ <em>Calculating video duration & estimates...</em>';
        }
        const data = await fetchJson(`${API_PREFIX}/video/metadata?url=${encodeURIComponent(url)}`);
        if (data && data.duration > 0) {
          state.currentVideoDuration = data.duration;
          if (els.urlMetaBadge) {
            els.urlMetaBadge.style.display = 'block';
            els.urlMetaBadge.innerHTML = `🎥 <strong>${escapeHtml(data.title)}</strong> — Duration: <strong>${escapeHtml(data.duration_formatted)}</strong> (${data.duration}s)`;
          }
          renderModelOptions(data.duration);
        }
      } catch (e) {
        console.warn('Could not fetch video metadata:', e);
        if (els.urlMetaBadge) els.urlMetaBadge.style.display = 'none';
      }
    }, 400);
  };
  els.videoUrl.addEventListener('input', fetchUrlMeta);
  els.videoUrl.addEventListener('paste', fetchUrlMeta);
}

setupSourceTabs();
