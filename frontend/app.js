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
  Object.entries(props).forEach(([k, v]) => e.setAttribute(k, v));
  children.flat().forEach(c => e.append(typeof c === 'string' ? document.createTextNode(c) : c));
  return e;
}

async function fetchApprovals() {
  const items = await api('/approvals');
  if (Array.isArray(items)) {
    items.forEach(it => updateChatApprovalCard(it));
  }
}

// Chat integration
async function sendChatQuery(query) {
  const modelEl = document.getElementById('model_select');
  const model = modelEl ? modelEl.value || null : null;
  const payload = { query: query, include_logs: true, log_limit: 20, model };
  return api('/chat', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
}

async function streamChatQuery(query) {
  const modelEl = document.getElementById('model_select');
  const model = modelEl ? modelEl.value || null : null;
  const payload = { query: query, include_logs: true, log_limit: 20, model };

  const res = await fetch('/chat/stream', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${txt}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let finalPayload = null;
  const container = document.getElementById('chat_messages');
  const msg = el('div', {}, el('strong', {}, 'Assistant: '), el('span', {}, ''));
  container.append(msg);
  const span = msg.querySelector('span');

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    const chunk = decoder.decode(value, { stream: true });
    buffer += chunk;

    const lines = buffer.split('\n');
    buffer = lines.pop() || '';
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      let event;
      try {
        event = JSON.parse(trimmed);
      } catch (e) {
        continue;
      }
      if (event.type === 'chunk' && typeof event.text === 'string') {
        span.textContent += event.text;
      } else if (event.type === 'final' && event.payload) {
        finalPayload = event.payload;
      } else if (event.type === 'error' && event.error) {
        throw new Error(event.error);
      }
    }
  }

  if (buffer.trim()) {
    try {
      const event = JSON.parse(buffer.trim());
      if (event.type === 'chunk' && typeof event.text === 'string') {
        span.textContent += event.text;
      } else if (event.type === 'final' && event.payload) {
        finalPayload = event.payload;
      } else if (event.type === 'error' && event.error) {
        throw new Error(event.error);
      }
    } catch (e) {
      // ignore trailing decode noise
    }
  }

  if (!finalPayload) {
    finalPayload = { summary: span.textContent, reasoning: '', confidence: 0.0, suggested_actions: [] };
  }

  if (finalPayload.summary) {
    span.textContent = finalPayload.summary;
  }

  return finalPayload;
}

async function fetchModels() {
  try {
    const models = await api('/models');
    const sel = document.getElementById('model_select');
    if (!sel) return;
    // Clear existing options except the first
    const keep = sel.firstElementChild ? sel.firstElementChild.value : '';
    sel.innerHTML = '';
    sel.append(el('option', {value: ''}, 'default model'));
    if (Array.isArray(models)) {
      models.forEach(m => {
        sel.append(el('option', {value: m}, m));
      });
    }
  } catch (e) {
    console.warn('Failed to load models', e);
  }
}

function renderChatMessage(role, text) {
  const box = el('div', {}, el('strong', {}, role+': '), el('span', {}, text));
  const container = document.getElementById('chat_messages');
  container.append(box);
  container.scrollTop = container.scrollHeight;
}

function renderApprovalInChat(item) {
  const container = document.getElementById('chat_messages');
  const card = el('div', {class: 'card', id: `chat_approval_${item.id}`});
  card.append(el('div', {}, el('strong', {}, item.action || 'Approval request')));
  card.append(el('div', {}, `id: ${item.id} status: ${item.status}`));
  if (item.requested_by) card.append(el('div', {}, `requested_by: ${item.requested_by}`));
  if (item.risk) card.append(el('div', {}, `risk: ${item.risk}`));
  if (item.source_query) card.append(el('div', {}, `source: ${item.source_query}`));
  if (item.command) card.append(el('pre', {}, item.command));
  

  const actions = el('div');
  if (item.status === 'pending') {
    const approveBtn = el('button', {class: 'btn btn-primary'}, 'Approve');
    approveBtn.onclick = () => decide(item.id, 'approved');
    const rejectBtn = el('button', {class: 'btn btn-danger'}, 'Reject');
    rejectBtn.onclick = () => decide(item.id, 'rejected');
    actions.append(approveBtn, rejectBtn);
  }
  if (item.status === 'approved' && item.command) {
    const execBtn = el('button', {class: 'btn btn-secondary'}, 'Execute');
    execBtn.onclick = () => executeInline(item.id);
    actions.append(execBtn);
  }
  
  card.append(actions);
  container.append(card);
  container.scrollTop = container.scrollHeight;
}

