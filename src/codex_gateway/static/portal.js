const dialog = document.getElementById('portal-result');
document.querySelectorAll('form[data-portal]').forEach(form => form.addEventListener('submit', async event => {
  event.preventDefault();
  if (form.dataset.confirm && !confirm(form.dataset.confirm)) return;
  const button = form.querySelector('button[type=submit],button:not([type])');
  const errorBox = form.querySelector('[data-form-error]');
  if (errorBox) errorBox.hidden = true;
  button.disabled = true;
  try {
    const response = await fetch(form.action, {method:'POST', body:new FormData(form), headers:{'X-Requested-With':'XMLHttpRequest'}});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data));
    document.getElementById('portal-message').textContent = data.message || '已保存';
    document.getElementById('portal-secret').textContent = data.secret || '';
    form.closest('dialog')?.close();
    dialog.showModal();
  } catch(error) {
    if (errorBox) { errorBox.textContent = error.message; errorBox.hidden = false; }
    else { document.getElementById('portal-message').textContent = error.message; document.getElementById('portal-secret').textContent = ''; dialog.showModal(); }
  }
  finally { button.disabled = false; }
}));
document.getElementById('portal-close').onclick = () => location.reload();
let controller;
const debug = document.getElementById('debug-form');
if (debug) {
  document.getElementById('debug-stop').onclick = () => controller?.abort();
  debug.onsubmit = async event => {
    event.preventDefault(); controller?.abort(); controller = new AbortController();
    const output = document.getElementById('debug-output'), status = document.getElementById('debug-status');
    output.textContent = ''; status.textContent = '请求中…';
    const started = performance.now();
    try {
      const fields = new FormData(debug), endpoint = fields.get('endpoint');
      const response = await fetch(endpoint, {method:endpoint === '/v1/models' ? 'GET':'POST', headers:{Authorization:'Bearer '+fields.get('key'), 'Content-Type':'application/json'}, body:endpoint === '/v1/models' ? undefined:JSON.stringify(JSON.parse(fields.get('body'))), signal:controller.signal});
      status.textContent = 'HTTP '+response.status;
      const reader = response.body.getReader(), decoder = new TextDecoder();
      while (true) { const {done,value} = await reader.read(); if(done) break; output.textContent += decoder.decode(value,{stream:true}); }
      output.textContent += decoder.decode();
      status.textContent += ' · '+Math.round(performance.now()-started)+' ms';
    } catch(error) { status.textContent = error.message; }
  };
}

// Save each price independently without discarding edits in other rows.
document.querySelectorAll('[data-price-form]').forEach(form => {
  const row = form.closest('tr');
  const message = form.querySelector('[data-price-message]');
  row.querySelectorAll('input:not([type=hidden])').forEach(input => input.addEventListener('input', () => {
    message.textContent = '未保存';
    message.className = '';
  }));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    const submitted = new FormData(form);
    button.disabled = true;
    message.textContent = '保存中…';
    message.className = '';
    try {
      const response = await fetch(form.action, {method:'POST',body:submitted,headers:{'X-Requested-With':'XMLHttpRequest'}});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error?.message || (typeof data.detail === 'string' ? data.detail : '保存失败，请检查价格'));
      const status = row.querySelector('[data-price-status]');
      status.textContent = '已定价'; status.className = 'badge badge-ok';
      const current = new FormData(form);
      message.textContent = ['model','input_price','output_price'].every(key => current.get(key) === submitted.get(key)) ? '已保存' : '有新修改未保存';
    } catch (error) { message.textContent = error.message; message.className = 'price-error'; }
    finally { button.disabled = false; }
  });
});

const workerDialog = document.getElementById('worker-login-dialog');
if (workerDialog) {
  let timer, activeController;
  workerDialog.addEventListener('close', () => { clearTimeout(timer); activeController?.abort(); if (workerDialog.dataset.reload === '1') location.reload(); });
  document.querySelectorAll('[data-worker-login], [data-worker-account]').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault(); clearTimeout(timer); activeController?.abort();
    activeController = new AbortController();
    const signal = activeController.signal;
    const message = document.getElementById('worker-login-message');
    const link = document.getElementById('worker-login-url');
    const code = document.getElementById('worker-login-code');
    const title = document.getElementById('worker-login-title');
    const loginContent = document.getElementById('worker-login-content');
    const accountInfo = document.getElementById('worker-account-info');
    const pollStatus = document.getElementById('worker-login-poll');
    const pollText = document.getElementById('worker-login-poll-text');
    title.textContent = form.hasAttribute('data-worker-login') ? '登录 Worker' : 'Worker 账号';
    workerDialog.dataset.reload = '';
    message.textContent = '正在读取 Worker…'; loginContent.hidden = true; link.removeAttribute('href'); code.textContent = '';
    accountInfo.hidden = true; accountInfo.textContent = '';
    pollStatus.hidden = false; pollStatus.classList.remove('has-error'); pollText.textContent = '等待登录完成…';
    workerDialog.showModal();
    const call = async url => {
      const response = await fetch(url, {method:'POST',body:new FormData(form),headers:{'X-Requested-With':'XMLHttpRequest'},signal});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error?.message || data.detail || 'Worker 操作失败');
      return data;
    };
    try {
      const data = await call(form.action);
      message.textContent = data.message;
      if (data.account) { accountInfo.hidden = false; workerDialog.dataset.reload = '1'; accountInfo.textContent = `账号：${data.account.email || '未登录'}\n类型：${data.account.type || '—'}\n套餐：${data.account.plan || '—'}`; }
      if (data.login_url) { link.href=data.login_url; loginContent.hidden=false; code.textContent=data.user_code || '—'; pollStatus.hidden = !data.poll_url; }
      if (data.poll_url) {
        let attempts=0;
        const poll = async () => {
          if (!workerDialog.open || signal.aborted) return;
          try {
            const status = await call(data.poll_url);
            if (status.logged_in) {
              title.textContent = '登录成功'; message.textContent = status.message;
              loginContent.hidden = true; workerDialog.dataset.reload = '1'; return;
            }
            pollStatus.classList.remove('has-error'); pollText.textContent = '尚未检测到登录，继续等待…';
          } catch(error) { if (!signal.aborted) { pollStatus.classList.add('has-error'); pollText.textContent = error.message + '，稍后重试…'; } }
          if (++attempts<60 && !signal.aborted) timer=setTimeout(poll,5000);
          else if (!signal.aborted) { pollStatus.classList.add('has-error'); pollText.textContent = '等待超时，请完成登录后手动探测 Worker。'; }
        };
        timer=setTimeout(poll,5000);
      }
    } catch(error) { if (!signal.aborted) message.textContent=error.message; }
  }));
}
