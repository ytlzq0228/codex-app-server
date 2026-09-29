(() => {
  document.querySelectorAll('dialog[data-password-dialog]').forEach(dialog => dialog.addEventListener('close', () => {
    dialog.querySelector('form').reset();
    const error = dialog.querySelector('[data-form-error]');
    error.textContent = '';
    error.hidden = true;
  }));
  document.querySelectorAll('[data-modal-open]').forEach(button => button.addEventListener('click', () => {
    document.getElementById(button.dataset.modalOpen).showModal();
  }));
  document.querySelectorAll('[data-modal-close]').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
  const dialog = document.getElementById('request-detail-dialog');
  const content = document.getElementById('request-detail-content');
  let controller;
  dialog.addEventListener('close', () => { controller?.abort(); content.replaceChildren(); });
  document.querySelectorAll('[data-request-detail]').forEach(link => link.addEventListener('click', async event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    controller?.abort();
    const active = new AbortController();
    controller = active;
    content.textContent = '正在加载请求详情…';
    dialog.showModal();
    try {
      const response = await fetch(link.href, {headers:{'X-Requested-With':'XMLHttpRequest'}, signal:active.signal});
      if (!response.ok) throw new Error(response.status === 401 ? '登录已过期，请重新登录。' : response.status === 404 ? '请求不存在或无权查看。' : '加载失败，请关闭后重试。');
      const documentBody = new DOMParser().parseFromString(await response.text(), 'text/html');
      const panel = documentBody.querySelector('main.content > section.panel');
      if (!panel) throw new Error('无法读取请求详情，请重新登录后重试。');
      if (!active.signal.aborted) content.replaceChildren(document.importNode(panel, true));
    } catch (error) {
      if (!active.signal.aborted) content.textContent = error.message;
    }
  }));
})();
