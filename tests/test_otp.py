import base64
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from assistant.gmail import GmailError
from assistant.otp import CodeNotifier, extract_code, notification
from assistant.store import Store


def mail(subject='Your verification code', body='Your verification code is 123456.', labels=None):
    return {'id': 'm1', 'internalDate': str(int(time.time()*1000)), 'labelIds': labels or ['INBOX', 'UNREAD'],
            'payload': {'mimeType': 'text/plain', 'headers': [{'name': 'Subject', 'value': subject},
                {'name': 'From', 'value': 'Example Service <no-reply@example.com>'}],
                'body': {'data': base64.urlsafe_b64encode(body.encode()).decode()}}}


class ExtractTests(unittest.TestCase):
    def test_russian_english_and_grouped_codes(self):
        for subject, body, expected in [
            ('Вход', 'Ваш одноразовый временный код: 654321. Действует 10 минут.', '654321'),
            ('Your security code', 'Code: AB12CD', 'AB12CD'),
            ('Код подтверждения', '123-456', '123456'),
            ('Verification code 123456', 'Verification code 123456', '123456')]:
            with self.subTest(subject=subject):
                self.assertEqual(extract_code(subject, body), expected)

    def test_no_code_ambiguous_and_marketing(self):
        for subject, body in [('Sale', 'Your discount code is SAVE20'), ('Your verification code', 'Visit https://example.com/123456'),
            ('Код подтверждения', 'Дата: 2026-10-18'), ('Security code', '123456 or 654321'), ('Order', 'Order number 123456')]:
            with self.subTest(body=body):
                self.assertIsNone(extract_code(subject, body))

    def test_notification_contains_only_sender_and_code(self):
        value = notification(mail(body='Your verification code is 123456. Private email text.'))
        self.assertIn('Example Service (example.com)', value)
        self.assertIn('Код: 123456', value)
        self.assertNotIn('Private email', value)
        for label in ('SPAM', 'TRASH', 'SENT', 'DRAFT'):
            self.assertIsNone(notification(mail(labels=[label])))


class NotifierTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.folder.name) / 'db')
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.gmail = SimpleNamespace(request=AsyncMock(), get_message=AsyncMock(return_value=mail()))
        self.notifier = CodeNotifier(self.gmail, self.store, self.bot, 42)

    def tearDown(self):
        self.store.db.close()
        self.folder.cleanup()

    async def test_initial_poll_only_baselines_no_old_codes(self):
        self.gmail.request.return_value = {'historyId': '100'}
        await self.notifier.poll()
        self.assertEqual(self.store.setting('otp_history_id'), '100')
        self.gmail.get_message.assert_not_called()
        self.bot.send_message.assert_not_called()

    async def test_paginated_new_messages_send_once_to_owner_and_survive_reset(self):
        self.store.setting('otp_history_id', '100')
        record = {'id': '101', 'messagesAdded': [{'message': {'id': 'm1'}}]}
        self.gmail.request.side_effect = [{'history': [record], 'nextPageToken': 'p2'}, {'history': [record], 'historyId': '103'}]
        await self.notifier.poll()
        self.store.clear()
        await self.notifier.process('m1')
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.call_args.kwargs['chat_id'], 42)
        self.assertTrue(self.bot.send_message.call_args.kwargs['protect_content'])
        self.assertEqual(self.store.setting('otp_history_id'), '103')
        # No codes or message bodies persisted in dialog or notification bookkeeping.
        self.assertEqual(self.store.history(), [])
        self.assertNotIn('123456', str(self.store.db.execute('SELECT * FROM otp_deliveries').fetchall()))

    async def test_stale_or_disabled_do_not_send(self):
        message = mail()
        message['internalDate'] = str(int((time.time()-1000)*1000))
        self.gmail.get_message.return_value = message
        await self.notifier.process('m1')
        self.store.setting('otp_enabled', '0')
        await self.notifier.poll()
        self.gmail.request.assert_not_called()
        self.bot.send_message.assert_not_called()

    async def test_uncertain_telegram_send_not_repeated(self):
        self.bot.send_message.side_effect = RuntimeError('timeout')
        await self.notifier.process('m1')
        await self.notifier.process('m1')
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(self.store.db.execute('SELECT status FROM otp_deliveries').fetchone()[0], 'uncertain')

    async def test_expired_history_resets_baseline(self):
        self.store.setting('otp_history_id', '1')
        self.gmail.request.side_effect = [GmailError(404), {'historyId': '200'}]
        await self.notifier.poll()
        self.assertEqual(self.store.setting('otp_history_id'), '200')
        self.bot.send_message.assert_not_called()
