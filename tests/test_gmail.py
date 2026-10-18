import base64
from email import policy
from email.parser import BytesParser
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from assistant.agent import Agent
from assistant.calendar import Calendar
from assistant.gmail import Gmail, note_message, body_text
from assistant.store import Store
from assistant.main import apply_confirmation


class GmailTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_trash_routes_to_gmail_not_calendar(self):
        gmail = SimpleNamespace(modify_email=AsyncMock())
        calendar = SimpleNamespace(write=AsyncMock())
        result = await apply_confirmation({'action': 'trash_email', 'args': {'message_id': 'm1'}}, calendar, gmail)
        gmail.modify_email.assert_awaited_once_with(action='trash_email', message_id='m1')
        calendar.write.assert_not_called()
        self.assertIn('корзину Gmail', result)

    async def test_read_note_converts_html_and_does_not_mark_read(self):
        gmail = Gmail(None)
        text = '<html><head><style>secret-style</style></head><body><div>Фильмы</div><div>Дюна<br>Прибытие</div></body></html>'
        gmail.request = AsyncMock(side_effect=[{'id': 'n1', 'labelIds': ['notes1', 'UNREAD'],
            'payload': {'mimeType': 'text/html', 'headers': [{'name': 'Subject', 'value': 'Кино'}],
                        'body': {'data': base64.urlsafe_b64encode(text.encode()).decode()}}},
            {'labels': [{'name': 'Notes', 'id': 'notes1'}]}])
        result = await gmail.read('n1', notes=True)
        self.assertIn('Дюна\nПрибытие', result['text'])
        self.assertNotIn('secret-style', result['text'])
        self.assertTrue(result['unread'])
        self.assertTrue(all(call.args[0] == 'GET' for call in gmail.request.call_args_list))

    async def test_mail_mutations_cannot_touch_notes(self):
        gmail = Gmail(None)
        gmail.get_message = AsyncMock(return_value={'id': 'n1', 'labelIds': ['notes1']})
        gmail.notes_label = AsyncMock(return_value='notes1')
        gmail.request = AsyncMock()
        with self.assertRaisesRegex(ValueError, 'заметка'):
            await gmail.modify_email('n1', 'trash_email')
        gmail.request.assert_not_called()

    def test_alternative_prefers_plain_and_ignores_attachments(self):
        def part(mime, text, **kwargs):
            return {'mimeType': mime, 'body': {'data': base64.urlsafe_b64encode(text.encode()).decode()}, **kwargs}
        payload = {'mimeType': 'multipart/mixed', 'parts': [
            {'mimeType': 'multipart/alternative', 'parts': [part('text/html', '<p>HTML</p>'), part('text/plain', 'Plain')]},
            part('text/plain', 'Hidden attachment', filename='private.txt')]}
        self.assertEqual(body_text(payload).strip(), 'Plain')

    async def test_search_notes_uses_actual_label_and_pagination(self):
        gmail = Gmail(None)
        gmail.notes_label = AsyncMock(return_value='notes1')
        gmail.request = AsyncMock(side_effect=[{'messages': [{'id': 'n1'}], 'nextPageToken': 'next'},
            {'id': 'n1', 'labelIds': ['notes1'], 'snippet': 'Кино'}])
        result = await gmail.search('фильмы', page_token='previous', notes=True)
        self.assertEqual(result['next_page_token'], 'next')
        params = gmail.request.call_args_list[0].kwargs['params']
        self.assertEqual(params['labelIds'], 'notes1')
        self.assertEqual(params['pageToken'], 'previous')

    async def test_read_failure_does_not_claim_empty_mailbox(self):
        gmail = Gmail(None)
        gmail.notes_label = AsyncMock(side_effect=RuntimeError('Gmail unavailable'))
        with self.assertRaises(RuntimeError):
            await gmail.search('фильм', notes=True)

    async def test_trash_agent_stages_without_modifying_mail(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'db')
            self.addCleanup(store.db.close)
            call = SimpleNamespace(type='function_call', name='trash_email', arguments='{"message_id":"m1"}', call_id='c1')
            call.model_dump = lambda **kw: {'type': 'function_call', 'name': call.name, 'arguments': call.arguments, 'call_id': 'c1', 'id': 'fc1'}
            create = AsyncMock(side_effect=[SimpleNamespace(output=[call]), SimpleNamespace(output=[], output_text='Подтвердите.')])
            gmail = SimpleNamespace(read=AsyncMock(return_value={'title': 'Письмо', 'from': 'sender'}), modify_email=AsyncMock())
            agent = Agent(SimpleNamespace(responses=SimpleNamespace(create=create)), 'test', Calendar(None), store, gmail=gmail)
            _, buttons = await agent.answer('Удали письмо')
            gmail.modify_email.assert_not_called()
            pending = store.take(buttons[0][0])
            self.assertEqual(pending, {'action': 'trash_email', 'args': {'message_id': 'm1'}})

    def test_note_format_preserves_text_and_escapes_html(self):
        raw = note_message('Идея', 'Строка 1\n<script>данные</script>', 'test@example.invalid')
        message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
        self.assertEqual(message['Subject'], 'Идея')
        self.assertEqual(message['X-Uniform-Type-Identifier'], 'com.apple.mail-note')
        self.assertIn('Строка 1<br>&lt;script&gt;данные&lt;/script&gt;', message.get_content())

    async def test_invalid_title_does_not_access_gmail(self):
        gmail = Gmail(None)
        gmail.request = AsyncMock()
        with self.assertRaises(ValueError):
            await gmail.create_note('name\nInjected: value', 'Text')
        gmail.request.assert_not_called()

    async def test_create_uses_notes_label_without_inbox_or_sending(self):
        gmail = Gmail(None)
        gmail.request = AsyncMock(side_effect=[{'emailAddress': 'test@example.invalid'},
            {'labels': [{'id': 'notes1', 'name': 'Notes'}]}, {'id': 'message1'}])
        result = await gmail.create_note('Тест', 'Содержание')
        self.assertEqual(result['status'], 'created')
        self.assertEqual(gmail.request.call_args.args, ('POST', 'messages'))
        self.assertEqual(gmail.request.call_args.kwargs['json']['labelIds'], ['notes1'])

    async def test_agent_does_not_repeat_same_uncertain_write(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'db')
            self.addCleanup(store.db.close)
            def call(number):
                item = SimpleNamespace(type='function_call', name='create_note',
                    arguments='{"title":"Тест","content":"Текст"}', call_id='call'+str(number))
                item.model_dump = lambda **kw: {'type': 'function_call', 'name': item.name,
                    'arguments': item.arguments, 'call_id': item.call_id, 'id': 'fc'+str(number)}
                return item
            create = AsyncMock(side_effect=[SimpleNamespace(output=[call(1)]), SimpleNamespace(output=[call(2)]),
                SimpleNamespace(output=[], output_text='Проверьте Notes.')])
            gmail = SimpleNamespace(create_note=AsyncMock(side_effect=RuntimeError('Нет ответа Gmail')))
            agent = Agent(SimpleNamespace(responses=SimpleNamespace(create=create)), 'test', Calendar(None), store, gmail=gmail)
            text, buttons = await agent.answer('Создай заметку')
            self.assertEqual(text, 'Проверьте Notes.')
            gmail.create_note.assert_awaited_once()
            self.assertTrue(any(t.get('name') == 'create_note' for t in create.call_args.kwargs['tools']))
            self.assertEqual(buttons, [])
