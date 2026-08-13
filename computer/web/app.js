/* Claude-Control mobile viewer — client logic (no build step, served by relay server). */
'use strict';

const els = {
  dot: document.getElementById('wsDot'),
  status: document.getElementById('wsStatus'),
  reconnectBtn: document.getElementById('reconnectBtn'),
  sessionSelect: document.getElementById('sessionSelect'),
  conversation: document.getElementById('conversation'),
  emptyState: document.getElementById('emptyState'),
  footer: document.getElementById('footerInfo'),
  toast: document.getElementById('toast'),
};

const STATUS_TEXT = { active: '进行中', attention: '等待审批', idle: '空闲', ended: '已结束' };

const state = {
  ws: null,
  sessions: new Map(),       // id -> summary
  selected: null,            // selected sessionId
  messages: new Map(),       // sessionId -> [record, ...] (render cache)
  autoScroll: true,
  reconnectDelay: 1000,
  reconnectTimer: null,
  cursor: null,
  historyReq: null,
  rendered: new Map(),       // idx -> DOM element（当前选中会话）
};

/* ---------------- helpers ---------------- */

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = ts instanceof Date ? new Date(ts) : new Date(ts * 1000);
  if (isNaN(d)) return '';
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function showToast(msg, isErr = false) {
  els.toast.textContent = msg;
  els.toast.className = 'toast' + (isErr ? ' err' : '');
  els.toast.hidden = false;
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => { els.toast.hidden = true; }, 3200);
}

function setStatus(text, connected) {
  els.dot.className = 'dot ' + (connected ? 'dot-on' : (text === 'connecting' ? 'dot-pending' : 'dot-off'));
  els.status.textContent = text;
  const sel = state.selected ? state.sessions.get(state.selected) : null;
  els.footer.textContent = connected
    ? (sel ? `已连接 · ${sel.status === 'attention' ? '⚠ 等待审批 · ' : ''}会话 ${state.selected.slice(0, 8)}` : '已连接 · 未选择会话')
    : '未连接';
}

/* ---------------- session list / selector ---------------- */

function rebuildSelector() {
  const sessions = [...state.sessions.values()];
  const prev = state.selected;
  const opts = sessions
    .sort((a, b) => (b.mtime || 0) - (a.mtime || 0))
    .map(s => {
      const opt = document.createElement('option');
      opt.value = s.id;
      const title = (s.title || s.lastPrompt || s.id).slice(0, 42);
      const mark = s.status === 'active' ? '▶' : s.status === 'attention' ? '⚠' : s.status === 'idle' ? '▪' : '✕';
      const cwd = s.cwd ? ' · ' + s.cwd : '';
      opt.textContent = `${mark} ${title}${cwd}`;
      opt.dataset.status = s.status;
      return opt;
    });
  els.sessionSelect.innerHTML = '';
  if (!opts.length) {
    els.sessionSelect.appendChild(Object.assign(document.createElement('option'), { textContent: '— 无可用会话 —', value: '' }));
    els.sessionSelect.disabled = true;
  } else {
    els.sessionSelect.append(...opts);
    els.sessionSelect.disabled = false;
  }
  if (prev && state.sessions.has(prev)) {
    els.sessionSelect.value = prev;
  } else if (opts.length) {
    const active = sessions.find(s => s.status === 'active' || s.status === 'attention') || sessions[0];
    els.sessionSelect.value = active.id;
  }
  const target = els.sessionSelect.value;
  if (target !== state.selected) selectSession(target);
}

function selectSession(id) {
  state.selected = id;
  setStatus(state.ws && state.ws.readyState === WebSocket.OPEN ? '已连接' : '未连接',
    !!(state.ws && state.ws.readyState === WebSocket.OPEN));
  renderEmpty('正在加载会话历史…');
  if (id) {
    state.historyReq = id;
    send({ type: 'get-history', sessionId: id });
  }
}

function renderEmpty(text) {
  els.conversation.innerHTML = '';
  const stub = document.createElement('div');
  stub.className = 'empty-state';
  stub.innerHTML = `<div class="empty-icon">📡</div><p>${escapeHtml(text || '等待连接服务器…')}</p>`;
  els.conversation.appendChild(stub);
  els.emptyState = stub;
}

function send(obj) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    try { state.ws.send(JSON.stringify(obj)); } catch (_) {}
  }
}

/* ---------------- rendering ---------------- */

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function buildMessage(rec) {
  const msg = el('div', 'msg ' + rec.role);
  const head = el('div', 'msg-head');
  head.appendChild(el('span', 'msg-role', rec.role === 'user' ? '你' : 'Claude'));
  if (rec.model) head.appendChild(el('span', 'msg-model', rec.model));
  if (rec.ts) head.appendChild(el('span', 'msg-time', fmtTime(rec.ts)));
  msg.appendChild(head);
  const body = el('div', 'msg-body');
  (rec.content || []).forEach(blk => body.appendChild(buildBlock(blk)));
  msg.appendChild(body);
  return msg;
}

