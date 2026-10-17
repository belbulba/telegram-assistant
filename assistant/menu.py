"""Calendar shortcuts that do not spend model usage."""
from datetime import datetime, timedelta, timezone

from telegram import ReplyKeyboardMarkup

TODAY = '📅 Сегодня'
TOMORROW = '🌅 Завтра'
WEEK = '🗓 Ближайшие 7 дней'
ADD = '➕ Добавить событие'
STATUS = '🔌 Подключения'
USAGE = '📊 Лимиты'
PERIODS = {TODAY: 'today', TOMORROW: 'tomorrow', WEEK: 'week'}


def keyboard():
    return ReplyKeyboardMarkup([[TODAY, TOMORROW], [WEEK, ADD], [STATUS, USAGE]],
                               resize_keyboard=True, is_persistent=True,
                               input_field_placeholder='Сообщение или кнопка меню')


def event_time(value, tz):
    dt = datetime.fromisoformat(value['dateTime'].replace('Z', '+00:00'))
    # Calendar.request asks Graph to return all dates in UTC.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


async def agenda(calendar, period, now=None):
    now = now or datetime.now(calendar.tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'today':
        start, end, title = midnight, midnight + timedelta(days=1), 'Сегодня'
    elif period == 'tomorrow':
        start, end, title = midnight + timedelta(days=1), midnight + timedelta(days=2), 'Завтра'
    elif period == 'week':
        start, end, title = now, now + timedelta(days=7), 'Ближайшие 7 дней'
    else:
        raise ValueError('Неизвестный период календаря.')
    result = await calendar.list(start=start.isoformat(), end=end.isoformat())
    lines = [f'{title} · {calendar.tz.key}']
    if not result['events']:
        lines.append('Событий нет.')
    for event in result['events']:
        begin = event_time(event['start'], calendar.tz)
        finish = event_time(event['end'], calendar.tz)
        if event.get('isAllDay'):
            timing = begin.strftime('%d.%m') + ' · весь день'
        else:
            timing = begin.strftime('%d.%m %H:%M') + ' — ' + finish.strftime(
                '%H:%M' if begin.date() == finish.date() else '%d.%m %H:%M')
        lines.append(f"\n{timing}\n{event.get('subject') or '(без названия)'}")
    if result.get('truncated'):
        lines.append('\nПоказана часть событий. Выбери меньший период.')
    return '\n'.join(lines)
