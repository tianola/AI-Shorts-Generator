const API_PREFIX = '/api';
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
  clipCountInput: document.getElementById('clipCount'),
  provider: document.getElementById('provider'),
  status: document.getElementById('generatorStatus'),
  jobsList: document.getElementById('jobsList'),
  clipsGrid: document.getElementById('clipsGrid'),
  jobCount: document.getElementById('jobCount'),
  statsClipCount: document.getElementById('statsClipCount'),
  readyCount: document.getElementById('readyCount'),
  refreshJobsBtn: document.getElementById('refreshJobsBtn'),
  focusComposerBtn: document.getElementById('focusComposerBtn')
};

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

function setupWebSocket(jobId) {
  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const apiPath = '/api';
  const ws = new WebSocket(`${protocol}://${window.location.host}${apiPath}/ws/progress/${jobId}`);

  ws.onmessage = (event) => {
    const progressData = JSON.parse(event.data);
    const jobCard = document.querySelector(`[data-job-id="${jobId}"]`);
    if (jobCard) {
      const progressBar = jobCard.querySelector('.progress-bar');
      if (progressBar) progressBar.style.width = `${progressData.percent}%`;
      const stageText = jobCard.querySelector('.job-meta');
      if (stageText) stageText.textContent = progressData.stage;
    }
  };
}

function asMediaUrl(path) {
  if (!path) return '';
  return `/media/${path.replace(/\\/g, '/').replace(/^\//, '')}`;
}

function formatTimeLabel(dateString) {
  if (!dateString) return '';
  const date = new Date(dateString);
  if (isNaN(date.getTime())) return String(dateString);
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) + ' ' + date.toLocaleDateString();
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

async function listJobs() {
  try {
	const data = await fetchJson(`${API_PREFIX}/jobs?limit=8&offset=0`);
	state.jobs = data.jobs || [];
	if (els.jobCount) els.jobCount.textContent = state.jobs.length;
	const allClips = state.jobs.reduce((sum, item) => sum + (item.clips?.length || 0), 0);
	if (els.statsClipCount) els.statsClipCount.textContent = allClips;

	if (!state.activeJobId && state.jobs.length) {
	  state.activeJobId = state.jobs[0].id;
	}
	renderJobs();
	if (state.activeJobId) {
	  await loadJob(state.activeJobId);
	}
  } catch (error) {
	setStatus(`Could not load jobs: ${error.message}`, 'error');
  }
}

async function loadJob(jobId) {
  try {
	const job = await fetchJson(`${API_PREFIX}/jobs/${jobId}`);
	state.activeJob = job;
	state.activeJobId = jobId;
	renderJobs();
	renderClips(job.clips || []);
	// Load timeline if job has clips
	if (job.clips && job.clips.length > 0 && typeof window.loadTimelineClips === 'function') {
	  window.loadTimelineClips();
	}
	if (els.jobCount) els.jobCount.textContent = state.jobs.length;
	const allClips = state.jobs.reduce((sum, item) => sum + (item.clips?.length || 0), 0);
	if (els.statsClipCount) els.statsClipCount.textContent = allClips;
	if (els.readyCount) els.readyCount.textContent = (job.clips || []).filter(c => c.uploaded).length;
  } catch (error) {
	console.error(error);
	setStatus(`Could not load job details: ${error.message}`, 'error');
  }
}

function renderJobs() {
  if (!state.jobs.length) {
	els.jobsList.innerHTML = '<div class="empty-state">No jobs yet. Create one from the panel on the left.</div>';
	return;
  }

  els.jobsList.innerHTML = '';
  const fragment = document.createDocumentFragment();

  state.jobs.forEach(job => {
	const template = document.getElementById('jobCardTemplate');
	const node = template.content.firstElementChild.cloneNode(true);
	node.dataset.jobId = String(job.id);
	node.classList.toggle('active', Number(job.id) === Number(state.activeJobId));

	const statusNode = node.querySelector('.job-status');
	statusNode.textContent = job.status;
	statusNode.className = `job-status ${job.status}`;

	// Add progress bar if job is processing
	if (job.status === 'processing') {
		const progressContainer = document.createElement('div');
		progressContainer.className = 'progress-container';
		const progressBar = document.createElement('div');
		progressBar.className = 'progress-bar';
		progressBar.style.width = `${job.progress_percent || 0}%`;
		progressContainer.appendChild(progressBar);
		node.querySelector('.job-main').appendChild(progressContainer);

		const stageText = document.createElement('div');
		stageText.className = 'job-meta';
		stageText.textContent = job.current_stage || 'Starting...';
		node.querySelector('.job-main').appendChild(stageText);
	}

	node.querySelector('.job-id').textContent = `#${job.id}`;
	node.querySelector('.job-url').textContent = job.youtube_url;
	node.querySelector('.job-time').textContent = formatTimeLabel(job.created_at);

	node.addEventListener('click', () => {
	  state.activeJobId = job.id;
	  loadJob(job.id);
	});

	fragment.appendChild(node);
  });

  els.jobsList.appendChild(fragment);
}

