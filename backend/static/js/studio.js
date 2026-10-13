/**
 * studio.js - part of the job dashboard, split out of app.js.
 *
 * The clip studio modal: preview, trim, caption style and the AI
 * prompt box.
 */

import { loadJob } from './jobs.js';
import { state } from './state.js';
import { setStatus } from './status.js';
import { API_PREFIX, asMediaUrl, escapeHtml, fetchJson, isYouTubeVideoId } from './util.js';


export let currentStudioClip = null;

export function setupStudioModal() {
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

export async function sendStudioAiPrompt() {
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

export function openClipStudio(clip) {
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
      // Which arm this clip shipped with and what it earned, so the studio shows
      // the experiment's result next to the video it belongs to. Both are blank
      // until a stats sync has run (POST /api/analytics/sync-views).
      const arm = clip.hook_style
        ? ` · hook arm <strong>${escapeHtml(clip.hook_style)}</strong>` : '';
      const views = clip.view_count === null || clip.view_count === undefined
        ? '' : ` · <strong>${Number(clip.view_count).toLocaleString('en-US')}</strong> views`;
      ytStatusEl.innerHTML = `🟢 <strong>Live on YouTube:</strong> <a href="https://youtube.com/shorts/${encodeURIComponent(clip.youtube_video_id)}" target="_blank" style="color: #38bdf8; text-decoration: underline;">youtube.com/shorts/${escapeHtml(clip.youtube_video_id)}</a>${arm}${views}`;
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
