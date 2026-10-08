(async () => {
  await globalThis.I18n.ready;
  const {t, lang} = globalThis.I18n;
  const list = name => t(name).split(',');
  const locale = {
    weekdays: {shorthand: list('calendar.weekdays.short'), longhand: list('calendar.weekdays.long')},
    months: {shorthand: list('calendar.months.short'), longhand: list('calendar.months.long')},
    firstDayOfWeek: lang === 'CN' ? 1 : 0,
    rangeSeparator: t('calendar.range'), weekAbbreviation: t('calendar.week'),
    scrollTitle: t('calendar.scroll'), toggleTitle: t('calendar.toggle'),
    yearAriaLabel: t('calendar.year'), monthAriaLabel: t('calendar.month'),
    hourAriaLabel: t('calendar.hour'), minuteAriaLabel: t('calendar.minute'),
    amPM: list('calendar.am_pm'), time_24hr: true,
    ordinal: () => '',
  };
  document.querySelectorAll('input[data-calendar-type],input[type="date"],input[type="datetime-local"],input[type="month"]').forEach(input => {
    if (input.disabled || input._flatpickr) return;
    const type = input.dataset.calendarType || input.type;
    // Preserve local-time history bounds and the server's ISO date/month format.
    if (type === 'datetime-local' && input.dataset.instant) {
      const date = new Date(input.dataset.instant);
      if (Number.isFinite(date.getTime())) {
        const pad = n => String(n).padStart(2, '0');
        input.value = date.getFullYear() + '-' + pad(date.getMonth()+1) + '-' + pad(date.getDate()) +
          'T' + pad(date.getHours()) + ':' + pad(date.getMinutes()) + ':' + pad(date.getSeconds());
      }
    }
    flatpickr(input, {
      locale, disableMobile: true, allowInput: true,
      dateFormat: type === 'month' ? 'Y-m' : type === 'datetime-local' ? 'Y-m-d\\TH:i:S' : 'Y-m-d',
      ariaDateFormat: lang === 'CN' ? 'Y年n月j日' : 'F j, Y',
      enableTime: type === 'datetime-local', enableSeconds: type === 'datetime-local', time_24hr: true,
      minDate: input.min || undefined, maxDate: input.max || undefined,
      plugins: type === 'month' ? [new monthSelectPlugin({dateFormat:'Y-m', altFormat:'Y-m'})] : [],
      onReady(_, __, instance) {
        for (const [element, key] of [[instance.prevMonthNav, 'calendar.previous'], [instance.nextMonthNav, 'calendar.next']]) {
          element.setAttribute('role','button'); element.tabIndex=0;
          element.setAttribute('aria-label',t(key));
          element.addEventListener('keydown', event => {
            if (event.key === 'Enter' || event.key === ' ') {event.preventDefault();element.click();}
          });
        }
        const actions = document.createElement('div');
        actions.className = 'calendar-actions';
        for (const [key, action] of [
          ['calendar.clear', () => {instance.clear();instance.close();instance.input.focus();}],
          ['calendar.close', () => {instance.close();instance.input.focus();}],
        ]) {
          const button = document.createElement('button');
          button.type='button';button.textContent=t(key);
          button.addEventListener('click', action);actions.append(button);
        }
        instance.calendarContainer.append(actions);
      },
    });
  });
})();
