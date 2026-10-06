(() => {
  const host = document.querySelector('[data-token-trend]');
  if (!host) return;
  const daily = [...host.querySelectorAll('[data-date]')].map(node => ({date:node.dataset.date, tokens:Number(node.dataset.tokens)}));
  const ns = 'http://www.w3.org/2000/svg';
  function element(tag, attrs, text) {
    const node = document.createElementNS(ns, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text != null) node.textContent = text;
    return node;
  }
  const svg = element('svg', {viewBox:'0 0 900 280', role:'img', 'aria-label':'过去30天每日 Token 用量折线图，精确数值见下方表格', width:'100%'});
  const peak = Math.max(1, ...daily.map(day => day.tokens));
  const x = i => 82 + i * 790 / 29;
  const y = n => 230 - n / peak * 200;
  for (let i = 0; i <= 4; i++) {
    const value = peak * i / 4;
    svg.append(element('line', {x1:82, x2:872, y1:y(value), y2:y(value), stroke:'#e2e8f0'}));
    svg.append(element('text', {x:74, y:y(value)+4, 'text-anchor':'end', fill:'#64748b', 'font-size':12}, window.TokenFormat.tokens(Math.round(value))));
  }
  svg.append(element('polyline', {points:daily.map((day,i) => `${x(i)},${y(day.tokens)}`).join(' '), fill:'none', stroke:'#2563eb', 'stroke-width':2.5}));
  daily.forEach((day, i) => {
    const dot = element('circle', {cx:x(i), cy:y(day.tokens), r:4, fill:'#2563eb', tabindex:0, 'aria-label':`${day.date}: ${day.tokens} Token`});
    dot.append(element('title', {}, `${day.date}: ${day.tokens.toLocaleString()} Token`));
    svg.append(dot);
    if ([0,7,14,21,29].includes(i)) svg.append(element('text', {x:x(i), y:260, 'text-anchor':'middle', fill:'#64748b', 'font-size':12}, day.date.slice(5)));
  });
  host.replaceChildren(svg);
})();
