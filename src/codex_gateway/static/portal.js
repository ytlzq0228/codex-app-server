const dialog = document.getElementById('portal-result');
document.querySelectorAll('form[data-portal]').forEach(form => form.addEventListener('submit', async event => {
  event.preventDefault();
  const button = form.querySelector('button');
  button.disabled = true;
  try {
    const response = await fetch(form.action, {method:'POST', body:new FormData(form), headers:{'X-Requested-With':'XMLHttpRequest'}});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data));
    document.getElementById('portal-message').textContent = data.message || '已保存';
    document.getElementById('portal-secret').textContent = data.secret || '';
    dialog.showModal();
  } catch(error) { document.getElementById('portal-message').textContent = error.message; document.getElementById('portal-secret').textContent = ''; dialog.showModal(); }
  finally { button.disabled = false; }
}));
document.getElementById('portal-close').onclick = () => location.reload();
let controller;
const debug = document.getElementById('debug-form');
if (debug) {
  document.getElementById('debug-stop').onclick = () => controller?.abort();
  debug.onsubmit = async event => {
    event.preventDefault(); controller?.abort(); controller = new AbortController();
    const output = document.getElementById('debug-output'), status = document.getElementById('debug-status');
    output.textContent = ''; status.textContent = '请求中…';
    const started = performance.now();
    try {
      const fields = new FormData(debug), endpoint = fields.get('endpoint');
      const response = await fetch(endpoint, {method:endpoint === '/v1/models' ? 'GET':'POST', headers:{Authorization:'Bearer '+fields.get('key'), 'Content-Type':'application/json'}, body:endpoint === '/v1/models' ? undefined:JSON.stringify(JSON.parse(fields.get('body'))), signal:controller.signal});
      status.textContent = 'HTTP '+response.status;
      const reader = response.body.getReader(), decoder = new TextDecoder();
      while (true) { const {done,value} = await reader.read(); if(done) break; output.textContent += decoder.decode(value,{stream:true}); }
      output.textContent += decoder.decode();
      status.textContent += ' · '+Math.round(performance.now()-started)+' ms';
    } catch(error) { status.textContent = error.message; }
  };
}
