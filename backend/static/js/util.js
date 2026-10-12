/**
 * util.js - part of the job dashboard, split out of app.js.
 *
 * Small things every view needs: HTML escaping (nothing here trusts
 * a video title), URL sanitising, and the fetch wrapper that turns a
 * non-2xx API reply into an Error with the server's message.
 */

export const API_PREFIX = '/api';

// Every value below comes from the API or the local filesystem and is spliced
// into innerHTML templates; without this a video title such as
// "<img src=x onerror=alert(1)>" runs in the page.
export function escapeHtml(value) {
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
export function pct(value) {
  return (value === null || value === undefined) ? 'n/a' : `${Math.round(value * 100)}%`;
}

// YouTube video ids are exactly 11 characters. Older builds stored placeholders
// such as 'queued', which produced links to a video that does not exist.
export function isYouTubeVideoId(value) {
  return typeof value === 'string' && /^[A-Za-z0-9_-]{11}$/.test(value);
}

// Same escaping, plus a scheme allow-list, for values used inside href="".
export function safeUrl(value) {
  const url = String(value || '').trim();
  if (!/^(https?:)?\/\//i.test(url) && !/^data:image\//i.test(url)) return '#';
  return escapeHtml(url);
}

export function asMediaUrl(path) {
  if (!path) return '';
  return `/media/${path.replace(/\\/g, '/').replace(/^\//, '')}`;
}

export function formatTimeLabel(dateString) {
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

export async function fetchJson(url, options = {}) {
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

export function formatDuration(sec) {
  if (!sec || sec <= 0) return '0s';
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}
