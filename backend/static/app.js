const API_PREFIX = '/api';

// Every value below comes from the API or the local filesystem and is spliced
// into innerHTML templates; without this a video title such as
// "<img src=x onerror=alert(1)>" runs in the page.
function escapeHtml(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Breakdown signals arrive as 0..1 floats; render them honestly (a missing
// signal is 'n/a', never an invented 50%).
function pct(value) {
  return (value === null || value === undefined) ? 'n/a' : `${Math.round(value * 100)}%`;
}

// YouTube video ids are exactly 11 characters. Older builds stored placeholders
// such as 'queued', which produced links to a video that does not exist.
function isYouTubeVideoId(value) {
  return typeof value === 'string' && /^[A-Za-z0-9_-]{11}$/.test(value);
}

// Same escaping, plus a scheme allow-list, for values used inside href="".
function safeUrl(value) {
  const url = String(value || '').trim();
  if (!/^(https?:)?\/\//i.test(url) && !/^data:image\//i.test(url)) return '#';
  return escapeHtml(url);
}

const state = {
  jobs: [],
  activeJobId: null,
  activeJob: null,
  jobTimers: {}
};
window.state = state;

const els = {
  form: document.getElementById('jobForm'),
  videoUrl: document.getElementById('videoUrl'),
  urlMetaBadge: document.getElementById('urlMetaBadge'),
  clipCountInput: document.getElementById('clipCount'),
  provider: document.getElementById('provider'),
  captionStyle: document.getElementById('captionStyle'),
  layoutMode: document.getElementById('layoutMode'),
  whisperModel: document.getElementById('whisperModel'),
  hardwareInfo: document.getElementById('hardwareInfo'),
  transcriptionEstimate: document.getElementById('transcriptionEstimate'),
  status: document.getElementById('generatorStatus'),
  jobsList: document.getElementById('jobsList'),
  clipsGrid: document.getElementById('clipsGrid'),
  jobCount: document.getElementById('jobCount'),
  statsClipCount: document.getElementById('statsClipCount'),
  readyCount: document.getElementById('readyCount'),
  refreshJobsBtn: document.getElementById('refreshJobsBtn'),
  focusComposerBtn: document.getElementById('focusComposerBtn'),
  sourceTabUrl: document.getElementById('sourceTabUrl'),
  sourceTabUpload: document.getElementById('sourceTabUpload'),
  sourceUrlPanel: document.getElementById('sourceUrlPanel'),
  sourceUploadPanel: document.getElementById('sourceUploadPanel'),
  videoFile: document.getElementById('videoFile'),
  uploadDropZone: document.getElementById('uploadDropZone'),
  uploadDropContent: document.getElementById('uploadDropContent'),
  uploadFileInfo: document.getElementById('uploadFileInfo'),
  uploadFileName: document.getElementById('uploadFileName'),
  uploadFileSize: document.getElementById('uploadFileSize'),
  uploadRemoveBtn: document.getElementById('uploadRemoveBtn'),
  activeJobProgressPanel: document.getElementById('activeJobProgressPanel'),
  activeJobIdBadge: document.getElementById('activeJobIdBadge'),
  activeJobPercentText: document.getElementById('activeJobPercentText'),
  activeJobStageLabel: document.getElementById('activeJobStageLabel'),
  activeJobClipsCount: document.getElementById('activeJobClipsCount'),
  activeJobProgressBar: document.getElementById('activeJobProgressBar'),
  stepDownload: document.getElementById('stepDownload'),
  stepAudio: document.getElementById('stepAudio'),
  stepTranscribe: document.getElementById('stepTranscribe'),
  stepAI: document.getElementById('stepAI'),
  stepRender: document.getElementById('stepRender')
};

let selectedUploadFile = null;
let activeSourceMode = 'url'; // 'url' | 'upload'
state.systemModels = [];
state.currentVideoDuration = 300; // default 5 min reference until video is loaded

function setSourceMode(mode) {
  activeSourceMode = mode;
  const isUrl = mode === 'url';
  els.sourceTabUrl?.classList.toggle('active', isUrl);
  els.sourceTabUpload?.classList.toggle('active', !isUrl);
  if (els.sourceUrlPanel) els.sourceUrlPanel.style.display = isUrl ? 'block' : 'none';
  if (els.sourceUploadPanel) els.sourceUploadPanel.style.display = isUrl ? 'none' : 'block';
}

function formatFileSize(bytes) {
  if (!bytes) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function setupSourceTabs() {
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

function handleFileSelected(file) {
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

function setStatus(message, tone = 'default') {
  if (!els.status) {
	els.status = document.getElementById('generatorStatus');
  }
  if (!els.status) return;
  els.status.textContent = message;
  els.status.style.borderColor = tone === 'error'
	? 'rgba(255,77,141,0.35)'
	: tone === 'success'
	  ? 'rgba(104,240,179,0.32)'
	  : 'rgba(255,255,255,0.04)';
  els.status.style.color = tone === 'error' ? '#ffd9e7' : '#dfeaff';
}

function updateProgressUI(jobId, stage, percent, status, clipsCount) {
  const p = Math.max(0, Math.min(100, Number(percent) || 0));
  
  if (els.activeJobProgressPanel) {
    if (status === 'processing' || (status === 'completed' && p === 100) || status === 'created') {
      els.activeJobProgressPanel.style.display = 'block';
    } else if (status === 'failed') {
      els.activeJobProgressPanel.style.display = 'block';
    }
  }

  if (els.activeJobIdBadge) els.activeJobIdBadge.textContent = `#${jobId}`;
  if (els.activeJobPercentText) {
    els.activeJobPercentText.textContent = `${p}%`;
    els.activeJobPercentText.style.color = status === 'failed' ? '#ff4d8d' : (p === 100 ? '#68f0b3' : '#38bdf8');
  }
  if (els.activeJobStageLabel) {
    let icon = '⚡';
    const sLower = (stage || '').toLowerCase();
    if (sLower.includes('download')) icon = '📥';
    else if (sLower.includes('audio')) icon = '🎵';
    else if (sLower.includes('transcrib')) icon = '🎙️';
    else if (sLower.includes('clip') && sLower.includes('select')) icon = '🎯';
    else if (sLower.includes('render')) icon = '🎬';
    else if (status === 'completed') icon = '✅';
    else if (status === 'failed') icon = '❌';
    els.activeJobStageLabel.textContent = `${icon} ${stage || 'Processing...'}`;
  }
  if (els.activeJobClipsCount) {
    els.activeJobClipsCount.textContent = (clipsCount !== undefined && clipsCount > 0) ? `🎞️ ${clipsCount} clips ready` : '';
  }
  if (els.activeJobProgressBar) {
    els.activeJobProgressBar.style.width = `${p}%`;
    if (status === 'failed') {
      els.activeJobProgressBar.style.background = '#ff4d8d';
    } else if (p === 100) {
      els.activeJobProgressBar.style.background = 'linear-gradient(90deg, #34d399, #10b981)';
    } else {
      els.activeJobProgressBar.style.background = 'linear-gradient(90deg, #38bdf8, #818cf8, #34d399)';
    }
  }

  // Update Breadcrumbs
  const steps = [
    { el: els.stepDownload, min: 2, max: 20, label: '1. Download' },
    { el: els.stepAudio, min: 21, max: 24, label: '2. Audio' },
    { el: els.stepTranscribe, min: 25, max: 50, label: '3. Transcribe' },
    { el: els.stepAI, min: 51, max: 56, label: '4. AI Select' },
    { el: els.stepRender, min: 57, max: 100, label: '5. 9:16 Render' }
  ];

  steps.forEach(s => {
    if (!s.el) return;
    if (p >= s.max) {
      s.el.style.background = 'rgba(52, 211, 153, 0.18)';
      s.el.style.borderColor = 'rgba(52, 211, 153, 0.45)';
      s.el.style.color = '#68f0b3';
      s.el.textContent = `${s.label} ✓`;
    } else if (p >= s.min && p < s.max) {
      s.el.style.background = 'rgba(56, 189, 248, 0.25)';
      s.el.style.borderColor = '#38bdf8';
      s.el.style.color = '#38bdf8';
      s.el.style.fontWeight = '700';
      s.el.textContent = `${s.label} 🔄`;
    } else {
      s.el.style.background = 'rgba(255, 255, 255, 0.04)';
      s.el.style.borderColor = 'rgba(255, 255, 255, 0.05)';
      s.el.style.color = '#94a3b8';
      s.el.style.fontWeight = 'normal';
      s.el.textContent = s.label;
    }
  });

  // Also update sidebar job card if present
  const jobCard = document.querySelector(`[data-job-id="${jobId}"]`);
  if (jobCard) {
    const pWrap = jobCard.querySelector('.job-progress-wrap');
    const pBar = jobCard.querySelector('.job-card-progress-bar');
    const sText = jobCard.querySelector('.job-stage-text');
    if (pWrap) pWrap.style.display = (status === 'processing' || p < 100) ? 'block' : 'none';
    if (pBar) pBar.style.width = `${p}%`;
    if (sText) {
      sText.style.display = (status === 'processing' || p < 100) ? 'block' : 'none';
      sText.textContent = stage || 'Processing...';
    }
  }
}

state.playedSounds = state.playedSounds || {};

function playSuccessChime(jobId) {
  if (jobId && state.playedSounds[`done_${jobId}`]) return;
  if (jobId) state.playedSounds[`done_${jobId}`] = true;
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    const notes = [523.25, 659.25, 783.99, 1046.50]; // C5, E5, G5, C6 (joyful chord)
    notes.forEach((freq, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(freq, ctx.currentTime + i * 0.08);
      gain.gain.setValueAtTime(0, ctx.currentTime + i * 0.08);
      gain.gain.linearRampToValueAtTime(0.25, ctx.currentTime + i * 0.08 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + i * 0.08 + 0.35);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(ctx.currentTime + i * 0.08);
      osc.stop(ctx.currentTime + i * 0.08 + 0.38);
    });
  } catch (e) {
    console.warn('Audio chime could not play:', e);
  }
}

function playErrorSound(jobId) {
  if (jobId && state.playedSounds[`err_${jobId}`]) return;
  if (jobId) state.playedSounds[`err_${jobId}`] = true;
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    const freqs = [350.0, 220.0]; // Low alert tone
    freqs.forEach((freq, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sawtooth';
      osc.frequency.setValueAtTime(freq, ctx.currentTime + i * 0.16);
      gain.gain.setValueAtTime(0, ctx.currentTime + i * 0.16);
      gain.gain.linearRampToValueAtTime(0.22, ctx.currentTime + i * 0.16 + 0.03);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + i * 0.16 + 0.3);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(ctx.currentTime + i * 0.16);
      osc.stop(ctx.currentTime + i * 0.16 + 0.32);
    });
  } catch (e) {
    console.warn('Error audio could not play:', e);
  }
}

function setupWebSocket(jobId) {
  if (!jobId) return;
  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const apiPath = '/api';
  try {
    const ws = new WebSocket(`${protocol}://${window.location.host}${apiPath}/ws/progress/${jobId}`);

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        const stage = data.stage || 'Processing...';
        const percent = data.percent !== undefined ? data.percent : 0;
        const status = data.status || 'processing';
        const clipsCount = data.clips_count || 0;

        updateProgressUI(jobId, stage, percent, status, clipsCount);

        if (status === 'completed') {
          playSuccessChime(jobId);
        } else if (status === 'failed') {
          playErrorSound(jobId);
        }

        // If job just completed or gained new clips, live refresh the job details and clips grid!
        if (state.activeJobId === jobId || !state.activeJobId) {
          if (clipsCount > (state.activeJob?.clips?.length || 0) || status === 'completed') {
            loadJob(jobId, false);
          }
        }
      } catch (e) {
        console.error('Error parsing progress websocket payload:', e);
      }
    };

    ws.onerror = () => {
      // Quiet fallback — polling will cover it
    };
  } catch (err) {
    console.warn('WebSocket connection failed, relying on polling:', err);
  }
}

function asMediaUrl(path) {
  if (!path) return '';
  return `/media/${path.replace(/\\/g, '/').replace(/^\//, '')}`;
}

function formatTimeLabel(dateString) {
  if (!dateString) return '';
  let parseString = dateString;
  if (!parseString.endsWith('Z') && !parseString.includes('+')) {
    parseString += 'Z';
  }
  const date = new Date(parseString);
  if (isNaN(date.getTime())) return String(dateString);
  const timeStr = date.toLocaleTimeString('el-GR', { hour: '2-digit', minute: '2-digit', hour12: false });
  const dateStr = date.toLocaleDateString('el-GR', { day: '2-digit', month: '2-digit', year: 'numeric' });
  return `${timeStr}  ${dateStr}`;
}
window.formatTimeLabel = formatTimeLabel;

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
	headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
	...options
  });

  if (!response.ok) {
	const body = await response.text();
	throw new Error(body || `Request failed: ${response.status}`);
  }

  return response.json();
}
window.fetchJson = fetchJson;

