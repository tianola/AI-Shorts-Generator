/**
 * jobs.js - part of the job dashboard, split out of app.js.
 *
 * The queue: create a job, list them, open one, render its clips, and
 * poll a job the websocket is not watching.
 */

import { activeSourceMode, renderModelOptions, selectedUploadFile } from './source.js';
import { els, state } from './state.js';
import { playErrorSound, playSuccessChime, setStatus, setupWebSocket, updateProgressUI, updateTranscriptionEstimateBadge } from './status.js';
import { openClipStudio } from './studio.js';
import { API_PREFIX, asMediaUrl, escapeHtml, fetchJson, formatTimeLabel, isYouTubeVideoId, pct } from './util.js';


export async function loadSystemInfo() {
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

export async function listJobs() {
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

export function updateCounters(jobs) {
  const totalClips = jobs.reduce((acc, job) => acc + (job.clips ? job.clips.length : 0), 0);
  const readyClips = jobs.reduce((acc, job) => acc + (job.clips ? job.clips.filter(c => c.uploaded).length : 0), 0);

  if (els.jobCount) els.jobCount.textContent = String(jobs.length);
  if (els.statsClipCount) els.statsClipCount.textContent = String(totalClips);
  if (els.readyCount) els.readyCount.textContent = String(readyClips);
}

state.showAllJobs = false;

export function renderJobs(jobs) {
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

export async function loadJob(jobId, showStatus = true) {
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

export function renderClips(clips) {
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

export async function createJob(event) {
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

export async function pollJob(jobId) {
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
