/**
 * status.js - part of the job dashboard, split out of app.js.
 *
 * The status line, the progress bar, the step list, the chime, and
 * the websocket that feeds them.
 */

import { loadJob } from './jobs.js';
import { els, state } from './state.js';
import { escapeHtml, formatDuration } from './util.js';


export function setStatus(message, tone = 'default') {
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

export function updateProgressUI(jobId, stage, percent, status, clipsCount) {
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

export function playSuccessChime(jobId) {
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

export function playErrorSound(jobId) {
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

export function setupWebSocket(jobId) {
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

export function updateTranscriptionEstimateBadge(durationSec) {
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