function formatDuration(sec) {
  if (!sec || sec <= 0) return '0s';
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function updateTranscriptionEstimateBadge(durationSec) {
  if (!els.transcriptionEstimate || !state.systemModels || !state.systemModels.length) return;
  const activeModelId = els.whisperModel ? els.whisperModel.value : 'small';
  const modelObj = state.systemModels.find(m => m.id === activeModelId) || state.systemModels[0];
  if (modelObj) {
    let est = '~5-10 sec';
    if (!modelObj.is_cloud) {
      const calcSec = Math.max(5, Math.round(durationSec * (modelObj.rtf || 0.22)));
      est = calcSec < 60 ? `~${calcSec} sec` : `~${(calcSec / 60).toFixed(1).replace('.0', '')} min`;
    }
    els.transcriptionEstimate.style.display = 'block';
    els.transcriptionEstimate.innerHTML = `⏱️ <strong>Estimated Transcription:</strong> ${est} for ${formatDuration(durationSec)} video (${escapeHtml(modelObj.name)})`;
  }
}

function renderModelOptions(durationSec = 300) {
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

async function loadSystemInfo() {
  try {
    const data = await fetchJson(`${API_PREFIX}/system/info`);
    if (els.hardwareInfo) {
      els.hardwareInfo.innerHTML = `
        <strong>PC Specs:</strong> ${escapeHtml(data.hardware.cpu)} | ${escapeHtml(data.hardware.ram)} RAM | GPU: ${escapeHtml(data.hardware.gpu)} (${escapeHtml(data.hardware.vram)})
      `;
    }
    if (data.models) {
      state.systemModels = data.models;
      renderModelOptions(state.currentVideoDuration || 300);
    }
  } catch (error) {
    console.error('Failed to load system info:', error);
  }
}

async function listJobs() {
  try {
    const data = await fetchJson(`${API_PREFIX}/jobs`);
    const jobs = data.jobs;
    state.jobs = jobs;
    renderJobs(jobs);

    if (jobs.length > 0 && !state.activeJobId) {
      state.activeJobId = jobs[0].id;
      await loadJob(jobs[0].id);
    } else if (state.activeJobId) {
      await loadJob(state.activeJobId);
    }

    updateCounters(jobs);
  } catch (error) {
    setStatus(`Could not load jobs: ${error.message}`, 'error');
  }
}

function updateCounters(jobs) {
  const totalClips = jobs.reduce((acc, job) => acc + (job.clips ? job.clips.length : 0), 0);
  const readyClips = jobs.reduce((acc, job) => acc + (job.clips ? job.clips.filter(c => c.uploaded).length : 0), 0);

  if (els.jobCount) els.jobCount.textContent = String(jobs.length);
  if (els.statsClipCount) els.statsClipCount.textContent = String(totalClips);
  if (els.readyCount) els.readyCount.textContent = String(readyClips);
}

state.showAllJobs = false;

function renderJobs(jobs) {
  if (!els.jobsList) return;
  els.jobsList.innerHTML = '';

  const toggleBtn = document.getElementById('toggleJobsListBtn');

  if (jobs.length === 0) {
	els.jobsList.innerHTML = '<div class=\"empty-state\">No jobs found yet. Create your first viral clip above.</div>';
	if (toggleBtn) toggleBtn.style.display = 'none';
	return;
  }

  const template = document.getElementById('jobCardTemplate');
  const displayJobs = state.showAllJobs ? jobs : jobs.slice(0, 5);

  if (toggleBtn) {
    if (jobs.length > 5) {
      toggleBtn.style.display = 'block';
      toggleBtn.textContent = state.showAllJobs ? `🔼 Show Recent (5)` : `📂 Show All Jobs (${jobs.length})`;
      toggleBtn.onclick = () => {
        state.showAllJobs = !state.showAllJobs;
        renderJobs(state.jobs);
      };
    } else {
      toggleBtn.style.display = 'none';
    }
  }

  displayJobs.forEach(job => {
	const clone = template.content.cloneNode(true);
	const btn = clone.querySelector('.job-card');
	const idEl = clone.querySelector('.job-id');
	const statusEl = clone.querySelector('.job-status');
	const urlEl = clone.querySelector('.job-url');
	const timeEl = clone.querySelector('.job-time');
	const stageText = clone.querySelector('.job-stage-text');
	const progressWrap = clone.querySelector('.job-progress-wrap');
	const progressBar = clone.querySelector('.job-card-progress-bar');

	btn.dataset.jobId = String(job.id);
	if (String(job.id) === String(state.activeJobId)) {
	  btn.classList.add('active');
	}

	idEl.textContent = `#${job.id}`;
	statusEl.textContent = job.status;
	statusEl.className = `job-status ${job.status}`;

	if (job.status === 'processing' || job.status === 'created') {
	  if (progressWrap) progressWrap.style.display = 'block';
	  if (stageText) {
	    stageText.style.display = 'block';
	    stageText.textContent = job.current_stage || 'Processing...';
	  }
	  if (progressBar && job.progress_percent !== undefined) {
	    progressBar.style.width = `${job.progress_percent}%`;
	  }
	  setupWebSocket(job.id);
	}

	urlEl.textContent = job.youtube_url || 'Uploaded MP4';
	timeEl.textContent = formatTimeLabel(job.created_at);

	btn.addEventListener('click', () => {
	      state.activeJobId = job.id;
	      renderJobs(state.jobs);
	      loadJob(job.id);
	    });

	    // Add job control buttons: Start, Resume, Restart
	    const actionsDiv = clone.querySelector('.job-actions') || document.createElement('div');
	    actionsDiv.className = 'job-actions';
	    actionsDiv.innerHTML = `
	      <button class="job-control-btn start-btn" title="Start Job" ${job.status === 'processing' || job.status === 'created' ? 'disabled' : ''}>▶️ Start</button>
	      <button class="job-control-btn resume-btn" title="Resume Job" ${job.status === 'failed' || job.status === 'completed' ? 'disabled' : ''}>🔁 Resume</button>
	      <button class="job-control-btn restart-btn" title="Restart Job" ${job.status === 'failed' || job.status === 'completed' ? 'disabled' : ''}>🔄 Restart</button>
	    `;
	    if (!clone.querySelector('.job-actions')) {
	      const footer = clone.querySelector('.job-card-footer') || clone.querySelector('.job-card');
	      footer.appendChild(actionsDiv);
	    }

	    // Add event listeners for the new buttons
	    actionsDiv.querySelectorAll('.job-control-btn').forEach(button => {
	      button.addEventListener('click', async (e) => {
	        e.stopPropagation();
	        const btn = button;
	        const jobId = btn.closest('[data-job-id]').dataset.jobId;
	        btn.disabled = true;
	        const originalText = btn.textContent;
	        btn.textContent = '⏳';
	        try {
	          let endpoint = '';
	          if (btn.classList.contains('start-btn')) {
	            endpoint = `/api/jobs/${jobId}/start`;
	          } else if (btn.classList.contains('resume-btn')) {
	            endpoint = `/api/jobs/${jobId}/resume`;
	          } else if (btn.classList.contains('restart-btn')) {
	            endpoint = `/api/jobs/${jobId}/restart`;
	          }
	          if (endpoint) {
	            const response = await fetchJson(`${endpoint}`, { method: 'POST' });
	            // Optimistically update UI
	            if (btn.classList.contains('start-btn')) {
	              statusEl.textContent = 'created';
	              statusEl.className = 'job-status created';
	              if (progressWrap) progressWrap.style.display = 'block';
	              if (stageText) {
	                stageText.style.display = 'block';
	                stageText.textContent = 'Initializing...';
	              }
	              if (progressBar) {
	                progressBar.style.width = '2%';
	                progressBar.style.background = 'linear-gradient(90deg, #38bdf8, #818cf8)';
	              }
	              setupWebSocket(jobId);
	            } else if (btn.classList.contains('resume-btn') || btn.classList.contains('restart-btn')) {
	              statusEl.textContent = 'created';
	              statusEl.className = 'job-status created';
	              if (progressWrap) progressWrap.style.display = 'block';
	              if (stageText) {
	                stageText.style.display = 'block';
	                stageText.textContent = 'Initializing...';
	              }
	              if (progressBar) {
	                progressBar.style.width = '2%';
	                progressBar.style.background = 'linear-gradient(90deg, #38bdf8, #818cf8)';
	              }
	              setupWebSocket(jobId);
	            }
	          }
	        } catch (err) {
	          console.error('Job control failed:', err);
	          // Re-enable button on error
	          btn.disabled = false;
	          btn.textContent = originalText;
	          setStatus(`Failed to ${btn.title.toLowerCase()} job: ${err.message}`, 'error');
	        } finally {
	          btn.disabled = false;
	          btn.textContent = originalText;
	        }
	      });
	    });

	    els.jobsList.appendChild(btn);
  });
}

async function loadJob(jobId, showStatus = true) {
  try {
	const job = await fetchJson(`${API_PREFIX}/jobs/${jobId}`);
	state.activeJob = job;
	renderClips(job.clips || []);

	// Update live progress dashboard
	if (job.status === 'processing' || job.status === 'created') {
	  updateProgressUI(job.id, job.current_stage || 'Processing...', job.progress_percent || 0, job.status, (job.clips || []).length);
	  setupWebSocket(job.id);
	} else if (job.status === 'completed') {
	  updateProgressUI(job.id, 'All clips completed!', 100, job.status, (job.clips || []).length);
	} else if (job.status === 'failed') {
	  updateProgressUI(job.id, job.error_message || 'Job Failed', 100, job.status, (job.clips || []).length);
	}
  } catch (error) {
	if (showStatus) setStatus(`Could not load job details: ${error.message}`, 'error');
  }
}

function renderClips(clips) {
  const batchActions = document.getElementById('jobBatchActions');
  if (!els.clipsGrid) return;
  els.clipsGrid.innerHTML = '';

  if (clips.length === 0) {
    if (batchActions) batchActions.style.display = 'none';
	els.clipsGrid.innerHTML = '<div class="empty-state">No clips generated for this job yet.</div>';
	return;
  }

  if (batchActions) batchActions.style.display = 'flex';

  const fragment = document.createDocumentFragment();

  clips.forEach(clip => {
	  const card = document.createElement('article');
	  card.className = 'clip-card';

	  const preview = document.createElement('div');
	  preview.className = 'clip-preview';

	  if (clip.thumbnail_path) {
		const img = document.createElement('img');
		img.src = asMediaUrl(clip.thumbnail_path);
		img.alt = clip.title || 'Clip thumbnail';
		preview.appendChild(img);
	  } else if (clip.file_path) {
		const video = document.createElement('video');
		video.src = asMediaUrl(clip.file_path);
		video.preload = 'metadata';
		preview.appendChild(video);
	  }

	  const badge = document.createElement('div');
	  badge.className = 'overlay-badge';
	  const rawScore = clip.score || 0.85;
	  const viralityVal = rawScore > 1.0 ? Math.round(rawScore) : Math.round(rawScore * 100);
	  const bd = clip.score_breakdown;
	  const tooltipText = bd
	    ? `Virality Breakdown:
• LLM: ${pct(bd.llm)} (40%)
• Audio: ${pct(bd.audio)} (25%)
• Keywords: ${pct(bd.keyword)} (20%)
• Density: ${pct(bd.density)} (10%)
• Structure: ${pct(bd.structural)} (5%)`
	    : `Composite virality score ${viralityVal}/100
(No per-signal breakdown stored for this clip)`;
	  badge.title = tooltipText;
	  badge.style.cursor = 'help';
	  badge.innerHTML = `<span class="dot pink"></span> <span>${viralityVal}/100 VIRAL</span>`;
	  preview.appendChild(badge);

	  const content = document.createElement('div');
	  content.className = 'clip-content';

	  const topline = document.createElement('div');
	  topline.className = 'clip-topline';

	  const title = document.createElement('h3');
	  title.className = 'clip-title';
	  title.textContent = clip.title || `Clip #${clip.index + 1}`;

	  const score = document.createElement('div');
	  score.className = 'clip-score';
	  score.textContent = `${(clip.end - clip.start).toFixed(1)}s`;

	  topline.appendChild(title);
	  topline.appendChild(score);
	  content.appendChild(topline);

	  const meta = document.createElement('div');
	  meta.className = 'clip-meta';
	  meta.textContent = `${clip.start.toFixed(1)}s - ${clip.end.toFixed(1)}s | ${clip.caption_style || 'mrbeast'} | ${clip.layout_mode || 'single_speaker'}`;
	  content.appendChild(meta);

	  const actions = document.createElement('div');
	  actions.className = 'clip-actions';

	  const studioBtn = document.createElement('button');
	  studioBtn.className = 'primary-btn';
	  studioBtn.style.padding = '0.55rem 0.85rem';
	  studioBtn.style.fontSize = '0.78rem';
	  studioBtn.innerHTML = '🎬 <strong>Opus Studio</strong>';
	  studioBtn.addEventListener('click', () => openClipStudio(clip));
	  actions.appendChild(studioBtn);

	  if (isYouTubeVideoId(clip.youtube_video_id)) {
		const ytBtn = document.createElement('a');
		ytBtn.className = 'primary-btn';
		ytBtn.style.padding = '0.55rem 0.85rem';
		ytBtn.style.fontSize = '0.78rem';
		ytBtn.style.background = 'linear-gradient(135deg, #FF0000, #CC0000)';
		ytBtn.href = `https://youtube.com/shorts/${encodeURIComponent(clip.youtube_video_id)}`;
		ytBtn.target = '_blank';
		ytBtn.rel = 'noreferrer';
		ytBtn.innerHTML = '🔴 <strong>Shorts</strong>';
		actions.appendChild(ytBtn);
	  } else {
		const ytBtn = document.createElement('button');
		ytBtn.className = 'ghost-btn';
		ytBtn.style.padding = '0.55rem 0.85rem';
		ytBtn.style.fontSize = '0.78rem';
		ytBtn.innerHTML = '🚀 <strong>YouTube</strong>';
		ytBtn.addEventListener('click', () => {
		  openClipStudio(clip);
		  document.querySelector('.tab-nav-btn[data-tab="youtube"]')?.click();
		});
		actions.appendChild(ytBtn);
	  }

	  if (clip.file_path) {
		const rawBtn = document.createElement('a');
		rawBtn.className = 'ghost-btn link-btn';
		rawBtn.href = asMediaUrl(clip.file_path);
		rawBtn.target = '_blank';
		rawBtn.rel = 'noreferrer';
		rawBtn.textContent = 'MP4';
		rawBtn.style.textAlign = 'center';
		actions.appendChild(rawBtn);
	  }

	  content.appendChild(actions);
	  card.appendChild(preview);
	  card.appendChild(content);

	  fragment.appendChild(card);
  });

  els.clipsGrid.appendChild(fragment);
}

async function createJob(event) {
  event.preventDefault();
  const stylePrompt = document.getElementById('stylePrompt')?.value.trim() || '';
  const whisperModel = els.whisperModel ? els.whisperModel.value : 'tiny';
  const captionStyle = els.captionStyle ? els.captionStyle.value : 'mrbeast';
  const layoutMode = els.layoutMode ? els.layoutMode.value : 'auto';
  const targetClips = els.clipCountInput ? (parseInt(els.clipCountInput.value, 10) || 10) : 10;
  const autoUpload = document.getElementById('autoUploadCheck') ? document.getElementById('autoUploadCheck').checked : true;
  const llmTranslate = document.getElementById('llmTranslateCheck')?.checked || false;

  if (activeSourceMode === 'upload') {
    if (!selectedUploadFile) {
      setStatus('Please choose a video file to upload.', 'error');
      return;
    }
    setStatus(`Uploading ${selectedUploadFile.name}...`);
    try {
      const formData = new FormData();
      formData.append('file', selectedUploadFile);
      if (stylePrompt) formData.append('style_prompt', stylePrompt);
      formData.append('whisper_model', whisperModel);
      formData.append('caption_style', captionStyle);
      formData.append('layout_mode', layoutMode);
      formData.append('target_clips', targetClips);
      formData.append('auto_upload', autoUpload);
      formData.append('llm_translate', llmTranslate);

      const response = await fetch(`${API_PREFIX}/jobs/upload`, { method: 'POST', body: formData });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || 'Failed to create job from upload');
      }

      setStatus(`Job #${data.job_id} created (${targetClips} clips). Waiting for generation...`);
      state.activeJobId = data.job_id;
      updateProgressUI(data.job_id, 'Initializing...', 2, 'processing', 0);
      setupWebSocket(data.job_id);
      await listJobs();
      pollJob(data.job_id);
    } catch (error) {
      setStatus(`Upload failed: ${error.message}`, 'error');
    }
    return;
  }

  const videoUrl = els.videoUrl ? els.videoUrl.value.trim() : '';
  if (!videoUrl) {
	setStatus('Please enter a video URL or direct MP4 link.', 'error');
	return;
  }

  setStatus('Creating a new generated short batch...');
  try {
	const params = new URLSearchParams({ 
      youtube_url: videoUrl, 
      whisper_model: whisperModel,
      caption_style: captionStyle,
      layout_mode: layoutMode,
      target_clips: targetClips,
      auto_upload: autoUpload,
      llm_translate: llmTranslate
    });
	if (stylePrompt) params.append('style_prompt', stylePrompt);
	const response = await fetch(`${API_PREFIX}/jobs?${params.toString()}`, { method: 'POST' });
	const data = await response.json();

	if (!response.ok) {
	  throw new Error(data.detail || 'Failed to create job');
	}

	setStatus(`Job #${data.job_id} created (${targetClips} target clips). Processing...`);
	state.activeJobId = data.job_id;
	updateProgressUI(data.job_id, 'Starting video download...', 2, 'processing', 0);
	setupWebSocket(data.job_id);
	await listJobs();
	pollJob(data.job_id);
  } catch (error) {
	setStatus(`Job creation failed: ${error.message}`, 'error');
  }
}

