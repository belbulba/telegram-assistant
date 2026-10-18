import asyncio
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo
import httpx

class Calendar:
    def __init__(self, auth, timezone='Europe/Minsk'):
        self.auth = auth
        self.tz = ZoneInfo(timezone)

    async def request(self, method, path, scopes=None, **kwargs):
        token = await asyncio.to_thread(self.auth.token, scopes) if scopes else await asyncio.to_thread(self.auth.token)
        headers = {'Authorization': 'Bearer ' + token, 'Prefer': 'outlook.timezone="UTC"'}
        headers.update(kwargs.pop('headers', {}))
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(method, 'https://graph.microsoft.com/v1.0' + path,
                headers=headers, **kwargs)
        if response.is_error:
            permission = 'ShortNotes.ReadWrite' if scopes else 'Calendars.ReadWrite'
            raise RuntimeError(f'Outlook вернул HTTP {response.status_code}. Проверьте авторизацию и права {permission}.')
        return response.json() if response.content else {'status': 'deleted'}

    def instant(self, value):
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            raise ValueError('Дата должна включать смещение часового пояса, например +03:00.')
        return dt

    def payload(self, args):
        start, end = self.instant(args['start']), self.instant(args['end'])
        if end <= start:
            raise ValueError('Конец события должен быть позже начала.')
        minutes = args['reminder_minutes']
        if minutes is not None and not 0 <= minutes <= 40320:
            raise ValueError('Напоминание: от 0 до 40320 минут.')
        return {'subject': args['title'], 'start': {'dateTime': start.astimezone(ZoneInfo('UTC')).replace(tzinfo=None).isoformat(), 'timeZone': 'UTC'},
                'end': {'dateTime': end.astimezone(ZoneInfo('UTC')).replace(tzinfo=None).isoformat(), 'timeZone': 'UTC'},
                'isReminderOn': minutes is not None, 'reminderMinutesBeforeStart': minutes or 0}

    async def list(self, start, end):
        if self.instant(end) <= self.instant(start):
            raise ValueError('Неверный диапазон дат.')
        params = {'startDateTime': start, 'endDateTime': end, '$top': '100', '$select': 'id,subject,start,end,isAllDay,isReminderOn,reminderMinutesBeforeStart,type', '$orderby': 'start/dateTime'}
        result = await self.request('GET', '/me/calendarView', params=params)
        events = result.get('value', [])
        # Follow only Graph-owned pagination paths; never send OAuth tokens to another host.
        while result.get('@odata.nextLink'):
            link = result['@odata.nextLink']
            prefix = 'https://graph.microsoft.com/v1.0'
            if not link.startswith(prefix + '/') or len(events) >= 1000:
                return {'events': events, 'truncated': True}
            result = await self.request('GET', link[len(prefix):])
            events.extend(result.get('value', []))
        return {'events': events, 'truncated': False}

    async def get(self, event_id):
        return await self.request('GET', '/me/events/' + quote(event_id, safe=''))

    async def write(self, action, args, etag=None):
        if action == 'create_event':
            body = self.payload(args)
            body['transactionId'] = args['transaction_id']
            return await self.request('POST', '/me/events', json=body)
        path = '/me/events/' + quote(args['event_id'], safe='')
        # Check the version again before applying a confirmed operation.
        current = await self.get(args['event_id'])
        if etag and current.get('@odata.etag') != etag:
            raise ValueError('Событие изменилось после запроса. Найдите его заново.')
        if action == 'delete_event':
            return await self.request('DELETE', path, headers={'If-Match': etag} if etag else {})
        return await self.request('PATCH', path, json=self.payload(args), headers={'If-Match': etag} if etag else {})
