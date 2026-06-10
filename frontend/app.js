// ═══════════════════════════════════════════════════════════════════════════
// Proxmox AI — Modern UI Controller
// ═══════════════════════════════════════════════════════════════════════════

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${txt}`);
  }
  return res.json();
}

function el(tag, props = {}, ...children) {
  const e = document.createElement(tag);
  Object.entries(props).forEach(([k, v]) => {
    if (k === 'class') e.className = v;
    else e.setAttribute(k, v);
  });
  children.flat().forEach(c => e.append(typeof c === 'string' ? document.createTextNode(c) : c));
  return e;
}

function hasUnresolvedPlaceholder(command) {
  return typeof command === 'string' && /<[^>]+>/.test(command);
}

const TOOL_NAMES = new Set([
  'scan_containers', 'get_logs', 'search_logs',
  'start_container', 'stop_container', 'restart_container',
]);

function isShellCommand(cmd) {
  if (typeof cmd !== 'string' || !cmd.trim()) return false;
  if (cmd.startsWith('tool_call:')) return false;
  const firstWord = cmd.trim().split(/\s+/)[0];
  if (TOOL_NAMES.has(firstWord)) return false;
  if (/^\w+\s*\(/.test(cmd.trim())) return false;
  if (hasUnresolvedPlaceholder(cmd)) return false;
  return true;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}

function parseMarkdown(text) {
  let html = escapeHtml(text || '');
  html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.*?)\*/g, '<em>$1</em>');
  html = html.replace(/`(.*?)`/g, '<code>$1</code>');
  html = html.replace(/\n/g, '<br>');
  return html;
}

// Keep command output short when feeding it back to the model — a journalctl
// dump can be tens of thousands of tokens, which the local model can't afford.
function truncateForPrompt(text, max = 1500) {
  if (!text || text.length <= max) return text;
  const head = text.slice(0, Math.floor(max * 0.67));
  const tail = text.slice(-Math.floor(max * 0.33));
  return `${head}\n...[output truncated]...\n${tail}`;
}

// ═══════════════════════════════════════════════════════════════════════════
// Chat
// ═══════════════════════════════════════════════════════════════════════════

const chatHistory = [];
const MAX_HISTORY = 10;
let lastUserQuery = '';

function getChatHistory() {
  return chatHistory.slice(-MAX_HISTORY).map(m => ({ role: m.role, content: m.content }));
}

const _renderedActionKeys = new Set();

// Render one suggested action in chat: create a pending approval, then offer
// Approve & Run / Reject. Execution only ever happens through /execute with an
// approved approval id.
async function handleSuggestedAction(action, container) {
  const cmd = action.command || '';
  const label = action.action || 'Suggested action';
  const risk = action.risk || 'medium';

  const dedupKey = `${label}::${cmd}`;
  if (_renderedActionKeys.has(dedupKey)) return;
  _renderedActionKeys.add(dedupKey);

  if (!isShellCommand(cmd)) {
    if (!cmd) {
      container.append(el('div', {class: 'tool-result'}, `Suggested: ${label}`));
      container.scrollTop = container.scrollHeight;
    }
    return;
  }

  let approval;
  try {
    approval = await api('/approvals', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({
      action: label,
      command: cmd,
      target: action.target || null,
      risk,
      source_query: lastUserQuery,
      requested_by: 'assistant',
    })});
  } catch (e) {
    renderChatMessage('system', `Failed to create approval request: ${e.message}`);
    return;
  }
  fetchApprovals();

  const actionDiv = el('div', {style:'margin-top:8px;padding:12px 14px;background:rgba(59,130,246,0.06);border:1px solid rgba(59,130,246,0.15);border-radius:10px;'});
  actionDiv.append(el('div', {style:'font-size:11px;color:var(--text-muted);margin-bottom:4px;'}, `Awaiting your approval: ${label}`));
  actionDiv.append(el('div', {class: 'rec-command', style:'margin:0 0 8px 0;'}, cmd));
  actionDiv.append(el('span', {class: `rec-tag risk-${risk === 'low' ? 'low' : risk === 'high' ? 'high' : 'medium'}`, style:'margin-right:8px;'}, `risk: ${risk}`));

  const approveBtn = el('button', {class: 'btn btn-success btn-sm'}, '✓ Approve & Run');
  const rejectBtn = el('button', {class: 'btn btn-danger btn-sm', style:'margin-left:6px;'}, '✕ Reject');

  approveBtn.onclick = async () => {
    approveBtn.disabled = true;
    rejectBtn.disabled = true;
    try {
      await api(`/approvals/${approval.id}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify({decision: 'approved', reviewer: 'web-ui', note: 'approved via chat'})});
      const res = await api('/execute', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({approval_id: approval.id})});
      actionDiv.remove();
      rememberExecution(approval.id, res);
      await reportExecution(res);
    } catch (e) {
      actionDiv.remove();
      renderChatMessage('system', `Execution failed: ${e.message}`);
      const followupQuery = `[System: The command "${cmd}" failed to execute. Error: ${e.message}]\n\nPlease explain why this failed and suggest a fix if possible.`;
      try { await streamChatQuery(followupQuery); } catch (_) {}
    } finally {
      fetchApprovals();
    }
  };

  rejectBtn.onclick = async () => {
    approveBtn.disabled = true;
    rejectBtn.disabled = true;
    try {
      await api(`/approvals/${approval.id}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify({decision: 'rejected', reviewer: 'web-ui', note: 'rejected via chat'})});
    } catch (e) {
      renderChatMessage('system', `Failed to reject: ${e.message}`);
    }
    actionDiv.style.opacity = '0.5';
    approveBtn.remove();
    rejectBtn.remove();
    actionDiv.append(el('span', {class: 'rec-tag status-rejected'}, 'rejected'));
    fetchApprovals();
  };

  const buttons = el('div', {style:'margin-top:8px;'});
  buttons.append(approveBtn, rejectBtn);
  actionDiv.append(buttons);
  container.append(actionDiv);
  container.scrollTop = container.scrollHeight;
}

// Show execution output in chat and ask the model for a short interpretation.
async function reportExecution(res) {
  const cleanOutput = (res.stdout || '').trim();
  const stderrOutput = (res.stderr || '').trim();
  const fullOutput = stderrOutput ? `${cleanOutput}\n\nError output:\n${stderrOutput}`.trim() : cleanOutput;

  renderChatMessage('system', `Executed \`${res.command}\` (exit ${res.returncode}):\n${fullOutput || '(no output)'}`);

  const followupQuery = `[System: The command "${res.command}" was executed. Exit code: ${res.returncode}. Output:\n${truncateForPrompt(fullOutput)}]\n\nPlease briefly summarize what this output means for the user.`;
  try { await streamChatQuery(followupQuery); } catch (aiErr) { renderChatMessage('assistant', `Error processing result: ${aiErr.message}`); }
}

async function streamChatQuery(query) {
  // Snapshot the history BEFORE adding the current query — the backend appends
  // the query itself, so including it here would send it twice.
  const history = getChatHistory();
  chatHistory.push({ role: 'user', content: query });
  lastUserQuery = query;

  const modelEl = document.getElementById('model_select');
  const model = modelEl ? modelEl.value || null : null;
  const payload = { query, model, history };

  const res = await fetch('/chat/stream', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${txt}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const ctx = {
    finalPayload: null,
    rawResponseText: '',
    thinkingLines: [],
    container: document.getElementById('chat_messages'),
  };

  // Build assistant message with avatar + bubble
  const msgWrapper = el('div', {class: 'chat-msg assistant'});
  const avatar = el('div', {class: 'chat-avatar'}, 'AI');
  const bubble = el('div', {class: 'chat-bubble'});

  // Thinking panel
  const thinkingPanel = el('div', {class: 'thinking-panel'});
  const thinkingSummary = el('div', {class: 'thinking-summary'}, 'Thinking…');
  const thinkingBody = el('div', {class: 'thinking-body', style: 'display: none;'});
  thinkingSummary.onclick = () => {
    thinkingBody.style.display = thinkingBody.style.display === 'none' ? 'block' : 'none';
  };
  thinkingPanel.append(thinkingSummary, thinkingBody);

  const responseEl = el('span', {});
  bubble.append(thinkingPanel, responseEl);
  msgWrapper.append(avatar, bubble);
  ctx.container.append(msgWrapper);
  ctx.container.scrollTop = ctx.container.scrollHeight;

  ctx.responseEl = responseEl;
  ctx.thinkingBody = thinkingBody;

  function handleStreamEvent(event) {
    if (event.type === 'chunk' && typeof event.text === 'string') {
      ctx.rawResponseText += event.text;
      ctx.responseEl.innerHTML = parseMarkdown(ctx.rawResponseText);
    } else if (event.type === 'tool_call_result') {
      const result = event.result || event.error || '';
      if (result) {
        ctx.thinkingLines.push(`[Tool result] ${result.substring(0, 300)}`);
        ctx.thinkingBody.innerHTML = parseMarkdown(ctx.thinkingLines.join('\n\n'));
      }
    } else if (event.type === 'tool_call') {
      const args = event.args || {};
      ctx.thinkingLines.push(`[Tool call] ${event.tool || 'tool'}: ${JSON.stringify(args).substring(0, 200)}`);
      ctx.thinkingBody.textContent = ctx.thinkingLines.join('\n\n');
    } else if (event.type === 'suggested_action' && event.action) {
      handleSuggestedAction(event.action, ctx.container);
    } else if (event.type === 'final' && event.payload) {
      ctx.finalPayload = event.payload;
    } else if (event.type === 'error' && event.error) {
      throw new Error(event.error);
    }
  }

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const lines = buffer.split('\n');
    buffer = lines.pop() || '';
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      let event;
      try { event = JSON.parse(trimmed); } catch (e) { continue; }
      handleStreamEvent(event);
    }
  }

  if (buffer.trim()) {
    try {
      handleStreamEvent(JSON.parse(buffer.trim()));
    } catch (e) { /* ignore trailing noise */ }
  }

  const finalPayload = ctx.finalPayload || { summary: ctx.rawResponseText, reasoning: '', confidence: 0.0, suggested_actions: [] };
  const reasoning = finalPayload.reasoning || '';
  if (reasoning) {
    ctx.thinkingLines.push(`[Reasoning] ${reasoning}`);
    thinkingBody.textContent = ctx.thinkingLines.join('\n\n');
  }
  if (reasoning || ctx.thinkingLines.length > 0) {
    thinkingSummary.textContent = reasoning
      ? `💭 ${reasoning.substring(0, 80)}${reasoning.length > 80 ? '…' : ''} (click to expand)`
      : `💭 Thought for a moment (click to expand)`;
  } else {
    thinkingPanel.remove();
  }

  if (finalPayload.summary) {
    responseEl.innerHTML = parseMarkdown(finalPayload.summary);
    chatHistory.push({ role: 'assistant', content: finalPayload.summary });
  }

  ctx.container.scrollTop = ctx.container.scrollHeight;
  return finalPayload;
}