async function pollJob(jobId) {
  if (state.jobTimers[jobId]) {
	clearInterval(state.jobTimers[jobId]);
  }

  state.jobTimers[jobId] = setInterval(async () => {
	try {
	  const job = await fetchJson(`${API_PREFIX}/jobs/${jobId}`);
	  if (job.status === 'processing' || job.status === 'created') {
		if (!window[`ws_${jobId}`]) {
			setupWebSocket(jobId);
			window[`ws_${jobId}`] = true;
		}
		updateProgressUI(jobId, job.current_stage || 'Processing...', job.progress_percent || 0, job.status, (job.clips || []).length);
	  }
	  if (job.status === 'completed' || job.status === 'failed') {
		clearInterval(state.jobTimers[jobId]);
		delete state.jobTimers[jobId];
		updateProgressUI(jobId, job.status === 'completed' ? 'All clips rendered!' : (job.error_message || 'Failed'), 100, job.status, (job.clips || []).length);
		
		if (job.status === 'completed') {
		  playSuccessChime(jobId);
		} else {
		  playErrorSound(jobId);
		}

		setStatus(job.status === 'completed'
		  ? `Job #${jobId} finished with ${job.clips.length} clips.`
		  : `Job #${jobId} failed: ${job.error_message || 'Unknown error'}`
		  , job.status === 'completed' ? 'success' : 'error');
		await listJobs();
	  }
	  if (Number(state.activeJobId) === Number(jobId)) {
		state.activeJob = job;
		renderClips(job.clips || []);
	  }
	} catch (error) {
	  console.error(error);
	}
  }, 2000);
}

