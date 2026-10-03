(() => {
  const $ = (selector) => document.querySelector(selector);
  const params = new URLSearchParams(window.location.search);
  const escapeHtml = (value) => String(value == null ? '' : value).replace(/[&<>'"]/g, (char) => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;' }[char]));
  const setStatus = (text, error) => { const node = $('#new-status'); node.className = `chat-status ${error ? 'error-text' : ''}`; node.textContent = text || ''; };
  const setConnection = (ok, text) => { const node = $('#connection'); node.className = `connection ${ok ? 'ok' : 'error'}`; node.innerHTML = `<i></i>${escapeHtml(text)}`; };
  async function loadHomes() {
    const response = await fetch('/api/homes', { cache:'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json(); const select = $('#home');
    select.innerHTML = (data.homes || []).map((home) => `<option value="${escapeHtml(home.home_id)}">${escapeHtml(home.label)} · ${escapeHtml(home.path)}</option>`).join('');
    if (params.get('home_id')) select.value = params.get('home_id');
    setConnection(true, '已连接');
  }
  async function submit(event) {
    event.preventDefault();
    const button = $('#new-send'); const message = ($('#message').value || '').trim();
    if (!message || button.disabled) return;
    button.disabled = true; $('#message').disabled = true; setStatus('正在创建线程…'); $('#live-reply').textContent = '';
    try {
      const response = await fetch('/api/chat/new', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ home_id:$('#home').value, message, model:$('#model').value, cwd:$('#cwd').value }) });
      if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(body.message || `HTTP ${response.status}`); }
      if (!response.body) throw new Error('浏览器不支持流式响应');
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''; let threadId = ''; let done = false;
      while (true) {
        const part = await reader.read(); if (part.done) break; buffer += decoder.decode(part.value, {stream:true });
        const packets = buffer.split('\n\n'); buffer = packets.pop() || '';
        for (const packet of packets) {
          const line = packet.split('\n').find((item) => item.indexOf('data: ') === 0); if (!line) continue;
          let data; try { data = JSON.parse(line.slice(6)); } catch (_) { continue; }
          if (data.type === 'status') setStatus(data.message || '处理中…');
          else if (data.type === 'created') { threadId = data.thread_id || (data.thread && data.thread.id) || ''; setStatus('线程已创建，正在生成第一条回复…'); }
          else if (data.type === 'started') setStatus('已开始执行…');
          else if (data.type === 'notification' && data.method === 'item/agentMessage/delta') { $('#live-reply').textContent += (data.params && (data.params.delta || data.params.text || '')); setStatus('正在生成回复…'); }
          else if (data.type === 'completed') { done = true; setStatus('完成，正在打开详情页…'); }
          else if (data.type === 'approval_required') setStatus('检测到审批请求，已拒绝；继续等待结果。', true);
          else if (data.type === 'error') throw new Error(data.message || 'app-server 执行失败');
        }
      }
      if (threadId) { window.location.href = `/session.html?home_id=${encodeURIComponent($('#home').value)}&thread_id=${encodeURIComponent(threadId)}`; }
      else if (!done) throw new Error('未收到新线程 ID');
    } catch (error) { setStatus(error.message || '创建失败', true); button.disabled = false; $('#message').disabled = false; }
  }
  $('#new-form').addEventListener('submit', submit);
  loadHomes().catch((error) => { setConnection(false, '读取失败'); setStatus(error.message || '无法读取 Home', true); });
})();
