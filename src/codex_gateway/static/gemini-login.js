(() => {
  const dialog = document.getElementById('gemini-login-dialog');
  if (!dialog) return;
  const status = document.getElementById('gemini-login-status');
  const title = document.getElementById('gemini-login-title');
  const options = document.getElementById('gemini-login-options');
  const authorization = document.getElementById('gemini-authorization');
  const link = document.getElementById('gemini-login-link');
  const code = document.getElementById('gemini-auth-code');
  const account = document.getElementById('gemini-login-account');
  const progress = document.getElementById('gemini-login-progress');
  const progressText = document.getElementById('gemini-login-progress-text');
  const retry = document.getElementById('provider-login-retry');
  const expiry = document.getElementById('provider-login-expiry');
  let current;
  const visible = run => current === run && !run.closed;
  function showProgress(message) {
    progressText.textContent = message || '正在完成登录设置，请稍候…';
    progress.hidden = false;
    dialog.setAttribute('aria-busy', 'true');
  }
  function hideControls() {
    options.hidden = true; options.replaceChildren();
    authorization.hidden = true; link.removeAttribute('href');
    account.hidden = true; account.replaceChildren();
    progress.hidden = true; retry.hidden = true;
    dialog.removeAttribute('aria-busy');
  }
  async function call(run, action, extra = {}) {
    const body = new FormData();
    body.set('csrf_token', run.csrf); body.set('session_id', run.session || '');
    Object.entries(extra).forEach(([key, value]) => body.set(key, value));
    const response = await fetch(run.base + action, {method: 'POST', body, cache: 'no-store'});
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(data.error?.message || (typeof data.detail === 'string' ? data.detail : '登录操作未完成，请重试'));
      error.status = response.status; throw error;
    }
    return data;
  }
  function render(run, data) {
    if (!visible(run)) return;
    // The CLI can keep the previous menu visible briefly after accepting a choice.
    // Keep the progress state until it advances, so the choice cannot be submitted twice.
    if (run.pendingMenu && data.stage === 'choose' && data.menu_id === run.pendingMenu
        && !data.logged_in && !data.error) return;
    expiry.textContent = !data.logged_in && Number.isFinite(data.expires_in) ? '授权会话剩余约 ' + Math.ceil(data.expires_in / 60) + ' 分钟' : '';
    const view = JSON.stringify([data.stage, data.menu_id, data.login_url, data.logged_in, data.error, data.title, data.message, data.verification]);
    if (view === run.view) return;
    run.view = view; hideControls();
    title.textContent = data.title || '登录 ' + run.label + ' 订阅账号';
    status.textContent = data.message || '请选择账号的登录方式。';
    if (data.logged_in) {
      run.done = true; run.loggedIn = true; run.reload = true; code.value = '';
      title.textContent = data.verification?.ok ? '登录成功，推理测试通过' : '登录成功，推理测试未通过'; status.textContent = data.verification?.message || '账号已连接，等待自动探测。';
      for (const [label, value] of [['账号', data.account?.email], ['订阅套餐', data.account?.planType], ['项目', data.account?.project]]) {
        const dt = document.createElement('dt'), dd = document.createElement('dd');
        dt.textContent = label; dd.textContent = value || '—'; account.append(dt, dd);
      }
      account.hidden = false;
      retry.hidden = !!data.verification?.ok; retry.textContent = '重新探测账号';
      return;
    }
    if (data.error) { status.textContent = data.error; run.done = true; retry.hidden = false; retry.textContent = '重新开始登录'; return; }
    if (data.stage !== 'waiting') run.pendingMenu = null;
    if (data.stage === 'waiting') {
      showProgress(run.pendingMenu ? run.pendingMessage : (data.message || '正在等待登录服务响应…'));
    } else if (data.stage === 'choose') {
      options.hidden = false;
      for (const option of data.options || []) {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'button'; button.textContent = option.label;
        button.addEventListener('click', () => submit(run, {key: 'select:' + option.id, menu_id: data.menu_id}));
        options.append(button);
      }
    } else if (data.stage === 'authorize' && data.login_url) {
      try {
        const url = new URL(data.login_url);
        if (url.protocol !== 'https:' || url.hostname !== (run.provider === 'claude' ? 'claude.com' : 'accounts.google.com')) throw new Error();
        link.href = url.href; authorization.hidden = false;
      } catch { status.textContent = '无法读取有效的授权链接，请关闭后重新登录。'; run.done = true; }
    }
  }
  async function poll(run) {
    if (!visible(run) || run.done || run.busy) return;
    try { const data = await call(run, 'status'); if (!run.busy) render(run, data); }
    catch (error) { if (visible(run)) { status.textContent = error.message; run.done = true; hideControls(); retry.hidden = false; retry.textContent = '重试'; } }
    if (visible(run) && !run.done && !run.busy) { clearTimeout(run.timer); run.timer = setTimeout(() => poll(run), 1200); }
  }
  async function submit(run, input) {
    if (!visible(run) || run.busy || run.done) return;
    run.busy = true; clearTimeout(run.timer);
    const confirmingTerms = input.menu_id?.startsWith('onboarding-terms');
    const trustingWorkspace = input.menu_id === 'workspace-trust' && input.key === 'select:0';
    if (confirmingTerms || trustingWorkspace) {
      run.pendingMenu = input.menu_id;
      run.pendingMessage = trustingWorkspace ? '正在确认 Worker 工作区，请稍候…' : '正在完成登录设置，请稍候…';
      run.view = null; hideControls();
      title.textContent = trustingWorkspace ? '正在确认 Worker 工作区' : '正在完成登录设置';
      status.textContent = trustingWorkspace ? '登录服务正在处理工作区信任确认。' : '登录服务正在处理条款确认。';
      showProgress(run.pendingMessage);
    }
    dialog.querySelectorAll('#gemini-login-options button, #gemini-code-form button').forEach(button => button.disabled = true);
    try {
      const data = await call(run, 'input', input);
      if (data.stage || data.logged_in || data.error) render(run, data);
      else if (visible(run) && input.key === 'code') { hideControls(); showProgress('授权码已提交，正在等待登录结果…'); }
    }
    catch (error) { if (visible(run)) { run.pendingMenu = null; progress.hidden = true; dialog.removeAttribute('aria-busy'); status.textContent = error.message; } }
    finally {
      run.busy = false;
      if (visible(run)) {
        dialog.querySelectorAll('#gemini-login-options button, #gemini-code-form button').forEach(button => button.disabled = false);
        clearTimeout(run.timer); run.timer = setTimeout(() => poll(run), 400);
      }
    }
  }
  async function logoutBeforeLogin(run) {
    if (!run.needsLogout) return;
    status.textContent = '正在退出当前账号…';
    showProgress('正在安全退出原账号…');
    await call(run, 'logout');
    run.needsLogout = false; run.reload = true;
    if (run.closed) location.reload();
  }
  document.querySelectorAll('[data-provider-login], [data-gemini-login]').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    if (current && !current.closed) return;
    if (form.dataset.confirm && !confirm(form.dataset.confirm)) return;
    const fields = new FormData(form);
    const provider = form.dataset.provider || 'gemini';
    const run = {provider, label: provider === 'claude' ? 'Claude' : 'Gemini', base: (form.dataset.geminiAdmin === 'true' ? '/admin/workers/' : '/user/workers/') + form.dataset.workerId + '/provider-login/', csrf: fields.get('csrf_token'), needsLogout: fields.get('force') === 'true'};
    current = run; hideControls(); code.value = ''; expiry.textContent = '';
    dialog.querySelector('.eyebrow').textContent = run.label.toUpperCase();
    link.textContent = '打开 ' + (provider === 'claude' ? 'Claude' : 'Google') + ' 授权页面';
    dialog.querySelector('.form-help').textContent = provider === 'claude' ? '使用 Claude 付费订阅账号完成授权，再粘贴授权码。' : '使用拥有订阅权益的账号完成授权。项目与许可证将在登录过程中选择。';
    document.querySelector('#gemini-code-form button').disabled = false;
    title.textContent = '登录 ' + run.label + ' 订阅账号'; status.textContent = '正在启动登录…'; dialog.showModal();
    showProgress('正在启动登录服务…');
    try {
      await logoutBeforeLogin(run);
      if (run.closed) return;
      const data = await call(run, 'start'); run.session = data.session_id;
      if (run.closed) { await call(run, 'input', {key: 'cancel'}); return; }
      render(run, data); poll(run);
    } catch (error) { if (visible(run)) { status.textContent = error.message; run.done = true; hideControls(); retry.hidden = false; retry.textContent = '重试'; } }
  }));
  retry.addEventListener('click', async () => {
    const run = current;
    if (!run || !visible(run) || run.busy) return;
    run.busy = true; retry.disabled = true;
    clearTimeout(run.timer); run.view = null;
    try {
      if (run.loggedIn) {
        status.textContent = '正在重新探测账号…';
        const response = await fetch(run.base.replace(/provider-login\/$/, 'probe'), {
          method: 'POST', body: new URLSearchParams({csrf_token: run.csrf}), cache: 'no-store'
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error?.message || data.detail || '探测失败');
        if (visible(run)) {
          status.textContent = data.message || '探测已完成'; run.reload = true;
          if (data.ok) { title.textContent = '登录成功，推理测试通过'; retry.hidden = true; }
        }
      } else {
        if (run.session) {
          try { await call(run, 'input', {key: 'cancel'}); }
          catch (error) { if (error.status !== 409) throw error; }
        }
        if (!visible(run)) return;
        run.session = ''; run.done = false;
        await logoutBeforeLogin(run);
        if (run.closed) return;
        const data = await call(run, 'start'); run.session = data.session_id;
        if (run.closed) { await call(run, 'input', {key: 'cancel'}); return; }
        render(run, data);
      }
    } catch (error) {
      if (visible(run)) { status.textContent = error.message; run.done = true; hideControls(); retry.hidden = false; }
    } finally {
      run.busy = false; retry.disabled = false;
      if (visible(run) && !run.done) poll(run);
    }
  });
  document.getElementById('gemini-code-form').addEventListener('submit', event => {
    event.preventDefault(); const value = code.value.trim();
    if (!value || !current) return;
    code.value = ''; status.textContent = '正在完成登录并测试模型访问，请稍候…'; submit(current, {key: 'code', code: value});
  });
  dialog.addEventListener('close', () => {
    const run = current; if (!run) return;
    run.closed = true; clearTimeout(run.timer); code.value = ''; hideControls();
    const cancel = run.session && !run.loggedIn ? call(run, 'input', {key: 'cancel'}).catch(() => {}) : Promise.resolve();
    cancel.finally(() => { if (run.reload) location.reload(); });
  });
})();
