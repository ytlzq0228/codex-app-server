(async () => {
  await globalThis.I18n.ready;
(() => {
  const root = document.querySelector('#history');
  if (!root) return;
  const base = root.dataset.historyBase || '/admin/history';
  const showUserAgent = base === '/admin/history';
  const pageParameter = root.dataset.historyPageParam || 'history_page';
  // The user portal hides which Worker served each request; administrators keep it.
  const showWorker = root.dataset.historyShowWorker !== 'false';
  const form = root.querySelector('[data-history-filters]');
  const results = root.querySelector('[data-history-results]');
  const pagination = root.querySelector('[data-history-pagination]');
  const error = root.querySelector('[data-history-error]');
  const summary = root.querySelector('[data-history-summary]');
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const {tokens, total: totalTokens} = window.TokenFormat;
  const price = n => n == null ? globalThis.I18n.t("未定价") : esc(n);
  const time = value => esc(new Date(value).toLocaleString());
  const badge = status => `<span class="badge ${status >= 400 && status !== 499 ? 'badge-error' : 'badge-ok'}">${esc(status)}</span>`;
  let controller;
  let generation = 0;
  async function json(url, signal) {
    const response = await fetch(url, {signal, headers: {Accept:'application/json'}, cache:'no-store'});
    if (response.redirected || !response.headers.get('content-type')?.includes('application/json')) throw new Error(globalThis.I18n.t("登录状态已失效，请刷新页面重新登录"));
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : globalThis.I18n.t("加载失败，请重试"));
    return data;
  }
  function parameters() {
    const params = new URLSearchParams(new FormData(form));
    for (const [key,value] of [...params]) if (!value) params.delete(key);
    return params;
  }
  function render(data, signal, version) {
    summary.textContent = globalThis.I18n.t("共 {v0} 条请求，聚合为 {v1} 个会话", undefined, {v0:data.request_total,v1:data.total});
    results.innerHTML = `<div class="table-wrap"><table><thead><tr><th>${globalThis.I18n.t("最近请求")}</th><th>${globalThis.I18n.t("逻辑会话 ID")}</th><th>Key</th><th>${globalThis.I18n.t("接口")}${showUserAgent ? "/UA" : ""}</th><th>${globalThis.I18n.t("模型（最近请求）")}</th><th>${globalThis.I18n.t("请求数")}</th><th>${globalThis.I18n.t("最近请求状态")}</th><th>${globalThis.I18n.t("总 Token")}</th><th>${globalThis.I18n.t("总价格（USD）")}</th><th></th></tr></thead><tbody></tbody></table></div>`;
    const body = results.querySelector('tbody');
    if (!data.groups.length) body.innerHTML = `<tr><td colspan="10" class="empty">${globalThis.I18n.t("暂无请求历史")}</td></tr>`;
    data.groups.forEach((group,index) => {
      const agent = group.latest_user_agent || '—';
      const agentChars = Array.from(agent);
      const agentLabel = agentChars.length > 10 ? agentChars.slice(0, 10).join('') + '...' : agent;
      const row = document.createElement('tr'); row.className = 'history-row';
      row.innerHTML = `<td>${time(group.latest_at)}</td><td><code title="${esc(group.conversation_id)}">${esc(group.conversation_id)}</code>${group.logical?'':`<small>${globalThis.I18n.t("旧记录：以 Thread / 请求 ID 标识")}</small>`}<small>${group.thread_count} ${globalThis.I18n.t("个 Worker Thread")}</small></td><td>${esc(group.key_name || globalThis.I18n.t("已删除"))}</td><td><span class="badge">${esc(group.endpoint || 'unknown')}</span>${showUserAgent ? `<small title="${esc(agent)}">${esc(agentLabel)}</small>` : ''}</td><td>${esc(group.latest_model || '—')}</td><td>${group.request_count}</td><td>${badge(group.latest_status)}</td><td>${totalTokens(group.input_tokens, group.output_tokens)}</td><td>${price(group.cost_usd)}${group.unpriced_count?`<small>${globalThis.I18n.t("另有")} ${group.unpriced_count} ${globalThis.I18n.t("条未定价")}</small>`:''}</td><td><button type="button" class="button button-small" aria-expanded="false">${globalThis.I18n.t("展开")}</button></td>`;
      const detail = document.createElement('tr'); detail.className = 'history-detail history-conversation-detail'; detail.hidden = true; detail.id = `history-group-${index}`;
      detail.innerHTML = `<td colspan="10"><div class="conversation-records"><div class="conversation-summary"><span>${globalThis.I18n.t("输入")} ${tokens(group.input_tokens)} Token</span><span>${globalThis.I18n.t("输出")} ${tokens(group.output_tokens)} Token</span><span>${globalThis.I18n.t("累计耗时")} ${group.duration_ms} ms</span><span>${globalThis.I18n.t("总价格 USD：")}${price(group.cost_usd)}</span></div><p data-detail-error class="alert alert-error" hidden></p><div class="table-wrap"><table><thead><tr><th>${globalThis.I18n.t("时间")}</th><th>${globalThis.I18n.t("请求 ID")}</th><th>${showWorker ? 'Worker' : 'Thread'}</th><th>${globalThis.I18n.t("模型")}</th><th>${globalThis.I18n.t("状态")}</th><th>Token</th><th>${globalThis.I18n.t("耗时")}</th><th>${globalThis.I18n.t("价格（USD）")}</th></tr></thead><tbody></tbody></table></div><div class="history-more"><button type="button" class="button button-small">${globalThis.I18n.t("加载请求")}</button></div></div></td>`;
      const toggle = row.querySelector('button'); toggle.setAttribute('aria-controls', detail.id);
      const more = detail.querySelector('button'); const detailError = detail.querySelector('[data-detail-error]');
      let nextPage = 1, loaded = 0, busy = false;
      async function loadRequests() {
        if (busy || !nextPage) return;
        busy = true; more.disabled = true; more.textContent = globalThis.I18n.t("正在加载…"); detailError.hidden = true;
        try {
          const params = new URLSearchParams({conversation:group.conversation_id,key_id:group.key_id || 'development',endpoint:group.endpoint || 'unknown',page:nextPage});
          const batch = await json(base+'/requests?'+params, signal);
          if (version !== generation) return;
          const rows = detail.querySelector('tbody');
          for (const request of batch.requests) {
            const item = document.createElement('tr'); item.dataset.historyRequest = '';
            item.innerHTML = `<td>${time(request.created_at)}</td><td><a data-request-detail href="/user/usage/${encodeURIComponent(request.request_id)}">${esc(request.request_id)}</a><small>${esc(request.owner_username || '—')}</small></td><td>${showWorker ? `${esc(request.worker_name || '—')}<small>Thread：${esc(request.thread_id || globalThis.I18n.t("未记录"))}</small>` : esc(request.thread_id || globalThis.I18n.t("未记录"))}<small>${globalThis.I18n.t("关联：")}${esc(request.evidence || 'legacy_thread')}</small></td><td>${esc(request.model)}</td><td>${badge(request.status_code)}${request.error_code?`<small>${esc(request.error_code)}</small>`:''}</td><td>${totalTokens(request.input_tokens, request.output_tokens)}</td><td>${request.duration_ms} ms</td><td>${price(request.cost_usd)}</td>`;
            rows.append(item);
          }
          loaded += batch.requests.length; nextPage = batch.page < batch.pages ? batch.page+1 : 0;
          more.hidden = !nextPage; more.textContent = globalThis.I18n.t("更多（剩余 {v0} 条）", undefined, {v0:Math.max(0,batch.total-loaded)});
          if (!loaded) rows.innerHTML = `<tr><td colspan="8" class="empty">${globalThis.I18n.t("暂无请求历史")}</td></tr>`;
        } catch (e) {
          if (e.name !== 'AbortError') {detailError.textContent = e.message; detailError.hidden = false; more.textContent = globalThis.I18n.t("重试加载");}
        } finally {busy = false; more.disabled = false;}
      }
      more.addEventListener('click', loadRequests);
      toggle.addEventListener('click', () => {
        detail.hidden = !detail.hidden; toggle.textContent = detail.hidden ? globalThis.I18n.t("展开") : globalThis.I18n.t("收起"); toggle.setAttribute('aria-expanded', String(!detail.hidden));
        if (!detail.hidden && !loaded) loadRequests();
      });
      row.addEventListener('click', event => {if (!event.target.closest('button,a,form')) toggle.click();});
      body.append(row,detail);
    });
    pagination.replaceChildren();
    for (const [label,page] of [[globalThis.I18n.t("上一页"),data.page-1],[globalThis.I18n.t("下一页"),data.page+1]]) {
      const button = document.createElement('button');button.type = 'button';button.className = 'button button-small';button.textContent = label;button.disabled = page<1 || page>data.pages;
      button.addEventListener('click', () => load(page));pagination.append(button);
      if (label===globalThis.I18n.t("上一页")) {const text=document.createElement('span');text.textContent=globalThis.I18n.t("第 {v0} / {v1} 页", undefined, {v0:data.page,v1:data.pages});pagination.append(text);}
    }
  }
  async function load(page, push=true) {
    controller?.abort();controller = new AbortController();const version = ++generation;
    const signal = controller.signal;const params = parameters();params.set(pageParameter,page);
    error.hidden = true;summary.textContent = globalThis.I18n.t("正在加载请求历史…");results.replaceChildren();pagination.replaceChildren();results.setAttribute('aria-busy','true');
    try {
      const responseData = window.initialPageHistory || await json(base+'/data?'+params,signal);
      delete window.initialPageHistory;
      const data = responseData.history || responseData;
      if (version !== generation) return;
      render(data,signal,version);params.set(pageParameter,data.page);
      if(push) history.pushState(null,'',base+'?'+params);else history.replaceState(null,'',base+'?'+params);
    } catch(e) {if(e.name!=='AbortError'){summary.textContent=globalThis.I18n.t("请求历史加载失败");error.textContent=e.message;error.hidden=false;}}
    finally {if(version===generation) results.removeAttribute('aria-busy');}
  }
  form.addEventListener('submit', event => {event.preventDefault();if(form.checkValidity()) load(1);});
  // Back/forward restores filter controls and the searchable Key picker together.
  window.addEventListener('popstate', () => location.reload());
  load(Math.max(1,Number(new URLSearchParams(location.search).get(pageParameter)) || 1),false);
})();

})();
