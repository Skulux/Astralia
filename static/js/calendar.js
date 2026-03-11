(function () {
  const calendars = document.querySelectorAll('[data-calendar]');
  if (!calendars.length) {
    return;
  }

  const weekdayFormatter = new Intl.DateTimeFormat('de-DE', { weekday: 'short' });
  const dayFormatter = new Intl.DateTimeFormat('de-DE', { day: 'numeric' });
  const fullDateFormatter = new Intl.DateTimeFormat('de-DE', {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
  });
  const rangeMonthFormatter = new Intl.DateTimeFormat('de-DE', { month: 'long' });
  const rangeYearFormatter = new Intl.DateTimeFormat('de-DE', { year: 'numeric' });

  const recurrenceLabels = {
    weekly: 'Wöchentlich',
    monthly: 'Monatlich',
    yearly: 'Jährlich',
  };

  const MS_PER_DAY = 24 * 60 * 60 * 1000;

  const normalizeDate = (value) => {
    if (!value) {
      return null;
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return null;
    }
    date.setHours(0, 0, 0, 0);
    return date;
  };

  const startOfWeek = (date) => {
    const result = new Date(date);
    const day = result.getDay();
    const diff = (day + 6) % 7; // Monday as first day
    result.setDate(result.getDate() - diff);
    result.setHours(0, 0, 0, 0);
    return result;
  };

  const addDays = (date, days) => {
    const result = new Date(date);
    result.setDate(result.getDate() + days);
    return result;
  };

  const toISODate = (date) => date.toISOString().slice(0, 10);

  const formatRange = (start, end) => {
    if (start.getFullYear() === end.getFullYear() && start.getMonth() === end.getMonth()) {
      return `${dayFormatter.format(start)} – ${dayFormatter.format(end)} ${rangeMonthFormatter.format(end)} ${rangeYearFormatter.format(end)}`;
    }
    if (start.getFullYear() === end.getFullYear()) {
      return `${dayFormatter.format(start)} ${rangeMonthFormatter.format(start)} – ${dayFormatter.format(end)} ${rangeMonthFormatter.format(end)} ${rangeYearFormatter.format(end)}`;
    }
    return `${dayFormatter.format(start)} ${rangeMonthFormatter.format(start)} ${rangeYearFormatter.format(start)} – ${dayFormatter.format(end)} ${rangeMonthFormatter.format(end)} ${rangeYearFormatter.format(end)}`;
  };

  const normalizeRecurrence = (value) => {
    if (!value) {
      return 'none';
    }
    const normalized = String(value).trim().toLowerCase();
    if (normalized === 'weekly' || normalized === 'wöchentlich' || normalized === 'woechentlich') {
      return 'weekly';
    }
    if (normalized === 'monthly' || normalized === 'monatlich') {
      return 'monthly';
    }
    if (normalized === 'yearly' || normalized === 'jährlich' || normalized === 'jaehrlich' || normalized === 'annually') {
      return 'yearly';
    }
    return 'none';
  };

  const sortEvents = (events) => {
    events.sort((a, b) => {
      const aTime = a.time || '';
      const bTime = b.time || '';
      if (aTime && bTime) {
        const timeComparison = aTime.localeCompare(bTime);
        if (timeComparison !== 0) {
          return timeComparison;
        }
      } else if (aTime) {
        return -1;
      } else if (bTime) {
        return 1;
      }
      const aLabel = a.label || '';
      const bLabel = b.label || '';
      return aLabel.localeCompare(bLabel);
    });
    return events;
  };

  const occursOnDate = (event, dayDate) => {
    const baseDate = event.baseDate;
    if (!baseDate) {
      return false;
    }
    const dayTime = dayDate.getTime();
    const baseTime = baseDate.getTime();
    const recurrence = event.recurrence;

    if (!recurrence || recurrence === 'none') {
      return dayTime === baseTime;
    }

    if (recurrence === 'weekly') {
      if (dayTime < baseTime) {
        return false;
      }
      const diffDays = Math.round((dayTime - baseTime) / MS_PER_DAY);
      return diffDays % 7 === 0;
    }

    if (recurrence === 'monthly') {
      if (dayTime < baseTime) {
        return false;
      }
      const diffMonths =
        (dayDate.getFullYear() - baseDate.getFullYear()) * 12 +
        (dayDate.getMonth() - baseDate.getMonth());
      if (diffMonths < 0) {
        return false;
      }
      const baseDay = baseDate.getDate();
      const lastDayOfMonth = new Date(dayDate.getFullYear(), dayDate.getMonth() + 1, 0).getDate();
      const targetDay = Math.min(baseDay, lastDayOfMonth);
      return dayDate.getDate() === targetDay;
    }

    if (recurrence === 'yearly') {
      const baseMonth = baseDate.getMonth();
      if (dayDate.getMonth() !== baseMonth) {
        return false;
      }
      const baseDay = baseDate.getDate();
      const lastDayOfMonth = new Date(dayDate.getFullYear(), baseMonth + 1, 0).getDate();
      const targetDay = Math.min(baseDay, lastDayOfMonth);
      return dayDate.getDate() === targetDay;
    }

    return false;
  };

  const buildAccessibleSummary = (date, events) => {
    const parts = [fullDateFormatter.format(date)];
    const eventSummaries = events
      .map((event) => {
        const segments = [];
        const timeText = event.time_display || (event.time ? `${event.time} Uhr` : '');
        if (timeText) {
          segments.push(timeText);
        }
        if (event.label) {
          segments.push(event.label);
        }
        if (event.recurrence && event.recurrence !== 'none') {
          const recurrenceLabel = recurrenceLabels[event.recurrence];
          if (recurrenceLabel) {
            segments.push(recurrenceLabel);
          }
        }
        return segments.join(' · ');
      })
      .filter((text) => Boolean(text));

    if (eventSummaries.length) {
      parts.push(eventSummaries.join(' | '));
    }
    return parts.join(' – ');
  };

  calendars.forEach((calendar) => {
    let parsedEvents = [];
    try {
      const data = calendar.getAttribute('data-calendar-events');
      parsedEvents = Array.isArray(data) ? data : JSON.parse(data || '[]');
    } catch (error) {
      parsedEvents = [];
    }

    const preparedEvents = parsedEvents
      .map((event) => {
        if (!event || typeof event !== 'object') {
          return null;
        }
        const baseDate = normalizeDate(event.date);
        if (!baseDate) {
          return null;
        }
        return {
          id: event.id || '',
          baseDate,
          label: event.label ? String(event.label).trim() : '',
          time: event.time ? String(event.time).trim() : '',
          time_display: event.time_display ? String(event.time_display).trim() : '',
          url: event.url ? String(event.url).trim() : '',
          icon_url: event.icon_url ? String(event.icon_url).trim() : '',
          icon_label: event.icon_label ? String(event.icon_label).trim() : '',
          recurrence: normalizeRecurrence(event.recurrence),
        };
      })
      .filter((event) => Boolean(event));

    const rangeLabel = calendar.querySelector('[data-calendar-range]');
    const grid = calendar.querySelector('[data-calendar-grid]');
    const prevButton = calendar.querySelector('[data-calendar-prev]');
    const nextButton = calendar.querySelector('[data-calendar-next]');

    let currentStart = startOfWeek(new Date());

    const eventsForDate = (date) => {
      const events = preparedEvents
        .filter((event) => occursOnDate(event, date))
        .map((event) => ({
          id: event.id,
          label: event.label,
          time: event.time,
          time_display: event.time_display,
          url: event.url,
          icon_url: event.icon_url,
          icon_label: event.icon_label,
          recurrence: event.recurrence,
        }));
      return sortEvents(events);
    };

    const buildEventEntry = (event) => {
      const hasUrl = Boolean(event.url);
      const element = document.createElement(hasUrl ? 'a' : 'div');
      element.className = 'home-calendar__event';
      if (hasUrl) {
        element.href = event.url;
        if (/^https?:/i.test(event.url)) {
          element.target = '_blank';
          element.rel = 'noopener';
        }
      }

      if (event.icon_url) {
        element.classList.add('home-calendar__event--has-icon');
        const icon = document.createElement('img');
        icon.className = 'home-calendar__event-icon';
        icon.src = event.icon_url;
        icon.alt = event.icon_label || '';
        icon.width = 24;
        icon.height = 24;
        element.appendChild(icon);
      }

      const content = document.createElement('div');
      content.className = 'home-calendar__event-content';

      const timeText = event.time_display || (event.time ? `${event.time} Uhr` : '');
      if (timeText) {
        const timeElement = document.createElement('span');
        timeElement.className = 'home-calendar__event-time';
        timeElement.textContent = timeText;
        content.appendChild(timeElement);
      }

      const labelElement = document.createElement('span');
      labelElement.className = 'home-calendar__event-label';
      labelElement.textContent = event.label || 'Event';
      content.appendChild(labelElement);

      if (event.recurrence && event.recurrence !== 'none') {
        const recurrenceLabel = recurrenceLabels[event.recurrence];
        if (recurrenceLabel) {
          const recurrenceElement = document.createElement('span');
          recurrenceElement.className = 'home-calendar__event-recurrence';
          recurrenceElement.textContent = recurrenceLabel;
          content.appendChild(recurrenceElement);
        }
      }

      element.appendChild(content);
      return element;
    };

    const buildDay = (date, providedEvents) => {
      const events = Array.isArray(providedEvents) ? providedEvents : eventsForDate(date);
      const hasEvents = events.length > 0;
      const element = document.createElement('div');
      element.className = 'home-calendar__day';
      if (hasEvents) {
        element.classList.add('has-event');
      }

      element.setAttribute('data-date', toISODate(date));
      element.setAttribute('aria-label', buildAccessibleSummary(date, events));

      const weekday = document.createElement('span');
      weekday.className = 'home-calendar__weekday';
      weekday.textContent = weekdayFormatter.format(date);

      const dayNumber = document.createElement('span');
      dayNumber.className = 'home-calendar__date';
      dayNumber.textContent = dayFormatter.format(date);

      element.appendChild(weekday);
      element.appendChild(dayNumber);

      if (hasEvents) {
        const eventsContainer = document.createElement('div');
        eventsContainer.className = 'home-calendar__events';
        events.forEach((event) => {
          eventsContainer.appendChild(buildEventEntry(event));
        });
        element.appendChild(eventsContainer);
      }

      return element;
    };

    const buildEmptyState = () => {
      const element = document.createElement('div');
      element.className = 'home-calendar__empty';
      element.textContent = 'Keine Events für diese Woche';
      return element;
    };

    const render = () => {
      const end = addDays(currentStart, 6);
      if (rangeLabel) {
        rangeLabel.textContent = formatRange(currentStart, end);
      }
      if (grid) {
        const days = [];
        for (let offset = 0; offset < 7; offset += 1) {
          const dayDate = addDays(currentStart, offset);
          days.push({
            date: dayDate,
            events: eventsForDate(dayDate),
          });
        }

        const eventDays = days.filter((day) => day.events.length > 0);
        const hasAnyEvents = eventDays.length > 0;
        grid.classList.toggle('home-calendar__grid--has-events', hasAnyEvents);
        grid.classList.toggle('home-calendar__grid--no-events', !hasAnyEvents);

        grid.innerHTML = '';
        if (!hasAnyEvents) {
          grid.appendChild(buildEmptyState());
          return;
        }

        eventDays.forEach((day) => {
          const dayElement = buildDay(day.date, day.events);
          grid.appendChild(dayElement);
        });
      }
    };

    prevButton?.addEventListener('click', () => {
      currentStart = addDays(currentStart, -7);
      render();
    });

    nextButton?.addEventListener('click', () => {
      currentStart = addDays(currentStart, 7);
      render();
    });

    render();
  });
})();