function buildBlock(blk) {
  switch (blk.type) {
    case 'text': {
      const wrap = el('div', 'blk');
      if (blk.isCode || looksLikeCode(blk.text)) wrap.appendChild(el('pre', 'blk-text code', blk.text));
      else wrap.appendChild(el('div', 'blk-text', blk.text));
      return wrap;
    }
    case 'thinking': {
      const wrap = el('div', 'blk blk-thinking');
      const btn = el('button', 'think-toggle', '🧠 思考过程（点击展开）');
      const body = el('div', 'think-body', blk.thinking || '');
      btn.addEventListener('click', () => {
        body.classList.toggle('open');
        btn.textContent = body.classList.contains('open') ? '🧠 思考过程（收起）' : '🧠 思考过程（点击展开）';
      });
      wrap.append(btn, body);
      return wrap;
    }
    case 'tool_use': {
      const wrap = el('div', 'blk blk-tool');
      const head = el('button', 'tool-head');
      head.innerHTML = `<span class="tool-icon">🔧</span><span class="tool-name">${escapeHtml(blk.name || 'tool')}</span>`;
      const preview = el('span', 'tool-preview', summarizeInput(blk.input));
      const arrow = el('span', 'tool-arrow', '▾');
      head.append(preview, arrow);
      const body = el('div', 'tool-body');
      if (blk.input) {
        body.append(el('div', 'tool-label', 'INPUT'), el('pre', 'tool-input', JSON.stringify(blk.input, null, 2)));
      }
      if (blk.result) {
        const l = el('div', 'tool-label', 'RESULT' + (blk.isError ? ' (错误)' : ''));
        body.append(l, el('pre', 'tool-result' + (blk.isError ? ' is-error' : ' is-done'), blk.result));
      } else if (blk.id) {
        body.append(el('div', 'tool-label', '未返回结果'));
      }
      head.addEventListener('click', () => {
        const open = body.classList.toggle('open');
        arrow.textContent = open ? '▴' : '▾';
        body.scrollIntoView({ block: 'nearest' });
      });
      wrap.append(head, body);
      return wrap;
    }
    case 'tool_result': {
      const wrap = el('div', 'blk blk-tool');
      const head = el('button', 'tool-head');
      head.innerHTML = `<span class="tool-icon">📄</span><span class="tool-name">工具结果</span>`;
      const arrow = el('span', 'tool-arrow', '▾');
      head.appendChild(arrow);
      const body = el('div', 'tool-body');
      body.append(el('pre', 'tool-result' + (blk.isError ? ' is-error' : ' is-done'), blk.result || ''));
      head.addEventListener('click', () => {
        const open = body.classList.toggle('open');
        arrow.textContent = open ? '▴' : '▾';
      });
      wrap.append(head, body);
      return wrap;
    }
    case 'sys': {
      const wrap = el('div', 'sys-line');
      wrap.appendChild(el('span', 'tag', blk.text));
      return wrap;
    }
    default:
      return el('div', 'blk', JSON.stringify(blk));
  }
}

function looksLikeCode(t) {
  if (!t) return false;
  const s = t.trim();
  if (s.length < 60 || (s.match(/\n/g) || []).length < 2) return false;
  return /[{}]|\b(function|def|import|class|const|let|=>|SELECT |curl |npm |git )/.test(s);
}

function summarizeInput(input) {
  if (!input) return '';
  const s = JSON.stringify(input);
  return s.length > 60 ? s.slice(0, 60) + '…' : s;
}

/* ---------------- message handling ---------------- */

function appendRecords(records, isHistory) {
  if (!records || !records.length) return;
  const sessionId = records[0].sessionId;
  const isSelected = sessionId === state.selected;
  const cache = state.messages.get(sessionId) || [];

  if (isHistory) {
    state.messages.set(sessionId, records.slice());
    if (isSelected) {
      els.conversation.innerHTML = '';
      state.rendered.clear();
      records.forEach(r => renderRecord(r));
      scrollToBottom();
    }
    return;
  }

  let added = 0;
  records.forEach(rec => {
    // 按 idx 去重（历史与实时可能重叠）
    if (rec.idx != null && cache.some(x => x.idx === rec.idx)) return;
    cache.push(rec);
    added++;
    if (isSelected) renderRecord(rec);
  });
  state.messages.set(sessionId, cache);
  if (isSelected && added) scrollToBottomIfAuto();
}

