(async () => {
  await globalThis.I18n.ready;
(() => {
  if (!document.querySelector('#monitoring')) return;
  const colors = ['#22a06b', '#e5a000', '#e35b5b', '#94a3b8', '#8462cf'];
  const $ = id => document.getElementById(id);
  const el = (tag, text, className) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (className) n.className = className; return n; };
  const stamp = at => new Date(at).toLocaleString(globalThis.I18n.locale);
  const svgNode = (tag, attrs, text) => { const n = document.createElementNS('http://www.w3.org/2000/svg', tag); Object.entries(attrs).forEach(([k,v]) => n.setAttribute(k,v)); if (text != null) n.textContent = text; return n; };
  function legend(series) {
    const n = el('div', null, 'monitor-legend');
    series.forEach(s => { const item = el('span'); const dot = el('i', null, 'monitor-dot'); dot.style.background = s.color; item.append(dot, document.createTextNode(s.label)); n.append(item); }); return n;
  }
  function chart(target, rows, series, interval, percent, days) {
    target.replaceChildren();
    if (!rows.length) { target.append(el('p', globalThis.I18n.t("暂无历史采样"), 'monitor-empty')); return; }
    target.append(legend(series));
    const width = Math.max(1, target.clientWidth), height = target.id === 'monitor-state-chart' ? 220 : 260;
    const left = 48, right = width - 16, bottom = height - 36;
    const svg = svgNode('svg', {width, height, viewBox:`0 0 ${width} ${height}`, role:'img', 'aria-label': percent ? globalThis.I18n.t("订阅池用量历史折线图") : globalThis.I18n.t("Worker 状态历史折线图")});
    const end = Date.now(), start = end - days * 86400000;
    const rawMax = rows.reduce((max, r) => Math.max(max, r.data.total || 0), 1);
    const step = percent ? 25 : Math.max(1, Math.ceil(rawMax/4));
    const max = step*4;
    const x = at => left + (new Date(at).getTime() - start) / (end-start) * (right-left);
    const y = value => bottom - value/max*(bottom-16);
    for (let i=0;i<=4;i++) { const value = max*i/4; svg.append(svgNode('line',{x1:left,x2:right,y1:y(value),y2:y(value)}),svgNode('text',{x:left-8,y:y(value)+4,'text-anchor':'end'},`${Number(value.toFixed(1))}${percent?'%':''}`)); }
    const ticks = width < 600 ? 2 : 4;
    for (let i=0;i<=ticks;i++) svg.append(svgNode('text',{x:left+i*(right-left)/ticks,y:height-8,'text-anchor':i===0?'start':i===ticks?'end':'middle'},new Date(start+(end-start)*i/ticks).toLocaleString(globalThis.I18n.locale, {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})));
    series.forEach(s => {
      let points = [], last = null;
      const flush = () => { if(points.length) svg.append(svgNode('polyline',{points:points.join(' '),fill:'none',stroke:s.color,'stroke-width':2})); points=[]; };
      rows.forEach(row => {
        const value = s.value(row.data), time = new Date(row.at).getTime();
        if (value == null || (last != null && time-last > interval*1.5)) flush();
        last=time;
        if(value == null) return;
        points.push(`${x(row.at)},${y(value)}`);
      }); flush();
    });
    const hover = svgNode('g', {'pointer-events':'none', visibility:'hidden'});
    const markers = series.map(s => { const dot = svgNode('circle', {r:4, fill:s.color}); hover.append(dot); return dot; });
    svg.append(hover);
    const tooltip = el('small', '', 'monitor-tooltip');
    svg.addEventListener('pointermove', event => {
      const at = start + (event.offsetX-left)/(right-left)*(end-start);
      const row = rows.reduce((best, row) => Math.abs(new Date(row.at)-at) < Math.abs(new Date(best.at)-at) ? row : best);
      hover.setAttribute('visibility', 'visible');
      const details = series.map((s, i) => {
        const value = s.value(row.data);
        markers[i].setAttribute('visibility', value == null ? 'hidden' : 'visible');
        if (value == null) return '';
        markers[i].setAttribute('cx', x(row.at)); markers[i].setAttribute('cy', y(value));
        const range = s.range?.(row.data);
        return s.label + ': ' + value.toFixed(1) + (percent ? '%' : '') +
          (range ? ' ' + globalThis.I18n.t(percent ? '范围' : '末值 / 峰值') + ' [' + range.map(v => v.toFixed(1)).join('–') + ']' : '');
      });
      tooltip.textContent = stamp(row.at) + ' · ' + details.filter(Boolean).join(' · ');
    });
    svg.addEventListener('pointerleave', () => { hover.setAttribute('visibility', 'hidden'); tooltip.textContent=''; });
    target.append(svg, tooltip);
    if (!rows.some(row => series.some(s => s.value(row.data) != null))) target.append(el('p', globalThis.I18n.t("此范围已有采样，但尚无有效额度数据"), 'monitor-empty'));
  }
  function render(data, days) {
    $('monitor-worker-total').textContent = data.states.total;
    const series = Object.entries(data.labels).map(([key,label],i) => ({key,label,color:colors[i],value:d=>d.counts[key],range:d=>[d.counts[key],d.max_counts?.[key] ?? d.counts[key]]}));
    const pie = el('div', null, 'monitor-pie'); let angle=0; const stops=[];
    const list=el('ul',null,'monitor-state-list');
    series.forEach(s=>{const count=data.states.counts[s.key], share=data.states.total?count/data.states.total*100:0; stops.push(`${s.color} ${angle}% ${angle+share}%`);angle+=share; const li=el('li'); const dot=el('i',null,'monitor-dot');dot.style.background=s.color;li.append(dot,el('span',s.label),el('strong',`${count} · ${share.toFixed(1)}%`));list.append(li);});
    pie.style.background=data.states.total?`conic-gradient(${stops.join(',')})`:'#e2e8f0';pie.setAttribute('role','img');pie.setAttribute('aria-label',globalThis.I18n.t("Worker 状态分布，共 {v0} 个，详见图例", undefined, {v0:data.states.total}));
    $('monitor-pie').replaceChildren(pie,list);
    const current = data.current_usage;
    for (const provider of ['codex', 'gemini', 'claude']) {
      const target = $('monitor-usage-' + provider), pool = current?.data.providers?.[provider];
      const usageSeries = [['risk',globalThis.I18n.t("综合风险")],['five_hour',globalThis.I18n.t("5 小时窗口")],['week',globalThis.I18n.t("周窗口")]].map(([key,label],i) => ({key,label,color:['#e35b5b','#168aad','#8462cf'][i],value:d=>d.providers?.[provider]?.windows?.[key]?.used ?? null,range:d=>{const w=d.providers?.[provider]?.windows?.[key];return w?.min != null ? [w.min,w.max] : null;}}));
      target.replaceChildren();
      if (pool?.windows) {
        usageSeries.forEach(s => {
          const w = pool.windows[s.key], article = el('article'), bar = el('div', null, 'monitor-progress');
          bar.setAttribute('role', 'progressbar'); bar.setAttribute('aria-label', provider + ' ' + s.label);
          bar.setAttribute('aria-valuemin', '0'); bar.setAttribute('aria-valuemax', '100');
          if (w.used != null) {
            bar.setAttribute('aria-valuenow', String(w.used));
            const fill = el('div', null, 'monitor-progress-fill'); fill.style.width = `${Math.max(0, Math.min(100, w.used))}%`; fill.style.background = s.color; bar.append(fill);
          } else { bar.classList.add('monitor-progress-unknown'); bar.setAttribute('aria-valuetext', globalThis.I18n.t("暂无数据")); }
          article.append(el('span', s.label), el('strong', w.used == null ? globalThis.I18n.t("暂无数据") : `${w.used.toFixed(1)}%`), bar,
            el('small', w.used == null ? globalThis.I18n.t("可用用量未知") : globalThis.I18n.t("剩余可用 {v0}%", undefined, {v0:(100-w.used).toFixed(1)})),
            el('small', globalThis.I18n.t("有效覆盖 {v0} / {v1} 个 Worker · 权重覆盖 {v2}%", undefined, {v0:w.covered,v1:pool.eligible,v2:pool.total_weight ? (w.covered_weight/pool.total_weight*100).toFixed(1) : '0'})));
          target.append(article);
        });
        const stale = Date.now()-new Date(current.observed_at).getTime() > 2*3600000;
        target.append(el('small', globalThis.I18n.t("{v0}采样：{v1} · 未知套餐 {v2} 个 Worker", undefined, {v0:stale ? globalThis.I18n.t("数据已过期 · ") : '',v1:stamp(current.observed_at),v2:pool.unknown_plans})));
      } else target.append(el('p', globalThis.I18n.t("等待首次按 Provider 采样")));
      if (data.history) chart($('monitor-chart-' + provider), data.history.subscription_usage, usageSeries, data.intervals.subscription_usage, true, days);
    }
    if (data.history) chart($('monitor-state-chart'),data.history.worker_states,series,data.intervals.worker_states,false,days);
  }
  let generation=0, latest=null, latestDays=7, cursor=null, controller=null, timer;
  function redraw() { if (latest?.states) render(latest, latestDays); }
  async function load(reset=false) {
    clearTimeout(timer);
    controller?.abort();
    if (document.hidden) return;
    const version=++generation, days=Number($('monitor-days').value);
    controller=new AbortController();
    const signal=controller.signal;
    if (reset || days!==latestDays) {
      latest=null; cursor=null; latestDays=days;
      for (const id of ['monitor-state-chart', ...['codex','gemini','claude'].map(p=>'monitor-chart-'+p)]) $(id).replaceChildren();
    }
    $('monitor-status').textContent=globalThis.I18n.t("正在读取监控数据…");
    const request = async view => {
      const query = new URLSearchParams({days,view});
      if (view==='history' && cursor) query.set('since',cursor);
      const response=await fetch('/admin/monitoring?'+query,{headers:{Accept:'application/json'},signal:AbortSignal.any([signal,AbortSignal.timeout(45000)])});
      if (!response.ok) throw Error();
      const data=await response.json();
      if (version!==generation || signal.aborted) return;
      latest ||= {};
      if (view==='summary') Object.assign(latest,data);
      else {
        const merged={};
        for (const metric of ['worker_states','subscription_usage']) {
          const interval=data.intervals[metric];
          const cutoff=Math.floor((new Date(data.server_time).getTime()-days*86400000)/interval)*interval;
          merged[metric]=[...(latest.history?.[metric] || []).filter(r =>
            new Date(r.at).getTime()>=cutoff && r.at<data.replace_from[metric]),...data.history[metric]];
        }
        Object.assign(latest,{history:merged,intervals:data.intervals});
        cursor=data.server_time;
      }
      redraw();
    };
    const results=await Promise.allSettled([request('summary'),request('history')]);
    if (version!==generation || signal.aborted) return;
    $('monitor-status').textContent=results.some(r=>r.status==='rejected') ?
      globalThis.I18n.t("监控读取失败，将自动重试") :
      globalThis.I18n.t("状态更新于 {v0}", undefined, {v0:new Date().toLocaleTimeString(globalThis.I18n.locale)});
    timer=setTimeout(()=>load(),60000);
  }
  let lastWidth = 0, resizeFrame;
  new ResizeObserver(entries => { const width = entries[0].contentRect.width; if (Math.abs(width-lastWidth)<1) return; lastWidth=width; cancelAnimationFrame(resizeFrame); resizeFrame=requestAnimationFrame(() => { redraw(); }); }).observe($('monitoring'));
  $('monitor-days').addEventListener('change',()=>load(true));
  document.addEventListener('visibilitychange',()=>{
    if (document.hidden) { clearTimeout(timer); controller?.abort(); }
    else load();
  });
  load();
})();

})();
