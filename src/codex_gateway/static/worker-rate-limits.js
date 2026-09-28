(() => {
  const queue = [];
  let running = 0;
  const pump = () => {
    while (running < 3 && queue.length) {
      running++;
      queue.shift()().finally(() => { running--; pump(); });
    }
  };
  const usageClass = percent => percent >= 90 ? 'usage-danger' : (percent >= 60 ? 'usage-warn' : 'usage-ok');
  const resetTime = seconds => new Date(seconds * 1000).toLocaleString('zh-CN', {
    year: 'numeric', month: 'numeric', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
  const renderWindow = (label, window) => {
    const line = document.createElement('div');
    line.className = 'usage-window';
    const meter = document.createElement('div');
    meter.className = 'usage-meter';
    const progress = document.createElement('progress');
    progress.max = 100;
    progress.value = window.used;
    progress.className = usageClass(progress.value);
    progress.setAttribute('aria-label', `${label}已用额度`);
    const caption = document.createElement('span');
    caption.className = 'usage-caption';
    caption.textContent = `${label}：已用 ${window.used}%`;
    meter.append(progress, caption);
    const reset = document.createElement('span');
    reset.className = 'usage-reset';
    reset.textContent = `重置：${window.resets_at != null ? resetTime(window.resets_at) : '—'}`;
    line.append(meter, reset);
    return line;
  };
  document.querySelectorAll('[data-rate-limits]').forEach(box => {
    if (box.dataset.loggedIn !== 'true') return;
    const content = box.querySelector('[data-rate-content]');
    queue.push(async () => {
      try {
        const body = new FormData();
        body.set('csrf_token', box.querySelector('[name=csrf_token]').value);
        const response = await fetch(box.dataset.rateLimits, {method: 'POST', body,
          headers: {'X-Requested-With': 'XMLHttpRequest'}});
        const data = await response.json();
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '读取失败');
        if (data.message) { content.textContent = data.message; return; }
        const bucket = data.buckets.find(item => item.five_hour || item.week);
        const windows = [];
        if (bucket?.five_hour) windows.push(renderWindow('5 小时', bucket.five_hour));
        if (bucket?.week) windows.push(renderWindow('周窗口', bucket.week));
        content.replaceChildren(...windows);
      } catch (error) {
        content.textContent = error.message || '额度读取失败';
      }
    });
    pump();
  });
})();