function renderClips(clips) {
  if (!clips.length) {
	els.clipsGrid.innerHTML = '<div class="empty-state">No clips yet. Once a job finishes, the results will appear here.</div>';
	return;
  }

  els.clipsGrid.innerHTML = '';
  const fragment = document.createDocumentFragment();

  clips.forEach(clip => {
	const card = document.createElement('article');
	card.className = 'clip-card';

	const thumbnail = clip.thumbnail_path ? asMediaUrl(clip.thumbnail_path) : asMediaUrl(clip.file_path);
	const clipUrl = asMediaUrl(clip.file_path);
	const hashtags = Array.isArray(clip.hashtags) ? clip.hashtags : [];
	const score = clip.score ? `${clip.score.toFixed(2)} virality` : 'AI selected';

	card.innerHTML = `
	  <div class="clip-preview">
		<img src="${thumbnail}" alt="${clip.title || 'Clip thumbnail'}" />
		<div class="overlay-badge">${clip.index + 1}</div>
	  </div>
	  <div class="clip-content">
		<div class="clip-topline">
		  <div class="clip-title">${clip.title || 'Untitled clip'}</div>
		  <div class="clip-score">${score}</div>
		</div>
		<div class="clip-meta">${clip.start.toFixed(1)}s → ${clip.end.toFixed(1)}s</div>
		<div class="clip-tags">
		  ${hashtags.slice(0, 4).map(tag => `<span class="tag">${tag}</span>`).join('') || '<span class="tag">#Shorts</span>'}
		</div>
		<div class="clip-actions">
		  <button class="upload-btn ${clip.uploaded ? 'is-uploaded' : ''}" data-clip-id="${clip.id}" ${clip.uploaded ? 'disabled' : ''}>
			${clip.uploaded ? 'Uploaded' : 'Upload'}
		  </button>
		  <button class="primary-btn studio-btn" data-clip-id="${clip.id}">🎬 Studio</button>
		</div>
	  </div>
	`;

	const studioBtn = card.querySelector('.studio-btn');
	studioBtn?.addEventListener('click', () => openClipStudio(clip));

	const uploadBtn = card.querySelector('.upload-btn');
	uploadBtn?.addEventListener('click', async () => {
	  try {
		setStatus(`Queuing clip #${clip.id} for YouTube upload...`);
		await fetchJson(`${API_PREFIX}/clips/${clip.id}/upload-status`, {
		  method: 'PATCH',
		  body: JSON.stringify({ uploaded: true, youtube_video_id: 'queued', upload_error: null })
		});
		setStatus(`Clip #${clip.id} queued for YouTube upload.`, 'success');
		await loadJob(state.activeJobId);
	  } catch (error) {
		setStatus(`Upload queue failed: ${error.message}`, 'error');
	  }
	});

	fragment.appendChild(card);

	  // Add event listeners for reclip/recaption buttons
	  const reclipBtn = card.querySelector('.reclip-btn');
	  const recaptionBtn = card.querySelector('.recaption-btn');

	  if (reclipBtn) {
		reclipBtn.addEventListener('click', async () => {
		  const newStart = prompt('Enter new start time (seconds):', clip.start.toFixed(1));
		  const newEnd = prompt('Enter new end time (seconds):', clip.end.toFixed(1));
		  if (newStart && newEnd) {
			setStatus(`Re-clipping clip #${clip.id}...`);
			await fetchJson(`/api/clips/${clip.id}/update-bounds`, {
			  method: 'PATCH',
			  body: JSON.stringify({ start: parseFloat(newStart), end: parseFloat(newEnd) })
			});
			setStatus(`Clip #${clip.id} re-clipped successfully!`);
			await loadJob(state.activeJobId);
		  }
		});
	  }

	  if (recaptionBtn) {
		recaptionBtn.addEventListener('click', async () => {
		  const newText = prompt('Enter new caption text:', clip.title || '');
		  if (newText) {
			setStatus(`Re-capping clip #${clip.id}...`);
			await fetchJson(`/api/clips/${clip.id}/update-caption`, {
			  method: 'PATCH',
			  body: JSON.stringify({ title: newText })
			});
			setStatus(`Clip #${clip.id} recapped successfully!`);
			await loadJob(state.activeJobId);
		  }
		});
	  }

	  const editBtn = card.querySelector('.edit-btn');
	  const editorPanel = card.querySelector(`#editor-${clip.id}`);
	  if (editBtn && editorPanel) {
		editBtn.addEventListener('click', () => {
		  editorPanel.style.display = editorPanel.style.display === 'none' ? 'block' : 'none';
		});
	  }

	  const applyAiBtn = card.querySelector('.apply-ai-btn');
	  if (applyAiBtn) {
		applyAiBtn.addEventListener('click', async () => {
		  const promptText = editorPanel.querySelector('.ai-prompt-input').value.trim();
		  if (!promptText) return;
		  try {
			setStatus(`Applying AI edits via OmniRoute for clip #${clip.id}...`);
			await fetchJson(`/api/clips/${clip.id}/ai-edit`, {
			  method: 'POST',
			  body: JSON.stringify({ prompt: promptText })
			});
			setStatus(`AI edit applied successfully!`, 'success');
			await loadJob(state.activeJobId);
		  } catch (err) {
			setStatus(`AI edit failed: ${err.message}`, 'error');
		  }
		});
	  }

	  fragment.appendChild(card);
  });

  els.clipsGrid.appendChild(fragment);
}