if (els.form) els.form.addEventListener('submit', createJob);
if (els.refreshJobsBtn) els.refreshJobsBtn.addEventListener('click', listJobs);
if (els.focusComposerBtn) els.focusComposerBtn.addEventListener('click', () => els.videoUrl?.focus());
if (els.whisperModel) {
  els.whisperModel.addEventListener('change', () => {
    updateTranscriptionEstimateBadge(state.currentVideoDuration || 300);
  });
}

// Live YouTube URL metadata fetching & transcription time calculation
let urlDebounceTimer = null;
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

let currentStudioClip = null;

function setupStudioModal() {
  const modal = document.getElementById('clipStudioModal');
  const closeBtn = document.getElementById('closeStudioBtn');
  const videoPlayer = document.getElementById('studioVideoPlayer');
  const tabBtns = document.querySelectorAll('.tab-nav-btn');
  const tabStyle = document.getElementById('tabContentStyle');
  const tabAi = document.getElementById('tabContentAi');
  const tabDatabase = document.getElementById('tabContentDatabase');
  const tabManual = document.getElementById('tabContentManual');

  if (!modal) return;

  closeBtn?.addEventListener('click', () => {
    modal.style.display = 'none';
    if (videoPlayer) videoPlayer.pause();
  });

  modal.addEventListener('click', (e) => {
    if (e.target === modal) {
      modal.style.display = 'none';
      if (videoPlayer) videoPlayer.pause();
    }
  });

  tabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      tabBtns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const target = btn.dataset.tab;

      if (tabStyle) tabStyle.style.display = 'none';
      if (tabAi) tabAi.style.display = 'none';
      if (tabDatabase) tabDatabase.style.display = 'none';
      if (tabManual) tabManual.style.display = 'none';
      const tabYoutube = document.getElementById('tabContentYoutube');
      if (tabYoutube) tabYoutube.style.display = 'none';

      if (target === 'style' && tabStyle) tabStyle.style.display = 'flex';
      else if (target === 'ai' && tabAi) tabAi.style.display = 'flex';
      else if (target === 'youtube' && tabYoutube) tabYoutube.style.display = 'flex';
      else if (target === 'database' && tabDatabase) tabDatabase.style.display = 'block';
      else if (target === 'manual' && tabManual) tabManual.style.display = 'flex';
    });
  });

  // Preset Selector Buttons
  document.querySelectorAll('.preset-card').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.preset-card').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
    });
  });

  // Layout Selector Buttons
  document.querySelectorAll('.layout-card').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.layout-card').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
    });
  });

  document.querySelectorAll('.template-card').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.template-card').forEach(b => {
        b.classList.remove('active');
        b.style.borderColor = 'rgba(255,255,255,0.1)';
        b.style.background = 'rgba(255,255,255,0.04)';
      });
      btn.classList.add('active');
      btn.style.borderColor = 'var(--pink)';
      btn.style.background = 'rgba(255, 45, 120, 0.15)';
    });
  });

  // Apply Style & Re-render
    document.getElementById('studioApplyStyleBtn')?.addEventListener('click', async () => {
      if (!currentStudioClip) return;
      const activePresetBtn = document.querySelector('.preset-card.active');
      const activeLayoutBtn = document.querySelector('.layout-card.active');
      const activeTemplateBtn = document.querySelector('.template-card.active');
      const captionStyle = activePresetBtn ? activePresetBtn.dataset.style : 'mrbeast';
      const layoutMode = activeLayoutBtn ? activeLayoutBtn.dataset.layout : 'single_speaker';
      const thumbnailTemplate = activeTemplateBtn ? parseInt(activeTemplateBtn.dataset.template, 10) : 0;
      const title = document.getElementById('studioStyleTitle')?.value.trim();
      const karaoke = document.getElementById('studioKaraokeToggle')?.checked ?? true;
      const motionTitle = document.getElementById('studioMotionTitleToggle')?.checked ?? true;

      const statusEl = document.getElementById('studioRerenderStatus');
      if (statusEl) statusEl.textContent = '⏳ Rendering fast preview with new style & layout...';

      try {
        // The endpoint answers {message, clip:{...}} — reading updated.file_path
        // left the preview pointing at the old render after every re-render.
        const res = await fetchJson(`${API_PREFIX}/clips/${currentStudioClip.id}/rerender`, {
          method: 'POST',
          body: JSON.stringify({ caption_style: captionStyle, layout_mode: layoutMode, title: title, karaoke: karaoke, motion_title: motionTitle, thumbnail_template: thumbnailTemplate })
        });
      const updated = res.clip || res;
      if (statusEl) statusEl.textContent = '✅ Re-render complete!';
      if (videoPlayer && updated.file_path) {
        videoPlayer.src = asMediaUrl(updated.file_path) + `?t=${Date.now()}`;
        videoPlayer.load();
        videoPlayer.play().catch(() => {});
      }
      currentStudioClip = updated;
      await loadJob(state.activeJobId);
    } catch (err) {
      if (statusEl) statusEl.textContent = `❌ Re-render failed: ${err.message}`;
    }
  });

  document.querySelectorAll('.quick-prompt-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const input = document.getElementById('studioAiInput');
      if (input) {
        input.value = btn.dataset.prompt;
        sendStudioAiPrompt();
      }
    });
  });

  document.getElementById('studioAiSendBtn')?.addEventListener('click', sendStudioAiPrompt);
  document.getElementById('studioAiInput')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') sendStudioAiPrompt();
  });

  document.getElementById('studioSaveManualBtn')?.addEventListener('click', async () => {
    if (!currentStudioClip) return;
    const newTitle = document.getElementById('studioEditTitle').value.trim();
    const newStart = parseFloat(document.getElementById('studioEditStart').value);
    const newEnd = parseFloat(document.getElementById('studioEditEnd').value);

    try {
      setStatus(`Trimming clip #${currentStudioClip.id}...`);
      
      // Precision Trim via new API
      await fetchJson(`/api/clips/${currentStudioClip.id}/trim`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start: newStart, end: newEnd })
      });

      if (newTitle && newTitle !== currentStudioClip.title) {
        await fetchJson(`/api/clips/${currentStudioClip.id}/update-caption`, {
          method: 'PATCH',
          body: JSON.stringify({ title: newTitle })
        });
      }

      setStatus(`Clip #${currentStudioClip.id} trimmed and updated successfully!`, 'success');
      
      await loadJob(state.activeJobId);
      modal.style.display = 'none';
      videoPlayer.pause();
    } catch (e) {
      setStatus(`Failed to trim/update clip: ${e.message}`, 'error');
    }
  });

  document.getElementById('studioUploadBtn')?.addEventListener('click', async () => {
    if (!currentStudioClip) return;
    // This used to PATCH uploaded=true with youtube_video_id='queued'. That
    // wrote a fake publish into the database: the clip card rendered a dead
    // youtube.com/shorts/queued link and the worker's duplicate-upload guard
    // (which skips any clip that already has a video id) then refused to
    // upload it for real. Run the actual upload flow instead.
    document.querySelector('.tab-nav-btn[data-tab="youtube"]')?.click();
    document.getElementById('ytDoUploadBtn')?.click();
  });
}

