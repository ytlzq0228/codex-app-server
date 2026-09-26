(() => {
  const queue = [];
  let running = 0;
  const pump = () => {
    while (running < 3 && queue.length) {
      running++;
      queue.shift()().finally(() => { running--; pump(); });
    }
  };
  document.querySelectorAll('[data-rate-limits]').forEach(box => {
    const button = box.querySelector('[data-rate-refresh]');
    const status = box.querySelector('[data-rate-status]');
    const content = box.querySelector('[data-rate-content]');
    const refresh = () => {
      if (button.disabled) return;
      button.disabled = true;
      status.textContent = '读取中…';
      queue.push(async () => {
        try {
          const body = new FormData();
          body.set('csrf_token', box.querySelector('[name=csrf_token]').value);
          const response = await fetch(box.dataset.rateLimits, {method:'POST', body, headers:{'X-Requested-With':'XMLHttpRequest'}});
          const data = await response.json();
          if (!response.ok) throw new Error(data.error?.message || data.detail || '读取失败');
          content.replaceChildren();
          for (const bucket of data.buckets) {
            const heading = document.createElement('strong'); heading.textContent = bucket.name; content.append(heading);
            for (const [key,label] of [['five_hour','5 小时'],['week','周窗口']]) {
              const window = bucket[key];
              const line = document.createElement('div');
              line.textContent = window ? `${label}：剩余 ${window.remaining}%` : `${label}：暂无数据`;
              if (window) {
                const bar = document.createElement('progress'); bar.max=100; bar.value=window.remaining; bar.setAttribute('aria-label', `${label}剩余额度`); line.append(bar);
                if (window.resets_at != null) { const reset=document.createElement('small'); reset.textContent='重置：'+new Date(window.resets_at*1000).toLocaleString(); line.append(reset); }
              }
              content.append(line);
            }
          }
          status.textContent='更新于 '+new Date(data.checked_at).toLocaleTimeString();
        } catch (error) {
          content.textContent='5 小时 / 周窗口：暂无数据'; status.textContent=error.message;
        } finally { button.disabled=false; }
      });
      pump();
    };
    button.addEventListener('click', refresh);
    if (box.dataset.loggedIn === 'true') refresh();
    else status.textContent='未登录或未确认登录';
  });
})();
