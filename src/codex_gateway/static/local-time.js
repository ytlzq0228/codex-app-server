(() => {
  const formatter = new Intl.DateTimeFormat(globalThis.I18n.locale, {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
  });
  function render(root) {
    const nodes = [...(root.matches?.('time[data-local-time]') ? [root] : []), ...root.querySelectorAll('time[data-local-time]')];
    nodes.forEach(node => {
      if (node.dataset.localized) return;
      const value = node.getAttribute('datetime');
      // Only parse explicit instants, never guess the timezone of a wall time.
      if (!/(Z|[+-]\d{2}:\d{2})$/.test(value)) return;
      const date = new Date(value);
      if (!Number.isFinite(date.getTime())) return;
      node.dataset.localized = 'true';
      node.textContent = formatter.format(date);
      node.title = formatter.resolvedOptions().timeZone + ' · ' + value;
    });
  }
  render(document);
  new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(node => {
    if (node.nodeType === 1) render(node);
  }))).observe(document.body, {childList: true, subtree: true});
})();
