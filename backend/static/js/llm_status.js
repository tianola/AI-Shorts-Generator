/**
 * llm_status.js - the LLM indicator in the top bar.
 *
 * Polls /api/llm/health every 30 s with a live probe (a model-list call, no tokens),
 * colours the dot by the overall state, and opens a panel with each model's state,
 * the probe result and the most recent calls and errors.
 */

const POLL_MS = 30000;

const STATE = {
  ok: { color: '#22c55e', label: 'OK' },
  degraded: { color: '#f59e0b', label: 'Errors' },
  down: { color: '#ef4444', label: 'Down' },
  unresponsive: { color: '#f97316', label: 'Unresponsive' },
  idle: { color: '#64748b', label: 'Idle' },
};

const KIND_LABEL = {
  ok: 'ok',
  no_json: 'no usable JSON',
  timeout: 'timeout',
  error: 'error',
};

function stateOf(name) {
  return STATE[name] || STATE.idle;
}

function el(tag, text, style) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (style) Object.assign(node.style, style);
  return node;
}

function ago(epochSeconds) {
  if (!epochSeconds) return 'never';
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - epochSeconds));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

function renderPanel(panel, data) {
  panel.replaceChildren();
  const head = el('div', `LLM: ${stateOf(data.overall).label} (${data.provider})`, {
    fontWeight: '700', marginBottom: '8px',
  });
  panel.appendChild(head);

  if (data.probe) {
    const p = data.probe;
    const line = p.reachable
      ? `Server answers in ${p.latency_ms} ms, ${p.models_listed ?? 0} models listed.`
      : `Server does not answer: ${p.detail || 'unknown error'}`;
    panel.appendChild(el('div', line, { marginBottom: '8px' }));
    if (p.detail && p.reachable) panel.appendChild(el('div', p.detail, { color: '#f59e0b', marginBottom: '8px' }));
  }

  if (!data.targets.length) {
    panel.appendChild(el('div', 'No LLM call made since the server started.', { color: '#94a3b8' }));
  }
  for (const t of data.targets) {
    const row = el('div', null, { marginBottom: '6px' });
    const s = stateOf(t.status);
    const title = el('div', `${t.target}: ${s.label}`, { color: s.color, fontWeight: '600' });
    row.appendChild(title);
    row.appendChild(el('div',
      `ok ${t.ok_count} · failed ${t.failed_count} · in flight ${t.in_flight} · last ok ${ago(t.last_ok_at)}`,
      { color: '#94a3b8', fontSize: '12px' }));
    if (t.last_error) {
      row.appendChild(el('div',
        `last error ${ago(t.last_error.at)}: ${KIND_LABEL[t.last_error.kind] || t.last_error.kind} – ${t.last_error.detail}`,
        { color: '#fca5a5', fontSize: '12px', wordBreak: 'break-word' }));
    }
    panel.appendChild(row);
  }

  if (data.recent_events.length) {
    panel.appendChild(el('div', 'Recent calls', { fontWeight: '600', margin: '10px 0 4px' }));
    for (const e of data.recent_events.slice(0, 15)) {
      const color = e.kind === 'ok' ? '#86efac' : '#fca5a5';
      const text = `${ago(e.at)} · ${e.target} · ${KIND_LABEL[e.kind] || e.kind} · ${e.latency_ms} ms${e.detail ? ' · ' + e.detail : ''}`;
      panel.appendChild(el('div', text, { color, fontSize: '12px', wordBreak: 'break-word' }));
    }
  }
}

export function setupLlmStatus() {
  const button = document.getElementById('topbarLlmBtn');
  const dot = document.getElementById('topbarLlmDot');
  const label = document.getElementById('topbarLlmLabel');
  if (!button || !dot || !label) return;

  const panel = el('div', null, {
    position: 'fixed', top: '64px', right: '16px', zIndex: '1000',
    width: 'min(440px, calc(100vw - 32px))', maxHeight: '70vh', overflowY: 'auto',
    background: '#0f172a', color: '#e2e8f0', border: '1px solid #334155',
    borderRadius: '10px', padding: '12px', boxShadow: '0 12px 32px rgba(0,0,0,0.4)',
    fontSize: '13px', display: 'none',
  });
  panel.id = 'llmStatusPanel';
  document.body.appendChild(panel);

  let latest = null;
  let inFlight = false;

  function paint(data) {
    const s = stateOf(data.overall);
    dot.style.background = s.color;
    label.textContent = `LLM: ${s.label}`;
    button.title = `LLM status: ${s.label}. Click for details.`;
    if (panel.style.display !== 'none') renderPanel(panel, data);
  }

  async function refresh() {
    if (inFlight) return;
    inFlight = true;
    try {
      const response = await fetch('/api/llm/health?probe=1&events=15', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      latest = await response.json();
      paint(latest);
    } catch (err) {
      dot.style.background = STATE.down.color;
      label.textContent = 'LLM: no answer';
      button.title = `The dashboard cannot reach the server: ${err.message}`;
    } finally {
      inFlight = false;
    }
  }

  button.addEventListener('click', () => {
    const open = panel.style.display === 'none';
    panel.style.display = open ? 'block' : 'none';
    if (open && latest) renderPanel(panel, latest);
    if (open) refresh();
  });

  document.addEventListener('click', (event) => {
    if (panel.style.display === 'none') return;
    if (panel.contains(event.target) || button.contains(event.target)) return;
    panel.style.display = 'none';
  });

  refresh();
  setInterval(refresh, POLL_MS);
}
