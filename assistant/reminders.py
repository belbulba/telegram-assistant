"""Durable one-off reminders; Telegram delivery does not use a language model."""
import asyncio
from datetime import datetime
import logging
import time
import uuid

from telegram.error import RetryAfter, Forbidden, BadRequest

log = logging.getLogger(__name__)
INTERVAL = 2
STATUSES = {'pending': 'ожидает', 'sending': 'отправляется', 'sent': 'отправлено',
            'uncertain': 'доставка не подтверждена', 'failed': 'ошибка доставки',
            'creating': 'создание события не завершено', 'cancelled': 'отменено'}


class Reminders:
    def __init__(self, store, tz):
        self.store, self.tz = store, tz

    def validate(self, title, remind_at):
        title = title.strip()
        if not title or len(title) > 500:
            raise ValueError('Текст напоминания: от 1 до 500 символов.')
        due = datetime.fromisoformat(remind_at)
        if due.tzinfo is None:
            raise ValueError('Время напоминания должно включать UTC offset.')
        if due.timestamp() <= time.time():
            raise ValueError('Время напоминания уже прошло. Уточните новое время.')
        return title, due.timestamp()

    def get(self, reminder_id):
        cursor = self.store.db.execute('SELECT * FROM reminders WHERE id=?', (reminder_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError('Напоминание не найдено. Сначала получите список.')
        return dict(zip([col[0] for col in cursor.description], row))

    def public(self, row):
        return {key: row[key] for key in ('id', 'title', 'version', 'status', 'event_id')} | {
            'remind_at': datetime.fromtimestamp(row['due'], self.tz).isoformat(),
            'status_text': STATUSES[row['status']]}

    def list(self):
        cursor = self.store.db.execute("SELECT * FROM reminders WHERE status != 'cancelled' ORDER BY CASE WHEN status IN ('pending','creating','uncertain','failed','sending') THEN 0 ELSE 1 END, due LIMIT 101")
        names = [col[0] for col in cursor.description]
        rows = [dict(zip(names, row)) for row in cursor.fetchall()]
        return {'reminders': [self.public(row) for row in rows[:100]], 'truncated': len(rows) > 100}

    def create(self, title, remind_at, *, status='pending', event_start=None):
        title, due = self.validate(title, remind_at)
        key = uuid.uuid4().hex
        with self.store.db:
            self.store.db.execute('INSERT INTO reminders(id,title,due,status,event_start,created) VALUES(?,?,?,?,?,?)',
                                  (key, title, due, status, event_start, time.time()))
        return self.public(self.get(key))

    async def create_with_event(self, calendar, title, start, end, reminder_minutes, remind_at):
        args = dict(title=title, start=start, end=end, reminder_minutes=reminder_minutes,
                    transaction_id=str(uuid.uuid4()))
        calendar.payload(args)
        self.validate(title, remind_at)
        if datetime.fromisoformat(remind_at) > calendar.instant(start):
            raise ValueError('Telegram-напоминание должно быть не позже начала события.')
        # Reserve locally first. An uncertain external write must never be retried blindly.
        row = self.create(title, remind_at, status='creating', event_start=calendar.instant(start).timestamp())
        try:
            event = await calendar.write('create_event', args)
            if not event.get('id'):
                raise RuntimeError('Outlook не вернул идентификатор события.')
        except Exception:
            raise RuntimeError('Создание события не подтверждено. Telegram-напоминание не включено. Проверьте Outlook и /reminders перед повтором.') from None
        with self.store.db:
            self.store.db.execute("UPDATE reminders SET event_id=?,status='pending' WHERE id=?", (event['id'], row['id']))
        return {'event': {'id': event['id'], 'title': title, 'start': start, 'end': end},
                'telegram_reminder': self.public(self.get(row['id']))}

    def prepare(self, action, reminder_id, title=None, remind_at=None):
        row = self.get(reminder_id)
        if row['status'] not in ('pending', 'uncertain', 'failed', 'creating'):
            raise ValueError('Напоминание уже отправлено, отменено или отправляется. Обновите список.')
        args = {'reminder_id': reminder_id, 'version': row['version']}
        if action == 'update_reminder':
            self.validate(title, remind_at)
            args.update(title=title, remind_at=remind_at)
        return args

    def apply(self, action, reminder_id, version, title=None, remind_at=None):
        args = self.prepare(action, reminder_id, title, remind_at)
        if args['version'] != version:
            raise ValueError('Напоминание изменилось. Получите список заново.')
        if action == 'update_reminder':
            title, due = self.validate(title, remind_at)
            sql = "UPDATE reminders SET title=?,due=?,status='pending',version=version+1,retry_at=0 WHERE id=? AND version=? AND status IN ('pending','uncertain','failed','creating')"
            values = (title, due, reminder_id, version)
        elif action == 'cancel_reminder':
            sql = "UPDATE reminders SET status='cancelled',version=version+1 WHERE id=? AND version=? AND status IN ('pending','uncertain','failed','creating')"
            values = (reminder_id, version)
        else:
            raise ValueError('Неизвестное действие с напоминанием.')
        with self.store.db:
            if self.store.db.execute(sql, values).rowcount != 1:
                raise ValueError('Напоминание уже изменено или отправляется.')
        return self.public(self.get(reminder_id))

    def sync_event(self, action, args):
        # Only changes made through this bot are visible here; sent reminders stay sent.
        with self.store.db:
            if action == 'delete_event':
                self.store.db.execute("UPDATE reminders SET status='cancelled',version=version+1 WHERE event_id=? AND status='pending'", (args['event_id'],))
            else:
                start = datetime.fromisoformat(args['start']).timestamp()
                self.store.db.execute("UPDATE reminders SET due=due+(?-event_start),event_start=?,title=?,version=version+1 WHERE event_id=? AND status='pending' AND event_start IS NOT NULL",
                                      (start, start, args['title'], args['event_id']))

    def overview(self):
        result = self.list()
        lines = ['Напоминания Telegram · ' + self.tz.key]
        if not result['reminders']:
            lines.append('Напоминаний пока нет. Например: «Напомни через час вынести мусор».')
        for row in result['reminders']:
            stamp = datetime.fromisoformat(row['remind_at']).strftime('%d.%m.%Y %H:%M')
            lines.append(f"\n{stamp} — {row['title']}\n{row['status_text']}" + (' · связано с Outlook' if row['event_id'] else ''))
        if result['truncated']:
            lines.append('\nПоказаны первые 100 записей.')
        return '\n'.join(lines)


class ReminderNotifier:
    def __init__(self, reminders, bot, owner):
        self.reminders, self.bot, self.owner = reminders, bot, owner

    def recover(self):
        # A crash after send and before commit leaves no safe way to know delivery.
        with self.reminders.store.db:
            self.reminders.store.db.execute("UPDATE reminders SET status='uncertain',version=version+1 WHERE status='sending'")

    async def poll(self):
        db = self.reminders.store.db
        rows = db.execute("SELECT id FROM reminders WHERE status='pending' AND due<=? AND retry_at<=? ORDER BY due LIMIT 20", (time.time(), time.time())).fetchall()
        for (key,) in rows:
            with db:
                claimed = db.execute("UPDATE reminders SET status='sending',version=version+1 WHERE id=? AND status='pending'", (key,)).rowcount
            if not claimed:
                continue
            row = self.reminders.get(key)
            when = datetime.fromtimestamp(row['due'], self.reminders.tz).strftime('%d.%m.%Y %H:%M')
            text = '🔔 Напоминание\n' + row['title'] + '\n\nЗапланировано: ' + when + ' · ' + self.reminders.tz.key
            if time.time() - row['due'] > 60:
                text += '\nПришло с задержкой.'
            status, retry_at = 'uncertain', 0
            try:
                await self.bot.send_message(chat_id=self.owner, text=text, parse_mode=None)
                status = 'sent'
            except RetryAfter as exc:
                delay = exc.retry_after.total_seconds() if hasattr(exc.retry_after, 'total_seconds') else exc.retry_after
                status, retry_at = 'pending', time.time() + max(1, delay)
            except (Forbidden, BadRequest):
                status = 'failed'
                log.warning('Reminder delivery rejected by Telegram.')
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning('Reminder delivery unconfirmed; automatic repeat suppressed.')
            finally:
                with db:
                    db.execute('UPDATE reminders SET status=?,retry_at=?,delivered=? WHERE id=?',
                               (status, retry_at, time.time() if status == 'sent' else None, key))

    async def run(self):
        self.recover()
        while True:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('Reminder polling failed: %s', type(exc).__name__)
            await asyncio.sleep(INTERVAL)
