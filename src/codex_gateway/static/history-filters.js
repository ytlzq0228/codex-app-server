(() => {
  const form = document.querySelector('[data-history-filters]');
  if (!form) return;
  const select = form.querySelector('[data-key-select]');
  const picker = document.createElement('details');
  picker.className = 'history-key-picker';
  const summary = document.createElement('summary');
  const panel = document.createElement('div');
  panel.className = 'history-key-menu';
  const search = document.createElement('input');
  search.type = 'search';
  search.placeholder = '搜索 Key 名称、ID 或归属用户';
  search.setAttribute('aria-label', '搜索 Key');
  search.autocomplete = 'off';
  const choices = document.createElement('div');
  choices.className = 'history-key-choices';
  const empty = document.createElement('p');
  empty.textContent = '没有匹配的 Key';
  empty.setAttribute('role', 'status');
  empty.hidden = true;
  const buttons = Array.from(select.options, option => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = option.textContent;
    button.dataset.value = option.value;
    button.addEventListener('click', () => {
      select.value = option.value;
      update();
      picker.open = false;
      summary.focus();
    });
    choices.append(button);
    return button;
  });
  function update() {
    summary.textContent = select.selectedOptions[0]?.textContent || '全部 Key';
    summary.title = summary.textContent;
    buttons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.value === select.value)));
  }
  search.addEventListener('input', () => {
    const query = search.value.trim().toLocaleLowerCase();
    buttons.forEach(button => { button.hidden = !button.textContent.toLocaleLowerCase().includes(query); });
    empty.hidden = buttons.some(button => !button.hidden);
  });
  search.addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); buttons.find(button => !button.hidden)?.click(); }
    if (event.key === 'ArrowDown') { event.preventDefault(); buttons.find(button => !button.hidden)?.focus(); }
  });
  picker.addEventListener('keydown', event => {
    if (event.key === 'Escape') { picker.open = false; summary.focus(); }
  });
  picker.addEventListener('toggle', () => { if (picker.open) search.focus(); });
  document.addEventListener('click', event => { if (!picker.contains(event.target)) picker.open = false; });
  panel.append(search, choices, empty);
  picker.append(summary, panel);
  select.after(picker);
  select.hidden = true;
  form.querySelector('label[for="history-key"]').addEventListener('click', event => {
    event.preventDefault(); summary.focus(); picker.open = true;
  });
  update();

  const bounds = Array.from(form.querySelectorAll('[data-time-bound]'));
  const pad = n => String(n).padStart(2, '0');
  bounds.forEach(input => {
    if (!input.dataset.instant) return;
    const date = new Date(input.dataset.instant);
    if (!Number.isFinite(date.getTime())) return;
    input.value = date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate()) + 'T' + pad(date.getHours()) + ':' + pad(date.getMinutes()) + ':' + pad(date.getSeconds());
  });
  bounds.forEach(input => input.addEventListener('input', () => bounds.forEach(field => field.setCustomValidity(''))));
  form.addEventListener('submit', event => {
    const [start, end] = bounds;
    if (start.value && end.value && new Date(start.value) >= new Date(end.value)) {
      event.preventDefault();
      end.setCustomValidity('结束时间必须晚于开始时间');
      end.reportValidity();
    }
  });
  form.addEventListener('formdata', event => {
    bounds.forEach(input => {
      if (input.value) event.formData.set(input.dataset.timeBound, new Date(input.value).toISOString());
    });
  });
  form.querySelector('[data-browser-timezone]').textContent = Intl.DateTimeFormat().resolvedOptions().timeZone;
})();