function updateChatApprovalCard(item) {
  const elId = `chat_approval_${item.id}`;
  const existing = document.getElementById(elId);
  if (!existing) {
    renderApprovalInChat(item);
    return;
  }
  // replace content
  existing.innerHTML = '';
  existing.append(el('div', {}, el('strong', {}, item.action || 'Approval request')));
  existing.append(el('div', {}, `id: ${item.id} status: ${item.status}`));
  if (item.requested_by) existing.append(el('div', {}, `requested_by: ${item.requested_by}`));
  if (item.risk) existing.append(el('div', {}, `risk: ${item.risk}`));
  if (item.source_query) existing.append(el('div', {}, `source: ${item.source_query}`));
  if (item.command) existing.append(el('pre', {}, item.command));

  const actions = el('div');
  if (item.status === 'pending') {
    const approveBtn = el('button', {class: 'btn btn-primary'}, 'Approve');
    approveBtn.onclick = () => decide(item.id, 'approved');
    const rejectBtn = el('button', {class: 'btn btn-danger'}, 'Reject');
    rejectBtn.onclick = () => decide(item.id, 'rejected');
    actions.append(approveBtn, rejectBtn);
  }
  if (item.status === 'approved' && item.command) {
    const execBtn = el('button', {class: 'btn btn-secondary'}, 'Execute');
    execBtn.onclick = () => executeInline(item.id);
    actions.append(execBtn);
  }
  
  existing.append(actions);
}

function showApprovalPreview(item) {
}

document.addEventListener('DOMContentLoaded', () => {
  const send = document.getElementById('chat_send');
  const input = document.getElementById('chat_input');
  fetchModels();
  send.onclick = async () => {
    const q = input.value.trim();
    if (!q) return;
    renderChatMessage('You', q);
    input.value = '';
    try {
      const reply = await streamChatQuery(q);
      // after streaming completes, keep the live message and add any structured actions
      if (reply.suggested_actions && reply.suggested_actions.length) {
        reply.suggested_actions.forEach(act => {
          // command preview
          const pre = el('pre', {}, act.command ? act.command : JSON.stringify(act, null, 2));
          const a = el('div', {style: 'margin-top:8px;border:1px solid #eee;padding:8px;border-radius:6px;background:#fafafa'},
            el('div', {}, el('strong', {}, act.action || 'Suggested action')),
            el('div', {}, `risk: ${act.risk || 'medium'}`),
            pre
          );

          if (act.command) {
            const createBtn = el('button', {class:'btn btn-primary'}, 'Create Approval');
            createBtn.onclick = () => createApprovalFromAction(act, q, false);
            const approveNowBtn = el('button', {class:'btn btn-secondary'}, 'Create & Approve');
            approveNowBtn.onclick = () => createApprovalFromAction(act, q, true);
            a.append(el('div', {style: 'margin-top:6px'}, createBtn, approveNowBtn));
          }

          document.getElementById('chat_messages').append(a);
        });
      }
    } catch (e) {
      // remove thinking and show error
      const container = document.getElementById('chat_messages');
      container.removeChild(container.lastChild);
      renderChatMessage('Assistant', 'Error: '+e.message);
    }
  };
});

async function createApprovalFromAction(action, source_query, autoApprove = false) {
  const payload = {
    action: action.action || 'action',
    command: action.command || null,
    target: action.target || null,
    risk: action.risk || 'medium',
    source_query: source_query,
    requested_by: 'web-ui',
  };
  try {
    const created = await api('/approvals', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    await fetchApprovals();
    // render inline in chat
    renderApprovalInChat(created);
    if (autoApprove) {
      try {
        const updated = await api(`/approvals/${created.id}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify({decision: 'approved', reviewer: 'web-ui', note: 'approved via chat'})});
        await fetchApprovals();
        updateChatApprovalCard(updated);
      } catch (e) {
        renderChatMessage('System', `Failed to auto-approve: ${e.message}`);
      }
    }
  } catch (e) { renderChatMessage('System', `Failed to create approval: ${e.message}`); }
}

// approvals are rendered inline in chat; legacy list removed

async function decide(id, decision) {
  try {
    const updated = await api(`/approvals/${id}`, {method: 'PATCH', headers: {'Content-Type':'application/json'}, body: JSON.stringify({decision, reviewer: 'web-ui', note: ''})});
    await fetchApprovals();
    updateChatApprovalCard(updated);
  } catch (e) { alert(e.message); }
}

async function execute(id) {
  try {
    const res = await api('/execute', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({approval_id: id})});
    document.getElementById('output').innerHTML = `<pre>${escapeHtml(JSON.stringify(res, null, 2))}</pre>`;
  } catch (e) { document.getElementById('output').innerText = e.message; }
}

async function executeInline(id) {
  try {
    const res = await api('/execute', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({approval_id: id})});
    const card = document.getElementById(`chat_approval_${id}`);
    if (card) {
      const out = el('pre', {}, JSON.stringify(res, null, 2));
      card.append(el('div', {}, el('strong', {}, 'Execution result:')),
                 out);
      card.scrollIntoView({behavior:'smooth'});
    }
    await fetchApprovals();
  } catch (e) {
    const card = document.getElementById(`chat_approval_${id}`);
    if (card) card.append(el('div', {}, `Execution failed: ${e.message}`));
  }
}

function escapeHtml(s) { return s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

fetchApprovals();
