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
    progress.hidden = true;
    dialog.removeAttribute('aria-busy');
  }
  async function call(run, action, extra = {}) {
    const body = new FormData();
    body.set('csrf_token', run.csrf); body.set('session_id', run.session || '');
    Object.entries(extra).forEach(([key, value]) => body.set(key, value));
    const response = await fetch(run.base + action, {method: 'POST', body, cache: 'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || (typeof data.detail === 'string' ? data.detail : '登录操作未完成，请重试'));
    return data;
  }
  function render(run, data) {
    if (!visible(run)) return;
    // The CLI can keep its terms screen visible briefly after accepting Enter.
    // Keep the pending state until it actually advances, so the choice cannot be submitted twice.
    if (run.confirmingTerms && data.stage === 'choose' && data.menu_id === 'onboarding-terms'
        && !data.logged_in && !data.error) return;
    const view = JSON.stringify([data.stage, data.menu_id, data.login_url, data.logged_in, data.error, data.title, data.message]);
    if (view === run.view) return;
    run.view = view; hideControls();
    title.textContent = data.title || '登录 Gemini 订阅账号';
    status.textContent = data.message || '请选择账号的登录方式。';
    if (data.logged_in) {
      run.done = true; run.loggedIn = true; run.reload = true; code.value = '';
      title.textContent = data.verification?.ok ? '登录成功，推理测试通过' : '登录成功，推理测试未通过'; status.textContent = data.verification?.message || '账号已连接，等待自动探测。';
      for (const [label, value] of [['账号', data.account?.email], ['订阅套餐', data.account?.planType], ['项目', data.account?.project]]) {
        const dt = document.createElement('dt'), dd = document.createElement('dd');
        dt.textContent = label; dd.textContent = value || '—'; account.append(dt, dd);
      }
      account.hidden = false; return;
    }
    if (data.error) { status.textContent = data.error; run.done = true; return; }
    if (data.stage !== 'waiting') run.confirmingTerms = false;
    if (data.stage === 'waiting') {
      showProgress(run.confirmingTerms ? '正在完成登录设置，请稍候…' : (data.message || '正在等待登录服务响应…'));
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
        if (url.protocol !== 'https:' || url.hostname !== 'accounts.google.com') throw new Error();
        link.href = url.href; authorization.hidden = false;
      } catch { status.textContent = '无法读取有效的 Google 授权链接，请关闭后重新登录。'; run.done = true; }
    }
  }
  async function poll(run) {
    if (!visible(run) || run.done || run.busy) return;
    try { const data = await call(run, 'status'); if (!run.busy) render(run, data); }
    catch (error) { if (visible(run)) { status.textContent = error.message; run.done = true; hideControls(); } }
    if (visible(run) && !run.done && !run.busy) { clearTimeout(run.timer); run.timer = setTimeout(() => poll(run), 1200); }
  }
  async function submit(run, input) {
    if (!visible(run) || run.busy || run.done) return;
    run.busy = true; clearTimeout(run.timer);
    const confirmingTerms = input.menu_id === 'onboarding-terms';
    if (confirmingTerms) {
      run.confirmingTerms = true;
      run.view = null; hideControls();
      title.textContent = '正在完成登录设置';
      status.textContent = '条款已确认，登录服务正在初始化账号环境。';
      showProgress('正在完成登录设置，请稍候…');
    }
    dialog.querySelectorAll('#gemini-login-options button, #gemini-code-form button').forEach(button => button.disabled = true);
    try { await call(run, 'input', input); }
    catch (error) { if (visible(run)) { run.confirmingTerms = false; progress.hidden = true; dialog.removeAttribute('aria-busy'); status.textContent = error.message; } }
    finally {
      run.busy = false;
      if (visible(run)) {
        dialog.querySelectorAll('#gemini-login-options button, #gemini-code-form button').forEach(button => button.disabled = false);
        clearTimeout(run.timer); run.timer = setTimeout(() => poll(run), 400);
      }
    }
  }
  document.querySelectorAll('[data-gemini-login]').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    if (current && !current.closed) return;
    if (form.dataset.confirm && !confirm(form.dataset.confirm)) return;
    const fields = new FormData(form);
    const run = {base: (form.dataset.geminiAdmin === 'true' ? '/admin/workers/' : '/user/workers/') + form.dataset.workerId + '/gemini-login/', csrf: fields.get('csrf_token')};
    current = run; hideControls(); code.value = '';
    document.querySelector('#gemini-code-form button').disabled = false;
    title.textContent = '登录 Gemini 订阅账号'; status.textContent = '正在启动登录…'; dialog.showModal();
    showProgress('正在启动登录服务…');
    try {
      if (fields.get('force') === 'true') {
        status.textContent = '正在退出当前账号…';
        showProgress('正在安全退出原账号…');
        await call(run, 'logout'); run.reload = true;
        if (run.closed) { location.reload(); return; }
        status.textContent = '原账号已退出，正在准备重新登录…';
      }
      const data = await call(run, 'start'); run.session = data.session_id;
      if (run.closed) { await call(run, 'input', {key: 'cancel'}); return; }
      render(run, data); poll(run);
    } catch (error) { if (visible(run)) { status.textContent = error.message; run.done = true; hideControls(); } }
  }));
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