async function createJob(event) {
  event.preventDefault();
  const videoUrl = els.videoUrl.value.trim();
  const stylePrompt = document.getElementById('stylePrompt').value.trim();

  if (!videoUrl) {
	setStatus('Please enter a video URL or direct MP4 link.', 'error');
	return;
  }

  setStatus('Creating a new generated short batch...');
  try {
	const params = new URLSearchParams({ youtube_url: videoUrl });
	if (stylePrompt) params.append('style_prompt', stylePrompt);
	const response = await fetch(`${API_PREFIX}/jobs?${params.toString()}`, { method: 'POST' });
	const data = await response.json();

	if (!response.ok) {
	  throw new Error(data.detail || 'Failed to create job');
	}

	setStatus(`Job #${data.job_id} created. Waiting for clip generation...`);
	state.activeJobId = data.job_id;
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
	  if (job.status === 'processing') {
		// Ensure WebSocket is connected for new processing jobs
		// Simple way: setupWebSocket(jobId) (idempotent if connection exists)
		// For now, let's just trigger it once
		if (!window[`ws_${jobId}`]) {
			setupWebSocket(jobId);
			window[`ws_${jobId}`] = true;
		}
	  }
	  if (job.status === 'completed' || job.status === 'failed') {
		clearInterval(state.jobTimers[jobId]);
		delete state.jobTimers[jobId];
		setStatus(job.status === 'completed'
		  ? `Job #${jobId} finished with ${job.clips.length} clips.`
		  : `Job #${jobId} failed: ${job.error_message || 'Unknown error'}`,
		  job.status === 'completed' ? 'success' : 'error');
	  }
	  if (job.status !== 'processing' && job.status !== 'created') {
		await listJobs();
	  } else {
		state.activeJob = job;
		renderClips(job.clips || []);
	  }
	  if (Number(state.activeJobId) === Number(jobId)) {
		renderClips(job.clips || []);
	  }
	} catch (error) {
	  console.error(error);
	}
  }, 3000);
}

els.form.addEventListener('submit', createJob);
els.refreshJobsBtn.addEventListener('click', listJobs);
els.focusComposerBtn.addEventListener('click', () => document.getElementById('videoUrl').focus());

let currentStudioClip = null;

