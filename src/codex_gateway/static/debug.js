(async () => {
  await globalThis.I18n.ready;
(() => {
  const form = document.getElementById('debug-form');
  if (!form) return;
  const endpoint = form.elements.endpoint, model = form.elements.model, stream = form.elements.stream, body = form.elements.body;
  const output = document.getElementById('debug-output'), status = document.getElementById('debug-status');
  const headers = document.getElementById('debug-headers'), stop = document.getElementById('debug-stop');
  let active, generated = '';
  function example() {
    const name = model.value;
    if (endpoint.value === '/v1/models') return '';
    const request = endpoint.value === '/v1/responses'
      ? {model: name, input: globalThis.I18n.t("你好，请简短介绍自己。"), stream: stream.checked}
      : {model: name, messages: [{role: 'user', content: globalThis.I18n.t("你好，请简短介绍自己。")}]};
    if (endpoint.value === '/v1/messages') request.max_tokens = 1024;
    if (endpoint.value !== '/v1/messages/count_tokens') request.stream = stream.checked;
    return JSON.stringify(request, null, 2);
  }
  function update() {
    const native = endpoint.value.startsWith('/v1/messages'), listing = endpoint.value === '/v1/models';
    for (const option of model.options) {
      option.hidden = option.disabled = !!option.value && native && option.dataset.provider !== 'claude';
    }
    if (!model.value || model.selectedOptions[0]?.disabled) model.value = [...model.options].find(option => option.value && !option.disabled)?.value || '';
    model.disabled = listing; stream.disabled = listing || endpoint.value.endsWith('/count_tokens');
    document.getElementById('debug-body-label').hidden = listing;
    document.getElementById('debug-help').textContent = native
      ? globalThis.I18n.t("Claude 原生接口：Token 计数为估算；生成长度、采样及停止参数使用 Worker 默认策略。")
      : globalThis.I18n.t("编辑 JSON 可测试图片、工具和其他参数。修改接口后，点击“填入请求示例”可替换当前内容。");
    if (!body.value || body.value === generated) body.value = generated = example();
  }
  endpoint.addEventListener('change', update); model.addEventListener('change', update); stream.addEventListener('change', update);
  document.getElementById('debug-example').onclick = () => {
    if (body.value && body.value !== generated && !confirm(globalThis.I18n.t("替换当前请求 JSON？"))) return;
    body.value = generated = example();
  };
  stop.onclick = () => active?.abort();
  form.addEventListener('submit', async event => {
    event.preventDefault();
    active?.abort(); const controller = new AbortController(); active = controller;
    const current = () => active === controller;
    const target = endpoint.value, listing = target === '/v1/models';
    output.textContent = ''; headers.textContent = ''; status.textContent = globalThis.I18n.t("请求中…"); stop.disabled = false;
    const started = performance.now();
    let reader;
    try {
      const payload = listing ? undefined : JSON.stringify(JSON.parse(body.value));
      const auth = {'Content-Type': 'application/json', Authorization: 'Bearer ' + form.elements.key.value.trim()};
      if (target.startsWith('/v1/messages')) auth['anthropic-version'] = '2023-06-01';
      const response = await fetch(target, {method: listing ? 'GET' : 'POST', headers: auth, body: payload, signal: controller.signal, cache: 'no-store'});
      if (!current()) return;
      status.textContent = 'HTTP ' + response.status;
      headers.textContent = ['x-request-id', 'x-gateway-model', 'x-gateway-generation-policy', 'x-gateway-token-count']
        .filter(name => response.headers.has(name)).map(name => name + ': ' + response.headers.get(name)).join('\n');
      if (!response.body) throw new Error(globalThis.I18n.t("响应内容为空"));
      reader = response.body.getReader(); const decoder = new TextDecoder();
      let size = 0;
      while (true) {
        const {done, value} = await reader.read();
        if (!current()) return;
        if (done) break;
        size += value.byteLength;
        if (size > 2 * 1024 * 1024) { controller.abort(); throw new Error(globalThis.I18n.t("响应超过 2 MiB，已停止读取，请使用 SDK 获取完整响应")); }
        output.textContent += decoder.decode(value, {stream: true});
      }
      output.textContent += decoder.decode();
      status.textContent += ' · ' + Math.round(performance.now() - started) + ' ms';
    } catch (error) {
      if (current()) status.textContent = error.name === 'AbortError' ? globalThis.I18n.t("请求已停止") : error.message;
    } finally {
      reader?.releaseLock();
      if (current()) { active = null; stop.disabled = true; }
    }
  });
  update();
})();

})();
