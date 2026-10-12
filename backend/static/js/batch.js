/**
 * batch.js - part of the job dashboard, split out of app.js.
 *
 * Upload every clip of the open job to both platforms in one go.
 */

import { loadJob } from './jobs.js';
import { state } from './state.js';
import { setStatus } from './status.js';
import { checkTikTokStatus } from './tiktok.js';
import { API_PREFIX, escapeHtml, fetchJson, safeUrl } from './util.js';


export function setupBatchUpload() {
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