async function sendStudioAiPrompt() {
  if (!currentStudioClip) return;
  const input = document.getElementById('studioAiInput');
  const chatBox = document.getElementById('studioAiChatBox');
  const promptText = input.value.trim();
  if (!promptText) return;

  const userMsg = document.createElement('div');
  userMsg.className = 'ai-chat-msg user';
  userMsg.textContent = promptText;
  chatBox.appendChild(userMsg);
  input.value = '';
  chatBox.scrollTop = chatBox.scrollHeight;

  const thinkingMsg = document.createElement('div');
  thinkingMsg.className = 'ai-chat-msg system';
  thinkingMsg.textContent = 'OmniRoute AI Director is re-editing your short...';
  chatBox.appendChild(thinkingMsg);
  chatBox.scrollTop = chatBox.scrollHeight;

  try {
    const res = await fetchJson(`/api/clips/${currentStudioClip.id}/ai-edit`, {
      method: 'POST',
      body: JSON.stringify({ prompt: promptText })
    });
    thinkingMsg.innerHTML = `✨ <strong>Success!</strong><br/>${escapeHtml(res.message || 'Clip updated successfully.')}`;
    chatBox.scrollTop = chatBox.scrollHeight;
    setStatus(`AI edit applied successfully!`, 'success');
    await loadJob(state.activeJobId);
    const videoPlayer = document.getElementById('studioVideoPlayer');
    if (videoPlayer && res.clip?.file_path) {
      videoPlayer.src = `${asMediaUrl(res.clip.file_path)}?t=${Date.now()}`;
      videoPlayer.play();
    }
  } catch (err) {
    thinkingMsg.textContent = `❌ AI edit failed: ${err.message}`;
    chatBox.scrollTop = chatBox.scrollHeight;
  }
}

function openClipStudio(clip) {
  currentStudioClip = clip;
  const modal = document.getElementById('clipStudioModal');
  const title = document.getElementById('studioModalTitle');
  const videoPlayer = document.getElementById('studioVideoPlayer');
  const timeBadge = document.getElementById('studioTimeBadge');
  const scoreBadge = document.getElementById('studioScoreBadge');
  const editTitle = document.getElementById('studioEditTitle');
  const editStart = document.getElementById('studioEditStart');
  const editEnd = document.getElementById('studioEditEnd');
  const openRawBtn = document.getElementById('studioOpenRawBtn');
    const openFolderBtn = document.getElementById('studioOpenFolderBtn');
    const chatBox = document.getElementById('studioAiChatBox');

  const studioStyleTitle = document.getElementById('studioStyleTitle');
  const viralityScoreVal = document.getElementById('studioViralityScoreVal');
  const viralityExplanation = document.getElementById('studioViralityExplanation');

  if (!modal) return;

  title.textContent = clip.title || `Clip #${clip.id}`;
  videoPlayer.src = asMediaUrl(clip.file_path) + `?t=${Date.now()}`;
  videoPlayer.play().catch(() => {});

  timeBadge.textContent = `${clip.start.toFixed(1)}s → ${clip.end.toFixed(1)}s`;
  const rawStudioScore = clip.score || 0.85;
  const viralityPct = rawStudioScore > 1.0 ? Math.round(rawStudioScore) : Math.round(rawStudioScore * 100);
  scoreBadge.textContent = `${viralityPct}/100 Virality`;

  if (studioStyleTitle) studioStyleTitle.value = clip.title || '';
  if (viralityScoreVal) viralityScoreVal.textContent = `${viralityPct}/100`;
  if (viralityExplanation) {
    viralityExplanation.textContent = clip.virality_explanation || 
      'High-retention hook with active speaker pacing, high semantic curiosity gap, and optimal 9:16 vertical framing.';
  }

  // Set active preset button
  const currentStyle = clip.caption_style || 'mrbeast';
  document.querySelectorAll('.preset-card').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.style === currentStyle);
  });

  // Set active layout button
  const currentLayout = clip.layout_mode || 'single_speaker';
  document.querySelectorAll('.layout-card').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.layout === currentLayout);
  });

  // Set active thumbnail template button
  const currentTemplate = String(clip.thumbnail_template ?? (clip.index ? clip.index % 3 : 0));
  document.querySelectorAll('.template-card').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.template === currentTemplate);
    if (btn.dataset.template === currentTemplate) {
      btn.style.borderColor = 'var(--pink)';
      btn.style.background = 'rgba(255, 45, 120, 0.15)';
    } else {
      btn.style.borderColor = 'rgba(255,255,255,0.1)';
      btn.style.background = 'rgba(255,255,255,0.04)';
    }
  });

  editTitle.value = clip.title || '';
  editStart.value = clip.start.toFixed(1);
  editEnd.value = clip.end.toFixed(1);
  if (openRawBtn) openRawBtn.href = asMediaUrl(clip.file_path);

  const dbClipIdInput = document.getElementById('dbClipId');
  const dbJobIdInput = document.getElementById('dbJobId');
  const dbStateInfoInput = document.getElementById('dbStateInfo');
  if (dbClipIdInput) dbClipIdInput.value = clip.id;
  if (dbJobIdInput) dbJobIdInput.value = clip.job_id || state.activeJobId || 'N/A';
  if (dbStateInfoInput) dbStateInfoInput.value = `Score: ${clip.score ? clip.score.toFixed(2) : '0.00'} | Style: ${clip.caption_style || 'mrbeast'} | Layout: ${clip.layout_mode || 'single_speaker'}`

  // Pre-fill YouTube Shorts tab inputs
  const ytTitleInput = document.getElementById('ytUploadTitle');
  const ytDescInput = document.getElementById('ytUploadDesc');
  const ytHashtagsInput = document.getElementById('ytUploadHashtags');
  const ytStatusEl = document.getElementById('ytUploadStatus');

  let defaultTags = ['#Shorts', '#Viral', '#Trending', '#FYP'];
  if (Array.isArray(clip.hashtags) && clip.hashtags.length > 0) {
    defaultTags = clip.hashtags.map(t => t.startsWith('#') ? t : `#${t}`);
  } else if (clip.platform_metadata?.youtube?.hashtags) {
    defaultTags = clip.platform_metadata.youtube.hashtags.map(t => t.startsWith('#') ? t : `#${t}`);
  }
  const tagsStr = defaultTags.join(' ');

  let rawTitle = clip.title || `Viral Short #${clip.id}`;
  if (!rawTitle.toLowerCase().includes('#shorts')) {
    rawTitle = `${rawTitle} #Shorts`;
  }
  if (ytTitleInput) ytTitleInput.value = rawTitle.slice(0, 100);

  let defaultDesc = clip.description || clip.platform_metadata?.youtube?.description || clip.title || 'High-impact vertical short generated by AI Shorts Generator.';
  if (ytDescInput) ytDescInput.value = `${defaultDesc}\n\n${tagsStr}\n\n#Shorts #YouTubeShorts`;
  if (ytHashtagsInput) ytHashtagsInput.value = tagsStr;

  if (ytStatusEl) {
    if (isYouTubeVideoId(clip.youtube_video_id)) {
      ytStatusEl.innerHTML = `🟢 <strong>Live on YouTube:</strong> <a href="https://youtube.com/shorts/${encodeURIComponent(clip.youtube_video_id)}" target="_blank" style="color: #38bdf8; text-decoration: underline;">youtube.com/shorts/${escapeHtml(clip.youtube_video_id)}</a>`;
    } else if (clip.uploaded) {
      // Legacy rows were written with uploaded=true and a placeholder id by the
      // old "queue upload" button; they were never actually published.
      ytStatusEl.innerHTML = '<span style="color: #f59e0b;">⚠️ Marked uploaded, but no YouTube video id was recorded — re-upload this clip.</span>';
    } else if (clip.upload_error) {
      ytStatusEl.innerHTML = `<span style="color: #ef4444;">⚠️ Last upload error: ${escapeHtml(clip.upload_error)}</span>`;
    } else {
      ytStatusEl.textContent = '';
    }
  }

  chatBox.innerHTML = `
      <div class="ai-chat-msg system">⚡ Opus AI Director online! 🎬 Ask me to drop a punchy hook, tune the pacing, trim dead air, or adapt the style.</div>
    `;

    if (openFolderBtn) {
      // Remove previous listeners using clone
      const newOpenFolderBtn = openFolderBtn.cloneNode(true);
      openFolderBtn.parentNode.replaceChild(newOpenFolderBtn, openFolderBtn);
      newOpenFolderBtn.addEventListener('click', async () => {
        try {
          await fetchJson(`${API_PREFIX}/jobs/${clip.job_id}/open-folder`, { method: 'POST' });
          setStatus(`Folder opened on host machine.`, 'success');
        } catch (err) {
          setStatus(`Could not open folder: ${err.message}`, 'error');
        }
      });
    }

    // Setup timeline editor
  const trackBar = document.getElementById('studioTimelineTrackBar');
  const selectionRange = document.getElementById('studioTimelineSelectionRange');
  const playhead = document.getElementById('studioTimelinePlayhead');
  const timeStartLabel = document.getElementById('studioTimeStartLabel');
  const timeEndLabel = document.getElementById('studioTimeEndLabel');
  const timeDurLabel = document.getElementById('studioTimeDurationLabel');

  const maxTotalDuration = Math.max(120, clip.end + 30);
  timeStartLabel.textContent = `0.0s`;
  timeEndLabel.textContent = `${maxTotalDuration.toFixed(1)}s`;

  function updateTimelineVisuals() {
    const startVal = parseFloat(editStart.value) || 0;
    const endVal = parseFloat(editEnd.value) || (startVal + 10);
    const dur = Math.max(0.1, endVal - startVal);
    timeDurLabel.textContent = `Duration: ${dur.toFixed(1)}s`;

    const leftPct = (startVal / maxTotalDuration) * 100;
    const widthPct = ((endVal - startVal) / maxTotalDuration) * 100;
    selectionRange.style.left = `${Math.max(0, Math.min(100, leftPct))}%`;
    selectionRange.style.width = `${Math.max(1, Math.min(100 - leftPct, widthPct))}%`;
  }

  editStart.addEventListener('input', updateTimelineVisuals);
  editEnd.addEventListener('input', updateTimelineVisuals);
  updateTimelineVisuals();

  videoPlayer.ontimeupdate = () => {
    if (!isNaN(videoPlayer.duration) && videoPlayer.duration > 0) {
      const curPct = (videoPlayer.currentTime / maxTotalDuration) * 100;
      playhead.style.left = `${Math.max(0, Math.min(100, curPct))}%`;
    }
  };

  trackBar.onclick = (e) => {
    const rect = trackBar.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const clickPct = Math.max(0, Math.min(1, clickX / rect.width));
    const targetTime = clickPct * maxTotalDuration;
    videoPlayer.currentTime = targetTime;
  };

  modal.style.display = 'flex';
}