async function fetchModels() {
  try {
    const models = await api('/models');
    const sel = document.getElementById('model_select');
    if (!sel) return;
    sel.innerHTML = '';
    sel.append(el('option', {value: ''}, 'Default model'));
    if (Array.isArray(models)) {
      models.forEach(m => sel.append(el('option', {value: m}, m)));
    }
  } catch (e) {
    console.warn('Failed to load models', e);
  }
}

function renderChatMessage(role, text) {
  const container = document.getElementById('chat_messages');
  const roleKey = role.toLowerCase();
  const isUser = roleKey === 'you' || roleKey === 'user';
  const isSystem = roleKey === 'system';

  const msgWrapper = el('div', {class: `chat-msg ${isUser ? 'user' : isSystem ? 'system' : 'assistant'}`});
  const avatar = el('div', {class: 'chat-avatar'}, isUser ? 'U' : isSystem ? '⚙' : 'AI');
  const bubble = el('div', {class: 'chat-bubble'});

  if (isSystem && text.includes('`')) {
    const parts = text.split(/(`[^`]+`)/g);
    parts.forEach(part => {
      if (part.startsWith('`') && part.endsWith('`')) {
        bubble.append(el('pre', {}, part.slice(1, -1)));
      } else {
        bubble.append(document.createTextNode(part));
      }
    });
  } else {
    bubble.innerHTML = parseMarkdown(text);
  }

  msgWrapper.append(avatar, bubble);
  container.append(msgWrapper);
  container.scrollTop = container.scrollHeight;
}

// ═══════════════════════════════════════════════════════════════════════════
// Recommendations (approval workflow)
// ═══════════════════════════════════════════════════════════════════════════

// Execution results by approval id so output survives list re-renders.
const _executionResults = {};

function rememberExecution(approvalId, res) {
  _executionResults[approvalId] = res;
}

function updateRecBadge(items) {
  const pending = (items || []).filter(item => item.status === 'pending').length;
  const badge = document.getElementById('rec_badge');
  if (badge) {
    badge.textContent = pending;
    badge.style.display = pending > 0 ? 'flex' : 'none';
  }
}

function recsEmptyState() {
  return el('div', {class: 'empty-state'},
    el('div', {class: 'empty-icon'}, '⚡'),
    el('div', {}, 'No recommendations yet'),
    el('div', {style:'font-size:12px'}, 'Ask the assistant about your homelab to get started'),
  );
}

function buildRecCard(item) {
  const statusCls = item.status === 'pending' ? 'rec-pending'
    : item.status === 'approved' ? 'rec-approved'
    : item.status === 'rejected' ? 'rec-rejected'
    : 'rec-executed';
  const card = el('div', {class: `rec-card ${statusCls}`, id: `rec_${item.id}`});

  card.append(el('div', {class: 'rec-action'}, item.action || 'Recommendation'));

  const meta = el('div', {class: 'rec-meta'});
  meta.append(el('span', {class: `rec-tag status-${item.status}`}, item.status));
  if (item.risk) {
    const riskLevel = item.risk === 'low' ? 'low' : item.risk === 'high' ? 'high' : 'medium';
    meta.append(el('span', {class: `rec-tag risk-${riskLevel}`}, `risk: ${item.risk}`));
  }
  if (item.requested_by) meta.append(el('span', {class: 'rec-tag', style:'background:rgba(255,255,255,0.04);color:var(--text-muted);'}, `by: ${item.requested_by}`));
  card.append(meta);

  if (item.source_query) card.append(el('div', {class: 'rec-source'}, `Source: ${item.source_query}`));
  if (item.command) card.append(el('div', {class: 'rec-command'}, item.command));
  if (item.review_note) card.append(el('div', {class: 'rec-source'}, item.review_note));

  const execResult = _executionResults[item.id];
  if (item.status === 'executed' && execResult) {
    card.append(el('div', {class: 'rec-output'}, (execResult.stdout || '').trim() || '(no output)'));
    if (execResult.stderr) {
      card.append(el('div', {class: 'rec-output rec-output-err'}, execResult.stderr));
    }
  }

  const actions = el('div', {class: 'rec-buttons'});
  if (item.status === 'pending') {
    const approveBtn = el('button', {class: 'btn btn-success btn-sm'}, '✓ Approve');
    approveBtn.onclick = () => decide(item.id, 'approved');
    const rejectBtn = el('button', {class: 'btn btn-danger btn-sm'}, '✕ Reject');
    rejectBtn.onclick = () => decide(item.id, 'rejected');
    actions.append(approveBtn, rejectBtn);
  }
  if (item.status === 'approved' && item.command) {
    const execBtn = el('button', {class: 'btn btn-primary btn-sm'}, '▶ Execute');
    if (hasUnresolvedPlaceholder(item.command)) {
      execBtn.disabled = true;
      actions.append(el('span', {style:'color:var(--accent-danger);font-size:12px;display:flex;align-items:center;'}, 'Contains placeholder'));
    } else {
      execBtn.onclick = () => executeInline(item.id);
    }
    actions.append(execBtn);
  }
  card.append(actions);
  return card;
}

async function fetchApprovals() {
  const container = document.getElementById('recs_list');
  if (!container) return;
  let items;
  try {
    items = await api('/approvals');
  } catch (e) {
    console.warn('Failed to load approvals', e);
    return;
  }
  container.innerHTML = '';
  if (!Array.isArray(items) || items.length === 0) {
    container.append(recsEmptyState());
  } else {
    items.forEach(item => container.append(buildRecCard(item)));
  }
  updateRecBadge(items);
}

async function decide(id, decision) {
  try {
    await api(`/approvals/${id}`, {method: 'PATCH', headers: {'Content-Type':'application/json'}, body: JSON.stringify({decision, reviewer: 'web-ui', note: ''})});
  } catch (e) {
    renderChatMessage('system', `Failed to update approval: ${e.message}`);
  }
  fetchApprovals();
}

async function executeInline(id) {
  try {
    const res = await api('/execute', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({approval_id: id})});
    rememberExecution(id, res);
    await fetchApprovals();
    await reportExecution(res);
  } catch (e) {
    renderChatMessage('system', `Execution failed: ${e.message}`);
    const followupQuery = `[System: An approved command failed to execute. Error: ${e.message}]\n\nPlease explain why this failed and suggest a fix if possible.`;
    try { await streamChatQuery(followupQuery); } catch (_) {}
    fetchApprovals();
  }
}

async function clearFinishedApprovals() {
  try {
    await api('/approvals/cleanup?remove_empty=true&finished=true', {method: 'POST'});
  } catch (e) {
    renderChatMessage('system', `Failed to clear recommendations: ${e.message}`);
  }
  fetchApprovals();
}

// ===========================================================================
// Settings sidebar
// ===========================================================================

const SETTING_FIELDS = [
  'app_env', 'app_host', 'app_port',
  'proxmox_url', 'proxmox_host_ip', 'proxmox_port', 'proxmox_realm',
  'proxmox_user', 'proxmox_token_id', 'proxmox_token_secret', 'proxmox_verify_ssl',
  'ollama_url', 'ollama_model', 'ollama_embed_model', 'ollama_num_ctx',
  'qdrant_url', 'qdrant_api_key', 'qdrant_current_collection_name', 'qdrant_history_collection_name',
  'qdrant_logs_collection_name',
  'loki_url', 'prometheus_url',
  'approval_db_path',
];

function settingsEl(id) {
  return document.getElementById('cfg_' + id);
}

async function loadSettings() {
  try {
    const data = await api('/settings');
    for (const field of SETTING_FIELDS) {
      const el = settingsEl(field);
      if (!el) continue;
      const val = data[field];
      if (val === null || val === undefined) continue;
      if (el.type === 'checkbox') {
        el.checked = !!val;
      } else {
        el.value = String(val);
      }
    }
  } catch (e) {
    console.warn('Failed to load settings', e);
    showSettingsMsg('Failed to load settings: ' + e.message, 'err');
  }
}

async function saveSettings() {
  const payload = {};
  for (const field of SETTING_FIELDS) {
    const el = settingsEl(field);
    if (!el) continue;
    if (el.type === 'checkbox') {
      payload[field] = el.checked;
    } else if (el.type === 'number') {
      const num = parseInt(el.value, 10);
      if (!isNaN(num)) payload[field] = num;
    } else {
      if (el.value.trim() !== '') payload[field] = el.value.trim();
    }
  }

  try {
    const res = await api('/settings', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    showSettingsMsg(res.message || '✓ Settings saved successfully.', 'ok');
  } catch (e) {
    showSettingsMsg('Save failed: ' + e.message, 'err');
  }
}

function showSettingsMsg(text, type) {
  const msgEl = document.getElementById('settings_msg');
  if (!msgEl) return;
  msgEl.textContent = text;
  msgEl.className = 'settings-msg ' + (type === 'ok' ? 'ok' : type === 'err' ? 'err' : '');
}

function resetSettingsForm() {
  loadSettings();
  showSettingsMsg('Form reset to current values.', '');
}

// ===========================================================================
// View switching (sidebar navigation)
// ===========================================================================

function switchView(viewId) {
  if (!viewId) return;

  // Update nav buttons
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  const activeBtn = document.querySelector(`[data-view="${viewId}"]`);
  if (activeBtn) activeBtn.classList.add('active');

  // Update views
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  const viewEl = document.getElementById(viewId);
  if (viewEl) viewEl.classList.add('active');

  // Settings is an overlay panel, not a main view
  const settingsPanel = document.getElementById('view_settings');
  if (settingsPanel) {
    if (viewId === 'view_settings') {
      settingsPanel.classList.add('show');
    } else {
      settingsPanel.classList.remove('show');
    }
  }

  // Auto-load data when switching to specific views
  if (viewId === 'view_containers') {
    loadContainers();
  }
  if (viewId === 'view_logs') {
    populateLogsContainerFilter();
    fetchLogs();
  }
  if (viewId === 'view_recommendations') {
    fetchApprovals();
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Containers
// ═══════════════════════════════════════════════════════════════════════════

let _allContainers = [];

function statusClass(status) {
  const s = (status || '').toLowerCase();
  if (s === 'running') return 'running';
  if (s === 'stopped') return 'stopped';
  return 'other';
}

function renderContainerCard(c) {
  const div = el('div', {class: 'container-card'});
  const header = el('div', {class: 'container-card-header'});
  const nameEl = el('div', {class: 'container-name'}, escapeHtml(c.name || 'unknown'));
  const statusCls = statusClass(c.status);
  const statusEl = el('span', {class: `container-status ${statusCls}`});
  statusEl.innerHTML = `<span class="dot"></span>${escapeHtml(c.status || '?')}`;
  header.append(nameEl, statusEl);
  div.append(header);

  const meta = el('div', {class: 'container-meta'});
  meta.innerHTML = `
    <span><span class="label">ID</span> ${c.vmid ?? '—'}</span>
    <span><span class="label">Type</span> ${escapeHtml(c.type || '—')}</span>
    <span><span class="label">Node</span> ${escapeHtml(c.node || '—')}</span>
    ${c.ip ? `<span><span class="label">IP</span> ${escapeHtml(c.ip)}</span>` : ''}
    ${c.hostname ? `<span><span class="label">Host</span> ${escapeHtml(c.hostname)}</span>` : ''}
  `;
  div.append(meta);
  return div;
}

function filterContainers() {
  const query = (document.getElementById('container_search')?.value || '').toLowerCase();
  const listEl = document.getElementById('container_list');
  const countEl = document.getElementById('container_count');
  const filtered = query
    ? _allContainers.filter(c => (c.name || '').toLowerCase().includes(query) || String(c.vmid).includes(query) || (c.node || '').toLowerCase().includes(query))
    : _allContainers;

  if (filtered.length === 0) {
    listEl.innerHTML = '<div class="empty-state" style="grid-column:1/-1"><div class="empty-icon">🔍</div><div>No containers match your filter</div></div>';
    countEl.textContent = '';
    return;
  }

  countEl.textContent = query
    ? `Showing ${filtered.length} of ${_allContainers.length} containers`
    : `${_allContainers.length} container${_allContainers.length !== 1 ? 's' : ''} discovered`;

  listEl.innerHTML = '';
  filtered.forEach(c => listEl.appendChild(renderContainerCard(c)));
}

async function loadContainers() {
  const listEl = document.getElementById('container_list');
  const countEl = document.getElementById('container_count');
  listEl.innerHTML = '<div class="empty-state" style="grid-column:1/-1"><div class="loading-spinner"></div><div>Loading containers…</div></div>';
  countEl.textContent = '';
  try {
    const containers = await api('/containers');
    if (!Array.isArray(containers) || containers.length === 0) {
      listEl.innerHTML = '<div class="empty-state" style="grid-column:1/-1"><div class="empty-icon">🖥</div><div>No containers found</div><div style="font-size:12px">Try scanning your Proxmox nodes</div></div>';
      return;
    }
    _allContainers = containers;
    filterContainers();
  } catch (e) {
    listEl.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><div class="empty-icon">⚠</div><div style="color:var(--accent-danger)">Error: ${escapeHtml(e.message)}</div></div>`;
  }
}

async function scanContainers() {
  const btn = document.getElementById('container_scan_btn');
  const listEl = document.getElementById('container_list');
  const countEl = document.getElementById('container_count');
  btn.disabled = true;
  btn.innerHTML = '<span class="loading-spinner"></span> Scanning…';
  listEl.innerHTML = '<div class="empty-state" style="grid-column:1/-1"><div class="loading-spinner" style="width:24px;height:24px;border-width:3px;"></div><div>Scanning Proxmox nodes…</div></div>';
  countEl.textContent = '';
  try {
    const result = await api('/scan', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' });
    if (result && result.containers && result.containers.length > 0) {
      _allContainers = result.containers;
      filterContainers();
    } else {
      listEl.innerHTML = '<div class="empty-state" style="grid-column:1/-1"><div class="empty-icon">📭</div><div>No containers returned from scan</div></div>';
      countEl.textContent = '';
    }
  } catch (e) {
    listEl.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><div class="empty-icon">⚠</div><div style="color:var(--accent-danger)">Scan failed: ${escapeHtml(e.message)}</div></div>`;
    countEl.textContent = '';
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> Scan Now';
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Logs
// ═══════════════════════════════════════════════════════════════════════════

function renderLogEntry(entry) {
  const div = el('div', {class: 'log-entry'});
  const ts = el('div', {class: 'log-ts'}, entry.timestamp || '');
  div.append(ts);

  const container = el('span', {class: 'log-container'}, entry.container || '');
  const msg = el('span', {class: 'log-msg'}, entry.message || '');
  div.append(container, msg);
  return div;
}

function showLogsStatus(msg, type) {
  const el = document.getElementById('logs_status');
  if (!el) return;
  el.textContent = msg;
  el.style.color = type === 'err' ? 'var(--accent-danger)' : type === 'ok' ? 'var(--accent-success)' : 'var(--text-muted)';
}

async function fetchLogs() {
  const listEl = document.getElementById('logs_list');
  const containerFilter = document.getElementById('logs_container_filter').value;
  const limitVal = parseInt(document.getElementById('logs_limit').value, 10) || 50;
  const searchQuery = document.getElementById('logs_search').value.trim();

  showLogsStatus('⏳ Loading…');
  listEl.innerHTML = '';

  try {
    let results;
    if (searchQuery) {
      const payload = { query: searchQuery, limit: limitVal };
      if (containerFilter) payload.container = containerFilter;
      const data = await api('/logs/search', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
      results = data.results || [];
    } else {
      const params = new URLSearchParams();
      if (containerFilter) params.set('container', containerFilter);
      params.set('limit', String(limitVal));
      results = await api(`/logs/recent?${params.toString()}`);
    }

    if (!Array.isArray(results) || results.length === 0) {
      showLogsStatus('No logs found. Logs must be ingested first (use Chat or POST /ingest/logs).');
      listEl.innerHTML = '<div class="empty-state"><div class="empty-icon">📋</div><div>No logs found</div><div style="font-size:12px">Logs must be ingested first</div></div>';
      return;
    }

    showLogsStatus(`✓ ${results.length} log entr${results.length !== 1 ? 'ies' : 'y'} loaded`, 'ok');
    listEl.innerHTML = '';
    results.forEach(entry => listEl.appendChild(renderLogEntry(entry)));
  } catch (e) {
    console.error('[Logs] Error:', e);
    showLogsStatus(`⚠ Error: ${e.message}`, 'err');
    listEl.innerHTML = '';
  }
}

async function populateLogsContainerFilter() {
  const sel = document.getElementById('logs_container_filter');
  // Keep the "All containers" option
  sel.innerHTML = '<option value="">All containers</option>';
  try {
    const containers = await api('/containers');
    if (Array.isArray(containers)) {
      containers.forEach(c => {
        const opt = document.createElement('option');
        opt.value = c.name || String(c.vmid);
        opt.textContent = `${c.name || c.vmid} (${c.type || '?'}, ${c.node || '?'})`;
        sel.appendChild(opt);
      });
    }
  } catch (e) {
    // Silently ignore — container filter is optional
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// DOM Ready — Event Listeners
// ═══════════════════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
  // Chat send
  const send = document.getElementById('chat_send');
  const input = document.getElementById('chat_input');
  if (send) {
    send.onclick = async () => {
      const q = input.value.trim();
      if (!q) return;
      renderChatMessage('user', q);
      input.value = '';
      input.disabled = true;
      send.disabled = true;
      try {
        await streamChatQuery(q);
      } catch (e) {
        const container = document.getElementById('chat_messages');
        const lastMsg = container.lastElementChild;
        if (lastMsg) lastMsg.remove();
        renderChatMessage('assistant', 'Error: ' + e.message);
      } finally {
        input.disabled = false;
        send.disabled = false;
        input.focus();
      }
    };
  }

  // Chat input Enter key
  if (input) {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        send?.click();
      }
    });
  }

  // Chat clear
  const chatClear = document.getElementById('chat_clear');
  if (chatClear) {
    chatClear.addEventListener('click', () => {
      document.getElementById('chat_messages').innerHTML = '';
      chatHistory.length = 0;
      _renderedActionKeys.clear();
    });
  }

  // Recommendations: clear finished (executed/rejected) approvals
  const recsClear = document.getElementById('recs_clear');
  if (recsClear) recsClear.addEventListener('click', clearFinishedApprovals);

  // Navigation
  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const viewId = btn.getAttribute('data-view');
      switchView(viewId);
    });
  });

  // Settings close
  const settingsClose = document.getElementById('settings_close');
  if (settingsClose) {
    settingsClose.addEventListener('click', () => {
      document.getElementById('view_settings').classList.remove('show');
      switchView('view_chat');
    });
  }

  // Settings save
  const settingsSave = document.getElementById('settings_save');
  if (settingsSave) settingsSave.addEventListener('click', saveSettings);

  // Settings reset
  const settingsReset = document.getElementById('settings_reset');
  if (settingsReset) settingsReset.addEventListener('click', resetSettingsForm);

  // Container scan
  const containerScanBtn = document.getElementById('container_scan_btn');
  if (containerScanBtn) containerScanBtn.addEventListener('click', scanContainers);

  // Container search filter
  const containerSearch = document.getElementById('container_search');
  if (containerSearch) containerSearch.addEventListener('input', filterContainers);

  // Logs fetch
  const logsFetchBtn = document.getElementById('logs_fetch_btn');
  if (logsFetchBtn) logsFetchBtn.addEventListener('click', fetchLogs);

  // Logs search Enter key
  const logsSearch = document.getElementById('logs_search');
  if (logsSearch) logsSearch.addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchLogs(); });

  // Initial loads
  fetchModels();
  loadSettings();
  fetchApprovals();
});
