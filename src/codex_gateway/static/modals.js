(async () => {
  await globalThis.I18n.ready;
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
  document.addEventListener('click', async event => {
    const link = event.target.closest('[data-request-detail]');
    if (!link) return;
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    controller?.abort();
    const active = new AbortController();
    controller = active;
    content.textContent = globalThis.I18n.t("正在加载请求详情…");
    dialog.showModal();
    try {
      const panel = await window.PageRenderer.detail(link.href, active.signal);
      if (!active.signal.aborted) content.replaceChildren(document.importNode(panel, true));
    } catch (error) {
      if (!active.signal.aborted) content.textContent = error.message;
    }
  });
})();

})();
