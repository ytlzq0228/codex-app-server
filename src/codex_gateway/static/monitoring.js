(() => {
  if (!document.querySelector('#monitoring')) return;
  const colors = ['#22a06b', '#e5a000', '#e35b5b', '#94a3b8', '#8462cf'];
  const $ = id => document.getElementById(id);
  const el = (tag, text, className) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (className) n.className = className; return n; };
  const stamp = at => new Date(at).toLocaleString();
  const svgNode = (tag, attrs, text) => { const n = document.createElementNS('http://www.w3.org/2000/svg', tag); Object.entries(attrs).forEach(([k,v]) => n.setAttribute(k,v)); if (text != null) n.textContent = text; return n; };
  function legend(series) {
    const n = el('div', null, 'monitor-legend');
    series.forEach(s => { const item = el('span'); const dot = el('i', null, 'monitor-dot'); dot.style.background = s.color; item.append(dot, document.createTextNode(s.label)); n.append(item); }); return n;
  }
  function chart(target, rows, series, interval, percent, days) {
    target.replaceChildren();
    if (!rows.length) { target.append(el('p', '暂无历史采样', 'monitor-empty')); return; }
    target.append(legend(series));
    const svg = svgNode('svg', {viewBox:'0 0 960 290', role:'img', 'aria-label': percent ? '订阅池用量历史折线图' : 'Worker 状态历史折线图'});
    const end = Date.now(), start = end - days * 86400000;
    const rawMax = Math.max(1, ...rows.map(r => r.data.total || 0));
    const step = percent ? 25 : Math.max(1, Math.ceil(rawMax/4));
    const max = step*4;
    const x = at => 55 + (new Date(at).getTime() - start) / (end-start) * 875;
    const y = value => 240 - value/max*210;
    for (let i=0;i<=4;i++) { const value = max*i/4; svg.append(svgNode('line',{x1:55,x2:930,y1:y(value),y2:y(value)}),svgNode('text',{x:45,y:y(value)+4,'text-anchor':'end'},`${Number(value.toFixed(1))}${percent?'%':''}`)); }
    for (let i=0;i<=4;i++) svg.append(svgNode('text',{x:55+i*875/4,y:275,'text-anchor':i===0?'start':i===4?'end':'middle'},new Date(start+(end-start)*i/4).toLocaleString([], {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})));
    series.forEach(s => {
      let points = [], last = null;
      const flush = () => { if(points.length) svg.append(svgNode('polyline',{points:points.join(' '),fill:'none',stroke:s.color,'stroke-width':2})); points=[]; };
      rows.forEach(row => {
        const value = s.value(row.data), time = new Date(row.at).getTime();
        if (value == null || (last != null && time-last > interval*1.5)) flush();
        last=time;
        if(value == null) return;
        points.push(`${x(row.at)},${y(value)}`);
        const dot=svgNode('circle',{cx:x(row.at),cy:y(value),r:2.5,fill:s.color});
        dot.append(svgNode('title',{},`${stamp(row.observed_at)} · ${s.label}：${value.toFixed(1)}${percent?'%':' 个'}`));svg.append(dot);
      }); flush();
    });
    target.append(svg);
    if (!rows.some(row => series.some(s => s.value(row.data) != null))) target.append(el('p', '此范围已有采样，但尚无有效额度数据', 'monitor-empty'));
  }
  function render(data, days) {
    $('monitor-worker-total').textContent = data.states.total;
    const series = Object.entries(data.labels).map(([key,label],i) => ({key,label,color:colors[i],value:d=>d.counts[key]}));
    const pie = el('div', null, 'monitor-pie'); let angle=0; const stops=[];
    const list=el('ul',null,'monitor-state-list');
    series.forEach(s=>{const count=data.states.counts[s.key], share=data.states.total?count/data.states.total*100:0; stops.push(`${s.color} ${angle}% ${angle+share}%`);angle+=share; const li=el('li'); const dot=el('i',null,'monitor-dot');dot.style.background=s.color;li.append(dot,el('span',s.label),el('strong',`${count} · ${share.toFixed(1)}%`));list.append(li);});
    pie.style.background=data.states.total?`conic-gradient(${stops.join(',')})`:'#e2e8f0';pie.setAttribute('role','img');pie.setAttribute('aria-label',`Worker 状态分布，共 ${data.states.total} 个，详见图例`);
    $('monitor-pie').replaceChildren(pie,list);
    const usageSeries=[['risk','综合风险'],['five_hour','5 小时窗口'],['week','周窗口']].map(([key,label],i)=>({key,label,color:['#e35b5b','#168aad','#8462cf'][i],value:d=>d.windows[key].used}));
    const current=data.current_usage; $('monitor-usage').replaceChildren();
    if(current){
      usageSeries.forEach(s=>{const w=current.data.windows[s.key], article=el('article');const bar=el('div',null,'monitor-progress');
        bar.setAttribute('role','progressbar');bar.setAttribute('aria-label',s.label);bar.setAttribute('aria-valuemin','0');bar.setAttribute('aria-valuemax','100');
        if(w.used!=null){bar.setAttribute('aria-valuenow',String(w.used));const fill=el('div',null,'monitor-progress-fill');fill.style.width=`${Math.max(0,Math.min(100,w.used))}%`;fill.style.background=s.color;bar.append(fill);}else{bar.classList.add('monitor-progress-unknown');bar.setAttribute('aria-valuetext','暂无数据');}
        article.append(el('span',s.label),el('strong',w.used==null?'暂无数据':`${w.used.toFixed(1)}%`),bar,el('small',`有效覆盖 ${w.covered} / ${current.data.eligible} 个 Worker · 权重覆盖 ${current.data.total_weight?(w.covered_weight/current.data.total_weight*100).toFixed(1):'0'}%`));$('monitor-usage').append(article);});
      const stale=Date.now()-new Date(current.observed_at).getTime()>2*3600000;
      $('monitor-usage').append(el('small',`${current.data.version < 2?'旧口径采样（缺失窗口未计入），下一小时更新 · ':''}${stale?'数据已过期 · ':''}采样：${stamp(current.observed_at)} · 未知套餐 ${current.data.unknown_plans} 个 Worker`));
    } else $('monitor-usage').append(el('p','等待首次小时采样'));
    chart($('monitor-usage-chart'),data.history.subscription_usage,usageSeries,3600000,true,days);
    chart($('monitor-state-chart'),data.history.worker_states,series,600000,false,days);
  }
  let generation=0;
  async function load(){const version=++generation, days=Number($('monitor-days').value);$('monitor-status').textContent='正在读取监控数据…';try{const response=await fetch(`/admin/monitoring?days=${days}`,{headers:{Accept:'application/json'}});if(!response.ok)throw Error();const data=await response.json();if(version!==generation)return;render(data,days);$('monitor-status').textContent=`状态更新于 ${new Date().toLocaleTimeString()}`;}catch{if(version===generation)$('monitor-status').textContent='监控读取失败，将自动重试';}}
  $('monitor-days').addEventListener('change',load);load();setInterval(load,60000);
})();