let youtubeStatus = { authenticated: false, channel_name: '', has_client_secrets: false };

async function checkYouTubeStatus() {
  try {
    const res = await fetchJson(`${API_PREFIX}/youtube/status`);
    youtubeStatus = res;
    
    const topbarBtn = document.getElementById('topbarYtStatusBtn');
    const topbarDot = document.getElementById('topbarYtDot');
    const topbarLabel = document.getElementById('topbarYtLabel');
    const bannerText = document.getElementById('ytAuthStatusText');
    const modalHeading = document.getElementById('modalYtStatusHeading');
    const modalSub = document.getElementById('modalYtStatusSub');

    if (res.authenticated) {
      if (topbarDot) topbarDot.style.background = '#22c55e';
      if (topbarLabel) topbarLabel.textContent = `YouTube: ${res.channel_name || 'Connected'}`;
      if (bannerText) bannerText.innerHTML = `🟢 <strong>Connected:</strong> <span style="color: #22c55e;">${escapeHtml(res.channel_name || 'YouTube Channel')}</span>`;
      if (modalHeading) modalHeading.innerHTML = `🟢 Connected Channel: ${escapeHtml(res.channel_name || 'Active')}`;
      if (modalSub) modalSub.textContent = 'Ready to upload shorts instantly!';
    } else {
      if (res.has_client_secrets) {
        if (topbarDot) topbarDot.style.background = '#f59e0b';
        if (topbarLabel) topbarLabel.textContent = 'Connect YouTube';
        if (bannerText) bannerText.innerHTML = `⚪ <strong>client_secrets.json detected.</strong> Click to connect channel.`;
        if (modalHeading) modalHeading.textContent = 'Status: Credentials found, authorization needed';
        if (modalSub) modalSub.textContent = 'Proceed to Step 2 to authorize your YouTube channel.';
      } else {
        if (topbarDot) topbarDot.style.background = '#94a3b8';
        if (topbarLabel) topbarLabel.textContent = 'YouTube Setup';
        if (bannerText) bannerText.innerHTML = `⚪ <strong>YouTube:</strong> Not configured (client_secrets.json needed)`;
        if (modalHeading) modalHeading.textContent = 'Status: No client_secrets.json detected';
        if (modalSub) modalSub.textContent = 'Please download client_secrets.json from Google Cloud Console and upload it in Step 1.';
      }
    }
  } catch (err) {
    console.warn('YouTube status check failed:', err);
  }
}

