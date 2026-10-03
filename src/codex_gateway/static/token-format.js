/* Shared display formatting; API payloads retain exact token counts. */
(() => {
  const count = value => {
    const number = Number(value ?? 0);
    return Number.isFinite(number) ? number : 0;
  };
  const tokens = value => {
    const number = count(value);
    return number > 1000000 ? (number / 1000000).toFixed(4) + ' million' : String(Math.trunc(number));
  };
  window.TokenFormat = Object.freeze({tokens, total: (...values) => tokens(values.reduce((sum, value) => sum + count(value), 0))});
})();
