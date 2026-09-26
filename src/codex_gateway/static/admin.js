const resultDialog = document.querySelector('#result-dialog');
let loginPollTimer = null;
let loginPollAttempts = 0;

const stopLoginPoll = () => {
  if (loginPollTimer) clearTimeout(loginPollTimer);
  loginPollTimer = null;
  loginPollAttempts = 0;
};

const requestJson = async (url, body) => {
  const response = await fetch(url, {
    method: 'POST',
    body,
    headers: {'X-Requested-With': 'XMLHttpRequest'},
  });
  if (response.status === 401) {
    location.href = '/user/login?next=/admin';
    throw new Error('登录已过期');
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || data.detail || '操作失败');
  return data;
};

const pollLogin = async (url) => {
  if (!resultDialog.open || !url) return;
  loginPollAttempts += 1;
  const status = document.querySelector('#login-poll-status');
  try {
    const body = new FormData();
    body.set('csrf_token', document.body.dataset.csrfToken);
    const data = await requestJson(url, body);
    if (data.logged_in) {
      stopLoginPoll();
      document.querySelector('#result-title').textContent = '登录成功';
      document.querySelector('#result-message').textContent = data.message;
      document.querySelector('#result-login').hidden = true;
      resultDialog.dataset.reload = '1';
      return;
    }
    status.innerHTML = '<span></span>尚未检测到登录，继续等待…';
  } catch (error) {
    status.innerHTML = `<span class="poll-error"></span>${error.message}，稍后重试…`;
  }
  if (loginPollAttempts < 100) loginPollTimer = setTimeout(() => pollLogin(url), 3000);
  else status.textContent = '等待超时，请完成登录后手动探测 Worker。';
};

const showResult = (data, reload = false) => {
  stopLoginPoll();
  document.querySelector('#result-title').textContent = data.title || '操作完成';
  document.querySelector('#result-message').textContent = data.message || '';
  const secret = document.querySelector('#result-secret');
  const login = document.querySelector('#result-login');
  secret.querySelector('code').textContent = '';
  login.querySelector('a').removeAttribute('href');
  login.querySelector('.device-code').textContent = '';
  document.querySelector('[data-copy-secret]').textContent = '复制';
  document.querySelector('#login-poll-status').innerHTML = '<span></span>等待登录完成…';
  secret.hidden = true;
  login.hidden = true;
  if (data.secret) {
    secret.querySelector('code').textContent = data.secret;
    secret.hidden = false;
  }
  if (data.login_url) {
    login.querySelector('a').href = data.login_url;
    login.querySelector('.device-code').textContent = data.user_code || '—';
    login.hidden = false;
  }
  resultDialog.dataset.reload = reload ? '1' : '';
  resultDialog.showModal();
  if (data.poll_url) pollLogin(data.poll_url);
};

document.querySelectorAll('[data-open-dialog]').forEach(button =>
  button.addEventListener('click', () => document.getElementById(button.dataset.openDialog).showModal()));
document.querySelectorAll('[data-close-dialog]').forEach(button =>
  button.addEventListener('click', () => button.closest('dialog').close()));
document.querySelectorAll('[data-edit-key]').forEach(button => button.addEventListener('click', () => {
  const dialog = document.querySelector('#edit-key-dialog');
  const form = dialog.querySelector('form');
  form.action = `/admin/keys/${button.dataset.keyId}/edit`;
  form.elements.name.value = button.dataset.keyName;
  form.elements.scheduling_mode.value = button.dataset.schedulingMode;
  form.elements.pinned_worker_id.value = button.dataset.pinnedWorkerId;
  dialog.showModal();
}));
document.querySelectorAll('[data-toggle-history]').forEach(button => button.addEventListener('click', () => {
  const detail = document.getElementById(button.dataset.toggleHistory);
  detail.hidden = !detail.hidden;
  button.textContent = detail.hidden ? '详情' : '收起';
}));
document.querySelectorAll('tr[data-history-row]').forEach(row => row.addEventListener('click', event => {
  if (event.target.closest('button, a, form')) return;
  row.querySelector('[data-toggle-history]')?.click();
}));
document.querySelectorAll('[data-close-result]').forEach(button => button.addEventListener('click', () => {
  stopLoginPoll();
  resultDialog.close();
  if (resultDialog.dataset.reload === '1') location.reload();
}));
document.querySelector('[data-copy-secret]')?.addEventListener('click', async () => {
  await navigator.clipboard.writeText(document.querySelector('#result-secret code').textContent);
  document.querySelector('[data-copy-secret]').textContent = '已复制';
});

document.querySelectorAll('form[data-ajax]').forEach(form => form.addEventListener('submit', async event => {
  event.preventDefault();
  if (form.dataset.confirm && !confirm(form.dataset.confirm)) return;
  const button = form.querySelector('button[type=submit],button:not([type])');
  if (button) {
    button.disabled = true;
    button.dataset.label = button.textContent;
    button.textContent = '处理中…';
  }
  try {
    const data = await requestJson(form.action, new FormData(form));
    form.closest('dialog')?.close();
    showResult(data, form.hasAttribute('data-reload'));
  } catch (error) {
    showResult({title: '操作失败', message: error.message});
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = button.dataset.label;
    }
  }
}));
