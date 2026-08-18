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
const HISTORY_PAGE = 50;      // 分页大小：每页最多渲染的记录数
let deepLinkSession = new URLSearchParams(location.search).get('session');  // dashboard 跳转的会话

const state = {
  ws: null,
  sessions: new Map(),       // id -> summary
  selected: null,            // selected sessionId
  messages: new Map(),       // sessionId -> [record, ...] (render cache)
  paging: new Map(),         // sessionId -> { oldestIdx, hasMore, loading }
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
  // dashboard 深链：优先选中指定会话（仅应用一次）
  if (deepLinkSession && state.sessions.has(deepLinkSession)) {
    els.sessionSelect.value = deepLinkSession;
    deepLinkSession = null;
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
    // 重置分页状态与渲染缓存，只拉最近一页，滚动上翻再加载更早记录
    state.paging.set(id, { oldestIdx: null, hasMore: true, loading: false });
    state.messages.set(id, []);
    state.rendered.clear();
    send({ type: 'get-history', sessionId: id, limit: HISTORY_PAGE });
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
    case 'image': {
      const wrap = el('div', 'blk');
      const img = document.createElement('img');
      img.className = 'blk-image';
      img.alt = '图片';
      img.loading = 'lazy';
      if (blk.data) img.src = `data:${blk.mediaType || 'image/png'};base64,${blk.data}`;
      wrap.appendChild(img);
      return wrap;
    }
    case 'document': {
      const wrap = el('div', 'blk');
      const name = blk.name || '文档';
      const a = document.createElement('a');
      a.className = 'blk-document';
      a.target = '_blank';
      a.rel = 'noopener';
      if (blk.data) {
        a.href = `data:${blk.mediaType || 'application/pdf'};base64,${blk.data}`;
        a.download = name;
      }
      const nameSpan = el('span', 'doc-name', '📄 ' + name);
      const typeSpan = el('span', 'doc-type', (blk.mediaType || '').split('/').pop() || '');
      a.append(nameSpan, typeSpan);
      wrap.appendChild(a);
      return wrap;
    }
    default:
      return el('div', 'blk blk-text', JSON.stringify(blk));
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

function appendRecords(records) {
  if (!records || !records.length) return;
  const sessionId = records[0].sessionId;
  const isSelected = sessionId === state.selected;
  const cache = state.messages.get(sessionId) || [];

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

// 分页历史：首屏整批替换，向上翻页时插到头部并保持滚动位置
function appendHistory(data) {
  const sessionId = data.sessionId;
  const records = data.records || [];
  const cache = state.messages.get(sessionId) || [];
  const paging = state.paging.get(sessionId) || { oldestIdx: null, hasMore: true, loading: false };
  paging.loading = false;
  paging.hasMore = !!data.hasMore;
  if (records.length) {
    const oldest = records.reduce(
      (m, r) => (r.idx != null && (m == null || r.idx < m)) ? r.idx : m, paging.oldestIdx);
    paging.oldestIdx = oldest;
  }
  state.paging.set(sessionId, paging);

  const isSelected = sessionId === state.selected;
  const isFirstPage = cache.length === 0;

  if (isFirstPage) {
    state.messages.set(sessionId, records.slice());
    if (isSelected) {
      els.conversation.innerHTML = '';
      state.rendered.clear();
      records.forEach(r => renderRecord(r));
      scrollToBottom();
    }
    return;
  }

  // 更早的一页：去重后插到缓存头部
  const seen = new Set(cache.map(r => r.idx));
  const fresh = records.filter(r => r.idx == null || !seen.has(r.idx));
  if (!fresh.length) { state.messages.set(sessionId, cache); return; }
  state.messages.set(sessionId, fresh.concat(cache));
  if (isSelected) prependRendered(fresh);
}

// 在 DOM 顶部插入更早的记录，并补偿新增高度，保持用户当前阅读位置不跳动
function prependRendered(records) {
  const prevHeight = els.conversation.scrollHeight;
  const prevTop = els.conversation.scrollTop;
  const frag = document.createDocumentFragment();
  records.forEach(r => { const el = buildRecordEl(r); if (el) frag.appendChild(el); });
  els.conversation.prepend(frag);
  els.conversation.scrollTop = prevTop + (els.conversation.scrollHeight - prevHeight);
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

// 只构建 DOM 元素（不插入、不动光标），供 append / prepend 共用
function buildRecordEl(rec) {
  let el;
  if (rec.role) {
    el = buildMessage(rec);
  } else if (rec.kind === 'sys') {
    el = buildBlock({ type: 'sys', text: rec.text });
  }
  if (el && rec.idx != null) {
    el.dataset.idx = rec.idx;
    state.rendered.set(rec.idx, el);
  }
  return el;
}

function renderRecord(rec) {
  if (rec.role) removeCursor();
  const el = buildRecordEl(rec);
  if (!el) return;
  els.conversation.appendChild(el);
  if (rec.role === 'assistant') addCursor();
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
  // 滚动到顶部且还有更早记录时，加载上一页
  if (els.conversation.scrollTop <= 0) maybeLoadOlder();
});

function maybeLoadOlder() {
  const id = state.selected;
  if (!id) return;
  const paging = state.paging.get(id);
  if (!paging || !paging.hasMore || paging.loading || paging.oldestIdx == null) return;
  paging.loading = true;
  state.paging.set(id, paging);
  send({ type: 'get-history', sessionId: id, beforeIdx: paging.oldestIdx, limit: HISTORY_PAGE });
}

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
        appendHistory(data);
      } else if (data.update) {
        applyRecordUpdates(data.records || [], data.sessionId);
      } else {
        appendRecords(data.records || []);
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
