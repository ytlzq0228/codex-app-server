document.querySelectorAll('[data-toggle-history]').forEach(button => {
  button.setAttribute('aria-expanded', 'false');
  button.setAttribute('aria-controls', button.dataset.toggleHistory);
  button.addEventListener('click', () => {
    const detail = document.getElementById(button.dataset.toggleHistory);
    detail.hidden = !detail.hidden;
    button.textContent = detail.hidden ? '展开' : '收起';
    button.setAttribute('aria-expanded', String(!detail.hidden));
  });
});
document.querySelectorAll('tr[data-history-row]').forEach(row => row.addEventListener('click', event => {
  if (event.target.closest('button, a, form')) return;
  row.querySelector('[data-toggle-history]')?.click();
}));
