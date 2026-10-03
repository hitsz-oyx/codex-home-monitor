(() => {
  const params = new URLSearchParams(window.location.search);
  const homeId = params.get('home_id') || '';
  const threadId = params.get('thread_id') || '';
  const state = { items: [], hasMore: false, cursor: null, loadingOlder: false };
  const $ = (selector) => document.querySelector(selector);
  const escapeHtml = (value) => String(value == null ? '' : value).replace(/[&<>'"]/g, (char) => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;' }[char]));
  const date = (value) => { if (!value) return '—'; const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? '—' : parsed.toLocaleString([], { year:'numeric', month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' }); };
  const goalLabel = (status) => ({ active:'进行中', paused:'已暂停', complete:'已完成', 'budget-limited':'预算耗尽', budgetLimited:'预算耗尽', usage_limited:'用量受限', blocked:'已阻塞', none:'无 Goal', unknown:'未知' }[status] || status || '未知');
  const setConnection = (ok, text) => { const node = $('#connection'); node.className = `connection ${ok ? 'ok' : 'error'}`; node.innerHTML = `<i></i>${escapeHtml(text)}`; };
  async function getJson(url) { const response = await fetch(url, { cache:'no-store' }); if (!response.ok) throw new Error(`${response.status}`); return response.json(); }

  function renderHeader(session) {
    $('#session-header').innerHTML = `<div class="session-hero-main"><span class="home-tag">${escapeHtml(session.home_label)}</span><h2>${escapeHtml(session.title)}</h2><p>${escapeHtml(session.preview || '暂无摘要')}</p><div class="session-meta"><span class="pill goal-pill goal-${escapeHtml(session.goal_status)}">${goalLabel(session.goal_status)}</span><span class="activity ${escapeHtml(session.activity)}">${escapeHtml(session.activity)}</span><span class="session-time">最近更新 ${date(session.updated_at)}</span></div></div><div class="thread-id"><span>THREAD ID</span><code>${escapeHtml(session.thread_id)}</code></div>`;
    const goal = session.goal;
    $('#goal-card').innerHTML = `<div class="detail-label">GOAL</div>${goal ? `<span class="pill goal-pill goal-${escapeHtml(goal.status)}">${goalLabel(goal.status)}</span><p class="objective">${escapeHtml(goal.objective || '没有目标描述')}</p><div class="kv"><span>Token 使用</span><span>${escapeHtml(goal.tokens_used)}${goal.token_budget ? ` / ${escapeHtml(goal.token_budget)}` : ''}</span><span>目标更新</span><span>${date(goal.updated_at)}</span></div>` : '<p class="muted">此会话没有持久化 Goal。</p>'}`;
    const turn = session.latest_turn || {};
    $('#meta-card').innerHTML = `<div class="detail-label">SESSION</div><div class="kv"><span>Home</span><span>${escapeHtml(session.home_label)}</span><span>工作目录</span><span title="${escapeHtml(session.cwd)}">${escapeHtml(session.cwd || '未知')}</span><span>模型</span><span>${escapeHtml(session.model || '未知')}</span><span>分支</span><span>${escapeHtml(session.git_branch || '未知')}</span><span>最近 turn</span><span>${escapeHtml(turn.status || '未知')}</span></div>`;
  }

  function codeBlock(text) { return text ? `<pre class="transcript-pre">${escapeHtml(text)}</pre>` : '<div class="muted">无输出</div>'; }
  function renderCommand(item) {
    const status = item.status || 'unknown';
    return `<article class="message message-tool command-message"><div class="message-kind">COMMAND</div><details class="command-details"><summary><span>命令执行 · ${escapeHtml(status)}</span><time>${date(item.created_at)}</time></summary><div class="command-details-body"><div class="command-line">$ ${escapeHtml(item.command || '')}</div>${codeBlock(item.text)}${item.cwd ? `<div class="message-foot">工作目录：${escapeHtml(item.cwd)}</div>` : ''}</div></details></article>`;
  }
  function renderItem(item) {
    const header = `<div class="message-head"><span>${escapeHtml(item.title || item.item_type)}</span><time>${date(item.created_at)}</time></div>`;
    if (item.kind === 'command') return renderCommand(item);
    if (item.kind === 'file') return `<article class="message message-file"><div class="message-kind">FILE CHANGE</div>${header}<div class="file-changes">${(item.changes || []).map((change) => `<details><summary>${escapeHtml(change.kind)} · ${escapeHtml(change.path)}</summary>${codeBlock(change.diff)}</details>`).join('')}</div></article>`;
    if (item.kind === 'search') return `<article class="message message-tool"><div class="message-kind">SEARCH</div>${header}<div class="message-text">${escapeHtml(item.query || item.text || '')}</div></article>`;
    if (item.kind === 'plan') return `<article class="message message-plan"><div class="message-kind">PLAN</div>${header}<div class="message-text markdown-lite">${escapeHtml(item.text || '')}</div></article>`;
    return `<article class="message message-${escapeHtml(item.role || 'system')}">${header}<div class="message-text">${escapeHtml(item.text || '无正文')}</div></article>`;
  }
  function renderItems(items) {
    const chunks = [];
    for (let index = 0; index < items.length;) {
      const item = items[index];
      if (item.kind !== 'command') { chunks.push(renderItem(item)); index += 1; continue; }
      const commands = [];
      const turnId = item.turn_id;
      while (index < items.length && items[index].kind === 'command' && items[index].turn_id === turnId) {
        commands.push(items[index]); index += 1;
      }
      const label = commands.length === 1 ? '命令执行' : `命令执行 · ${commands.length} 条`;
      chunks.push(`<details class="command-group"><summary><span>${label}</span><span class="command-group-note">点击展开单条命令</span></summary><div class="command-group-body">${commands.map(renderCommand).join('')}</div></details>`);
    }
    return chunks.join('');
  }
  function itemKey(item, index) { return item.item_id || `${item.turn_id || 'turn'}:${item.created_at || 'time'}:${index}`; }
  function updateCount() { $('#content-count').textContent = `${state.items.length} 条可显示内容${state.hasMore ? ' · 向上滚动加载更早记录' : ''}`; }
  function renderTranscript(initial) {
    const transcript = $('#transcript');
    if (initial) {
      state.items = initial.content || [];
      state.hasMore = Boolean(initial.has_more || initial.content_truncated);
      state.cursor = initial.next_before_created_at_ms || null;
      transcript.innerHTML = state.items.length ? renderItems(state.items) : '<div class="empty">没有可显示的正文内容</div>';
      updateCount();
      requestAnimationFrame(() => { transcript.scrollTop = transcript.scrollHeight; });
      return;
    }
    transcript.innerHTML = state.items.length ? renderItems(state.items) : '<div class="empty">没有可显示的正文内容</div>';
    updateCount();
  }
  async function loadOlder() {
    const transcript = $('#transcript');
    if (!state.hasMore || !state.cursor || state.loadingOlder) return;
    state.loadingOlder = true; const oldHeight = transcript.scrollHeight; const oldTop = transcript.scrollTop;
    try {
      const query = `/api/session?home_id=${encodeURIComponent(homeId)}&thread_id=${encodeURIComponent(threadId)}&content_limit=200&before_created_at_ms=${encodeURIComponent(state.cursor)}`;
      const older = await getJson(query);
      const existing = new Set(state.items.map(itemKey));
      const prepend = (older.content || []).filter((item, index) => !existing.has(itemKey(item, index)));
      state.items = prepend.concat(state.items);
      state.hasMore = Boolean(older.has_more || older.content_truncated);
      state.cursor = older.next_before_created_at_ms || null;
      renderTranscript(false);
      requestAnimationFrame(() => { transcript.scrollTop = oldTop + (transcript.scrollHeight - oldHeight); });
    } catch (error) {
      setConnection(false, '历史读取失败');
    } finally { state.loadingOlder = false; }
  }
  function setChatStatus(text, error) { const node = $('#chat-status'); if (!node) return; node.className = `chat-status ${error ? 'error-text' : ''}`; node.textContent = text || ''; }
  async function sendChat(event) {
    event.preventDefault();
    const input = $('#chat-input'); const button = $('#chat-send'); const live = $('#live-reply');
    const message = (input.value || '').trim();
    if (!message || button.disabled) return;
    button.disabled = true; input.disabled = true; live.textContent = ''; setChatStatus('正在连接 app-server…');
    try {
      const response = await fetch('/api/chat', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ home_id:homeId, thread_id:threadId, message }) });
      if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(body.message || `HTTP ${response.status}`); }
      if (!response.body) throw new Error('浏览器不支持流式响应');
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''; let sawDone = false;
      while (true) {
        const part = await reader.read(); if (part.done) break; buffer += decoder.decode(part.value, {stream:true});
        const packets = buffer.split('\n\n'); buffer = packets.pop() || '';
        for (const packet of packets) {
          const line = packet.split('\n').find((item) => item.indexOf('data: ') === 0); if (!line) continue;
          let data; try { data = JSON.parse(line.slice(6)); } catch (_) { continue; }
          if (data.type === 'status') setChatStatus(data.message || '处理中…');
          else if (data.type === 'started') setChatStatus('已开始执行，等待回复…');
          else if (data.type === 'approval_required') setChatStatus('检测到审批请求，已拒绝；继续等待结果。', true);
          else if (data.type === 'notification' && data.method === 'item/agentMessage/delta') { const delta = data.params && (data.params.delta || data.params.text || ''); live.textContent += delta; setChatStatus('正在生成回复…'); }
          else if (data.type === 'completed') { sawDone = true; setChatStatus('完成，正在刷新会话内容…'); }
          else if (data.type === 'error') throw new Error(data.message || 'app-server 执行失败');
        }
      }
      if (!sawDone) setChatStatus('连接已结束，正在刷新会话内容…');
      input.value = ''; setTimeout(load, 800);
    } catch (error) { setChatStatus(error.message || '发送失败', true); }
    finally { button.disabled = false; input.disabled = false; }
  }
  async function load() {
    if (!homeId || !threadId) { setConnection(false, '缺少会话参数'); $('#transcript').innerHTML = '<div class="empty error-text">URL 中缺少 home_id 或 thread_id</div>'; return; }
    try {
      const detail = await getJson(`/api/session?home_id=${encodeURIComponent(homeId)}&thread_id=${encodeURIComponent(threadId)}&content_limit=200`);
      renderHeader(detail); renderTranscript(detail); $('#chat-form').onsubmit = sendChat; $('#transcript').onscroll = () => { if ($('#transcript').scrollTop < 60) loadOlder(); }; setConnection(true, '已连接');
    } catch (error) { setConnection(false, '读取失败'); $('#session-header').innerHTML = '<div class="empty error-text">无法读取该会话</div>'; $('#transcript').innerHTML = '<div class="empty error-text">请返回列表重试</div>'; }
  }
  load();
})();
