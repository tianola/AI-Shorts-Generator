/**
 * tiktok.js - part of the job dashboard, split out of app.js.
 *
 * TikTok sign-in state and the per-clip upload panel.
 */

import { API_PREFIX, escapeHtml, fetchJson } from './util.js';


export let tiktokStatus = { authenticated: false, username: '', has_token: false };

export async function checkTikTokStatus() {
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

export function setupTikTokIntegration() {
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
