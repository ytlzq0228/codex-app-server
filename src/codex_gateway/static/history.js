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

document.querySelectorAll('[data-history-more]').forEach(button => {
  const detail = button.closest('.history-conversation-detail');
  const batchSize = Number(button.dataset.batchSize) || 20;
  const hiddenRows = () => Array.from(detail.querySelectorAll('[data-history-request][hidden]'));
  const update = () => {
    const remaining = hiddenRows().length;
    button.hidden = remaining === 0;
    button.textContent = remaining ? `更多（剩余 ${remaining} 条）` : '已显示全部';
  };
  button.addEventListener('click', () => {
    hiddenRows().slice(0, batchSize).forEach(row => { row.hidden = false; });
    update();
  });
  update();
});