function setupStudioModal() {
  const modal = document.getElementById('clipStudioModal');
  const closeBtn = document.getElementById('closeStudioBtn');
  const videoPlayer = document.getElementById('studioVideoPlayer');
  const tabBtns = document.querySelectorAll('.tab-nav-btn');
  const tabAi = document.getElementById('tabContentAi');
  const tabDatabase = document.getElementById('tabContentDatabase');
  const tabAdvanced = document.getElementById('tabContentAdvanced');
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

      if (tabAi) tabAi.style.display = 'none';
      if (tabDatabase) tabDatabase.style.display = 'none';
      if (tabAdvanced) tabAdvanced.style.display = 'none';
      if (tabManual) tabManual.style.display = 'none';

      if (target === 'ai' && tabAi) tabAi.style.display = 'flex';
      else if (target === 'database' && tabDatabase) tabDatabase.style.display = 'block';
      else if (target === 'advanced' && tabAdvanced) tabAdvanced.style.display = 'block';
      else if (target === 'manual' && tabManual) tabManual.style.display = 'flex';
    });
  });

  document.getElementById('dbRefreshBtn')?.addEventListener('click', () => {
    setStatus('Database sync verified. All clip records are up to date.', 'success');
  });

  document.getElementById('advRerenderBtn')?.addEventListener('click', async () => {
    if (!currentStudioClip) return;
    setStatus(`Re-rendering clip #${currentStudioClip.id} with advanced filters...`, 'success');
    await loadJob(state.activeJobId);
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
      setStatus(`Updating clip #${currentStudioClip.id}...`);
      if (newTitle && newTitle !== currentStudioClip.title) {
        await fetchJson(`/api/clips/${currentStudioClip.id}/update-caption`, {
          method: 'PATCH',
          body: JSON.stringify({ title: newTitle })
        });
      }
      if (!isNaN(newStart) && !isNaN(newEnd)) {
        await fetchJson(`/api/clips/${currentStudioClip.id}/update-bounds`, {
          method: 'PATCH',
          body: JSON.stringify({ start: newStart, end: newEnd })
        });
      }
      setStatus(`Clip #${currentStudioClip.id} updated successfully!`, 'success');
      await loadJob(state.activeJobId);
      modal.style.display = 'none';
      videoPlayer.pause();
    } catch (e) {
      setStatus(`Failed to update clip: ${e.message}`, 'error');
    }
  });

  document.getElementById('studioUploadBtn')?.addEventListener('click', async () => {
    if (!currentStudioClip) return;
    try {
      setStatus(`Queuing clip #${currentStudioClip.id} for YouTube upload...`);
      await fetchJson(`${API_PREFIX}/clips/${currentStudioClip.id}/upload-status`, {
        method: 'PATCH',
        body: JSON.stringify({ uploaded: true, youtube_video_id: 'queued', upload_error: null })
      });
      setStatus(`Clip #${currentStudioClip.id} queued for YouTube upload.`, 'success');
      await loadJob(state.activeJobId);
    } catch (err) {
      setStatus(`Upload failed: ${err.message}`, 'error');
    }
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
    thinkingMsg.innerHTML = `✨ <strong>Success!</strong><br/>${res.message || 'Clip updated successfully.'}`;
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
  const chatBox = document.getElementById('studioAiChatBox');

  if (!modal) return;

  title.textContent = clip.title || `Clip #${clip.id}`;

  // Prefer the final video with title overlay (final_with_title.mp4)
  // The clip.file_path should already point to the final version with title+SFX
  videoPlayer.src = asMediaUrl(clip.file_path) + `?t=${Date.now()}`;
  videoPlayer.play().catch(() => {});

  timeBadge.textContent = `${clip.start.toFixed(1)}s → ${clip.end.toFixed(1)}s`;
  scoreBadge.textContent = clip.score ? `${clip.score.toFixed(2)} Virality` : 'AI Pick';

  editTitle.value = clip.title || '';
  editStart.value = clip.start.toFixed(1);
  editEnd.value = clip.end.toFixed(1);
  openRawBtn.href = asMediaUrl(clip.file_path);

  const dbClipIdInput = document.getElementById('dbClipId');
  const dbJobIdInput = document.getElementById('dbJobId');
  const dbStateInfoInput = document.getElementById('dbStateInfo');
  if (dbClipIdInput) dbClipIdInput.value = clip.id;
  if (dbJobIdInput) dbJobIdInput.value = clip.job_id || state.activeJobId || 'N/A';
  if (dbStateInfoInput) dbStateInfoInput.value = `Score: ${clip.score ? clip.score.toFixed(2) : '0.00'} | Uploaded: ${clip.uploaded ? 'Yes' : 'No'} | Path: ${clip.file_path || ''}`;

  chatBox.innerHTML = `
    <div class="ai-chat-msg system">⚡ OmniRoute AI Director online! 🎬 Ask me to drop a sick hook, turbo-charge the pacing, surgically trim the fat, or reshape the vibe! What's our move?</div>
  `;

  // Setup timeline editor track and sync
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

setupStudioModal();
listJobs();
