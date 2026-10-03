(() => {
  const state = { homes: [], sessions: [], home: '', goal: '', query: '', selected: null, watchdog: { enabled:false }, refreshSeconds: window.CODEX_MONITOR_REFRESH_SECONDS || 5 };
  const $ = (selector) => document.querySelector(selector);
  const escapeHtml = (value) => String(value == null ? '' : value).replace(/[&<>'"]/g, (char) => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;' }[char]));
  const date = (value) => { if (!value) return '—'; const d = new Date(value); if (Number.isNaN(d.getTime())) return '—'; return d.toLocaleString([], { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' }); };
  const goalLabel = (status) => ({ active:'进行中', paused:'已暂停', complete:'已完成', 'budget-limited':'预算耗尽', budget_limited:'预算耗尽', budgetLimited:'预算耗尽', usage_limited:'用量受限', blocked:'已阻塞', none:'无 Goal', unknown:'未知' }[status] || status || '未知');
  const activityLabel = (status) => ({ working:'执行中', error:'错误', idle_goal:'等待继续', idle:'空闲' }[status] || status || '未知');

  async function getJson(url) { const response = await fetch(url, { cache:'no-store' }); if (!response.ok) throw new Error(`${response.status}`); return response.json(); }
  function setConnection(ok, text) { const el = $('#connection'); el.className = `connection ${ok ? 'ok' : 'error'}`; el.innerHTML = `<i></i>${text}`; }
  function renderWatchdog() { const button = $('#watchdog'); if (!button) return; button.textContent = `看门狗：${state.watchdog.enabled ? '开启' : '关闭'}`; button.classList.toggle('active', Boolean(state.watchdog.enabled)); }

  function renderStats() {
    const sessions = state.sessions; const goals = sessions.filter((s) => s.goal); const working = sessions.filter((s) => s.activity === 'working'); const activeGoals = goals.filter((s) => ['active','running'].includes(s.goal_status));
    const cards = [['会话总数', sessions.length, `${state.homes.length} 个 Home`], ['执行中', working.length, '最近 turn 正在运行'], ['活跃 Goal', activeGoals.length, `${goals.length} 个会话带 Goal`], ['最近更新', sessions[0] ? date(sessions[0].updated_at) : '—', sessions[0] ? sessions[0].home_label : '暂无数据']];
    $('#stats').innerHTML = cards.map(([label,value,hint]) => `<div class="stat"><div class="stat-label">${label}</div><div class="stat-value">${escapeHtml(value)}</div><div class="stat-hint">${escapeHtml(hint)}</div></div>`).join('');
  }
  function renderFilters() {
    const homeButtons = [['','全部 Home'], ...state.homes.map((h) => [h.home_id, h.label])];
    $('#home-filters').innerHTML = homeButtons.map(([key,label]) => `<button class="filter ${state.home === key ? 'active' : ''}" data-home="${escapeHtml(key)}">${escapeHtml(label)}</button>`).join('');
    $('#home-filters').querySelectorAll('[data-home]').forEach((button) => button.addEventListener('click', () => { state.home = button.dataset.home; loadSessions(); }));
    const counts = { '':state.sessions.length }; state.sessions.forEach((s) => { counts[s.goal_status] = (counts[s.goal_status] || 0) + 1; });
    const statuses = [['','全部'],['active','进行中'],['paused','已暂停'],['complete','已完成'],['budget-limited','预算耗尽'],['usage_limited','用量受限'],['blocked','已阻塞'],['none','无 Goal']];
    $('#goal-filters').innerHTML = statuses.map(([key,label]) => `<button class="filter ${state.goal === key ? 'active' : ''}" data-goal="${key}">${label} <small>${counts[key] || 0}</small></button>`).join('');
    $('#goal-filters').querySelectorAll('[data-goal]').forEach((button) => button.addEventListener('click', () => { state.goal = button.dataset.goal; loadSessions(); }));
  }
  function renderSessions() {
    const list = $('#sessions'); $('#result-count').textContent = `${state.sessions.length} 个结果`;
    if (!state.sessions.length) { list.innerHTML = '<div class="empty">没有符合条件的会话</div>'; return; }
    list.innerHTML = state.sessions.map((s) => `<article class="session-card ${state.selected && state.selected.key === s.key ? 'selected' : ''}" data-key="${escapeHtml(s.key)}">
      <div><div class="session-title">${escapeHtml(s.title)}</div><div class="session-preview">${escapeHtml(s.preview || '暂无摘要')}</div><div class="session-meta"><span class="home-tag">${escapeHtml(s.home_label)}</span><span class="pill goal-pill goal-${escapeHtml(s.goal_status)}">${goalLabel(s.goal_status)}</span><span class="activity ${escapeHtml(s.activity)}">${activityLabel(s.activity)}</span>${s.model ? `<span class="activity">${escapeHtml(s.model)}</span>` : ''}</div></div>
      <div><div class="session-time">${date(s.updated_at)}</div><div class="cwd" title="${escapeHtml(s.cwd)}">${escapeHtml(s.cwd || '')}</div></div>
    </article>`).join('');
    list.querySelectorAll('.session-card').forEach((card) => card.addEventListener('click', () => { const session = state.sessions.find((item) => item.key === card.dataset.key); if (session) window.location.href = `/session.html?home_id=${encodeURIComponent(session.home_id)}&thread_id=${encodeURIComponent(session.thread_id)}`; }));
  }
  function renderDetail() {
    const container = $('#detail'); if (!container) return; const s = state.selected; if (!s) { container.innerHTML = '<div class="empty detail-empty">选择一个会话查看详情</div>'; return; }
    const goal = s.goal; const turn = s.latest_turn;
    container.innerHTML = `<div class="detail-header"><span class="home-tag">${escapeHtml(s.home_label)}</span><h3>${escapeHtml(s.title)}</h3><div class="detail-sub">${escapeHtml(s.thread_id)}<br>${escapeHtml(s.cwd || '工作目录未知')}</div></div>
      <div class="detail-section"><div class="detail-label">Goal</div>${goal ? `<span class="pill goal-pill goal-${escapeHtml(goal.status)}">${goalLabel(goal.status)}</span><p class="objective">${escapeHtml(goal.objective || '没有目标描述')}</p><div class="kv"><span>Token 使用</span><span>${escapeHtml(goal.tokens_used)}${goal.token_budget ? ` / ${escapeHtml(goal.token_budget)}` : ''}</span><span>目标更新</span><span>${date(goal.updated_at)}</span></div>` : '<div class="muted">此会话没有持久化 Goal。</div>'}</div>
      <div class="detail-section"><div class="detail-label">Session</div><div class="kv"><span>活动状态</span><span>${activityLabel(s.activity)}</span><span>最近 turn</span><span>${escapeHtml((turn && turn.status) || '未知')}</span><span>模型</span><span>${escapeHtml(s.model || '未知')}</span><span>分支</span><span>${escapeHtml(s.git_branch || '未知')}</span></div></div>
      <div class="detail-section"><div class="detail-label">Recent events</div><div id="events" class="event-list"><div class="muted">读取事件…</div></div></div>`;
  }
  async function loadDetail(session) { try { const detail = await getJson(`/api/session?home_id=${encodeURIComponent(session.home_id)}&thread_id=${encodeURIComponent(session.thread_id)}`); const events = $('#events'); if (!events) return; events.innerHTML = detail.events && detail.events.length ? detail.events.map((e) => `<div class="event"><span>${escapeHtml(e.item_type)}</span><span>${date(e.created_at)}</span></div>`).join('') : '<div class="muted">没有可显示的事件</div>'; } catch (error) { const events = $('#events'); if (events) events.innerHTML = '<div class="error-text">读取事件失败</div>'; } }
  async function loadHomes() { const data = await getJson('/api/homes'); state.homes = data.homes || []; }
  async function loadWatchdog() { try { state.watchdog = await getJson('/api/watchdog'); renderWatchdog(); } catch (_) {} }
  async function loadSessions() { try { const query = new URLSearchParams({ limit:'1000' }); if (state.home) query.set('home_id', state.home); if (state.goal) query.set('goal_status', state.goal); if (state.query) query.set('q', state.query); const data = await getJson(`/api/sessions?${query}`); state.sessions = data.sessions || []; renderStats(); renderFilters(); renderSessions(); $('#last-sync').textContent = new Date().toLocaleTimeString([], { hour:'2-digit', minute:'2-digit', second:'2-digit' }); setConnection(true, '已连接'); } catch (error) { setConnection(false, '读取失败'); $('#sessions').innerHTML = '<div class="empty error-text">无法读取服务器数据</div>'; } }
  async function toggleWatchdog() { const button = $('#watchdog'); if (!button || button.disabled) return; button.disabled = true; try { state.watchdog = await fetch('/api/watchdog', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ enabled:!state.watchdog.enabled }) }).then((response) => { if (!response.ok) throw new Error(`${response.status}`); return response.json(); }); renderWatchdog(); } catch (_) { setConnection(false, '看门狗切换失败'); } finally { button.disabled = false; } }
  async function refresh() { try { await loadHomes(); await loadSessions(); await loadWatchdog(); } catch (error) { setConnection(false, '读取失败'); } }
  $('#refresh').addEventListener('click', refresh); $('#watchdog').addEventListener('click', toggleWatchdog); $('#search').addEventListener('input', (event) => { state.query = event.target.value; clearTimeout(window.__codexSearchTimer); window.__codexSearchTimer = setTimeout(loadSessions, 220); });
  $('#refresh-note').textContent = `每 ${state.refreshSeconds} 秒自动刷新`; refresh(); setInterval(refresh, state.refreshSeconds * 1000);
})();
