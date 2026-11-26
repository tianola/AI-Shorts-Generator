/**
 * youtube.js - part of the job dashboard, split out of app.js.
 *
 * YouTube sign-in state and the per-clip upload panel.
 */

import { loadJob } from './jobs.js';
import { state } from './state.js';
import { setStatus } from './status.js';
import { API_PREFIX, escapeHtml, fetchJson, safeUrl } from './util.js';


export let youtubeStatus = { authenticated: false, channel_name: '', has_client_secrets: false };

export async function checkYouTubeStatus() {
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
      // The panel used to guess from `has_client_secrets` alone, which told an
      // operator who was connected and then lost the authorization to go "click to
      // connect" without ever saying that Google had refused the saved token.
      const reason = res.reason || '';
      if (res.has_client_secrets) {
        if (topbarDot) topbarDot.style.background = '#f59e0b';
        if (topbarLabel) topbarLabel.textContent = 'Connect YouTube';
        if (bannerText) bannerText.innerHTML = `⚪ <strong>YouTube not connected:</strong> <span style="color: #f59e0b;">${escapeHtml(reason || 'client_secrets.json detected. Click to connect channel.')}</span>`;
        if (modalHeading) modalHeading.textContent = 'Status: Credentials found, but the channel is not connected';
        if (modalSub) modalSub.textContent = reason || 'Proceed to Step 2 to authorize your YouTube channel.';
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

export function setupYouTubeIntegration() {
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

}