function setupYouTubeIntegration() {
  const modal = document.getElementById('ytAuthModal');
  const openTopbarBtn = document.getElementById('topbarYtStatusBtn');
  const openStudioBannerBtn = document.getElementById('ytConnectBtn');
  const closeBtn = document.getElementById('closeYtAuthModalBtn');

  function openModal() {
    if (modal) {
      modal.style.display = 'flex';
      checkYouTubeStatus();
    }
  }

  openTopbarBtn?.addEventListener('click', openModal);
  openStudioBannerBtn?.addEventListener('click', openModal);
  closeBtn?.addEventListener('click', () => { if (modal) modal.style.display = 'none'; });
  modal?.addEventListener('click', (e) => {
    if (e.target === modal) modal.style.display = 'none';
  });

  // Step 1: Upload client_secrets.json
  const fileInput = document.getElementById('ytModalSecretsFileInput');
  const uploadBtn = document.getElementById('ytModalUploadSecretsBtn');
  const uploadStatus = document.getElementById('ytModalUploadStatus');

  uploadBtn?.addEventListener('click', () => fileInput?.click());
  fileInput?.addEventListener('change', async () => {
    if (!fileInput.files || fileInput.files.length === 0) return;
    const file = fileInput.files[0];
    const formData = new FormData();
    formData.append('file', file);
    if (uploadStatus) uploadStatus.textContent = 'Uploading credentials...';
    try {
      const resp = await fetch(`${API_PREFIX}/youtube/client-secrets`, {
        method: 'POST',
        body: formData
      });
      const data = await resp.json();
      if (resp.ok && data.success) {
        if (uploadStatus) uploadStatus.textContent = '✅ client_secrets.json saved!';
        checkYouTubeStatus();
      } else {
        if (uploadStatus) uploadStatus.textContent = `❌ ${data.detail || 'Upload failed'}`;
      }
    } catch (err) {
      if (uploadStatus) uploadStatus.textContent = `❌ Error: ${err.message}`;
    }
  });

  // Listen for callback window success message
  window.addEventListener('message', (event) => {
    // The OAuth popup is served by this same app, so anything else is not our callback.
    if (event.origin !== window.location.origin) return;
    if (event.data && event.data.type === 'YOUTUBE_AUTH_SUCCESS') {
      const channel = event.data.channel || 'YouTube Channel';
      const progressEl = document.getElementById('ytModalAuthProgress');
      if (progressEl) progressEl.innerHTML = `<span style="color: #22c55e; font-weight: 600;">🎉 Successfully Connected as ${escapeHtml(channel)}!</span>`;
      checkYouTubeStatus();
      setTimeout(() => { if (modal) modal.style.display = 'none'; }, 1500);
    }
  });

  // Step 2: Open Google Auth Consent (1-Click Loopback Flow)
  const getAuthUrlBtn = document.getElementById('ytModalGetAuthUrlBtn');
  const authProgressEl = document.getElementById('ytModalAuthProgress');
  let authPollInterval = null;

  getAuthUrlBtn?.addEventListener('click', async () => {
    try {
      getAuthUrlBtn.disabled = true;
      getAuthUrlBtn.textContent = '⏳ Preparing Google login...';
      if (authProgressEl) authProgressEl.innerHTML = '<span style="color: #38bdf8;">Opening Google login window...</span>';

      const data = await fetchJson(`${API_PREFIX}/youtube/auth-url`, { method: 'POST' });
      if (data.auth_url) {
        const width = 520;
        const height = 660;
        const left = Math.max(0, (window.screen.width - width) / 2);
        const top = Math.max(0, (window.screen.height - height) / 2);
        const authWindow = window.open(
          data.auth_url,
          'google_oauth_popup',
          `width=${width},height=${height},top=${top},left=${left},menubar=no,toolbar=no,location=no,status=no`
        );
        getAuthUrlBtn.innerHTML = '⏳ Waiting for Google sign-in...';
        if (authProgressEl) {
          authProgressEl.innerHTML = '<span style="color: #38bdf8;">👉 Please select your Google account in the popup window.</span>';
        }

        // Poll every 1.5 seconds to detect when authorization completes
        if (authPollInterval) clearInterval(authPollInterval);
        authPollInterval = setInterval(async () => {
          try {
            const statusData = await fetchJson(`${API_PREFIX}/youtube/status`);
            if (statusData && statusData.authenticated) {
              clearInterval(authPollInterval);
              authPollInterval = null;
              if (authProgressEl) {
                authProgressEl.innerHTML = `<span style="color: #22c55e; font-weight: 600;">🎉 Channel Connected: ${escapeHtml(statusData.channel_name || 'YouTube')}</span>`;
              }
              getAuthUrlBtn.textContent = '✅ Connected Successfully!';
              checkYouTubeStatus();
              setTimeout(() => { if (modal) modal.style.display = 'none'; }, 1500);
            }
          } catch (e) {
            // Ignore polling errors
          }
        }, 1500);

      } else {
        alert('Could not generate auth URL. Please make sure client_secrets.json is uploaded.');
        getAuthUrlBtn.textContent = '🚀 Connect YouTube Channel (1-Click)';
      }
    } catch (err) {
      alert(`Error: ${err.message}`);
      getAuthUrlBtn.textContent = '🚀 Connect YouTube Channel (1-Click)';
      if (authProgressEl) authProgressEl.innerHTML = `<span style="color: #ef4444;">❌ ${escapeHtml(err.message)}</span>`;
    } finally {
      getAuthUrlBtn.disabled = false;
    }
  });

  // Step 3: Submit Code
  const submitCodeBtn = document.getElementById('ytModalSubmitCodeBtn');
  const authCodeInput = document.getElementById('ytModalAuthCodeInput');
  const authResult = document.getElementById('ytModalAuthResult');

  submitCodeBtn?.addEventListener('click', async () => {
    const code = authCodeInput?.value.trim();
    if (!code) {
      if (authResult) authResult.innerHTML = '<span style="color: #ef4444;">Please paste the Google authorization code first.</span>';
      return;
    }
    if (authResult) authResult.textContent = '⏳ Verifying code with Google...';
    submitCodeBtn.disabled = true;
    try {
      const resp = await fetch(`${API_PREFIX}/youtube/authorize-code`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: code })
      });
      const data = await resp.json();
      if (resp.ok && data.success) {
        if (authResult) authResult.innerHTML = `<span style="color: #22c55e;">🎉 Authorized as <strong>${escapeHtml(data.channel_name || 'YouTube Channel')}</strong>!</span>`;
        checkYouTubeStatus();
        setTimeout(() => { if (modal) modal.style.display = 'none'; }, 1500);
      } else {
        if (authResult) authResult.innerHTML = `<span style="color: #ef4444;">❌ ${escapeHtml(data.detail || 'Authorization failed')}</span>`;
      }
    } catch (err) {
      if (authResult) authResult.innerHTML = `<span style="color: #ef4444;">❌ Error: ${escapeHtml(err.message)}</span>`;
    } finally {
      submitCodeBtn.disabled = false;
    }
  });

  // Studio YouTube Upload Action
  const doUploadBtn = document.getElementById('ytDoUploadBtn');
  doUploadBtn?.addEventListener('click', async () => {
    if (!currentStudioClip) return;
    const title = document.getElementById('ytUploadTitle')?.value.trim() || '';
    const desc = document.getElementById('ytUploadDesc')?.value.trim() || '';
    const hashtagsRaw = document.getElementById('ytUploadHashtags')?.value.trim() || '';
    const privacy = document.getElementById('ytUploadPrivacy')?.value || 'unlisted';
    const statusEl = document.getElementById('ytUploadStatus');

    // Parse hashtags
    const hashtags = hashtagsRaw
      .split(/[\s,]+/)
      .map(t => t.trim())
      .filter(t => t.length > 0)
      .map(t => t.startsWith('#') ? t : `#${t}`);

    if (statusEl) statusEl.innerHTML = '⏳ <strong>Uploading to YouTube Shorts via Data API v3...</strong> (Please do not close this window)';
    doUploadBtn.disabled = true;

    try {
      const res = await fetchJson(`${API_PREFIX}/clips/${currentStudioClip.id}/upload-youtube`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: title,
          description: desc,
          hashtags: hashtags,
          privacy_status: privacy
        })
      });

      if (res.success && res.video_id) {
        const shortUrl = res.url || `https://youtube.com/shorts/${res.video_id}`;
        if (statusEl) {
          statusEl.innerHTML = `🎉 <strong>Uploaded Successfully!</strong> <a href="${safeUrl(shortUrl)}" target="_blank" style="color: #38bdf8; text-decoration: underline; font-weight: bold;">View YouTube Short ↗</a>`;
        }
        currentStudioClip.uploaded = true;
        currentStudioClip.youtube_video_id = res.video_id;
        setStatus(`Clip #${currentStudioClip.id} uploaded to YouTube!`, 'success');
        await loadJob(state.activeJobId);
      } else {
        if (statusEl) statusEl.innerHTML = `<span style="color: #ef4444;">❌ Upload failed: ${escapeHtml(res.error || 'Unknown error')}</span>`;
      }
    } catch (err) {
      if (statusEl) statusEl.innerHTML = `<span style="color: #ef4444;">❌ Upload error: ${escapeHtml(err.message)}</span>`;
    } finally {
      doUploadBtn.disabled = false;
    }
  });
}

let tiktokStatus = { authenticated: false, username: '', has_token: false };

async function checkTikTokStatus() {
  try {
    const res = await fetchJson(`${API_PREFIX}/tiktok/status`);
    tiktokStatus = res;
    const topbarLabel = document.getElementById('topbarTikTokLabel');
    const topbarDot = document.getElementById('topbarTikTokDot');
    const banner = document.getElementById('tiktokStatusBanner');
    const usernameInput = document.getElementById('tiktokUsernameInput');
    const disconnectBtn = document.getElementById('disconnectTikTokBtn');

    if (res.authenticated) {
      if (topbarLabel) topbarLabel.textContent = `TikTok: ${res.username || 'Connected'}`;
      if (topbarDot) topbarDot.style.background = '#00f2fe';
      if (banner) banner.innerHTML = `🟢 <strong>Connected:</strong> <span style="color: #00f2fe;">${escapeHtml(res.username)}</span>`;
      if (usernameInput) usernameInput.value = res.username || '';
      if (disconnectBtn) disconnectBtn.style.display = 'block';
    } else {
      if (topbarLabel) topbarLabel.textContent = 'TikTok Auth';
      if (topbarDot) topbarDot.style.background = '#64748b';
      if (banner) banner.innerHTML = `⚪ <strong>No TikTok account connected.</strong> Link your TikTok handle to enable 1-click publishing.`;
      if (disconnectBtn) disconnectBtn.style.display = 'none';
    }
    return res;
  } catch (err) {
    console.warn('Could not check TikTok status:', err);
    return { authenticated: false };
  }
}

function setupTikTokIntegration() {
  const topbarBtn = document.getElementById('topbarTikTokStatusBtn');
  const modal = document.getElementById('tiktokAuthModal');
  const closeBtn = document.getElementById('closeTikTokModalBtn');
  const saveBtn = document.getElementById('saveTikTokAuthBtn');
  const disconnectBtn = document.getElementById('disconnectTikTokBtn');
  const usernameInput = document.getElementById('tiktokUsernameInput');
  const sessionInput = document.getElementById('tiktokSessionInput');
  const resultMsg = document.getElementById('tiktokAuthResultMsg');

  topbarBtn?.addEventListener('click', () => {
    checkTikTokStatus();
    if (modal) modal.style.display = 'flex';
  });

  closeBtn?.addEventListener('click', () => {
    if (modal) modal.style.display = 'none';
  });

  modal?.addEventListener('click', (e) => {
    if (e.target === modal) modal.style.display = 'none';
  });

  saveBtn?.addEventListener('click', async () => {
    const user = usernameInput?.value.trim() || '';
    const sess = sessionInput?.value.trim() || '';
    if (!user) {
      if (resultMsg) resultMsg.innerHTML = '<span style="color: #ef4444;">Please enter your TikTok username.</span>';
      return;
    }
    if (!sess) {
      if (resultMsg) resultMsg.innerHTML = '<span style="color: #ef4444;">Please paste your TikTok sessionid cookie or token.</span>';
      return;
    }

    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving...';
    try {
      const res = await fetchJson(`${API_PREFIX}/tiktok/connect`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: user, session_id: sess })
      });

      if (res.success) {
        if (resultMsg) resultMsg.innerHTML = `<span style="color: #22c55e;">🎉 Connected as <strong>${escapeHtml(res.username)}</strong>!</span>`;
        if (sessionInput) sessionInput.value = '';
        await checkTikTokStatus();
        setTimeout(() => {
          if (modal) modal.style.display = 'none';
          if (resultMsg) resultMsg.textContent = '';
        }, 1200);
      }
    } catch (err) {
      if (resultMsg) resultMsg.innerHTML = `<span style="color: #ef4444;">❌ Failed: ${escapeHtml(err.message)}</span>`;
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = '✅ Connect TikTok Account';
    }
  });

  disconnectBtn?.addEventListener('click', async () => {
    if (!confirm('Disconnect TikTok account?')) return;
    try {
      await fetchJson(`${API_PREFIX}/tiktok/disconnect`, { method: 'POST' });
      await checkTikTokStatus();
      if (resultMsg) resultMsg.innerHTML = '<span style="color: #cbd5e1;">Account disconnected.</span>';
    } catch (err) {
      console.warn('Disconnect failed:', err);
    }
  });
}

