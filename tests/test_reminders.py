import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from telegram.error import RetryAfter, Forbidden
from assistant.agent import Agent
from assistant.calendar import Calendar
from assistant.main import apply_confirmation, destination_request
from assistant.reminders import Reminders, ReminderNotifier
from assistant.store import Store


def future(minutes=60):
    return (datetime.now(ZoneInfo('Europe/Minsk')) + timedelta(minutes=minutes)).isoformat()


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'db'
        self.store = Store(self.path)
        self.reminders = Reminders(self.store, ZoneInfo('Europe/Minsk'))
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.notifier = ReminderNotifier(self.reminders, self.bot, 42)

    def tearDown(self):
        self.store.db.close()
        self.folder.cleanup()

    def due(self, **kwargs):
        row = self.reminders.create('Тест **буквально**', future(), **kwargs)
        self.store.db.execute('UPDATE reminders SET due=due-7200 WHERE id=?', (row['id'],))
        self.store.db.commit()
        return row

    async def test_durable_late_delivery_survives_reset_and_restart_without_duplicate(self):
        row = self.due()
        self.store.clear()
        self.store.db.close()
        self.store = Store(self.path)
        self.reminders = Reminders(self.store, ZoneInfo('Europe/Minsk'))
        notifier = ReminderNotifier(self.reminders, self.bot, 42)
        notifier.recover()
        await notifier.poll()
        await notifier.poll()
        self.bot.send_message.assert_awaited_once()
        args = self.bot.send_message.call_args.kwargs
        self.assertEqual(args['chat_id'], 42)
        self.assertIsNone(args['parse_mode'])
        self.assertIn('с задержкой', args['text'])
        self.assertIn('Тест **буквально**', args['text'])
        self.assertEqual(self.reminders.get(row['id'])['status'], 'sent')

    async def test_future_and_cancelled_reminders_not_sent(self):
        self.reminders.create('Будущее', future())
        row = self.due()
        self.reminders.apply('cancel_reminder', row['id'], row['version'])
        await self.notifier.poll()
        self.bot.send_message.assert_not_called()

    async def test_uncertain_delivery_and_crashed_send_not_repeated(self):
        row = self.due()
        self.bot.send_message.side_effect = RuntimeError('timeout')
        await self.notifier.poll()
        await self.notifier.poll()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.reminders.get(row['id'])['status'], 'uncertain')
        self.store.db.execute("UPDATE reminders SET status='sending' WHERE id=?", (row['id'],))
        self.store.db.commit()
        self.notifier.recover()
        self.assertEqual(self.reminders.get(row['id'])['status'], 'uncertain')

    async def test_rate_limit_is_retried_only_after_explicit_retry_after(self):
        row = self.due()
        self.bot.send_message.side_effect = RetryAfter(60)
        await self.notifier.poll()
        await self.notifier.poll()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.reminders.get(row['id'])['status'], 'pending')
        self.store.db.execute('UPDATE reminders SET retry_at=0')
        self.store.db.commit()
        self.bot.send_message.side_effect = None
        await self.notifier.poll()
        self.assertEqual(self.reminders.get(row['id'])['status'], 'sent')

    async def test_forbidden_and_shutdown_do_not_claim_success(self):
        for exc in (Forbidden('blocked'), asyncio.CancelledError()):
            with self.subTest(error=type(exc).__name__):
                row = self.due()
                self.bot.send_message.side_effect = exc
                if isinstance(exc, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await self.notifier.poll()
                    expected = 'uncertain'
                else:
                    await self.notifier.poll()
                    expected = 'failed'
                self.assertEqual(self.reminders.get(row['id'])['status'], expected)

    def test_invalid_times_and_empty_titles_do_not_persist(self):
        for title, stamp in [('', future()), ('a'*501, future()), ('Тест', '2030-01-01T10:00:00'), ('Тест', '2000-01-01T10:00:00+03:00')]:
            with self.assertRaises(ValueError):
                self.reminders.create(title, stamp)
        self.assertEqual(self.reminders.list()['reminders'], [])

    async def test_edit_and_cancel_require_confirmation_and_detect_stale_version(self):
        row = self.reminders.create('Старое', future())
        args = self.reminders.prepare('update_reminder', row['id'], 'Новое', future(120))
        self.assertEqual(self.reminders.get(row['id'])['title'], 'Старое')
        result = await apply_confirmation({'action':'update_reminder', 'args':args}, None, None, self.reminders)
        self.assertIn('изменено', result)
        with self.assertRaises(ValueError):
            self.reminders.apply('update_reminder', **args)
        args = self.reminders.prepare('cancel_reminder', row['id'])
        await apply_confirmation({'action':'cancel_reminder', 'args':args}, None, None, self.reminders)
        self.assertEqual(self.reminders.get(row['id'])['status'], 'cancelled')

    async def test_combined_event_creation_and_bot_edits_keep_reminder_link(self):
        calendar = Calendar(None)
        calendar.write = AsyncMock(return_value={'id':'event1'})
        result = await self.reminders.create_with_event(calendar, 'Встреча', future(120), future(180), None, future(100))
        row = self.reminders.get(result['telegram_reminder']['id'])
        self.assertEqual(row['event_id'], 'event1')
        new_start = datetime.fromisoformat(future(240)).timestamp()
        args = {'event_id':'event1', 'title':'Перенесено', 'start':datetime.fromtimestamp(new_start, calendar.tz).isoformat()}
        await apply_confirmation({'action':'update_event','args':args,'etag':'v'}, calendar, None, self.reminders)
        edited = self.reminders.get(row['id'])
        self.assertAlmostEqual(edited['due'] - row['due'], new_start - row['event_start'])
        await apply_confirmation({'action':'delete_event','args':{'event_id':'event1'},'etag':'v'}, calendar, None, self.reminders)
        self.assertEqual(self.reminders.get(row['id'])['status'], 'cancelled')

    async def test_uncertain_calendar_creation_never_enables_reminder_or_retries(self):
        calendar = Calendar(None)
        calendar.write = AsyncMock(side_effect=RuntimeError('network'))
        with self.assertRaisesRegex(RuntimeError, 'не подтверждено'):
            await self.reminders.create_with_event(calendar, 'Встреча', future(120), future(180), None, future(100))
        calendar.write.assert_awaited_once()
        self.assertEqual(self.reminders.list()['reminders'][0]['status'], 'creating')
        await self.notifier.poll()
        self.bot.send_message.assert_not_called()

    def test_choice_expires_and_is_consumed_once(self):
        key = self.store.stage({'action':'schedule_destination','request':'Завтра стоматолог'})
        self.assertIn('Только Telegram', destination_request(self.store, key, 'telegram'))
        with self.assertRaises(ValueError):
            destination_request(self.store, key, 'both')
        key = self.store.stage({'action':'schedule_destination','request':'Завтра стоматолог'})
        with self.store.db:
            self.store.db.execute('UPDATE pending SET created=0')
        with self.assertRaises(ValueError):
            destination_request(self.store, key, 'both')

    async def test_agent_choice_does_not_write_and_selected_telegram_blocks_calendar(self):
        def call(name, args, index):
            item = SimpleNamespace(type='function_call', name=name, arguments=__import__('json').dumps(args), call_id='c'+str(index))
            item.model_dump = lambda **kw: {'type':'function_call','name':name,'arguments':item.arguments,'call_id':item.call_id,'id':'f'+str(index)}
            return SimpleNamespace(output=[item])
        create = AsyncMock(side_effect=[call('choose_schedule_destination', {'request':'Завтра стоматолог'}, 1),
            call('create_reminder', {'title':'Стоматолог','remind_at':future()}, 2),
            SimpleNamespace(output=[],output_text='Выберите назначение.')])
        calendar = Calendar(None)
        calendar.write = AsyncMock()
        agent = Agent(SimpleNamespace(responses=SimpleNamespace(create=create)), 'test', calendar, self.store)
        _, buttons = await agent.answer('Завтра стоматолог')
        self.assertEqual(buttons[0][2], 'destination')
        self.assertEqual(self.reminders.list()['reminders'], [])
        calendar.write.assert_not_called()
        create.side_effect = [call('create_event', {}, 3), call('create_reminder', {'title':'Стоматолог','remind_at':future()}, 4),
            SimpleNamespace(output=[],output_text='Создано в Telegram.')]
        await agent.answer('Только Telegram', destination='telegram')
        calendar.write.assert_not_called()
        self.assertEqual(len(self.reminders.list()['reminders']), 1)

    async def test_two_notifiers_cannot_claim_same_reminder(self):
        self.due()
        other = ReminderNotifier(self.reminders, self.bot, 42)
        await asyncio.gather(self.notifier.poll(), other.poll())
        self.bot.send_message.assert_awaited_once()
