/* One catalog for server-rendered pages and browser interactions. */
(() => {
  const normalize = lang => /^(zh|cn)(-|_|$)/i.test(lang || '') ? 'CN' : /^en(-|_|$)/i.test(lang || '') ? 'EN' : null;
  const cookie = (document.cookie || '').split(';').map(v => v.trim()).find(v => v.startsWith('lang='));
  let lang = normalize(cookie?.slice(5)) || normalize(document.documentElement?.lang) || 'EN';
  let messages = {};
  const t = (name, language = lang, params = {}) => {
    const entry = messages[name] || {};
    const value = entry[normalize(language) || 'EN'] || entry.EN || name;
    return value.replace(/\{(\w+)\}/g, (match, key) => Object.hasOwn(params, key) ? String(params[key]) : match);
  };
  const ready = fetch('/static/messages.json', {cache: 'no-cache'})
    .then(response => { if (!response.ok) throw new Error('Translation catalog unavailable'); return response.json(); })
    .then(data => { messages = data; })
    .catch(error => { console.warn('Translation catalog unavailable; using message names.', error); });
  window.I18n = {t, ready, get lang() { return lang; }, get locale() { return lang === 'CN' ? 'zh-CN' : 'en'; }};
  document.addEventListener('change', event => {
    if (!event.target.matches('[data-language-switch]')) return;
    const next = normalize(event.target.value);
    if (!next || next === lang) return;
    document.cookie = 'lang=' + next + '; Path=/; Max-Age=31536000; SameSite=Lax' + (location.protocol === 'https:' ? '; Secure' : '');
    location.reload();
  });
})();