function setupBatchUpload() {
  const batchYtBtn = document.getElementById('batchUploadYtBtn');
  const batchTikTokBtn = document.getElementById('batchUploadTikTokBtn');
  const privacySelect = document.getElementById('batchPrivacySelect');
  const modal = document.getElementById('batchUploadModal');
  const closeBtn = document.getElementById('closeBatchUploadModalBtn');
  const kicker = document.getElementById('batchModalKicker');
  const heading = document.getElementById('batchModalTitle');
  const statusText = document.getElementById('batchUploadStatusText');
  const countText = document.getElementById('batchUploadCountText');
  const progressBar = document.getElementById('batchUploadProgressBar');
  const resultsList = document.getElementById('batchUploadResultsList');

  closeBtn?.addEventListener('click', () => {
    if (modal) modal.style.display = 'none';
  });

  modal?.addEventListener('click', (e) => {
    if (e.target === modal) modal.style.display = 'none';
  });

  // YouTube Batch Upload Handler
  batchYtBtn?.addEventListener('click', async () => {
    if (!state.activeJobId) {
      alert('Please select a job first.');
      return;
    }

    // 1. Check YouTube Auth status
    try {
      const auth = await fetchJson(`${API_PREFIX}/youtube/status`);
      if (!auth || !auth.authenticated) {
        const authModal = document.getElementById('ytAuthModal');
        if (authModal) authModal.style.display = 'flex';
        return;
      }
    } catch (e) {
      console.warn('Could not verify YouTube status:', e);
    }

    if (kicker) kicker.textContent = 'YouTube Shorts Automation';
    if (heading) heading.textContent = '🚀 Batch Uploading to YouTube';

    const privacy = privacySelect?.value || 'unlisted';
    if (modal) modal.style.display = 'flex';
    if (resultsList) resultsList.innerHTML = '<div style="color: var(--muted); font-size: 0.85rem;">⏳ Preparing clips and generating viral SEO tags...</div>';
    if (statusText) statusText.textContent = `🚀 Uploading all clips from Job #${state.activeJobId} to YouTube (${privacy})...`;
    if (countText) countText.textContent = 'In Progress...';
    if (progressBar) progressBar.style.width = '30%';

    batchYtBtn.disabled = true;
    batchYtBtn.innerHTML = '⏳ Uploading to YouTube...';

    try {
      const res = await fetchJson(`${API_PREFIX}/jobs/${state.activeJobId}/upload-all-youtube`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ privacy_status: privacy })
      });

      if (progressBar) progressBar.style.width = '100%';
      if (statusText) {
        statusText.innerHTML = `🎉 <strong>Batch YouTube Upload Complete!</strong> (${res.uploaded_count || 0}/${res.total_clips || 0} clips uploaded)`;
      }
      if (countText) {
        countText.textContent = `${res.uploaded_count || 0} / ${res.total_clips || 0}`;
      }

      if (resultsList && res.results) {
        resultsList.innerHTML = '';
        res.results.forEach(r => {
          const item = document.createElement('div');
          item.style.padding = '10px 14px';
          item.style.borderRadius = '8px';
          item.style.background = r.status === 'uploaded' ? 'rgba(34, 197, 94, 0.08)' : 'rgba(239, 68, 68, 0.08)';
          item.style.border = r.status === 'uploaded' ? '1px solid rgba(34, 197, 94, 0.25)' : '1px solid rgba(239, 68, 68, 0.25)';
          item.style.display = 'flex';
          item.style.justifyContent = 'space-between';
          item.style.alignItems = 'center';
          item.style.gap = '10px';

          if (r.status === 'uploaded') {
            item.innerHTML = `
              <div style="font-size: 0.85rem; font-weight: 600; color: #fff; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 320px;">
                🎬 ${escapeHtml(r.title)}
              </div>
              <a href="${safeUrl(r.shorts_url)}" target="_blank" style="background: linear-gradient(135deg, #FF0000, #CC0000); color: #fff; text-decoration: none; font-size: 0.75rem; font-weight: 700; padding: 5px 12px; border-radius: 6px; box-shadow: 0 2px 6px rgba(255,0,0,0.3); white-space: nowrap;">
                View Short ↗
              </a>
            `;
          } else {
            item.innerHTML = `
              <div style="font-size: 0.85rem; font-weight: 600; color: #ef4444;">
                ❌ ${escapeHtml(r.title)}: ${escapeHtml(r.error || 'Failed')}
              </div>
            `;
          }
          resultsList.appendChild(item);
        });
      }

      await loadJob(state.activeJobId);
      setStatus(`Batch YouTube upload completed for Job #${state.activeJobId}!`, 'success');

    } catch (err) {
      if (statusText) statusText.innerHTML = `<span style="color: #ef4444;">❌ Batch upload failed: ${escapeHtml(err.message)}</span>`;
      if (progressBar) progressBar.style.width = '0%';
    } finally {
      batchYtBtn.disabled = false;
      batchYtBtn.innerHTML = '<span>🚀 Upload All to YouTube</span>';
    }
  });

  // TikTok Batch Upload Handler
  batchTikTokBtn?.addEventListener('click', async () => {
    if (!state.activeJobId) {
      alert('Please select a job first.');
      return;
    }

    // Check TikTok auth first!
    const auth = await checkTikTokStatus();
    if (!auth || !auth.authenticated) {
      const tiktokModal = document.getElementById('tiktokAuthModal');
      if (tiktokModal) tiktokModal.style.display = 'flex';
      return;
    }

    if (kicker) kicker.textContent = 'TikTok Direct Publishing';
    if (heading) heading.textContent = '🎵 Batch Publishing to TikTok';

    if (modal) modal.style.display = 'flex';
    if (resultsList) resultsList.innerHTML = '<div style="color: var(--muted); font-size: 0.85rem;">⏳ Formatting clips with viral TikTok hashtags (#FYP, #Trending, #ClippinEvrything)...</div>';
    if (statusText) statusText.textContent = `🎵 Uploading all clips from Job #${state.activeJobId} to TikTok for ${auth.username || 'your channel'}...`;
    if (countText) countText.textContent = 'In Progress...';
    if (progressBar) progressBar.style.width = '35%';

    batchTikTokBtn.disabled = true;
    batchTikTokBtn.innerHTML = '⏳ Uploading to TikTok...';

    try {
      const res = await fetchJson(`${API_PREFIX}/jobs/${state.activeJobId}/upload-all-tiktok`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ privacy_level: (document.getElementById('batchPrivacySelect')?.value || 'unlisted') === 'public' ? 'PUBLIC_TO_EVERYONE' : 'PUBLIC_TO_SELF' })
      });

      if (progressBar) progressBar.style.width = '100%';
      if (statusText) {
        statusText.innerHTML = `🎉 <strong>Batch TikTok Upload Complete!</strong> (${res.uploaded_count || 0}/${res.total_clips || 0} clips published)`;
      }
      if (countText) {
        countText.textContent = `${res.uploaded_count || 0} / ${res.total_clips || 0}`;
      }

      if (resultsList && res.results) {
        resultsList.innerHTML = '';
        res.results.forEach(r => {
          const item = document.createElement('div');
          item.style.padding = '10px 14px';
          item.style.borderRadius = '8px';
          item.style.background = r.status === 'uploaded' ? 'rgba(0, 242, 254, 0.08)' : 'rgba(239, 68, 68, 0.08)';
          item.style.border = r.status === 'uploaded' ? '1px solid rgba(0, 242, 254, 0.25)' : '1px solid rgba(239, 68, 68, 0.25)';
          item.style.display = 'flex';
          item.style.justifyContent = 'space-between';
          item.style.alignItems = 'center';
          item.style.gap = '10px';

          if (r.status === 'uploaded') {
            item.innerHTML = `
              <div style="font-size: 0.85rem; font-weight: 600; color: #fff; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 320px;">
                🎵 ${escapeHtml(r.title)}
              </div>
              <a href="${safeUrl(r.tiktok_url)}" target="_blank" style="background: linear-gradient(135deg, #00f2fe, #4facfe); color: #000; text-decoration: none; font-size: 0.75rem; font-weight: 700; padding: 5px 12px; border-radius: 6px; box-shadow: 0 2px 6px rgba(0,242,254,0.3); white-space: nowrap;">
                View on TikTok ↗
              </a>
            `;
          } else {
            item.innerHTML = `
              <div style="font-size: 0.85rem; font-weight: 600; color: #ef4444;">
                ❌ ${escapeHtml(r.title)}: ${escapeHtml(r.error || 'Failed')}
              </div>
            `;
          }
          resultsList.appendChild(item);
        });
      }

      await loadJob(state.activeJobId);
      setStatus(`Batch TikTok upload completed for Job #${state.activeJobId}!`, 'success');

    } catch (err) {
      if (statusText) statusText.innerHTML = `<span style="color: #ef4444;">❌ TikTok batch upload failed: ${escapeHtml(err.message)}</span>`;
      if (progressBar) progressBar.style.width = '0%';
    } finally {
      batchTikTokBtn.disabled = false;
      batchTikTokBtn.innerHTML = '<span>🎵 Upload All to TikTok</span>';
    }
  });
}

setupStudioModal();
setupYouTubeIntegration();
setupTikTokIntegration();
setupBatchUpload();
checkYouTubeStatus();
checkTikTokStatus();
loadSystemInfo();
listJobs();

// Periodic live-sync for running jobs
setInterval(() => {
  const hasRunningJob = (state.jobs || []).some(j => j.status === 'processing' || j.status === 'created');
  if (hasRunningJob) {
    listJobs();
  }
}, 2500);