// 服务端已就地补上 tool_result 的历史记录，按 idx 替换本地缓存与 DOM 节点
function applyRecordUpdates(records, sessionId) {
  if (!records || !records.length) return;
  const isSelected = sessionId === state.selected;
  const cache = state.messages.get(sessionId) || [];
  records.forEach(rec => {
    const i = cache.findIndex(x => x.idx === rec.idx);
    if (i >= 0) cache[i] = rec; else cache.push(rec);
    if (isSelected && rec.idx != null) {
      const el = rec.role ? buildMessage(rec) : buildBlock({ type: 'sys', text: rec.text });
      el.dataset.idx = rec.idx;
      const old = state.rendered.get(rec.idx);
      if (old && old.parentNode) {
        old.replaceWith(el);
        if (state.cursor && !state.cursor.isConnected) state.cursor = null;
      } else {
        els.conversation.appendChild(el);
      }
      state.rendered.set(rec.idx, el);
      if (rec.role === 'assistant') addCursor();
    }
  });
  state.messages.set(sessionId, cache);
}

function renderRecord(rec) {
  let el;
  if (rec.role) {
    removeCursor();
    el = buildMessage(rec);
    els.conversation.appendChild(el);
    if (rec.role === 'assistant') addCursor();
  } else if (rec.kind === 'sys') {
    el = buildBlock({ type: 'sys', text: rec.text });
    els.conversation.appendChild(el);
  }
  if (el && rec.idx != null) {
    el.dataset.idx = rec.idx;
    state.rendered.set(rec.idx, el);
  }
}

function addCursor() {
  if (state.cursor) return;
  const last = els.conversation.lastElementChild;
  if (!last || !last.classList.contains('msg')) return;
  const c = el('span', 'cursor');
  state.cursor = c;
  last.appendChild(c);
}

function removeCursor() {
  if (state.cursor) { state.cursor.remove(); state.cursor = null; }
}

function scrollToBottomIfAuto() {
  if (state.autoScroll) scrollToBottom();
}
function scrollToBottom() {
  els.conversation.scrollTop = els.conversation.scrollHeight;
}

els.conversation.addEventListener('scroll', () => {
  state.autoScroll = els.conversation.scrollHeight - els.conversation.scrollTop - els.conversation.clientHeight < 60;
});

/* ---------------- websocket ---------------- */

function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  let url = `${proto}://${location.host}/ws`;
  const token = new URLSearchParams(location.search).get('token') || sessionStorage.getItem('cc_token');
  if (token) url += `?token=${encodeURIComponent(token)}`;
  return url;
}

function connect() {
  setStatus('connecting', false);
  let ws;
  try { ws = new WebSocket(wsUrl()); }
  catch (e) { showToast('连接失败：' + e.message, true); scheduleReconnect(); return; }
  state.ws = ws;

  ws.onopen = () => {
    if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
    state.reconnectDelay = 1000;
    setStatus('已连接', true);
    // 重连后补齐断线期间错过的消息
    if (state.selected) {
      state.historyReq = state.selected;
      send({ type: 'get-history', sessionId: state.selected });
    }
  };
  ws.onmessage = ev => {
    let data;
    try { data = JSON.parse(ev.data); } catch (_) { return; }
    handleMessage(data);
  };
  ws.onclose = ev => {
    if (state.ws !== ws) return;              // 过期的旧连接，忽略
    if (ev && ev.code === 1008) {             // 认证失败：提示并停止自动重连
      setStatus('认证失败：token 错误', false);
      showToast('认证失败：请检查 token', true);
      return;
    }
    setStatus('未连接', false);
    scheduleReconnect();
  };
  ws.onerror = () => {};
}

function scheduleReconnect() {
  if (state.reconnectTimer) clearTimeout(state.reconnectTimer);
  state.reconnectTimer = setTimeout(() => {
    state.reconnectDelay = Math.min(state.reconnectDelay * 1.6, 15000);
    connect();
  }, state.reconnectDelay);
}

function handleMessage(data) {
  switch (data.type) {
    case 'hello':
    case 'sessions':
      data.sessions.forEach(s => state.sessions.set(s.id, s));
      rebuildSelector();
      break;
    case 'messages':
      if (data.isHistory && data.sessionId !== state.historyReq) break; // 过期历史，丢弃
      if (data.isHistory) {
        appendRecords(data.records || [], true);
      } else if (data.update) {
        applyRecordUpdates(data.records || [], data.sessionId);
      } else {
        appendRecords(data.records || [], false);
      }
      break;
    case 'session-start':
    case 'session-update':
      if (data.session) {
        state.sessions.set(data.session.id, data.session);
        rebuildSelector();
      }
      break;
    case 'session-end':
      if (state.sessions.has(data.sessionId)) {
        state.sessions.get(data.sessionId).status = 'ended';
        rebuildSelector();
      }
      break;
    default:
      break;
  }
}

/* ---------------- init ---------------- */

els.reconnectBtn.addEventListener('click', () => {
  if (state.ws) { try { state.ws.close(); } catch (_) {} }
  state.reconnectDelay = 1000;
  connect();
});

els.sessionSelect.addEventListener('change', e => {
  if (e.target.value) selectSession(e.target.value);
});

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && (!state.ws || state.ws.readyState > WebSocket.OPEN)) connect();
});

connect();
